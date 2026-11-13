"""Musa web app: Starlette server with key/Host/Origin security, gallery API, thumbnails and SSE.

Security middleware order (pure ASGI, applied to every HTTP request):
1. Host check (anti DNS-rebinding): only 127.0.0.1:<port> / localhost:<port>.
2. Key check: everything except /static/* needs cookie `gia_key` or header
   `X-GIA-Key` (constant-time compare). `GET /?key=<k>` bootstraps the cookie.
3. Origin check on mutating methods (anti-CSRF): Origin must be the app itself.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from PIL import Image, ImageOps
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .compositions import CompositionManager, QueueRunner, public_view
from .errors import (GIAError, INVALID_COLLECTION, INVALID_PROMPT, INVALID_QUERY, INVALID_REF,
                     INVALID_STYLE, UNKNOWN_COLLECTION, error_payload)
from .gallery import image_index, query_images
from .jobs import AUTO_MODEL, JobQueue, model_entries
from .library import LibraryStore
from .service import ImageService
from .state import StateStore, display_prompt
from .style_store import MAX_IMAGE_BYTES, UserStyleStore
from .styles import PACKAGE_ROOT, preview_prompt, resolve_styles_path
from .uploads import UploadStore

log = logging.getLogger(__name__)

GEN_RE = re.compile(r"^g_[0-9a-f]{12}$")
KEY_RE = re.compile(r"^(g_[0-9a-f]{12})#(\d+)$")
THUMB_SIZE = 640
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


WEB_NEXT_ACTION = {
    "unauthorized": "Open /?key=<key> to set the gia_key cookie, or send the X-GIA-Key header.",
    "forbidden": "Send a valid Host header and, for mutating methods, an Origin matching the app.",
    "unknown_image": "The image is not in the gallery index (missing, deleted, or outside output_root).",
    "invalid_image": "The image file cannot be decoded (for example a decompression bomb).",
    "invalid_json": "Send a valid JSON object as the request body.",
    "invalid_query": "Check the query parameters (sort, since/until dates, limit >= 1) against the API contract.",
    "invalid_prompt": "Send a unique name (max 80 chars) and a non-empty text (max 4000 chars); "
                      "at most 200 prompts can be stored (delete some).",
    "unknown_prompt": "The prompt id or name is not saved; list them with GET /api/prompts.",
    "queue_full": "The queue is full (20 pending jobs); wait for jobs to finish or cancel queued ones.",
    "job_running": "The job is already running and cannot be cancelled; wait for it to finish.",
    "unknown_job": "The job is not in the queue (unknown id, or finished more than 10 minutes ago).",
    "unknown_upload": "The upload id is unknown or expired (uploads are deleted after 24 hours).",
    "internal_error": "Unexpected server error; check the server logs.",
    "not_supported": "This action is only available on macOS.",
}


def _web_payload(code: str, message: str, **details) -> dict:
    """Full error shape for web-specific codes (never added to errors.NEXT_ACTION,
    which belongs to the agent API). Web errors are never retryable."""
    return {"status": "error", "code": code, "message": message, "details": details,
            "retryable": False,
            "next_action": WEB_NEXT_ACTION.get(code, "Check the request and retry.")}


def _err(status: int, code: str, message: str, **extra) -> JSONResponse:
    payload = _web_payload(code, message)
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


def _gia_response(e: GIAError) -> JSONResponse:
    code, msg = e.code, str(e)
    if code.startswith("unknown_"):
        status = 404
    elif code == "queue_full":
        status = 429
    elif code == "job_running":
        status = 409
    elif code == "invalid_image":
        status = 422
    elif code == INVALID_COLLECTION and "already exists" in msg:
        status = 409
    elif code == INVALID_STYLE and ("not a png/jpeg" in msg or "larger than" in msg):
        status = 422
    elif code.startswith("invalid_"):
        status = 400
    else:
        status = 400
    if code in WEB_NEXT_ACTION:
        body = _web_payload(code, msg, **e.details)
    else:
        body = error_payload(e)
    return JSONResponse(body, status_code=status)


async def _gia_handler(request: Request, exc: GIAError) -> JSONResponse:
    return _gia_response(exc)


async def _internal_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return _err(500, "internal_error", "internal server error")


class _SecurityMiddleware:
    """Host -> key -> origin, in that order. /static/* is public."""

    def __init__(self, app, key: str, port: int):
        self.app, self.key, self.port = app, key, port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get("host", "") not in self.hosts:
            await _err(403, "forbidden", "host not allowed")(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        if not path.startswith("/static"):
            query = scope.get("query_string", b"").decode("latin-1")
            if path == "/" and method == "GET":
                given = parse_qs(query).get("key", [None])[0]
                if given is not None:
                    if secrets.compare_digest(given.encode(), self.key.encode()):
                        resp = RedirectResponse("/", status_code=303)
                        resp.set_cookie("gia_key", self.key, httponly=True, samesite="strict", path="/")
                        await resp(scope, receive, send)
                        return
                    await _err(401, "unauthorized", "invalid key")(scope, receive, send)
                    return
            cookie = SimpleCookie()
            cookie.load(headers.get("cookie", ""))
            provided = (cookie.get("gia_key").value if cookie.get("gia_key") else None) \
                or headers.get("x-gia-key", "")
            if not provided or not secrets.compare_digest(provided.encode(), self.key.encode()):
                await _err(401, "unauthorized", "missing or invalid key")(scope, receive, send)
                return
        if method in MUTATING and headers.get("origin", "") not in self.origins:
            await _err(403, "forbidden", "origin not allowed")(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _Ctx:
    """Per-app shared state, reachable from every handler via request.app.state."""

    def __init__(self, settings, service: ImageService, library: LibraryStore,
                 user_styles: UserStyleStore, poll_interval: float, *,
                 uploads: UploadStore | None = None,
                 now: Callable[[], float] | None = None,
                 purge_interval_s: float = 3600.0):
        self.settings = settings
        self.service = service
        self.library = library
        self.user_styles = user_styles
        self.poll_interval = poll_interval
        self.stop_event = asyncio.Event()
        self.uploads = uploads or UploadStore(settings.state_dir, settings)
        self.jobs: JobQueue | None = None
        self.compositions: CompositionManager | None = None
        self._now = now or time.time
        self.quota_cache: dict | None = None
        self.quota_cached_at = 0.0
        self.models_cache: dict | None = None   # /api/models body, MODELS_TTL_S
        self.models_cached_at = 0.0
        self.jobs_seq = 0
        self.jobs_event = asyncio.Event()
        self._finished_jobs: set[str] = set()
        self.purge_interval_s = purge_interval_s
        self._purge_task: asyncio.Task | None = None

    def now(self) -> float:
        return self._now()

    def _on_jobs_change(self) -> None:
        """Sync callback from JobQueue: wake SSE streams and invalidate the quota
        cache once a job finishes (it may have spent quota)."""
        self.jobs_seq += 1
        self.jobs_event.set()
        if self.jobs is not None:
            finished = {j.id for j in self.jobs.list() if j.status in ("done", "error")}
            if finished - self._finished_jobs:  # a new finished job may have spent quota
                self.quota_cache = None
            self._finished_jobs = finished

    def ensure_purger(self) -> None:
        """Start the hourly upload purge lazily (next to the queue worker, which
        also starts lazily on the first submit)."""
        if self._purge_task is None or self._purge_task.done():
            self._purge_task = asyncio.create_task(self._purge_loop())

    async def _purge_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.uploads.purge()
            except OSError:
                log.exception("upload purge failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.purge_interval_s)
            except asyncio.TimeoutError:
                pass

    async def aclose(self) -> None:
        if self.jobs is not None:
            await self.jobs.aclose()
        if self._purge_task is not None:
            self._purge_task.cancel()
            try:
                await self._purge_task
            except asyncio.CancelledError:
                pass
            self._purge_task = None

    def index(self) -> dict:
        return image_index(self.service.state, self.library, self.settings.output_root)

    def entry(self, gen: str, i) -> object | None:
        """Index entry for gen#i, or None. Anything outside the index (and therefore
        outside output_root) is a 404: client-supplied paths are never trusted."""
        if not GEN_RE.match(gen or ""):
            return None
        try:
            i = int(i)
        except (TypeError, ValueError):
            return None
        e = self.index().get(f"{gen}#{i}")
        if e is None:
            return None
        if not e.path.resolve().is_relative_to(self.settings.output_root.resolve()):
            return None
        return e

    def thumb_path(self, gen: str, i: int) -> Path:
        return self.settings.state_dir / "thumbs" / f"{gen}_{i}.jpg"


def _ctx(request: Request) -> _Ctx:
    return request.app.state.ctx


def _not_found(gen, i):
    return _err(404, "unknown_image", f"image {gen}#{i} not found")


async def _json_body(request: Request) -> dict:
    """Parse the request body as a JSON object; anything else is a 400 invalid_json."""
    try:
        body = await request.json()
    except Exception:
        raise GIAError("invalid_json", "request body is not valid JSON")
    if not isinstance(body, dict):
        raise GIAError("invalid_json", "request body must be a JSON object")
    return body


BODY_SLACK_BYTES = 64 * 1024  # multipart overhead allowance on top of the file cap


def _reject_oversized_body(request: Request, max_bytes: int, code: str) -> None:
    """Reject before parsing multipart when Content-Length already exceeds the cap."""
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        size = int(cl)
    except ValueError:
        return
    if size > max_bytes + BODY_SLACK_BYTES:
        limit = max_bytes // (1024 * 1024)
        human = f"{limit} MB" if limit else f"{(max_bytes + BODY_SLACK_BYTES) // 1024} KB"  # effective cap
        raise GIAError(code, f"request body larger than {human}")


def _item_json(ctx: _Ctx, e, dims_cache: dict, anims: dict) -> dict:
    rec = e.record
    catalog = ctx.service.catalog()
    style = catalog.styles.get(rec.style) if rec.style else None
    w, h = e.width, e.height
    if (w is None or h is None) and e.key not in dims_cache:
        try:
            with Image.open(e.path) as im:
                dims_cache[e.key] = im.size
        except Exception:  # includes Image.DecompressionBombError: dims stay None
            dims_cache[e.key] = (None, None)
        w, h = dims_cache[e.key]
    return {
        "key": e.key, "gen_id": rec.id, "index": e.index,
        "conversation_id": rec.conversation_id,
        "prompt": display_prompt(rec),
        "style": rec.style,
        "style_name": style.name if style else None,
        "model": rec.model, "ts": rec.ts, "ratio": rec.aspect_hint,
        "width": w, "height": h,
        "favorite": ctx.library.is_favorite(e.key),
        "collections": ctx.library.collections_of(e.key),
        "thumb_url": f"/api/thumb/{rec.id}/{e.index}",
        "image_url": f"/api/image/{rec.id}/{e.index}",
        "ref": f"gen:{rec.id}#{e.index}",
        "animation": _item_animation(anims.get(e.key)),
    }


def _style_json(ctx: _Ctx, s) -> dict:
    gem = ctx.service.gems.get(s.id)
    return {"id": s.id, "name": s.name, "family": s.family, "description": s.description, "append": s.append,
            "credit": s.credit, "has_ref": s.ref is not None, "source": s.source,
            "cover_url": f"/api/styles/{s.id}/cover" if s.ref else None,
            "gem": {"gem_id": gem["gem_id"], "name": gem["name"],
                    "custom": bool(gem.get("custom", False))} if gem else None}


def _collection_json(ctx: _Ctx, c, idx: dict) -> dict:
    members = [k for k in ctx.library.members(c.id) if k in idx]
    members.sort(key=lambda k: (idx[k].record.ts, k), reverse=True)
    covers = [f"/api/thumb/{k.split('#')[0]}/{k.split('#')[1]}" for k in members[:3]]
    return {"id": c.id, "name": c.name, "description": c.description,
            "count": len(members), "covers": covers}


# -- pages & files ---------------------------------------------------------

class _NoCacheStatic(StaticFiles):
    """Code and styles revalidate on every load (ETag -> 304), so a `musa update` is never masked by a stale browser copy."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css", ".html", ".json")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


async def index_page(request: Request):
    web = PACKAGE_ROOT / "web"
    # ?v=<mtime>: cada versión del código tiene URL propia, así una copia vieja en la caché del navegador nunca la tapa.
    v = int(max((web / n).stat().st_mtime for n in ("app.js", "app.css", "index.html", "musa-scene.js")))
    html = (web / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css", "musa-scene.js"):
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

async def manifest_page(request: Request):
    path = PACKAGE_ROOT / "web" / "manifest.json"
    if path.is_file():
        resp = FileResponse(path, media_type="application/manifest+json")
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    return JSONResponse({})


async def get_update_endpoint(request: Request):
    from .updates import check_update
    ctx = _ctx(request)
    force = request.query_params.get("force") == "1"
    info = await asyncio.to_thread(check_update, ctx.settings.state_dir, force=force)
    return JSONResponse(info)


async def get_image(request: Request):
    ctx = _ctx(request)
    e = ctx.entry(request.path_params["gen"], request.path_params["i"])
    if e is None:
        return _not_found(request.path_params["gen"], request.path_params["i"])
    return FileResponse(e.path)


def _build_thumb(src: Path, dst: Path) -> None:
    """Atomic thumbnail: write to a unique tmp file in the same dir, then os.replace."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_SIZE, THUMB_SIZE))
            im.convert("RGB").save(tmp, "JPEG", quality=80)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def get_thumb(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    e = ctx.entry(gen, i)
    if e is None:
        return _not_found(gen, i)
    thumb = ctx.thumb_path(gen, int(i))

    def _ensure() -> Path | None:
        if thumb.exists() and thumb.stat().st_mtime >= e.path.stat().st_mtime:
            return thumb
        try:
            _build_thumb(e.path, thumb)
        except Image.DecompressionBombError:
            # A bomb is not servable as a thumbnail; treat the image as unavailable (404),
            # not as a style error.
            return None
        return thumb

    path = await run_in_threadpool(_ensure)
    if path is None:
        return _err(404, "invalid_image", f"image {gen}#{i} cannot be decoded as a thumbnail")
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# -- generations -----------------------------------------------------------

async def list_generations(request: Request):
    ctx = _ctx(request)
    q = request.query_params
    idx = ctx.index()
    collection = q.get("collection")
    if collection is not None:
        coll = ctx.library.get_collection(collection)  # accepts id or name
        if coll is None:
            raise GIAError("unknown_collection", f"collection {collection!r} does not exist",
                           valid=[c.id for c in ctx.library.collections()])
        collection = coll.id  # query_images expects the id
    styles = ctx.service.catalog().styles
    try:
        entries, total = query_images(
            idx, ctx.library,
            query=q.get("query"), collection=collection,
            filter=q.get("filter", "all"), style=q.get("style"),
            model=q.get("model"), sort=q.get("sort", "newest"),
            since=q.get("since"), until=q.get("until"),
            style_names={sid: s.name for sid, s in styles.items()},
            limit=int(q.get("limit", 60)), offset=int(q.get("offset", 0)))
    except (ValueError, OverflowError) as e:
        raise GIAError(INVALID_QUERY, str(e))
    dims: dict = {}
    anims = ctx.service.animations.all()  # one read per request
    return JSONResponse({"items": [_item_json(ctx, e, dims, anims) for e in entries], "total": total})


# -- saved prompts -----------------------------------------------------------

MAX_PROMPT_BODY_BYTES = 64 * 1024


async def _prompt_body(request: Request) -> dict:
    _reject_oversized_body(request, MAX_PROMPT_BODY_BYTES, INVALID_PROMPT)
    body = await _json_body(request)
    for k in ("name", "text"):
        if k in body and not isinstance(body[k], str):
            raise GIAError(INVALID_PROMPT, f"field {k!r} must be a string")
    return body


async def list_prompts(request: Request):
    return JSONResponse(_ctx(request).service.list_prompts())


async def create_prompt(request: Request):
    body = await _prompt_body(request)
    p = _ctx(request).service.save_prompt(body.get("name", ""), body.get("text", ""))
    return JSONResponse(p, status_code=201)


async def update_prompt(request: Request):
    body = await _prompt_body(request)
    p = _ctx(request).service.update_prompt(request.path_params["pid"],
                                            name=body.get("name"), text=body.get("text"))
    return JSONResponse(p)


async def delete_prompt(request: Request):
    return JSONResponse(_ctx(request).service.delete_prompt(request.path_params["pid"]))


async def use_prompt(request: Request):
    return JSONResponse(_ctx(request).service.use_prompt(request.path_params["pid"]))


# -- batch actions / zip download ---------------------------------------------

MAX_BATCH_BODY_BYTES = 64 * 1024


async def batch_images(request: Request):
    ctx = _ctx(request)
    _reject_oversized_body(request, MAX_BATCH_BODY_BYTES, INVALID_QUERY)
    body = await _json_body(request)
    action, collection = body.get("action"), body.get("collection")
    if collection is not None and not isinstance(collection, str):
        raise GIAError(INVALID_QUERY, "field 'collection' must be a string")
    result = await run_in_threadpool(ctx.service.batch_images, action, body.get("keys"), collection)
    return JSONResponse(result)


class _TempFileResponse(FileResponse):
    """FileResponse that deletes its file once the send ends, however it ends (done, client
    disconnect or exception), unlike a BackgroundTask, which only runs after a clean send."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(Path(self.path).unlink, missing_ok=True)


async def zip_images(request: Request):
    ctx = _ctx(request)
    _reject_oversized_body(request, MAX_BATCH_BODY_BYTES, INVALID_QUERY)
    body = await _json_body(request)
    try:
        path, filename = await run_in_threadpool(ctx.service.zip_images, body.get("keys"))
    except GIAError as e:
        if e.code == INVALID_REF:  # unknown/deleted/unreadable keys: 404 (details.missing lists unknown ones)
            return JSONResponse(error_payload(e), status_code=404)
        raise
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    resp = _TempFileResponse(path, media_type="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp



# -- favorites / delete / reveal -------------------------------------------

async def set_favorite(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    e = ctx.entry(gen, i)
    if e is None:
        return _not_found(gen, i)
    fav = request.method == "PUT"
    ctx.library.set_favorite(e.key, fav)
    return JSONResponse({"key": e.key, "favorite": fav})


async def delete_image(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    e = ctx.entry(gen, i)
    if e is None:
        return _not_found(gen, i)

    try:
        res = await run_in_threadpool(ctx.service.delete_image, f"gen:{gen}#{i}")
    except GIAError as err:
        if err.code == INVALID_REF:  # deleted between the index check and the call
            return _not_found(gen, i)
        raise
    return JSONResponse({"key": res["key"], "deleted": True})


async def reveal_image(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    e = ctx.entry(gen, i)
    if e is None:
        return _not_found(gen, i)
    if sys.platform != "darwin":
        return _err(501, "not_supported", "reveal is only available on macOS")
    subprocess.run(["open", "-R", str(e.path)], check=False)
    return JSONResponse({"ok": True})


# -- animations --------------------------------------------------------------

_ANIMATION_FIELDS = ("cols", "rows", "mode", "key_color", "tolerance", "fps", "loop")


def _animation_json(ficha: dict) -> dict:
    gen, _, i = ficha["key"].partition("#")
    return {**ficha, "sheet_url": f"/api/animations/{gen}/{i}/sheet.png?rev={ficha['rev']}"}


def _item_animation(ficha: dict | None) -> dict | None:
    if ficha is None:
        return None
    a = _animation_json(ficha)
    return {k: a[k] for k in ("frames", "fps", "loop", "mode", "status", "rev", "frame_w", "frame_h",
                              "sheet_url")}


async def get_animation(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    if ctx.entry(gen, i) is None:
        return _not_found(gen, i)
    ficha = await run_in_threadpool(ctx.service.get_animation, f"gen:{gen}#{int(i)}")
    return JSONResponse(_animation_json(ficha))


async def put_animation(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    if ctx.entry(gen, i) is None:
        return _not_found(gen, i)
    _reject_oversized_body(request, MAX_BATCH_BODY_BYTES, "invalid_animation")
    body = await _json_body(request)
    extra = sorted(set(body) - set(_ANIMATION_FIELDS) - {"redetect"})
    if extra:
        raise GIAError("invalid_animation", f"unknown animation fields: {', '.join(extra)}", fields=extra)
    redetect = body.get("redetect", False)
    if not isinstance(redetect, bool):
        raise GIAError("invalid_animation", "redetect must be a boolean", field="redetect")
    fields = {k: body[k] for k in _ANIMATION_FIELDS if k in body}
    ficha = await run_in_threadpool(ctx.service.set_animation, f"gen:{gen}#{int(i)}", redetect=redetect, **fields)
    return JSONResponse(_animation_json(ficha))


async def delete_animation(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    if ctx.entry(gen, i) is None:
        return _not_found(gen, i)
    return JSONResponse(await run_in_threadpool(ctx.service.delete_animation, f"gen:{gen}#{int(i)}"))


async def animation_sheet(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    if ctx.entry(gen, i) is None:
        return _not_found(gen, i)
    path = await run_in_threadpool(ctx.service.animation_sheet, f"gen:{gen}#{int(i)}")
    resp = FileResponse(path, media_type="image/png")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


def _build_animation_zip(state_dir: Path, png: Path, meta: dict, base: str) -> Path:
    tmp_dir = Path(state_dir) / "tmp_downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)  # mode 0600
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as zf:
            zf.write(png, f"{base}.png")
            zf.writestr(f"{base}.json", json.dumps(meta, indent=2))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


async def animation_export(request: Request):
    ctx = _ctx(request)
    gen, i = request.path_params["gen"], request.path_params["i"]
    if ctx.entry(gen, i) is None:
        return _not_found(gen, i)
    png, meta, base = await run_in_threadpool(ctx.service.animation_export, f"gen:{gen}#{int(i)}")
    path = await run_in_threadpool(_build_animation_zip, ctx.settings.state_dir, png, meta, base)
    resp = _TempFileResponse(path, media_type="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{base}-animacion.zip"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


# -- collections -------------------------------------------------------------

async def list_collections(request: Request):
    ctx = _ctx(request)
    idx = ctx.index()
    return JSONResponse([_collection_json(ctx, c, idx) for c in ctx.library.collections()])


async def create_collection(request: Request):
    ctx = _ctx(request)
    body = await _json_body(request)
    c = ctx.library.create_collection(str(body.get("name") or ""),
                                      str(body.get("description") or ""))
    return JSONResponse(_collection_json(ctx, c, ctx.index()), status_code=201)


async def update_collection(request: Request):
    ctx = _ctx(request)
    body = await _json_body(request)
    c = ctx.library.update_collection(
        request.path_params["cid"],
        name=body["name"] if "name" in body else None,
        description=body["description"] if "description" in body else None)
    return JSONResponse(_collection_json(ctx, c, ctx.index()))


async def delete_collection(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    ctx.library.delete_collection(cid)
    return JSONResponse({"deleted": cid})


def _key_entry(ctx: _Ctx, key: str):
    m = KEY_RE.match(key or "")
    if not m:
        return None, _err(400, "invalid_ref", f"bad image key {key!r}; expected g_<12 hex>#<index>")
    e = ctx.entry(m.group(1), m.group(2))
    if e is None:
        return None, _not_found(m.group(1), m.group(2))
    return e, None


async def add_collection_item(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    if ctx.library.get_collection(cid) is None:
        raise GIAError("unknown_collection", f"collection {cid!r} does not exist",
                       valid=[c.id for c in ctx.library.collections()])
    body = await _json_body(request)
    e, err = _key_entry(ctx, str(body.get("key") or ""))
    if err is not None:
        return err
    return JSONResponse({"key": e.key, "collections": ctx.library.add(e.key, cid)})


async def remove_collection_item(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    gen, i = request.path_params["gen"], request.path_params["i"]
    if ctx.library.get_collection(cid) is None:
        raise GIAError("unknown_collection", f"collection {cid!r} does not exist",
                       valid=[c.id for c in ctx.library.collections()])
    e = ctx.entry(gen, i)
    if e is None:
        return _not_found(gen, i)
    return JSONResponse({"key": e.key, "collections": ctx.library.remove(e.key, cid)})


# -- styles ------------------------------------------------------------------

async def list_styles(request: Request):
    ctx = _ctx(request)
    return JSONResponse([_style_json(ctx, s) for s in ctx.service.catalog().styles.values()])


async def style_cover(request: Request):
    ctx = _ctx(request)
    sid = request.path_params["sid"]
    s = ctx.service.catalog().styles.get(sid)
    if s is None or s.ref is None:
        return _err(404, "unknown_style", f"style {sid!r} has no cover")
    ref = s.ref.resolve()
    allowed = (PACKAGE_ROOT.resolve(), (ctx.settings.state_dir / "styles").resolve(),
               resolve_styles_path(ctx.settings).parent.resolve())
    if not any(ref.is_relative_to(a) for a in allowed):
        return _err(404, "unknown_style", f"style cover of {sid!r} is outside the allowed roots")
    return FileResponse(ref)


async def create_style(request: Request):
    ctx = _ctx(request)
    ctype = request.headers.get("content-type", "")
    image = None
    if ctype.startswith("multipart/form-data"):
        _reject_oversized_body(request, MAX_IMAGE_BYTES, INVALID_STYLE)  # 422 before parsing
        async with request.form() as form:  # closes spooled upload files on exit
            name = str(form.get("name") or "")
            description = str(form.get("description") or "")
            append = str(form.get("append") or "")
            family = str(form.get("family") or "otros")
            upload = form.get("file")
            if upload is not None and hasattr(upload, "read"):
                data = await upload.read(MAX_IMAGE_BYTES + 1)  # never read more than 20 MB + 1 byte
                image = (data, upload.filename or "upload")
    else:
        body = await _json_body(request)
        name = str(body.get("name") or "")
        description = str(body.get("description") or "")
        append = str(body.get("append") or "")
        family = str(body.get("family") or "otros")
        if body.get("from_key"):
            e, err = _key_entry(ctx, str(body["from_key"]))
            if err is not None:
                return err
            image = (e.path.read_bytes(), e.path.name)
    s = ctx.user_styles.create(name=name, description=description, append=append,
                               family=family, image=image,
                               taken_ids=set(ctx.service.catalog().styles))
    return JSONResponse(_style_json(ctx, s), status_code=201)


async def delete_style(request: Request):
    ctx = _ctx(request)
    sid = request.path_params["sid"]
    s = ctx.service.catalog().styles.get(sid)
    if s is None:
        raise GIAError("unknown_style", f"style {sid!r} does not exist",
                       valid=sorted(ctx.service.catalog().styles))
    if s.source != "user":
        return JSONResponse(error_payload(GIAError(
            INVALID_STYLE, f"built-in style {sid!r} cannot be deleted")), status_code=403)
    await ctx.service.delete_user_style(sid)
    return JSONResponse({"deleted": sid})


async def update_style(request: Request):
    ctx = _ctx(request)
    sid = request.path_params["sid"]
    s = ctx.service.catalog().styles.get(sid)
    if s is None:
        raise GIAError("unknown_style", f"style {sid!r} does not exist",
                       valid=sorted(ctx.service.catalog().styles))
    if s.source != "user":
        return JSONResponse(error_payload(GIAError(
            INVALID_STYLE, f"built-in style {sid!r} cannot be edited")), status_code=403)
    fields: dict = {}
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        _reject_oversized_body(request, MAX_IMAGE_BYTES, INVALID_STYLE)  # 422 before parsing
        async with request.form() as form:  # closes spooled upload files on exit
            for k in ("name", "description", "append", "family"):
                if k in form:
                    fields[k] = str(form.get(k) or "")
            if str(form.get("remove_image") or "").lower() in ("1", "true", "on", "yes"):
                fields["remove_image"] = True
            upload = form.get("file")
            if upload is not None and hasattr(upload, "read"):
                data = await upload.read(MAX_IMAGE_BYTES + 1)  # never read more than 20 MB + 1 byte
                fields["image"] = (data, upload.filename or "upload")
    else:
        body = await _json_body(request)
        for k in ("name", "description", "append", "family"):
            if k in body:
                if not isinstance(body[k], str):
                    raise GIAError("invalid_json", f"field {k!r} must be a string")
                fields[k] = body[k]
        if "remove_image" in body:
            if not isinstance(body["remove_image"], bool):
                raise GIAError("invalid_json", "field 'remove_image' must be a boolean")
            fields["remove_image"] = body["remove_image"]
        if body.get("from_key"):
            e, err = _key_entry(ctx, str(body["from_key"]))
            if err is not None:
                return err
            fields["image"] = (e.path.read_bytes(), e.path.name)
    if "family" in fields and not fields["family"].strip():
        fields["family"] = "otros"
    updated = await ctx.service.update_user_style(sid, **fields)
    return JSONResponse(_style_json(ctx, updated))


async def style_preview(request: Request):
    ctx = _ctx(request)
    sid = request.path_params["sid"]
    q = request.query_params
    p = preview_prompt(ctx.service.catalog(), prompt=q.get("prompt") or "<prompt>", style=sid,
                       aspect_hint=q.get("aspect") or None)  # unknown_style -> 404, invalid_aspect -> 400
    return JSONResponse({"final_prompt": p["final_prompt"], "aspect_hint": p["aspect_hint"],
                         "has_ref": p["style_ref"] is not None})


async def list_templates(request: Request):
    ctx = _ctx(request)
    return JSONResponse({n: {"description": t.description, "vars": list(t.vars)}
                         for n, t in ctx.service.catalog().templates.items()})


async def put_style_gem(request: Request):
    ctx = _ctx(request)
    sid = request.path_params["sid"]
    body = await _json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise GIAError("invalid_json", "field 'enabled' is required and must be a boolean")
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise GIAError("invalid_json", "field 'instructions' must be a string")
    await ctx.service.set_style_gem(sid, enabled, instructions)  # unknown_style -> 404
    return JSONResponse(_style_json(ctx, ctx.service.catalog().styles[sid]))


async def get_gem_instructions(request: Request):
    ctx = _ctx(request)
    sid = request.path_params["sid"]
    return JSONResponse(ctx.service.style_gem_instructions(sid))  # unknown_style -> 404


# -- state & SSE ---------------------------------------------------------------

async def get_state(request: Request):
    ctx = _ctx(request)
    idx = ctx.index()
    favs = ctx.library.favorites()
    return JSONResponse({
        "counts": {
            "images": len(idx),
            "collections": len(ctx.library.collections()),
            "favorites": sum(1 for k in favs if k in idx),
            "styles": len(ctx.service.catalog().styles),
        },
        "output_root": str(ctx.settings.output_root),
        "app_version": __version__,
    })


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


async def events(request: Request):
    ctx = _ctx(request)

    async def stream():
        state = ctx.service.state
        def _mtimes():
            return (_mtime(state.file), ctx.library.mtime(), ctx.user_styles.mtime(),
                    ctx.service.gems.mtime(), ctx.service.prompts.mtime(), ctx.service.animations.mtime(),
                    ctx.service.compositions.mtime())

        last = _mtimes()
        last_seq = ctx.jobs_seq
        while not ctx.stop_event.is_set():
            try:
                await asyncio.wait_for(ctx.jobs_event.wait(), timeout=ctx.poll_interval)
            except asyncio.TimeoutError:
                pass
            ctx.jobs_event.clear()
            cur = _mtimes()
            if cur != last:
                last = cur
                yield {"event": "changed",
                       "data": json.dumps({"generations": cur[0], "library": cur[1], "styles": cur[2],
                                           "gems": cur[3], "prompts": cur[4], "animations": cur[5],
                                           "compositions": cur[6]})}
            if ctx.jobs is not None and ctx.jobs_seq != last_seq:
                last_seq = ctx.jobs_seq
                yield {"event": "jobs", "data": json.dumps(_jobs_payload(ctx))}

    return EventSourceResponse(stream(), ping=15)


# -- models / uploads / jobs / quota / usage ------------------------------------

async def _models_body(ctx: _Ctx) -> dict:
    """/api/models body, cached MODELS_TTL_S; also feeds the queue's model validation.
    A backend failure serves the static fallback list (not cached, so the next call retries)."""
    now = ctx.now()
    if ctx.models_cache is not None and now - ctx.models_cached_at < MODELS_TTL_S:
        return ctx.models_cache
    backend = ctx.service.backend
    try:
        entries = model_entries(await backend.list_models())
        live = True
    except Exception as e:
        log.warning("list_models failed, serving the static model list: %s", e)
        entries, live = model_entries(None), False
    requested = ctx.settings.default_model
    resolved = requested
    if live:  # skip the resolver when the backend is down (it would re-init the client)
        try:
            resolved = await backend.resolve_model_name(requested) or requested
        except Exception:
            pass
    body = {"models": entries, "default": {"requested": requested, "resolved": resolved}}
    if live or not ctx.jobs.has_models():  # never overwrite the live list with the fallback
        ctx.jobs.set_models(entries, resolved if live else None)
    if live:
        ctx.models_cache, ctx.models_cached_at = body, now
    return body


async def list_models(request: Request):
    return JSONResponse(await _models_body(_ctx(request)))


async def create_upload(request: Request):
    ctx = _ctx(request)
    _reject_oversized_body(request, UploadStore.MAX_BYTES, "invalid_image")  # 422 before parsing
    async with request.form() as form:  # closes spooled upload files on exit
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise GIAError(INVALID_QUERY, "multipart field 'file' is required")
        data = await f.read(UploadStore.MAX_BYTES + 1)  # never read more than 20 MB + 1 byte
    uid = ctx.uploads.save(data)  # GIAError("invalid_image") -> 422
    return JSONResponse({"id": uid, "thumb_url": f"/api/uploads/{uid}/thumb"}, status_code=201)


async def get_upload_thumb(request: Request):
    ctx = _ctx(request)
    uid = request.path_params["uid"]
    src = ctx.uploads.path(uid)  # GIAError("unknown_upload") -> 404, never a client path
    thumb = ctx.settings.state_dir / "thumbs" / f"upload_{uid}.jpg"

    def _ensure() -> Path | None:
        if thumb.exists() and thumb.stat().st_mtime >= src.stat().st_mtime:
            return thumb
        try:
            _build_thumb(src, thumb)  # atomic write
        except Exception:
            return None  # not decodable (e.g. SVG): 404, never a 500
        return thumb

    path = await run_in_threadpool(_ensure)
    if path is None:
        return _err(404, "invalid_image", f"upload {uid} cannot be decoded as a thumbnail")
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


def _jobs_payload(ctx: _Ctx) -> dict:
    return {"jobs": [j.to_json() for j in ctx.jobs.list()], "eta_s": ctx.jobs.eta_s()}


def _parse_refs(raw) -> tuple[list[str], list[str]]:
    """refs entries are {"gen": "gen:..."} or {"upload": "u_..."} (bare strings
    are treated as gen refs). Returns (gen_refs, upload_ids)."""
    if not isinstance(raw, list):
        raise GIAError(INVALID_QUERY, "refs must be a list")
    refs, uploads = [], []
    for item in raw:
        if isinstance(item, str):
            refs.append(item)
        elif isinstance(item, dict) and "gen" in item:
            refs.append(str(item["gen"]))
        elif isinstance(item, dict) and "upload" in item:
            uploads.append(str(item["upload"]))
        else:
            raise GIAError(INVALID_QUERY,
                           f"bad ref entry {item!r}; expected {{'gen': ...}} or {{'upload': ...}}")
    return refs, uploads


async def create_job(request: Request):
    ctx = _ctx(request)
    body = await _json_body(request)
    refs, uploads = _parse_refs(body.get("refs") or [])
    ctx.ensure_purger()  # hourly upload purge starts next to the queue worker
    if body.get("model") not in (None, AUTO_MODEL):
        await _models_body(ctx)  # validate against the account's live models (cached)
    jobs = ctx.jobs.submit(prompt=body.get("prompt"), style=body.get("style"),
                           model=body.get("model"), ratio=body.get("ratio"),
                           n=body.get("n", 1), refs=refs, uploads=uploads,
                           collection=body.get("collection"), template=body.get("template"),
                           template_vars=body.get("template_vars"), edit_of=body.get("edit_of"),
                           animation=body.get("animation"))
    return JSONResponse({"batch": jobs[0].batch, "jobs": [j.to_json() for j in jobs]},
                        status_code=202)


async def list_jobs(request: Request):
    return JSONResponse(_jobs_payload(_ctx(request)))


async def cancel_job(request: Request):
    ctx = _ctx(request)
    job = ctx.jobs.cancel(request.path_params["jid"])  # unknown_job -> 404, job_running -> 409
    return JSONResponse(job.to_json())


QUOTA_TTL_S = 60.0
MODELS_TTL_S = 60.0
QUOTA_FIELDS = ("tier", "window_5h_pct", "weekly_pct", "resets_at_5h", "resets_at_weekly")


def _estimate_images_left(q: dict, state: "StateStore") -> tuple[int | None, int]:
    """(estimated images left in the 5 h window, images used in it), from history + quota pct."""
    try:
        reset = datetime.fromisoformat(q.get("resets_at_5h") or "")
        window_start = (reset - timedelta(hours=5)).astimezone(timezone.utc).isoformat()
        used = state.count_images_since(window_start)
    except (ValueError, TypeError):
        return None, 0
    pct = q.get("window_5h_pct") or 0
    if pct <= 0 or used == 0:
        return None, used
    free = max(0.0, 100.0 - pct)
    return int(free * used / pct), used


def _quota_unavailable(message: str, next_action: str, cookie_present: bool = False,
                       code: str = "quota_unavailable") -> JSONResponse:
    return JSONResponse({"available": False, "code": code, "message": message, "next_action": next_action,
                         "cookie_present": cookie_present})


async def get_quota(request: Request):
    """Quota from backend.status(), cached for 60 s; ?refresh=1 forces a refetch.
    Only the §4.3 fields are exposed, never cookies."""
    ctx = _ctx(request)
    now = ctx.now()
    if request.query_params.get("refresh") != "1" and ctx.quota_cache is not None \
            and now - ctx.quota_cached_at < QUOTA_TTL_S:
        return JSONResponse(ctx.quota_cache)
    try:
        st = await ctx.service.backend.status()
    except Exception as e:
        log.warning("quota status failed: %s", e)
        return _quota_unavailable("quota status unavailable",
                                  "Check the session with `musa auth`, then retry.", code="auth_expired")
    st = st if isinstance(st, dict) else {}
    cookie_present = bool(st.get("cookie_present"))
    if not cookie_present:
        return _quota_unavailable("no session cookie",
                                  "Run `musa auth` to sign in, then retry.", cookie_present, code="auth_expired")
    if not st.get("valid"):
        return _quota_unavailable(st.get("message") or "session is not valid",
                                  "Run `musa auth` to sign in again, then retry.", cookie_present, code="auth_expired")
    q = st.get("quota") or {}
    body = {f: q.get(f) for f in QUOTA_FIELDS}
    if all(body[f] is None for f in QUOTA_FIELDS):
        return _quota_unavailable("quota data not available",
                                  "Wait a moment and retry, or check the session with `musa auth`.",
                                  cookie_present)
    body["account_status"] = st.get("account_status")
    body["cookie_present"] = True
    est, used = _estimate_images_left(q, ctx.service.state)
    body["images_used_5h"] = used
    body["est_images_left_5h"] = est
    ctx.quota_cache = body
    ctx.quota_cached_at = now
    return JSONResponse(body)


def _activity_json(ctx: _Ctx, limit: int = 20) -> list[dict]:
    """Last `limit` visible generate/edit records, newest first."""
    catalog = ctx.service.catalog()
    recs = [r for r in ctx.service.state.visible_records() if r.type in ("generate", "edit")]
    out = []
    for r in recs[-limit:][::-1]:
        style = catalog.styles.get(r.style) if r.style else None
        saved = sum(1 for im in r.images if im.get("path"))
        if r.status == "ok":
            st = "ok"
        elif r.status == "generated_not_saved":
            st = "not_saved"
        else:
            st = "error"
        out.append({"id": r.id, "ts": r.ts, "type": r.type, "prompt": display_prompt(r),
                    "model": r.model, "style": r.style,
                    "style_name": style.name if style else None,
                    "images": saved, "elapsed_s": r.elapsed_s, "status": st})
    return out


async def get_usage(request: Request):
    ctx = _ctx(request)
    idx = ctx.index()
    output_bytes = 0
    for e in idx.values():
        try:
            output_bytes += e.path.stat().st_size
        except OSError:
            pass
    root = ctx.settings.output_root
    du = shutil.disk_usage(root if root.exists() else ctx.settings.state_dir)
    return JSONResponse({
        "per_day": ctx.service.state.images_per_day(7),
        "activity": _activity_json(ctx, 20),
        "storage": {"output_bytes": output_bytes, "disk_free": du.free, "disk_total": du.total},
    })


async def collection_activity(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    if ctx.library.get_collection(cid) is None:
        raise GIAError(UNKNOWN_COLLECTION, f"collection {cid!r} does not exist",
                       valid=[c.name for c in ctx.library.collections()])
    return JSONResponse({"collection": cid, "activity": ctx.library.activity(cid, limit=50)})


# -- compositions ------------------------------------------------------------

MAX_COMPOSITION_BODY_BYTES = 64 * 1024
_COMPOSITION_FIELDS = ("name", "prompt", "pieces", "ratio", "model", "style", "refs")
_COMP_PATCH_FIELDS = ("name", "order")
_LAYER_PATCH_FIELDS = ("visible", "tolerance", "offset", "animation")


async def _composition_body(request: Request, allowed: tuple[str, ...], *, optional: bool = False) -> dict:
    """JSON object body for composition routes: capped, and unknown fields are invalid_composition."""
    _reject_oversized_body(request, MAX_COMPOSITION_BODY_BYTES, "invalid_composition")
    raw = await request.body()
    if len(raw) > MAX_COMPOSITION_BODY_BYTES:
        raise GIAError("invalid_composition", "request body larger than 64 KB")
    body = {} if optional and not raw.strip() else await _json_body(request)
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise GIAError("invalid_composition", f"unknown fields: {', '.join(extra)}", fields=extra)
    return body


def _sheet_url(key: str, rev) -> str:
    gen, _, i = key.partition("#")
    return f"/api/animations/{gen}/{i}/sheet.png" + (f"?rev={rev}" if rev is not None else "")


def _composition_json(ctx: _Ctx, d: dict) -> dict:
    """Spec 2 §7 detail: the record + width/height/master_url + per-layer image_url/source_url."""
    cid = d["id"]
    master_key = d["master"].get("image")
    d["master_url"] = f"/api/image/{master_key.replace('#', '/')}" if d.get("master_exists") and master_key else None
    for ly in d["layers"]:
        ly["image_url"] = (f"/api/compositions/{cid}/layers/{ly['id']}.png?rev={ly['rev']}"
                           if ly.get("file") else None)
        ly["source_url"] = (f"/api/image/{ly['image'].replace('#', '/')}"
                            if ly.get("source_exists") and ly.get("image") else None)
        an = ly.get("animation")
        if an and an.get("type") == "frames":
            an["sheet_url"] = _sheet_url(an["image"], an.get("sheet_rev"))
    return public_view(d)


def _running_job_ids(ctx: _Ctx) -> set[str]:
    return {j.id for j in ctx.jobs.list() if j.status == "running"} if ctx.jobs is not None else set()


async def _composition_detail(ctx: _Ctx, cid: str) -> dict:
    d = await run_in_threadpool(ctx.compositions.detail, cid, _running_job_ids(ctx))
    return _composition_json(ctx, d)


def _cover_url(s: dict, index: dict) -> str | None:
    cover = s["cover"]
    if cover["kind"] == "preview":
        return f"/api/compositions/{s['id']}/preview.png?rev={s['rev']}"
    if cover["kind"] == "master" and cover["image"] in index:
        return f"/api/thumb/{cover['image'].replace('#', '/')}"
    return None


async def list_compositions(request: Request):
    ctx = _ctx(request)
    index = ctx.index()
    items = [{"id": s["id"], "name": s["name"], "rev": s["rev"], "progress": s["progress"],
              "cover_url": _cover_url(s, index), "created": s["created"], "updated": s["updated"]}
             for s in ctx.compositions.summaries()]
    return JSONResponse({"items": items})


async def create_composition(request: Request):
    ctx = _ctx(request)
    body = await _composition_body(request, _COMPOSITION_FIELDS)
    if body.get("model") not in (None, AUTO_MODEL):
        await _models_body(ctx)  # feeds the queue's model validation
    ctx.ensure_purger()
    comp = await ctx.compositions.create(
        prompt=body.get("prompt"), pieces=body.get("pieces"), name=body.get("name"), ratio=body.get("ratio"),
        model=body.get("model"), style=body.get("style"), refs=body.get("refs"))
    return JSONResponse(await _composition_detail(ctx, comp["id"]), status_code=201)


async def get_composition(request: Request):
    ctx = _ctx(request)
    return JSONResponse(await _composition_detail(ctx, request.path_params["cid"]))


async def patch_composition(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    body = await _composition_body(request, _COMP_PATCH_FIELDS)
    ctx.compositions.patch(cid, name=body.get("name"), order=body.get("order"))
    return JSONResponse(await _composition_detail(ctx, cid))


async def delete_composition(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    await run_in_threadpool(ctx.compositions.delete, cid)
    return JSONResponse({"id": cid, "deleted": True})


async def retry_composition(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    await ctx.compositions.retry(cid)
    return JSONResponse(await _composition_detail(ctx, cid), status_code=202)


async def patch_composition_layer(request: Request):
    ctx = _ctx(request)
    cid, lid = request.path_params["cid"], request.path_params["lid"]
    body = await _composition_body(request, _LAYER_PATCH_FIELDS)
    kw = {"animation": body["animation"]} if "animation" in body else {}
    await run_in_threadpool(lambda: ctx.compositions.patch_layer(
        cid, lid, visible=body.get("visible"), tolerance=body.get("tolerance"), offset=body.get("offset"), **kw))
    return JSONResponse(await _composition_detail(ctx, cid))


async def regenerate_composition_layer(request: Request):
    ctx = _ctx(request)
    body = await _composition_body(request, ("key_color",), optional=True)
    ctx.ensure_purger()
    job = await ctx.compositions.regenerate(request.path_params["cid"], request.path_params["lid"],
                                            key_color=body.get("key_color"))
    return JSONResponse({"job": job}, status_code=202)


async def correct_composition_layer(request: Request):
    ctx = _ctx(request)
    body = await _composition_body(request, ("prompt",))
    ctx.ensure_purger()
    job = await ctx.compositions.correct(request.path_params["cid"], request.path_params["lid"],
                                         body.get("prompt"))
    return JSONResponse({"job": job}, status_code=202)


def _png_response(path: Path) -> FileResponse:
    resp = FileResponse(path, media_type="image/png")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


async def composition_layer_png(request: Request):
    ctx = _ctx(request)
    path = await run_in_threadpool(ctx.compositions.layer_path, request.path_params["cid"],
                                   request.path_params["lid"])
    return _png_response(path)


async def composition_preview(request: Request):
    ctx = _ctx(request)
    path = await run_in_threadpool(ctx.compositions.preview_path, request.path_params["cid"])
    return _png_response(path)


async def composition_export(request: Request):
    ctx = _ctx(request)
    path, filename = await run_in_threadpool(ctx.compositions.export_zip, request.path_params["cid"])
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    resp = _TempFileResponse(path, media_type="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


class _SceneUrls:
    """`file` strings of the Musa scene.json: absolute URLs with ?rev=."""

    def __init__(self, cid: str):
        self.base = f"/api/compositions/{cid}/layers"

    def layer(self, ly: dict) -> str:
        return f"{self.base}/{ly['id']}.png?rev={ly['rev']}"

    def sheet(self, ly: dict, key: str, ficha: dict) -> str:
        return _sheet_url(key, ficha.get("rev"))

    def state(self, ly: dict, k: int, st: dict) -> str:
        return f"{self.base}/{ly['id']}/states/{k}.png?rev={st['rev']}"


async def composition_scene_json(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    scene = await run_in_threadpool(ctx.compositions.scene, cid, _SceneUrls(cid))
    return JSONResponse(scene, headers={"Cache-Control": "no-cache"})


async def composition_scene_webp(request: Request):
    ctx = _ctx(request)
    path, filename = await run_in_threadpool(ctx.compositions.export_webp, request.path_params["cid"])
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    resp = _TempFileResponse(path, media_type="image/webp")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def frames_composition_layer(request: Request):
    ctx = _ctx(request)
    body = await _composition_body(request, ("action", "grid"))
    ctx.ensure_purger()
    job = await ctx.compositions.frames(request.path_params["cid"], request.path_params["lid"],
                                        body.get("action"), body.get("grid"))
    return JSONResponse({"job": job}, status_code=202)


async def add_composition_state(request: Request):
    ctx = _ctx(request)
    body = await _composition_body(request, ("prompt",))
    ctx.ensure_purger()
    job = await ctx.compositions.add_state(request.path_params["cid"], request.path_params["lid"],
                                           body.get("prompt"))
    return JSONResponse({"job": job}, status_code=202)


def _state_index(request: Request) -> int:
    raw = request.path_params["k"]
    if not raw.isascii() or not raw.isdigit():
        raise GIAError("invalid_composition", f"k debe ser un entero ≥ 1, no {raw!r}")
    return int(raw)


async def remove_composition_state(request: Request):
    ctx = _ctx(request)
    cid = request.path_params["cid"]
    await run_in_threadpool(ctx.compositions.remove_state, cid, request.path_params["lid"], _state_index(request))
    return JSONResponse(await _composition_detail(ctx, cid))


async def composition_state_png(request: Request):
    ctx = _ctx(request)
    path = await run_in_threadpool(ctx.compositions.state_path, request.path_params["cid"],
                                   request.path_params["lid"], _state_index(request))
    return _png_response(path)


# -- app factory -----------------------------------------------------------------

def create_app(*, settings, service: ImageService, library: LibraryStore,
               user_styles: UserStyleStore, key: str, port: int,
               poll_interval: float = 1.0, jobs: JobQueue | None = None,
               uploads: UploadStore | None = None,
               now: Callable[[], float] | None = None,
               purge_interval_s: float = 3600.0) -> Starlette:
    ctx = _Ctx(settings, service, library, user_styles, poll_interval,
               uploads=uploads, now=now, purge_interval_s=purge_interval_s)
    ctx.uploads.purge()  # drop expired uploads at startup
    service.animations.prune(set(ctx.index()))  # fichas of hidden/orphaned images
    if jobs is None:
        jobs = JobQueue(service, ctx.uploads, on_change=ctx._on_jobs_change)
    elif jobs.on_change is None:
        jobs.on_change = ctx._on_jobs_change
    ctx.jobs = jobs
    ctx.compositions = CompositionManager(service, service.compositions, QueueRunner(jobs))
    jobs.on_finish = ctx.compositions.on_job_finished
    routes = [
        Route("/", index_page, methods=["GET"]),
        Route("/manifest.json", manifest_page, methods=["GET"]),
        Route("/api/update", get_update_endpoint, methods=["GET"]),
        Route("/api/state", get_state, methods=["GET"]),
        Route("/api/generations", list_generations, methods=["GET"]),
        Route("/api/image/{gen}/{i}", get_image, methods=["GET"]),
        Route("/api/thumb/{gen}/{i}", get_thumb, methods=["GET"]),
        Route("/api/favorites/{gen}/{i}", set_favorite, methods=["PUT", "DELETE"]),
        Route("/api/images/batch", batch_images, methods=["POST"]),
        Route("/api/images/zip", zip_images, methods=["POST"]),
        Route("/api/images/{gen}/{i}", delete_image, methods=["DELETE"]),
        Route("/api/images/{gen}/{i}/reveal", reveal_image, methods=["POST"]),
        Route("/api/collections", list_collections, methods=["GET"]),
        Route("/api/collections", create_collection, methods=["POST"]),
        Route("/api/collections/{cid}", update_collection, methods=["PATCH"]),
        Route("/api/collections/{cid}", delete_collection, methods=["DELETE"]),
        Route("/api/collections/{cid}/items", add_collection_item, methods=["POST"]),
        Route("/api/collections/{cid}/items/{gen}/{i}", remove_collection_item, methods=["DELETE"]),
        Route("/api/collections/{cid}/activity", collection_activity, methods=["GET"]),
        Route("/api/styles", list_styles, methods=["GET"]),
        Route("/api/styles", create_style, methods=["POST"]),
        Route("/api/styles/{sid}/cover", style_cover, methods=["GET"]),
        Route("/api/styles/{sid}/preview", style_preview, methods=["GET"]),
        Route("/api/templates", list_templates, methods=["GET"]),
        Route("/api/styles/{sid}/gem", put_style_gem, methods=["PUT"]),
        Route("/api/styles/{sid}/gem/instructions", get_gem_instructions, methods=["GET"]),
        Route("/api/styles/{sid}", update_style, methods=["PUT"]),
        Route("/api/styles/{sid}", delete_style, methods=["DELETE"]),
        Route("/api/models", list_models, methods=["GET"]),
        Route("/api/prompts", list_prompts, methods=["GET"]),
        Route("/api/prompts", create_prompt, methods=["POST"]),
        Route("/api/prompts/{pid}/use", use_prompt, methods=["POST"]),
        Route("/api/prompts/{pid}", update_prompt, methods=["PUT"]),
        Route("/api/prompts/{pid}", delete_prompt, methods=["DELETE"]),
        Route("/api/uploads", create_upload, methods=["POST"]),
        Route("/api/uploads/{uid}/thumb", get_upload_thumb, methods=["GET"]),
        Route("/api/jobs", create_job, methods=["POST"]),
        Route("/api/jobs", list_jobs, methods=["GET"]),
        Route("/api/jobs/{jid}", cancel_job, methods=["DELETE"]),
        Route("/api/quota", get_quota, methods=["GET"]),
        Route("/api/usage", get_usage, methods=["GET"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/api/animations/{gen}/{i}", get_animation, methods=["GET"]),
        Route("/api/animations/{gen}/{i}", put_animation, methods=["PUT"]),
        Route("/api/animations/{gen}/{i}", delete_animation, methods=["DELETE"]),
        Route("/api/animations/{gen}/{i}/sheet.png", animation_sheet, methods=["GET"]),
        Route("/api/animations/{gen}/{i}/export.zip", animation_export, methods=["GET"]),
        Route("/api/compositions", list_compositions, methods=["GET"]),
        Route("/api/compositions", create_composition, methods=["POST"]),
        Route("/api/compositions/{cid}", get_composition, methods=["GET"]),
        Route("/api/compositions/{cid}", patch_composition, methods=["PATCH"]),
        Route("/api/compositions/{cid}", delete_composition, methods=["DELETE"]),
        Route("/api/compositions/{cid}/retry", retry_composition, methods=["POST"]),
        Route("/api/compositions/{cid}/preview.png", composition_preview, methods=["GET"]),
        Route("/api/compositions/{cid}/export.zip", composition_export, methods=["GET"]),
        Route("/api/compositions/{cid}/layers/{lid}.png", composition_layer_png, methods=["GET"]),
        Route("/api/compositions/{cid}/layers/{lid}", patch_composition_layer, methods=["PATCH"]),
        Route("/api/compositions/{cid}/layers/{lid}/regenerate", regenerate_composition_layer, methods=["POST"]),
        Route("/api/compositions/{cid}/layers/{lid}/correct", correct_composition_layer, methods=["POST"]),
        Route("/api/compositions/{cid}/scene.json", composition_scene_json, methods=["GET"]),
        Route("/api/compositions/{cid}/scene.webp", composition_scene_webp, methods=["GET"]),
        Route("/api/compositions/{cid}/layers/{lid}/frames", frames_composition_layer, methods=["POST"]),
        Route("/api/compositions/{cid}/layers/{lid}/states", add_composition_state, methods=["POST"]),
        Route("/api/compositions/{cid}/layers/{lid}/states/{k}.png", composition_state_png, methods=["GET"]),
        Route("/api/compositions/{cid}/layers/{lid}/states/{k}", remove_composition_state, methods=["DELETE"]),
        Mount("/static", app=_NoCacheStatic(directory=PACKAGE_ROOT / "web"), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.ctx = ctx
    app.state.stop_event = ctx.stop_event
    app.add_exception_handler(GIAError, _gia_handler)
    app.add_exception_handler(Exception, _internal_handler)
    app.add_middleware(_SecurityMiddleware, key=key, port=port)
    return app
