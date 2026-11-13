"""Single home for the generate/edit pipeline shared by the CLI and the MCP server."""
import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .animations import AnimationStore
from .client import Backend, GenerateResult, GEM_MISSING
from .config import Settings
from .errors import (AUTH_EXPIRED, AUTH_FAILED, INVALID_ANIMATION, QUOTA_GUARD,
                     TEMPORARILY_BLOCKED, UNKNOWN_ANIMATION, UPSTREAM_ERROR, USAGE_LIMIT,
                     GIAError, INVALID_COLLECTION, INVALID_QUERY, INVALID_REF, INVALID_STYLE, UNKNOWN_COLLECTION,
                     UNKNOWN_PROMPT, UNKNOWN_STYLE, error_payload)
from .gallery import ImageEntry, image_index
from .library import LibraryStore
from .prompts import PromptStore
from .refs import GEN_REF_RE, ResolvedRef, resolve_refs, validate_trusted_image
from .state import StateStore, display_prompt
from .storage import check_copy_dest, copy_file, slugify
from .style_gems import StyleGemStore, default_instructions, gem_name
from .sprites import (GRIDS, cache_path, detect_cells, edit_suffix, purge_cache, render_sheet, sheet_prompt,
                      validate_animation_request, validate_grid, validate_settings, write_sheet)
from .style_store import UserStyleStore
from .styles import (PACKAGE_ROOT, Style, StyleCatalog, build_prompt, load_catalog, preview_prompt,
                     resolve_styles_path)

MAX_BATCH = 8
BATCH_ACTIONS = ("favorite", "unfavorite", "add_to_collection", "remove_from_collection", "delete")
MAX_BATCH_IMAGES = 200
MAX_ZIP_IMAGES = 100
MAX_ZIP_BYTES = 500 * 1024 * 1024
_TMP_ZIP_MAX_AGE_S = 3600
_KEY_RE = re.compile(r"^(?:gen:(g_[0-9a-f]{12})(?:#([0-9]{1,9}))?|(g_[0-9a-f]{12})#([0-9]{1,9}))\Z")
# A batch stops on these: retrying the next prompt would fail the same way (or burn quota).
_BATCH_STOP_CODES = frozenset({QUOTA_GUARD, USAGE_LIMIT, AUTH_EXPIRED, AUTH_FAILED, TEMPORARILY_BLOCKED})
# Fields that feed the Gem: default_instructions() also depends on whether the style has a reference image.
_GEM_FIELDS = frozenset({"name", "description", "append", "image", "remove_image"})
_SETTINGS_FIELDS = ("cols", "rows", "mode", "key_color", "tolerance", "fps", "loop")
_DETECT_FIELDS = ("cols", "rows", "mode", "key_color", "tolerance")

log = logging.getLogger(__name__)


@dataclass
class ServiceResult:
    result: GenerateResult
    quota: dict | None
    copies: dict[int, dict] = field(default_factory=dict)
    collection: dict | None = None
    warning: dict | None = None


class ImageService:
    def __init__(self, backend: Backend, settings: Settings, state: StateStore,
                 library: LibraryStore | None = None, styles: UserStyleStore | None = None,
                 gems: StyleGemStore | None = None, prompts: PromptStore | None = None,
                 animations: AnimationStore | None = None, compositions=None):
        self.backend, self.settings, self.state = backend, settings, state
        self.library = library or LibraryStore(settings.state_dir)
        self.styles = styles or UserStyleStore(settings.state_dir)
        self.gems = gems or StyleGemStore(settings.state_dir)
        self.prompts = prompts or PromptStore(settings.state_dir)
        self.animations = animations or AnimationStore(settings.state_dir)
        if compositions is None:
            from .compositions import CompositionStore  # lazy: compositions imports jobs, which imports this module
            compositions = CompositionStore(settings.state_dir)
        self.compositions = compositions
        self._last_catalog: StyleCatalog | None = None


    def catalog(self) -> StyleCatalog:
        try:
            self._last_catalog = load_catalog(self.settings)
        except Exception as e:
            log.warning("cannot load styles catalog: %s", e)
            return self._last_catalog or StyleCatalog()
        return self._last_catalog

    def _style_or_raise(self, style_id: str) -> Style:
        style = self.catalog().styles.get(style_id)
        if style is None:
            raise GIAError(UNKNOWN_STYLE, f"unknown style {style_id!r}",
                           valid=list(self.catalog().styles))
        return style

    async def set_style_gem(self, style_id: str, enabled: bool, instructions: str | None = None) -> dict:
        """Enable/disable the style's Gem. Returns {"style": id, "gem": {...} | None}."""
        style = self._style_or_raise(style_id)
        entry = self.gems.get(style_id)
        if not enabled:
            if entry is not None:
                await self._delete_gem_entry(style_id, entry)
            return {"style": style_id, "gem": None}
        name = gem_name(style)
        now = datetime.now(timezone.utc).isoformat()
        if entry is None:
            text = instructions if instructions else default_instructions(style)
            gem_id = await self.backend.create_gem(name, text, description=style.description)
            entry = {"gem_id": gem_id, "name": name, "instructions": text,
                     "custom": bool(instructions), "created": now, "updated": now}
        elif instructions is not None:
            text = instructions if instructions else default_instructions(style)
            await self.backend.update_gem(entry["gem_id"], name, text, description=style.description)
            entry = {**entry, "name": name, "instructions": text,
                     "custom": bool(instructions), "updated": now}
        self.gems.put(style_id, entry)
        return {"style": style_id,
                "gem": {"gem_id": entry["gem_id"], "name": entry["name"], "custom": entry["custom"]}}

    async def _delete_gem_entry(self, style_id: str, entry: dict) -> None:
        """Delete the backend Gem and drop the local entry; a Gem already gone is fine,
        a network error propagates and keeps the entry."""
        try:
            await self.backend.delete_gem(entry["gem_id"])
        except GIAError:
            if await self.backend.gem_exists(entry["gem_id"]):
                raise
        self.gems.remove(style_id)

    def style_gem_instructions(self, style_id: str) -> dict:
        style = self._style_or_raise(style_id)
        entry = self.gems.get(style_id)
        if entry is not None:
            return {"instructions": entry["instructions"], "custom": bool(entry.get("custom", False))}
        return {"instructions": default_instructions(style), "custom": False}

    def list_style_gems(self) -> list[dict]:
        return [{"style": sid, "gem_id": e["gem_id"], "name": e["name"], "custom": bool(e.get("custom", False))}
                for sid, e in self.gems.all().items()]

    async def delete_user_style(self, style_id: str) -> None:
        """Delete a user style and its Gem (if any)."""
        entry = self.gems.get(style_id)
        if entry is not None:
            await self._delete_gem_entry(style_id, entry)
        self.styles.delete(style_id)

    async def update_user_style(self, style_id: str, **fields) -> Style:
        """Edit a user style (fields as UserStyleStore.update) and sync its Gem, if any.

        The Gem name/description always follow the style; its instructions are regenerated from the
        template unless the user customised them."""
        known = self.catalog().styles.get(style_id)
        if known is not None and known.source != "user":
            raise GIAError(INVALID_STYLE, f"style {style_id!r} is built-in and cannot be edited")
        style = self.styles.update(style_id, **fields)
        entry = self.gems.get(style_id)
        if entry is not None and fields.keys() & _GEM_FIELDS:
            custom = bool(entry.get("custom", False))
            text = entry["instructions"] if custom else default_instructions(style)
            name = gem_name(style)
            try:
                await self.backend.update_gem(entry["gem_id"], name, text, description=style.description)
            except GIAError as e:
                if not await self.backend.gem_exists(entry["gem_id"]):
                    self.gems.remove(style_id)  # Gem gone upstream: the style is saved, drop the stale entry
                    return style
                raise GIAError(UPSTREAM_ERROR, f"style saved; Gem sync failed: {e}",
                               style=style_id, saved=True) from e
            self.gems.put(style_id, {**entry, "name": name, "instructions": text,
                                     "updated": datetime.now(timezone.utc).isoformat()})
        return style

    def describe_style(self, style_id: str, prompt: str | None = None) -> dict:
        """Offline description of a style plus the prompt that would be sent for `prompt`."""
        style = self._style_or_raise(style_id)
        preview = preview_prompt(self.catalog(), prompt=prompt or "<prompt>", style=style_id)
        entry = self.gems.get(style_id)
        return {"id": style.id, "name": style.name, "family": style.family, "description": style.description,
                "append": style.append, "credit": style.credit, "source": style.source,
                "has_ref": style.ref is not None,
                "gem": {"gem_id": entry["gem_id"], "custom": bool(entry.get("custom", False))} if entry else None,
                **preview}


    async def _guard(self) -> None:
        stop = float(self.settings.quota_stop_pct or 0)
        if stop <= 0:
            return
        q = await self.backend.quota()
        if not q:
            return
        for pct_key, reset_key in (("window_5h_pct", "resets_at_5h"), ("weekly_pct", "resets_at_weekly")):
            pct = q.get(pct_key)
            if isinstance(pct, (int, float)) and pct >= stop:
                raise GIAError(QUOTA_GUARD, f"{pct_key} at {pct}% >= GIA_QUOTA_STOP_PCT={stop:g}",
                               quota=q, resets_at=q.get(reset_key))

    def _style_ref_root(self, style: Style, ref: Path) -> Path:
        """Root that validate_trusted_image checks against: <state_dir>/styles for user styles,
        PACKAGE_ROOT for the packaged catalog, or the base catalog's own directory."""
        user_dir = Path(self.settings.state_dir) / "styles"
        base_dir = resolve_styles_path(self.settings).parent
        for c in (user_dir, PACKAGE_ROOT, base_dir):
            if ref.is_relative_to(c.resolve()):
                return c
        return user_dir if style.source == "user" else base_dir

    def _style_ref(self, style: Style) -> ResolvedRef:
        """Trusted reference image of a style: first ref sent to the backend, no allowlist."""
        ref = style.ref
        if ref is None:
            raise GIAError(INVALID_STYLE, f"style {style.id!r} has no reference image")
        try:
            p = validate_trusted_image(ref, self._style_ref_root(style, ref), self.settings)
        except GIAError as e:
            raise GIAError(INVALID_STYLE, f"style {style.id!r} reference image is not usable: {e}") from e
        return ResolvedRef(path=p, source=f"style:{style.id}", sha256=hashlib.sha256(p.read_bytes()).hexdigest())

    def _assign_collection(self, res: GenerateResult, collection: str) -> dict:
        coll = self.library.resolve_collection(collection, create=True)
        for key in self.result_keys(res):
            self.library.add(key, coll.id)
        return {"id": coll.id, "name": coll.name}

    async def _run(self, call, *, prompt, refs, uploads, style, style_text, template, template_vars, aspect_hint,
                   copy_to, collection=None) -> ServiceResult:
        if collection is not None and not collection.strip():
            raise GIAError(INVALID_COLLECTION, "collection name is empty")
        catalog = self.catalog() if (style or template) else StyleCatalog()
        final = build_prompt(catalog, prompt=prompt, style=style, style_text=style_text, template=template,
                             template_vars=template_vars, aspect_hint=aspect_hint)
        style_ref = self._style_ref(catalog.styles[style]) if style and style in catalog.styles and \
            catalog.styles[style].ref is not None else None
        upload_root = Path(self.settings.state_dir) / "uploads"
        upload_refs = [ResolvedRef(path=p, source=f"upload:{p.stem}",
                                   sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                       for p in (validate_trusted_image(u, upload_root, self.settings) for u in uploads or ())]
        user_refs = list(refs or [])
        total_refs = len(user_refs) + len(upload_refs) + (1 if style_ref is not None else 0)
        if total_refs > self.settings.max_refs:
            raise GIAError(INVALID_REF,
                           f"maximum {self.settings.max_refs} refs per call (style reference included)",
                           max_refs=self.settings.max_refs, got=total_refs)
        target = check_copy_dest(copy_to, self.settings) if copy_to else None
        await self._guard()
        with tempfile.TemporaryDirectory() as td:
            resolved = await resolve_refs(user_refs, self.settings, Path(td),
                                          lookup=self.state.generation, is_deleted=self.library.is_deleted)
            trusted = ([style_ref] if style_ref is not None else []) + upload_refs
            entry = self.gems.get(style) if style else None
            res = await call(final, [*trusted, *resolved], gem_id=entry["gem_id"] if entry else None)
        copies: dict[int, dict] = {}
        if target is not None:
            for i, img in enumerate(res.images):
                try:
                    copies[i] = {"copied_to": str(copy_file(img.path, target))}
                except OSError as e:
                    copies[i] = {"copy_error": f"{type(e).__name__}: {e}"}
        coll = self._assign_collection(res, collection) if collection is not None and res.record_id and res.images \
            else None
        warning = res.warning
        if warning and warning.get("code") == GEM_MISSING and style:
            entry = self.gems.get(style)
            if entry is not None and warning.get("gem_id") == entry["gem_id"]:
                self.gems.remove(style)
            # a mismatch means the conversation reused a stale gem_id: keep the live entry
        return ServiceResult(res, await self.backend.quota(), copies, collection=coll, warning=warning)

    async def generate(self, *, prompt, refs=None, uploads: Sequence[Path] = (), style=None, style_text=None,
                       template=None, template_vars=None, aspect_hint=None, model=None, project=None,
                       subdir=None, copy_to=None, collection=None, progress=None,
                       display_prompt=None, animation: dict | None = None) -> ServiceResult:
        model = model or self.settings.default_model
        settings = None
        shown = display_prompt or prompt
        if animation is not None:
            if template:
                raise GIAError(INVALID_ANIMATION, "animation cannot be combined with template")
            anim = validate_animation_request(animation)
            cols, rows, aspect_hint = GRIDS[anim["grid"]]
            prompt = sheet_prompt(prompt, cols, rows, anim["mode"], anim["key_color"])
            settings = {"cols": cols, "rows": rows, "mode": anim["mode"], "key_color": anim["key_color"],
                        "tolerance": 25, "fps": 10, "loop": "loop"}
        sr = await self._run(
            lambda final, resolved, gem_id=None: self.backend.generate(
                final, resolved, model=model, progress=progress, project=project, subdir=subdir,
                style=style, style_text=style_text, aspect_hint=aspect_hint, slug_source=shown,
                gem_id=gem_id),
            prompt=prompt, refs=refs, uploads=uploads, style=style, style_text=style_text, template=template,
            template_vars=template_vars, aspect_hint=aspect_hint, copy_to=copy_to, collection=collection)
        if settings is not None:
            await self._animate_result(sr, settings)
        return sr

    async def edit(self, conversation_id, *, prompt, refs=None, uploads: Sequence[Path] = (), style=None,
                   style_text=None, template=None, template_vars=None, aspect_hint=None, model=None,
                   project=None, subdir=None, copy_to=None, collection=None, progress=None,
                   display_prompt=None) -> ServiceResult:
        model = model or self.settings.default_model
        shown = display_prompt or prompt
        head = self._head_animation(conversation_id)
        if head is not None:
            prompt = prompt + edit_suffix(head)
        sr = await self._run(
            lambda final, resolved, gem_id=None: self.backend.edit(
                conversation_id, final, resolved, model=model, progress=progress, project=project,
                subdir=subdir, style=style, style_text=style_text, aspect_hint=aspect_hint,
                slug_source=shown, gem_id=gem_id),
            prompt=prompt, refs=refs, uploads=uploads, style=style, style_text=style_text, template=template,
            template_vars=template_vars, aspect_hint=aspect_hint, copy_to=copy_to, collection=collection)
        if head is not None:
            await self._animate_result(sr, {k: head[k] for k in _SETTINGS_FIELDS})
        return sr

    def list_generations(self, **filters) -> list[dict]:
        collection = filters.pop("collection", None)
        members = None
        if collection is not None:
            coll = self.library.get_collection(collection)
            if coll is None:
                raise GIAError(UNKNOWN_COLLECTION, f"collection {collection!r} does not exist",
                               valid=[c.id for c in self.library.collections()])
            members = set(self.library.members(coll.id))
        deleted = self.library.deleted()

        def visible_images(r) -> list[dict]:
            return [{"index": e.get("index", i), "path": e.get("path")}
                    for i, e in enumerate(r.images)
                    if f"{r.id}#{e.get('index', i)}" not in deleted]

        def wanted(r) -> bool:
            images = visible_images(r)
            if r.images and not images:
                return False
            return members is None or any(f"{r.id}#{e['index']}" in members for e in images)

        return [{"id": r.id, "ts": r.ts, "type": r.type, "conversation_id": r.conversation_id,
                 "prompt": display_prompt(r), "style": r.style, "project": r.project, "status": r.status,
                 "images": visible_images(r)}
                for r in self.state.list(where=wanted, **filters)]

    # -- saved prompts ----------------------------------------------------------
    def _prompt_id(self, prompt: str) -> str:
        """Resolve a prompt id (pr_xxxxxxxx) or name (case-insensitive) to its id."""
        key = (prompt or "").strip()
        items = self.prompts.list()
        for p in items:
            if p["id"] == key:
                return p["id"]
        for p in items:
            if p["name"].casefold() == key.casefold():
                return p["id"]
        raise GIAError(UNKNOWN_PROMPT, f"Unknown prompt: {prompt!r}", prompt=prompt)

    def list_prompts(self) -> list[dict]:
        return self.prompts.list()

    def save_prompt(self, name: str, text: str) -> dict:
        return self.prompts.create(name, text)

    def update_prompt(self, prompt: str, name: str | None = None, text: str | None = None) -> dict:
        return self.prompts.update(self._prompt_id(prompt), name=name, text=text)

    def delete_prompt(self, prompt: str) -> dict:
        pid = self._prompt_id(prompt)
        self.prompts.delete(pid)
        return {"deleted": pid}

    def use_prompt(self, prompt: str) -> dict:
        """Return the prompt and count one use."""
        return self.prompts.use(self._prompt_id(prompt))

    def list_collections(self) -> list[dict]:
        snap = self.library.snapshot()
        counts = {c["id"]: 0 for c in snap["collections"]}
        for v in snap["membership"].values():
            for cid in v:
                counts[cid] = counts.get(cid, 0) + 1
        return [{"id": c["id"], "name": c["name"], "description": c.get("description", ""),
                 "count": counts.get(c["id"], 0)}
                for c in snap["collections"]]

    def add_to_collection(self, image_ref: str, collection: str) -> dict:
        m = GEN_REF_RE.match(image_ref)
        if not m:
            raise GIAError(INVALID_REF, f"bad image ref {image_ref!r}; expected gen:g_<12 hex>[#index]")
        key = f"{m.group(1)}#{int(m.group(2) or 0)}"
        if key not in image_index(self.state, self.library, self.settings.output_root):
            raise GIAError(INVALID_REF, f"image {key} does not exist or was deleted")
        coll = self.library.resolve_collection(collection, create=True)
        return {"key": key, "collection": {"id": coll.id, "name": coll.name},
                "collections": self.library.add(key, coll.id)}

    def _entry(self, image_ref: str) -> ImageEntry:
        m = GEN_REF_RE.match(image_ref or "")
        if not m:
            raise GIAError(INVALID_REF, f"bad image ref {image_ref!r}; expected gen:g_<12 hex>[#index]")
        key = f"{m.group(1)}#{int(m.group(2) or 0)}"
        entry = image_index(self.state, self.library, self.settings.output_root).get(key)
        if entry is None:
            raise GIAError(INVALID_REF, f"image {key} does not exist or was deleted")
        return entry

    def _existing_collection(self, collection: str):
        return self.library.resolve_collection(collection, create=False)

    def rename_collection(self, collection: str, name: str, description: str | None = None) -> dict:
        coll = self._existing_collection(collection)
        new = self.library.update_collection(coll.id, name=name, description=description)
        return {"id": new.id, "name": new.name, "description": new.description,
                "count": len(self.library.members(new.id))}

    def delete_collection(self, collection: str) -> dict:
        coll = self._existing_collection(collection)
        count = len(self.library.members(coll.id))
        self.library.delete_collection(coll.id)
        return {"id": coll.id, "name": coll.name, "deleted": True, "images_affected": count}

    def remove_from_collection(self, image_ref: str, collection: str) -> dict:
        entry = self._entry(image_ref)
        coll = self._existing_collection(collection)
        return {"key": entry.key, "collection": {"id": coll.id, "name": coll.name},
                "collections": self.library.remove(entry.key, coll.id)}

    def set_favorite(self, image_ref: str, favorite: bool = True) -> dict:
        entry = self._entry(image_ref)
        self.library.set_favorite(entry.key, bool(favorite))
        return {"key": entry.key, "favorite": bool(favorite)}

    def get_image(self, image_ref: str) -> dict:
        e = self._entry(image_ref)
        r = e.record
        return {"ref": f"gen:{e.key}", "path": str(e.path), "prompt": display_prompt(r), "style": r.style,
                "model": r.model, "ts": r.ts, "width": e.width, "height": e.height,
                "favorite": self.library.is_favorite(e.key),
                "collections": self.library.collections_of(e.key),
                "conversation_id": r.conversation_id}

    def _purge_image(self, e: ImageEntry) -> None:
        """Delete an image's file, thumbnail, animation record + cache, and mark it deleted in the library."""
        e.path.unlink(missing_ok=True)
        gen = e.key.rsplit("#", 1)[0]
        (Path(self.settings.state_dir) / "thumbs" / f"{gen}_{e.index}.jpg").unlink(missing_ok=True)
        self.animations.remove(e.key)
        purge_cache(self.settings.state_dir, e.key)
        self.library.mark_deleted(e.key)

    # -- animations -------------------------------------------------------------
    def result_keys(self, res: GenerateResult) -> list[str]:
        """Image keys "<record_id>#<index>" of a result, mapped like _assign_collection."""
        rec = self.state.generation(res.record_id) if res.record_id else None
        keys = []
        for i, img in enumerate(res.images):
            idx = i
            if rec is not None:
                match = next((e for e in rec.images
                              if e.get("path") and Path(e["path"]) == Path(img.path)), None)
                if match is not None and match.get("index") is not None:
                    idx = int(match["index"])
            keys.append(f"{res.record_id}#{idx}")
        return keys

    def _build_animation(self, key: str, path: Path, settings: dict, cells: dict | None = None) -> dict:
        """Detect (unless `cells` is given), render and store the ficha plus its cached sheet. Blocking."""
        try:
            with Image.open(path) as im:
                im.load()
                img = im.copy()
        except (OSError, Image.DecompressionBombError, ValueError) as e:
            raise GIAError("invalid_image", f"image {key} cannot be decoded: {e}") from e
        if cells is None:
            cells = detect_cells(img, settings["cols"], settings["rows"], settings["mode"],
                                 settings["key_color"], settings["tolerance"])
        ficha = {**settings, **cells}
        sheet, meta = render_sheet(img, ficha)
        ficha.update(meta)
        ficha["frames"] = len(ficha["cells"])
        stored = self.animations.put(key, ficha)
        write_sheet(self.settings.state_dir, key, stored["rev"], sheet)
        return stored

    async def _animate_result(self, sr: ServiceResult, settings: dict) -> None:
        """Create a ficha (detected from scratch) for every saved image; failures leave no ficha and
        set an invalid_animation warning unless one is already present."""
        failed = False
        for key, img in zip(self.result_keys(sr.result), sr.result.images):
            try:
                await asyncio.to_thread(self._build_animation, key, Path(img.path), settings)
            except Exception:
                log.exception("animation processing failed for %s", key)
                failed = True
        if failed and sr.warning is None:
            p = error_payload(GIAError(INVALID_ANIMATION,
                                       "the image was saved but its animation could not be processed"))
            sr.warning = {"code": p["code"], "message": p["message"], "next_action": p["next_action"]}

    def _head_animation(self, conversation_id: str) -> dict | None:
        """Ficha of image 0 of the conversation's head record (see conversation_head), if any."""
        head = None
        for r in self.state.visible_records():
            if (r.conversation_id == conversation_id and r.type in ("generate", "edit")
                    and r.status in ("ok", "generated_not_saved")):
                head = r
        if head is None or not head.images:
            return None
        return self.animations.get(f"{head.id}#{head.images[0].get('index', 0)}")

    def _animation_key(self, image_ref: str) -> tuple[ImageEntry, str]:
        e = self._entry(image_ref)
        return e, e.key

    def get_animation(self, image_ref: str) -> dict:
        e, key = self._animation_key(image_ref)
        ficha = self.animations.get(key)
        if ficha is None:
            raise GIAError(UNKNOWN_ANIMATION, f"image {key} has no animation", key=key)
        return ficha

    def set_animation(self, image_ref: str, *, cols=None, rows=None, mode=None, key_color=None, tolerance=None,
                      fps=None, loop=None, redetect: bool = False) -> dict:
        """Create (cols and rows required) or update the image's ficha. Blocking: call from a thread."""
        e, key = self._animation_key(image_ref)
        given = validate_settings(cols=cols, rows=rows, mode=mode, key_color=key_color, tolerance=tolerance,
                                  fps=fps, loop=loop)
        old = self.animations.get(key)
        if old is None:
            if "cols" not in given or "rows" not in given:
                raise GIAError(INVALID_ANIMATION, "cols and rows are required to create an animation",
                               field="grid")
            base = {"mode": "sprite", "key_color": "green", "tolerance": 25, "fps": 10, "loop": "loop"}
        else:
            base = {k: old[k] for k in _SETTINGS_FIELDS}
        new = {**base, **given}
        validate_grid(new["cols"], new["rows"])
        if new["mode"] == "scene":
            new["key_color"] = None
        elif new["key_color"] is None:
            new["key_color"] = "green"
        new = {k: new[k] for k in _SETTINGS_FIELDS}
        if old is None or redetect or any(new[k] != old.get(k) for k in _DETECT_FIELDS):
            return self._build_animation(key, Path(e.path), new)
        return self.animations.put(key, {**old, **new})

    def delete_animation(self, image_ref: str) -> dict:
        e, key = self._animation_key(image_ref)
        if not self.animations.remove(key):
            raise GIAError(UNKNOWN_ANIMATION, f"image {key} has no animation", key=key)
        purge_cache(self.settings.state_dir, key)
        return {"key": key, "deleted": True}

    def animation_sheet(self, image_ref: str) -> Path:
        """Path of the normalized sheet PNG, regenerated if the cache file is missing. Blocking."""
        e, key = self._animation_key(image_ref)
        ficha = self.animations.get(key)
        if ficha is None:
            raise GIAError(UNKNOWN_ANIMATION, f"image {key} has no animation", key=key)
        path = cache_path(self.settings.state_dir, key, ficha["rev"])
        if path.is_file():
            return path
        settings = {k: ficha[k] for k in _SETTINGS_FIELDS}
        stored = self._build_animation(key, Path(e.path), settings, cells={
            "cells": ficha["cells"], "detected_by": ficha["detected_by"], "status": ficha["status"]})
        return cache_path(self.settings.state_dir, key, stored["rev"])

    def animation_export(self, image_ref: str) -> tuple[Path, dict, str]:
        """(sheet png path, descriptor json, base name) for exporting the animation."""
        e, key = self._animation_key(image_ref)
        path = self.animation_sheet(image_ref)
        ficha = self.animations.get(key)
        if ficha is None:
            raise GIAError(UNKNOWN_ANIMATION, f"image {key} has no animation", key=key)
        base = slugify(display_prompt(e.record), 30)
        meta = {"image": f"{base}.png", "frameWidth": ficha["frame_w"], "frameHeight": ficha["frame_h"],
                "frames": ficha["frames"], "columns": ficha["frames"], "rows": 1, "fps": ficha["fps"],
                "loop": ficha["loop"], "transparent": ficha["mode"] == "sprite"}
        return path, meta, base

    def delete_image(self, image_ref: str) -> dict:
        """Delete the local file and thumbnail and hide the image; Gemini's side is untouched."""
        e = self._entry(image_ref)
        self._purge_image(e)
        return {"key": e.key, "deleted": True}

    def conversation_head(self, key: str, *, index: dict | None = None) -> bool:
        """True if image `key` belongs to the head record of its conversation: the last record (by ts)
        of type generate/edit with status ok/generated_not_saved. A retry_download counts as its parent.
        `index` is an already built image_index (it is rebuilt when not given)."""
        key = self._bare_key(key)
        if key is None:
            return False
        if index is None:
            index = image_index(self.state, self.library, self.settings.output_root)
        entry = index.get(key)
        if entry is None:
            return False
        rec = entry.record
        rec_id = rec.parent_id if rec.type == "retry_download" and rec.parent_id else rec.id
        head = None
        for r in self.state.visible_records():
            if (r.conversation_id == rec.conversation_id and r.type in ("generate", "edit")
                    and r.status in ("ok", "generated_not_saved")):
                head = r
        return head is not None and head.id == rec_id

    @staticmethod
    def _bare_key(key: str) -> str | None:
        """'g_<12hex>[#i]' or 'gen:g_<12hex>[#i]' -> normalized 'g_<12hex>#<i>'; None if malformed."""
        m = GEN_REF_RE.match(key if (key or "").startswith("gen:") else f"gen:{key}")
        return f"{m.group(1)}#{int(m.group(2) or 0)}" if m else None

    def change_spec(self, target_key: str, change_prompt: str, ref_prompt: str, *, index: dict | None = None,
                    **common) -> dict:
        """JobQueue.submit arguments for changing image `target_key`: an edit when it is the head of its
        conversation, otherwise a new generation that takes it as a reference. `index`: see conversation_head."""
        key = self._bare_key(target_key) or target_key
        if self.conversation_head(key, index=index):
            return {"edit_of": f"gen:{key}", "prompt": change_prompt, **common}
        return {"refs": [f"gen:{key}"], "prompt": ref_prompt, **common}

    @staticmethod
    def _normalize_keys(keys, max_n: int) -> list[tuple[str, str]]:
        """[(original, "gen:<id>#<i>")]; accepts gen:<id>[#i] or <id>#<i>. Rejects empty, too many,
        non-string, malformed and duplicate keys with INVALID_QUERY."""
        if isinstance(keys, (str, bytes)) or not isinstance(keys, Sequence) or not keys:
            raise GIAError(INVALID_QUERY, "keys must be a non-empty list")
        if len(keys) > max_n:
            raise GIAError(INVALID_QUERY, f"too many keys: {len(keys)} (max {max_n})")
        out, seen = [], set()
        for k in keys:
            m = _KEY_RE.match(k) if isinstance(k, str) else None
            if m is None:
                raise GIAError(INVALID_QUERY, f"bad image key {str(k)[:60]!r}; expected gen:g_<12 hex>[#index] or g_<12 hex>#<index>")
            norm = f"gen:{m.group(1) or m.group(3)}#{int(m.group(2) or m.group(4) or 0)}"
            if norm in seen:
                raise GIAError(INVALID_QUERY, f"duplicate image key {k!r}")
            seen.add(norm)
            out.append((k, norm))
        return out

    def batch_images(self, action: str, keys, collection: str | None = None) -> dict:
        """Apply one action to up to MAX_BATCH_IMAGES images. Returns {"results": [{"key", "ok", "error"?}]}
        in input order; a failing key (unknown, deleted...) is reported there and does not stop the rest.
        An invalid action/key list or a missing collection argument raises GIAError(INVALID_QUERY).
        remove_from_collection with an unknown collection raises one global unknown_collection error before
        anything runs. add_to_collection creates/resolves the collection once, only if at least one key
        exists (an invalid name then raises invalid_collection globally); if no key exists, nothing is
        created and each key reports invalid_ref. The history is read once."""
        if action not in BATCH_ACTIONS:
            raise GIAError(INVALID_QUERY, f"invalid action {action!r}", valid=list(BATCH_ACTIONS))
        needs_coll = action in ("add_to_collection", "remove_from_collection")
        if needs_coll and (not isinstance(collection, str) or not collection.strip()):
            raise GIAError(INVALID_QUERY, f"action {action} requires a collection")
        pairs = self._normalize_keys(keys, MAX_BATCH_IMAGES)
        index = image_index(self.state, self.library, self.settings.output_root)
        coll = None
        if action == "remove_from_collection":
            coll = self._existing_collection(collection)
        elif action == "add_to_collection":
            if any(ref[len("gen:"):] in index for _, ref in pairs):
                coll = self.library.resolve_collection(collection, create=True)

        def apply(e: ImageEntry) -> None:
            if action in ("favorite", "unfavorite"):
                self.library.set_favorite(e.key, action == "favorite")
            elif action == "add_to_collection":
                self.library.add(e.key, coll.id)
            elif action == "remove_from_collection":
                self.library.remove(e.key, coll.id)
            else:
                self._purge_image(e)

        results = []
        for original, ref in pairs:
            key = ref[len("gen:"):]
            e = index.get(key)
            if e is None:
                err = GIAError(INVALID_REF, f"image {key} does not exist or was deleted")
                results.append({"key": original, "ok": False, "error": error_payload(err)})
                continue
            try:
                apply(e)
                results.append({"key": original, "ok": True})
            except GIAError as err:
                results.append({"key": original, "ok": False, "error": error_payload(err)})
            except Exception:
                log.exception("batch %s failed for %s", action, ref)
                results.append({"key": original, "ok": False,
                                "error": error_payload(GIAError(UPSTREAM_ERROR, "operation failed"))})
        return {"results": results}

    def zip_images(self, keys) -> tuple[Path, str]:
        """Zip (ZIP_STORED) the given images into <state_dir>/tmp_downloads/ (0600) and return
        (path, download filename); the caller must delete the file once sent (stale ones, older than
        an hour, are removed whenever a new zip is created).

        All-or-nothing: if any key is unknown, deleted or its file is missing/outside output_root,
        GIAError(INVALID_REF, missing=[...]) lists those keys and no zip is made. A source image that
        vanishes or becomes unreadable while zipping also raises INVALID_REF with that key in `missing`
        (partial zip removed); other I/O errors (e.g. disk full writing the zip) propagate as OSError
        after removing the partial zip. Max
        MAX_ZIP_IMAGES images and MAX_ZIP_BYTES in total (INVALID_QUERY). Entry names are generated
        (<date>_<slug>_<n>.<ext>), never client or filesystem paths."""
        pairs = self._normalize_keys(keys, MAX_ZIP_IMAGES)
        index = image_index(self.state, self.library, self.settings.output_root)
        root = Path(self.settings.output_root).resolve()
        entries, missing = [], []
        for original, ref in pairs:
            e = index.get(ref[len("gen:"):])
            real = e.path.resolve() if e is not None else None
            if real is None or not real.is_relative_to(root) or not real.is_file():
                missing.append(original)
            else:
                entries.append((original, e, real))
        if missing:
            raise GIAError(INVALID_REF, f"{len(missing)} image(s) do not exist or were deleted: {missing}",
                           missing=missing)
        try:
            total = 0
            for original, _, real in entries:
                current = original
                total += real.stat().st_size
        except OSError as err:
            raise GIAError(INVALID_REF, f"an image file is no longer readable: {err.__class__.__name__}",
                           missing=[current])
        if total > MAX_ZIP_BYTES:
            raise GIAError(INVALID_QUERY, f"selection is {total} bytes; the zip limit is {MAX_ZIP_BYTES}")
        tmp_dir = Path(self.settings.state_dir) / "tmp_downloads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _TMP_ZIP_MAX_AGE_S
        for old in tmp_dir.glob("*.zip"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        fd, name = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)  # mode 0600
        path = Path(name)
        current = None
        try:
            with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as zf:
                for n, (original, e, real) in enumerate(entries, 1):
                    current = (original, real)
                    ext = re.sub(r"[^a-z0-9]", "", real.suffix.lower())[:5] or "bin"
                    stamp = re.sub(r"[^0-9-]", "", str(e.record.ts)[:10]) or "undated"
                    zf.write(real, f"{stamp}_{slugify(display_prompt(e.record), 30)}_{n}.{ext}")
        except OSError as err:
            path.unlink(missing_ok=True)
            # only a failure on the source image is invalid_ref; writing the zip (ENOSPC, perms) propagates
            if current is not None and err.filename is not None and Path(err.filename) == current[1]:
                raise GIAError(INVALID_REF, f"an image file is no longer readable: {err.__class__.__name__}",
                               missing=[current[0]])
            raise
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path, f"gemini-images-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.zip"

    async def generate_batch(self, prompts: Sequence[str], *, style=None, collection=None, aspect_hint=None,
                             model=None, **kw) -> dict:
        """Run up to MAX_BATCH prompts sequentially with shared options.

        Per-prompt failures are reported in `results` and the batch continues, except for quota/auth
        errors, which stop it (`stopped_at` = index of the prompt that hit them)."""
        prompts = list(prompts)
        if not prompts or len(prompts) > MAX_BATCH:
            raise GIAError(INVALID_QUERY, f"prompts must hold 1..{MAX_BATCH} items, got {len(prompts)}")
        results: list[dict] = []
        stopped_at = None
        for i, prompt in enumerate(prompts):
            try:
                payload = to_payload(await self.generate(prompt=prompt, style=style, collection=collection,
                                                         aspect_hint=aspect_hint, model=model, **kw))
            except GIAError as e:
                payload = error_payload(e)
            except Exception:
                log.exception("batch prompt %d failed", i)
                payload = error_payload(GIAError(UPSTREAM_ERROR, "generation failed"))
            results.append(payload)
            if payload.get("code") in _BATCH_STOP_CODES:
                stopped_at = i
                break
        if stopped_at is not None:
            status = "stopped"
        else:
            status = "ok" if all(r["status"] == "ok" for r in results) else "partial"
        return {"status": status, "results": results, "stopped_at": stopped_at}


def to_payload(sr: ServiceResult) -> dict:
    r = sr.result
    images = []
    for i, s in enumerate(r.images):
        item = {"path": str(s.path), "width": s.width, "height": s.height, "bytes": s.nbytes,
                "format": s.format, "full_size": s.full_size, "index": i}
        item.update(sr.copies.get(i, {}))
        images.append(item)
    payload = {"status": r.status, "code": r.code, "conversation_id": r.conversation_id, "images": images,
               "gemini_text": r.gemini_text, "elapsed_s": r.elapsed_s, "quota": sr.quota}
    if r.status == "generated_not_saved":
        payload["details"] = {"reason": r.save_error}
    if sr.collection is not None:
        payload["collection"] = sr.collection
    if sr.warning is not None:
        payload["warning"] = sr.warning
    return payload
