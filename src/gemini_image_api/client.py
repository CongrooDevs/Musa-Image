"""Gemini web backend using internal async upstream client."""
import asyncio
import os
import re
import dataclasses
import inspect
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from filelock import AsyncFileLock
from .upstream import (
    GeminiClient as _UpstreamClient,
    set_log_level as _set_upstream_log_level,
    AccountStatus,
    AuthError,
    ImageGenerationError,
    ModelInvalidError,
    TemporarilyBlockedError,
    UsageLimitExceededError,
    TimeoutError as _UpstreamTimeout,
    GeneratedImage,
)

from .auth import ensure_client
from .config import Settings
from .errors import (
    AUTH_EXPIRED,
    CONTENT_BLOCKED,
    GENERATED_NOT_SAVED,
    NO_IMAGE,
    TEMPORARILY_BLOCKED,
    TIMEOUT,
    UNKNOWN_CONVERSATION,
    UNKNOWN_MODEL,
    USAGE_LIMIT,
    UPSTREAM_ERROR,
    GIAError,
)
from .logsetup import add_upstream_file_sink
from .state import GenerationRecord, StateStore
from .storage import SavedImage, save_image
from .styles import GENERATE_PREFIX

_default_client_cls = _UpstreamClient

_UPSTREAM_FIELDS = ("cid", "rid", "rcid", "image_id", "url")

# gem_missing is a warning code, not an agent error code (errors.py stays at 22 codes)
GEM_MISSING = "gem_missing"
GEM_CACHE_TTL_S = 60.0

# Upstream strips `http://googleusercontent.com/image_generation_content/<n>` placeholders but its
# pattern stops at the first digits, so the current `.../<n>_<id>` form leaves a `_<id>` line behind.
_PLACEHOLDER_RE = re.compile(r"https?://googleusercontent\.com/image_generation_content/[^\s\n]+", re.M)
_PLACEHOLDER_RESIDUE_RE = re.compile(r"^_\d+[ \t]*$\n?", re.M)


def _clean_text(text: str) -> str:
    cleaned = _PLACEHOLDER_RE.sub("", text)
    return _PLACEHOLDER_RESIDUE_RE.sub("", cleaned).strip()

@dataclass
class GenerateResult:
    status: str
    code: str | None
    conversation_id: str | None
    images: list[SavedImage]
    gemini_text: str
    elapsed_s: float
    chat_metadata: list | None
    save_error: str | None = None
    record_id: str | None = None
    warning: dict | None = None


class Backend:
    """Duck-typed protocol consumed by mcp_server and cli."""
    async def status(self) -> dict: raise NotImplementedError
    async def list_models(self) -> list[dict]: raise NotImplementedError
    async def resolve_model_name(self, name: str) -> str: raise NotImplementedError
    async def generate(self, prompt, refs, *, model, progress=None, **kw) -> GenerateResult: raise NotImplementedError
    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, **kw) -> GenerateResult: raise NotImplementedError
    async def retry_download(self, conversation_id, image_index) -> SavedImage: raise NotImplementedError
    async def delete_conversation(self, conversation_id) -> None: raise NotImplementedError
    async def create_gem(self, name: str, instructions: str, description: str = "") -> str: raise NotImplementedError
    async def update_gem(self, gem_id: str, name: str, instructions: str, description: str = "") -> None: raise NotImplementedError
    async def delete_gem(self, gem_id: str) -> None: raise NotImplementedError
    async def gem_exists(self, gem_id: str) -> bool: raise NotImplementedError
    async def quota(self) -> dict | None: return None
    async def aclose(self) -> None: return None


def _map_exc(e: Exception) -> GIAError:
    if isinstance(e, GIAError):
        return e
    if isinstance(e, AuthError):
        return GIAError(AUTH_EXPIRED, f"upstream auth: {e}", hint="run: gia auth")
    if isinstance(e, UsageLimitExceededError):
        return GIAError(USAGE_LIMIT, f"usage limit reached: {e}")
    if isinstance(e, TemporarilyBlockedError):
        return GIAError(TEMPORARILY_BLOCKED, f"temporarily blocked (429): {e}")
    if isinstance(e, ImageGenerationError):
        return GIAError(CONTENT_BLOCKED, f"image generation refused: {e}")
    if isinstance(e, ModelInvalidError):
        return GIAError(UNKNOWN_MODEL, f"unknown model: {e}")
    if isinstance(e, (asyncio.TimeoutError, _UpstreamTimeout)):
        return GIAError(TIMEOUT, "upstream call timed out")
    return GIAError(UPSTREAM_ERROR, f"{type(e).__name__}: {e}")


def _model_dict(m) -> dict:
    if isinstance(m, dict):
        name, avail = m.get("model_name"), m.get("is_available", m.get("available", True))
    else:
        name, avail = getattr(m, "model_name", None), getattr(m, "is_available", True)
    out = {"model_name": name or str(m), "available": bool(avail)}
    display = m.get("display_name") if isinstance(m, dict) else getattr(m, "display_name", None)
    if isinstance(display, str) and display:
        out["display_name"] = display
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_quota(info) -> dict | None:
    if not isinstance(info, dict):
        return None
    w5, wk = info.get("current_5h") or {}, info.get("weekly") or {}
    if not w5 and not wk:
        return None
    return {"tier": (info.get("tier") or {}).get("label"),
            "window_5h_pct": w5.get("usage_percentage"), "weekly_pct": wk.get("usage_percentage"),
            "resets_at_5h": w5.get("reset_at"), "resets_at_weekly": wk.get("reset_at")}


class GeminiWebBackend(Backend):
    def __init__(self, settings: Settings, state: StateStore, client_cls=None, auto_close=False):
        self.settings = settings
        settings.ensure_dirs()
        os.environ.setdefault("GEMINI_COOKIE_PATH", str(settings.upstream_cache_dir))
        self.state = state
        self.client_cls = client_cls or _default_client_cls
        self.auto_close = auto_close
        self._client = None
        self._alock = asyncio.Lock()
        # Dedicated file: StateStore takes a blocking FileLock on settings.lock_file, so holding that
        # one across a slow upstream call would freeze the event loop and other processes.
        self._flock = AsyncFileLock(str(settings.state_dir / "upstream.lock"))
        self._client_lock = asyncio.Lock()
        self._last_call: float | None = None
        self._clock = time.monotonic  # injectable for tests
        self._gems_cache: tuple[float, frozenset[str]] | None = None

    # -- client / models ---------------------------------------------------
    def _alive(self, c) -> bool:
        if not getattr(c, "_running", True):
            return False
        status = getattr(c, "account_status", None)
        return status is None or status == AccountStatus.AVAILABLE

    async def _ensure_client(self):
        async with self._client_lock:
            if self._client is not None and not self._alive(self._client):
                old, self._client = self._client, None
                try:
                    await old.close()
                except Exception:
                    pass
            if self._client is None:
                self._client = await ensure_client(self.settings, self.client_cls, auto_close=self.auto_close)
            return self._client

    async def aclose(self) -> None:
        async with self._client_lock:
            old, self._client = self._client, None
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass

    async def quota(self) -> dict | None:
        try:
            c = await self._ensure_client()
        except Exception:
            return None
        return normalize_quota(getattr(c, "usage_info", None))

    def _cookie_age_s(self) -> int | None:
        files = [self.settings.cookie_file, *self.settings.upstream_cache_dir.glob("*")]
        mtimes = [f.stat().st_mtime for f in files if f.is_file()]
        return int(time.time() - max(mtimes)) if mtimes else None

    async def _models(self) -> list[dict]:
        c = await self._ensure_client()
        res = c.list_models()
        if inspect.isawaitable(res):
            res = await res
        return [_model_dict(m) for m in (res or [])]

    async def status(self) -> dict:
        jar_ok = self.settings.cookie_file.exists()
        today = self.state.count_images_today()
        try:
            models = [m["model_name"] for m in await self._models()]
        except Exception as e:
            err = _map_exc(e)
            return {"cookie_present": jar_ok, "valid": False, "code": err.code, "message": str(err),
                    "images_today": today, "cookie_age_s": self._cookie_age_s()}
        c = self._client
        st = getattr(c, "account_status", None)
        return {"cookie_present": jar_ok, "valid": True, "models": models, "images_today": today,
                "account_status": getattr(st, "name", None), "cookie_age_s": self._cookie_age_s(),
                "quota": normalize_quota(getattr(c, "usage_info", None))}

    async def list_models(self) -> list[dict]:
        try:
            return await self._models()
        except Exception as e:
            raise _map_exc(e) from e

    async def resolve_model_name(self, name: str) -> str:
        """Account model_name behind an alias/display name; `name` itself when it does not resolve.
        Uses the registry the client already loaded (no generation call)."""
        try:
            c = await self._ensure_client()
            resolved = c.resolve_model(name)
        except Exception:
            return name
        model_name = getattr(resolved, "model_name", None)
        return model_name if isinstance(model_name, str) and model_name else name

    # -- gems ---------------------------------------------------------------
    async def _gem_op(self, op):
        """Run a gem-management call under the standard locks + throttle; errors via _map_exc."""
        try:
            async with self._alock:
                async with self._flock:
                    client = await self._ensure_client()
                    await self._throttle()
                    result = await op(client)
            return result
        except Exception as e:
            raise _map_exc(e) from e
        finally:
            self._gems_cache = None  # the jar may have changed, even on failure

    async def create_gem(self, name: str, instructions: str, description: str = "") -> str:
        gem = await self._gem_op(
            lambda c: c.create_gem(name=name, prompt=instructions, description=description))
        return gem.id

    async def update_gem(self, gem_id: str, name: str, instructions: str, description: str = "") -> None:
        await self._gem_op(
            lambda c: c.update_gem(gem_id, name, prompt=instructions, description=description))

    async def delete_gem(self, gem_id: str) -> None:
        await self._gem_op(lambda c: c.delete_gem(gem_id))

    async def gem_exists(self, gem_id: str) -> bool:
        """fetch_gems with a 60 s cache: two calls in a row make one upstream request."""
        now = self._clock()
        cached = self._gems_cache
        if cached is not None and now - cached[0] < GEM_CACHE_TTL_S:
            return gem_id in cached[1]
        try:
            async with self._alock:
                async with self._flock:
                    client = await self._ensure_client()
                    await self._throttle()
                    jar = await client.fetch_gems()
                    ids = frozenset(g.id for g in (jar or []))
            self._gems_cache = (self._clock(), ids)
            return gem_id in ids
        except Exception as e:
            raise _map_exc(e) from e

    async def _resolve_gem(self, gem_id):
        """Resolve a requested gem before a call. Returns (gem, warning); on a fetch failure the
        gem is used anyway — a network error must not silently disable it."""
        if not gem_id:
            return None, None
        try:
            if await self.gem_exists(gem_id):
                return gem_id, None
        except Exception:
            return gem_id, None
        return None, {"code": GEM_MISSING,
                      "gem_id": gem_id,
                      "message": f"Gem {gem_id} no longer exists; the call was made without it.",
                      "next_action": "Re-create or re-enable the style's Gem, or continue without it."}

    # -- upstream plumbing (call only while holding the locks) --------------
    async def _throttle(self) -> None:
        if self._last_call is not None:
            wait = self.settings.min_delay_s - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _call_upstream(self, prompt, ref_paths, model, metadata=None, gem=None):
        """Run generate_content on a fresh/resumed chat. Retries once after an auth rebuild."""
        for attempt in (0, 1):
            client = await self._ensure_client()
            kw = {"model": model}
            if metadata:
                kw["metadata"] = metadata
            if gem is not None:
                kw["gem"] = gem
            chat = client.start_chat(**kw)
            await self._throttle()
            gen_kw = {"model": model, "chat": chat, "temporary": self.settings.temp_chats}
            if gem is not None:
                gen_kw["gem"] = gem
            try:
                out = await asyncio.wait_for(
                    client.generate_content(prompt, files=[str(p) for p in ref_paths] or None, **gen_kw),
                    timeout=self.settings.gen_timeout_s)
                return out, chat
            except AuthError:
                if attempt == 1:
                    raise
                async with self._client_lock:  # _ensure_client holds this lock too; never call it from in here
                    old = client
                    if self._client is client:  # a concurrent caller may already have rebuilt it
                        self._client = None
                try:
                    await old.close()  # stop its refresh tasks / HTTP session
                except Exception:
                    pass
        raise AssertionError("unreachable")

    async def _fetch_bytes(self, img) -> tuple[bytes, bool]:
        """Download an upstream image: full size first, preview as fallback. Returns (data, full_size)."""
        client = getattr(img, "client_ref", None)
        if client and hasattr(client, "download_image"):
            last: Exception | None = None
            for full in (True, False):
                try:
                    data = await asyncio.wait_for(
                        client.download_image(img.url, full_size=full),
                        timeout=self.settings.download_timeout_s)
                    return data, full
                except Exception as e:
                    last = e
            raise last  # type: ignore[misc]

        tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=self._tmp_root()))
        try:
            last = None
            for full in (True, False):
                try:
                    path = await asyncio.wait_for(
                        img.save(path=str(tmp), full_size=full, verbose=False),
                        timeout=self.settings.download_timeout_s)
                    return Path(path).read_bytes(), full
                except Exception as e:  # try the preview next
                    last = e
            raise last  # type: ignore[misc]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _tmp_root(self) -> Path:
        root = self.settings.state_dir / "tmp_downloads"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _store(self, data, full, *, prompt, model, conv, refs_meta, project, subdir, index,
               slug_source=None) -> SavedImage:
        meta = {"prompt": prompt, "model": model, "conversation_id": conv,
                "ref_sha256": [r["sha256"] for r in refs_meta]}
        # The upstream prompt starts with GENERATE_PREFIX; slug from the user's own words instead.
        slug_text = slug_source or prompt.removeprefix(GENERATE_PREFIX)
        saved = save_image(data, output_root=self.settings.output_root, project=project, subdir=subdir,
                           prompt=slug_text, index=index, meta=meta, write_sidecar=self.settings.write_sidecar,
                           thumb_size=self.settings.thumb_size)
        return saved if full else dataclasses.replace(saved, full_size=False)

    @staticmethod
    def _upstream_fields(im) -> dict:
        return {k: getattr(im, k, "") or "" for k in _UPSTREAM_FIELDS}

    async def _run(self, prompt, refs, *, model, metadata, conv, project, subdir, progress, slug_source=None,
                   gem=None):
        """Upstream call + downloads. Returns (chat, text, saved, entries, errors)."""
        refs_meta = self._refs_meta(refs)
        if progress:
            await progress("generating", 0.3)
        out, chat = await self._call_upstream(prompt, [r.path for r in refs], model, metadata, gem=gem)
        text = _clean_text(getattr(out, "text", "") or "")
        gen = [im for im in (out.images or []) if isinstance(im, GeneratedImage)]
        saved: list[SavedImage] = []
        entries: list[dict] = []
        errors: list[str] = []
        for i, im in enumerate(gen):
            if progress:
                await progress("downloading", 0.6 + 0.4 * i / len(gen))
            entry = {"index": i, "path": None, **self._upstream_fields(im)}
            try:
                data, full = await self._fetch_bytes(im)
                s = self._store(data, full, prompt=prompt, model=model, conv=conv, refs_meta=refs_meta,
                                project=project, subdir=subdir, index=i, slug_source=slug_source)
                saved.append(s)
                entry.update(path=str(s.path), width=s.width, height=s.height, bytes=s.nbytes,
                             format=s.format, full_size=s.full_size)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")  # kept with path=None so retry_download can redo it
            entries.append(entry)
        return chat, text, saved, entries, errors

    @staticmethod
    def _refs_meta(refs) -> list[dict]:
        return [{"source": r.source, "path": str(r.path), "sha256": r.sha256} for r in refs]

    def _append(self, **kw) -> GenerationRecord:
        base = dict(id=StateStore.new_id("g"), ts=_now(), style=None, style_text=None, aspect_hint=None,
                    model=None, project=None, subdir=None, refs=[], images=[], status="ok", code=None,
                    gemini_text="", elapsed_s=0.0, parent_id=None, chat_metadata=None, prompt="",
                    user_prompt=None, gem_id=None)
        base.update(kw)
        rec = GenerationRecord(**base)
        self.state.append(rec)
        return rec

    async def _generate_or_edit(self, kind, conv, prompt, refs, *, model, progress, project, subdir, style,
                                style_text, aspect_hint, rec=None, slug_source=None, gem_id=None) -> GenerateResult:
        t0 = time.monotonic()
        refs_meta = self._refs_meta(refs)
        parent_meta = list(rec.chat_metadata) if rec else None
        if rec is not None and rec.gem_id:
            gem_id = rec.gem_id  # a conversation keeps its Gem; an explicit param is ignored
        warning = None
        try:
            gem, warning = await self._resolve_gem(gem_id)
            async with self._alock:
                # The file lock is not reentrant across instances: StateStore writes happen after release.
                async with self._flock:
                    chat, text, saved, entries, errors = await self._run(
                        prompt, refs, model=model, metadata=parent_meta, conv=conv,
                        project=project, subdir=subdir, progress=progress, slug_source=slug_source, gem=gem)
                elapsed = time.monotonic() - t0
                chat_meta = list(getattr(chat, "metadata", None) or []) or parent_meta
                if not entries:
                    status, code = "error", NO_IMAGE
                elif len(saved) < len(entries):
                    status, code = "generated_not_saved", GENERATED_NOT_SAVED
                else:
                    status, code = "ok", None
                appended = self._append(type=kind, conversation_id=conv, chat_metadata=chat_meta, prompt=prompt,
                             style=style, style_text=style_text, aspect_hint=aspect_hint, model=model,
                             project=project, subdir=subdir, refs=refs_meta, images=entries, status=status,
                             code=code, gemini_text=text, elapsed_s=elapsed,
                             parent_id=rec.id if rec else None, user_prompt=slug_source, gem_id=gem)
                return GenerateResult(status, code, conv, saved, text, elapsed, chat_meta,
                                      save_error=errors[0] if errors else None, record_id=appended.id,
                                      warning=warning)
        except Exception as e:
            err = _map_exc(e)
            return GenerateResult("error", err.code, conv, [], str(err), time.monotonic() - t0, parent_meta,
                                  warning=warning)

    async def generate(self, prompt, refs, *, model, progress=None, project=None, subdir=None,
                       style=None, style_text=None, aspect_hint=None, slug_source=None,
                       gem_id=None) -> GenerateResult:
        return await self._generate_or_edit(
            "generate", StateStore.new_id("c"), prompt, refs, model=model, progress=progress,
            project=project, subdir=subdir, style=style, style_text=style_text, aspect_hint=aspect_hint,
            slug_source=slug_source, gem_id=gem_id)

    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, project=None, subdir=None,
                   style=None, style_text=None, aspect_hint=None, slug_source=None,
                   gem_id=None) -> GenerateResult:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} not found",
                           hint="call generate first")
        return await self._generate_or_edit(
            "edit", conversation_id, prompt, refs, model=model, progress=progress,
            project=project if project is not None else rec.project,
            subdir=subdir if subdir is not None else rec.subdir,
            style=style, style_text=style_text, aspect_hint=aspect_hint, rec=rec,
            slug_source=slug_source, gem_id=gem_id)

    # -- retry / delete -------------------------------------------------------
    async def _history_image(self, client, rec, entry):
        """Fallback: locate the image in the upstream chat history."""
        hist = await asyncio.wait_for(client.read_chat(rec.chat_metadata[0]),
                                      timeout=self.settings.download_timeout_s)
        found = []
        for turn in getattr(hist, "turns", None) or []:  # latest first
            mo = getattr(turn, "model_output", None)
            found += [im for im in (getattr(mo, "images", None) or []) if isinstance(im, GeneratedImage)]
        for im in found:
            if entry.get("image_id") and getattr(im, "image_id", "") == entry["image_id"]:
                return im
        if found:
            return found[min(entry["index"], len(found) - 1)]
        return None

    async def retry_download(self, conversation_id, image_index) -> SavedImage:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} not found")
        entry = next((e for e in rec.images if e.get("index") == image_index), None)
        if entry is None or not entry.get("url"):
            raise GIAError(UPSTREAM_ERROR, f"no re-downloadable image {image_index} in {conversation_id}")
        refs_meta = rec.refs or []
        try:
            async with self._alock:
                async with self._flock:
                    client = await self._ensure_client()
                    await self._throttle()
                    try:
                        img = GeneratedImage(url=entry["url"], client_ref=client,
                                             **{k: entry.get(k) or "" for k in _UPSTREAM_FIELDS if k != "url"})
                        data, full = await self._fetch_bytes(img)
                    except Exception:
                        img = await self._history_image(client, rec, entry)
                        if img is None:
                            raise
                        data, full = await self._fetch_bytes(img)
                    saved = self._store(data, full, prompt=rec.prompt, model=rec.model, conv=conversation_id,
                                        refs_meta=refs_meta, project=rec.project, subdir=rec.subdir,
                                        index=image_index)
                new_images = [dict(e) for e in rec.images]
                for e in new_images:
                    if e.get("index") == image_index:
                        e.update(path=str(saved.path), width=saved.width, height=saved.height,
                                 bytes=saved.nbytes, format=saved.format, full_size=saved.full_size)
                ok = all(e.get("path") for e in new_images)
                self._append(type="retry_download", conversation_id=conversation_id,
                             chat_metadata=rec.chat_metadata, prompt=rec.prompt, style=rec.style,
                             style_text=rec.style_text, aspect_hint=rec.aspect_hint, model=rec.model,
                             project=rec.project, subdir=rec.subdir, refs=refs_meta, images=new_images,
                             status="ok" if ok else rec.status, code=None if ok else rec.code,
                             gemini_text=rec.gemini_text, parent_id=rec.id, user_prompt=rec.user_prompt,
                             gem_id=rec.gem_id)
                return saved
        except Exception as e:
            raise _map_exc(e) from e

    async def delete_conversation(self, conversation_id) -> None:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} not found")
        try:
            async with self._alock:
                async with self._flock:
                    client = await self._ensure_client()
                    await self._throttle()
                    await asyncio.wait_for(client.delete_chat(rec.chat_metadata[0]),
                                           timeout=self.settings.download_timeout_s)
                self._append(type="delete", conversation_id=conversation_id, chat_metadata=None,
                             parent_id=rec.id)
        except Exception as e:
            raise _map_exc(e) from e


def build_backend(settings=None, state=None, client_cls=None, auto_close=False) -> Backend:
    from .config import load_settings
    settings = settings or load_settings()
    state = state or StateStore(settings.state_dir)
    _set_upstream_log_level("WARNING")  # upstream DEBUG logs print the cookie-cache path (contains the PSID)
    add_upstream_file_sink(settings.state_dir)  # set_log_level drops all loguru sinks; re-add ours
    return GeminiWebBackend(settings, state, client_cls=client_cls, auto_close=auto_close)
