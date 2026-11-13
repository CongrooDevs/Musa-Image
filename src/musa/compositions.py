"""Layered compositions (spec 2): store, prompts, runners and the CompositionManager.

Records live in <state_dir>/compositions.json; processed PNGs in <state_dir>/compositions/<cmp_id>/ (0700).
The manager is async: `QueueRunner` (Musa) only enqueues and the job-finished hook (`on_job_finished`)
advances the flow; `DirectRunner` (MCP) runs every spec in place and calls the same hook before returning.
"""
import asyncio
import copy
import inspect
import io
import html
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from PIL import Image

from .errors import (GIAError, INVALID_COMPOSITION, INVALID_REF, UNKNOWN_COMPOSITION, UNKNOWN_STYLE,
                     UPSTREAM_ERROR, error_payload)
from .gallery import image_index
from .jobs import QUEUE_STOP_CODES, Job
from .layers import composite, layer_png, normalize_size, prepare_piece, search_offset
from .refs import GEN_REF_RE
from .sprites import GRIDS, bg_name
from .storage import slugify
from .styles import ASPECT_HINTS, PACKAGE_ROOT
from .scene_build import DEFAULT_HOLD, MAX_STATES, STATIC, animation_of, build_scene, frames_view, validate_animation_patch
from .scene_render import composite_at_t0, render_webp
_MISSING = object()

log = logging.getLogger(__name__)

MAX_COMPOSITIONS = 100
MAX_PIECES = 6
MAX_REFS = 3
MAX_NAME = 80
MAX_PROMPT = 4000
MAX_CORRECTION = 2000
DEFAULT_RATIO = "4:3"
DEFAULT_TOLERANCE = 25
BUSY = ("queued", "running")
_TMP_ZIP_MAX_AGE_S = 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_rev(prev: int | None = 0) -> int:
    """Monotonic rev (spec 1 §P4): current ms timestamp, or the previous one + 1."""
    return max(time.time_ns() // 1_000_000, int(prev or 0) + 1)


def _bad(msg: str, **details) -> GIAError:
    return GIAError(INVALID_COMPOSITION, msg, **details)


# -- prompts (§4) -----------------------------------------------------------
def master_prompt(prompt: str, piece_names: list[str]) -> str:
    return (f"{prompt}\n\nCompose the scene so that each of these elements is clearly separate, fully visible, "
            f"and does not overlap or touch the others: {'; '.join(piece_names)}.")


def background_edit_prompt(piece_names: list[str]) -> str:
    return (f"Remove {', '.join(piece_names)} completely. Keep everything else exactly identical: same framing, "
            "same positions and sizes of the remaining elements, same style and line work. Fill the removed "
            "areas with the background that would be behind them. Keep the same image size.")


def background_ref_prompt(piece_names: list[str]) -> str:
    return (f"From the reference image, remove {', '.join(piece_names)} completely. Keep everything else "
            "exactly identical: same framing, same positions and sizes of the remaining elements, same style "
            "and line work. Fill the removed areas with the background that would be behind them. "
            "Same image size and framing as the reference.")


def piece_prompt(name: str, others: list[str], key_color: str) -> str:
    rest = f" Remove everything else, including {', '.join(others)}, and replace it" if others \
        else " Remove everything else and replace it"
    return (f"Isolate only {name} from the reference image. Keep it exactly the same: same design, same size, "
            f"same position in the frame, same style and line work.{rest} with solid flat "
            f"{bg_name(key_color)}. No shadows, no floor, no other objects. Keep the same framing and image "
            "size as the reference.")


def correction_suffix(role: str, key_color: str | None) -> str:
    if role == "background":
        return " Keep the same framing and the same positions of everything else."
    return f" Keep the solid flat {bg_name(key_color)} background and the same position, size and framing."


def correction_edit_prompt(text: str, role: str, key_color: str | None) -> str:
    return text + correction_suffix(role, key_color)


def correction_ref_prompt(text: str, role: str, key_color: str | None) -> str:
    return f"Same image as the reference, with this change: {text}." + correction_suffix(role, key_color)


# -- validation ---------------------------------------------------------------
def _clean_str(value, label: str, lo: int, hi: int) -> str:
    if not isinstance(value, str):
        raise _bad(f"{label} debe ser texto")
    value = value.strip()
    if not lo <= len(value) <= hi:
        raise _bad(f"{label} debe tener entre {lo} y {hi} caracteres", field=label)
    return value


def validate_new(*, prompt, pieces, name=None, ratio=None, model=None, style=None, refs=None) -> dict:
    """Normalized fields of a new composition (name, prompt, pieces[str], ratio, model, style, refs)."""
    prompt = _clean_str(prompt, "prompt", 1, MAX_PROMPT)
    if name is None or (isinstance(name, str) and not name.strip()):
        name = prompt[:40].strip()
    name = _clean_str(name, "name", 1, MAX_NAME)
    if isinstance(pieces, (str, bytes)) or not isinstance(pieces, (list, tuple)):
        raise _bad("pieces debe ser una lista de nombres")
    clean = [_clean_str(p, "pieza", 1, MAX_NAME) for p in pieces]
    if not 1 <= len(clean) <= MAX_PIECES:
        raise _bad(f"hay que dar de 1 a {MAX_PIECES} piezas", got=len(clean))
    if len({p.casefold() for p in clean}) != len(clean):
        raise _bad("los nombres de las piezas no pueden repetirse")
    ratio = DEFAULT_RATIO if ratio is None else ratio
    if ratio not in ASPECT_HINTS:
        raise _bad(f"proporción no válida {ratio!r}", valid=sorted(ASPECT_HINTS))
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise _bad("model debe ser un id o null")
    model = None if model in (None, "auto") else model
    if style is not None and (not isinstance(style, str) or not style.strip()):
        raise _bad("style debe ser un id o null")
    refs = [] if refs is None else refs
    if not isinstance(refs, (list, tuple)):
        raise _bad("refs debe ser una lista")
    if len(refs) > MAX_REFS:
        raise _bad(f"máximo {MAX_REFS} referencias", max_refs=MAX_REFS, got=len(refs))
    norm_refs = []
    for r in refs:
        if isinstance(r, str) and r.strip():
            norm_refs.append(r.strip())
        elif isinstance(r, dict) and len(r) == 1 and isinstance(next(iter(r.values())), str) \
                and next(iter(r)) in ("gen", "upload"):
            norm_refs.append(dict(r))
        else:
            raise _bad(f"referencia no válida {r!r}")
    return {"name": name, "prompt": prompt, "pieces": clean, "ratio": ratio, "model": model,
            "style": style, "refs": norm_refs}


def _new_layer(role: str, piece_id: str | None, name: str) -> dict:
    return {"id": f"ly_{uuid.uuid4().hex[:6]}", "role": role, "piece": piece_id, "name": name,
            "image": None, "conversation_id": None, "status": "pending", "job": None, "error": None,
            "visible": True, "key_color": None if role == "background" else "green",
            "pending_key_color": None, "tolerance": DEFAULT_TOLERANCE, "bbox": None, "offset": [0, 0],
            "rev": 0, "file": None, "animation": dict(STATIC)}


# -- store (§3) ---------------------------------------------------------------
class CompositionStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "compositions.json"
        self.root = self.dir / "compositions"
        self._lock = FileLock(str(self.dir / "compositions.lock"))

    # -- io
    def _read(self) -> dict:
        if not self.file.exists():
            return {"version": 1, "compositions": []}
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("compositions"), list) \
                    or any(not isinstance(c, dict) or "id" not in c for c in data["compositions"]):
                raise ValueError("invalid shape")
        except (ValueError, UnicodeDecodeError) as e:
            bad = self.file.with_name(f"compositions.json.corrupt-{int(time.time())}")
            os.replace(self.file, bad)
            log.warning("compositions.json was unreadable (%s); moved to %s", e, bad.name)
            return {"version": 1, "compositions": []}
        data.setdefault("version", 1)
        return data

    def _write(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.file)

    # -- reads
    def get(self, cid: str) -> dict | None:
        with self._lock:
            for c in self._read()["compositions"]:
                if c["id"] == cid:
                    return c
        return None

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._read()["compositions"])

    def mtime(self) -> float:
        try:
            return self.file.stat().st_mtime
        except OSError:
            return 0.0

    def check_room(self) -> None:
        if len(self.all()) >= MAX_COMPOSITIONS:
            raise _bad(f"se alcanzó el máximo de {MAX_COMPOSITIONS} composiciones", max=MAX_COMPOSITIONS)

    # -- writes
    def create(self, *, prompt, pieces, name=None, ratio=DEFAULT_RATIO, model=None, style=None, refs=(),
               collection=None, origin="musa", owner_pid=None) -> dict:
        """Validate and store a new composition with all layers pending. Returns the record."""
        norm = validate_new(prompt=prompt, pieces=pieces, name=name, ratio=ratio, model=model, style=style,
                            refs=list(refs))
        if origin not in ("musa", "mcp"):
            raise _bad("origin debe ser musa o mcp")
        now = _now_iso()
        piece_recs = [{"id": f"pc_{uuid.uuid4().hex[:6]}", "name": n} for n in norm["pieces"]]
        layers = [_new_layer("background", None, "Fondo")]
        layers += [_new_layer("piece", p["id"], p["name"]) for p in piece_recs]
        comp = {"id": f"cmp_{uuid.uuid4().hex[:8]}", "name": norm["name"], "prompt": norm["prompt"],
                "pieces": piece_recs, "ratio": norm["ratio"], "model": norm["model"], "style": norm["style"],
                "refs": norm["refs"], "collection": collection, "origin": origin, "owner_pid": owner_pid,
                "master": {"image": None, "conversation_id": None, "status": "queued", "job": None,
                           "error": None, "width": None, "height": None},
                "layers": layers, "rev": next_rev(0), "created": now, "updated": now}
        with self._lock:
            data = self._read()
            if len(data["compositions"]) >= MAX_COMPOSITIONS:
                raise _bad(f"se alcanzó el máximo de {MAX_COMPOSITIONS} composiciones", max=MAX_COMPOSITIONS)
            data["compositions"].append(comp)
            self._write(data)
        return copy.deepcopy(comp)

    def update(self, cid: str, fn) -> dict | None:
        """Atomic read-modify-write: `fn(comp)` mutates the record in place. Returns the new record
        (or None if it no longer exists)."""
        with self._lock:
            data = self._read()
            comp = next((c for c in data["compositions"] if c["id"] == cid), None)
            if comp is None:
                return None
            fn(comp)
            comp["updated"] = _now_iso()
            self._write(data)
            return copy.deepcopy(comp)

    def remove(self, cid: str) -> bool:
        with self._lock:
            data = self._read()
            kept = [c for c in data["compositions"] if c["id"] != cid]
            if len(kept) == len(data["compositions"]):
                return False
            data["compositions"] = kept
            self._write(data)
        shutil.rmtree(self.root / cid, ignore_errors=True)
        return True

    def artifacts_dir(self, cid: str) -> Path:
        d = self.root / cid
        self.root.mkdir(parents=True, exist_ok=True)
        d.mkdir(mode=0o700, exist_ok=True)
        return d


# -- runners (§5) ---------------------------------------------------------------
class QueueRunner:
    """Musa: delegates to the JobQueue. The webapp installs `jobs.on_finish = manager.on_job_finished`."""

    def __init__(self, jobs):
        self.jobs = jobs

    def bind(self, manager) -> None:
        pass

    def check_capacity(self, n: int) -> None:
        self.jobs.list()  # prunes finished jobs
        self.jobs.check_capacity(n)

    def live_job_ids(self) -> set[str]:
        return {j.id for j in self.jobs.list() if j.status in BUSY}

    def submit(self, spec: dict) -> str:
        return self.jobs.submit(**spec)[0].id

    def submit_many(self, specs: list[dict]) -> list[str]:
        return [j.id for j in self.jobs.submit_many(specs)]


class DirectRunner:
    """MCP: runs each spec in place with service.generate/edit, then calls manager.on_job_finished."""

    def __init__(self, service):
        self.service = service
        self.manager = None

    def bind(self, manager) -> None:
        self.manager = manager

    def live_job_ids(self):
        return None

    def _make_job(self, spec: dict) -> Job:
        model = spec.get("model")
        return Job(id=f"j_{uuid.uuid4().hex[:8]}", batch=f"b_{uuid.uuid4().hex[:8]}", prompt=spec["prompt"],
                   style=spec.get("style"), model=None if model in (None, "auto") else model,
                   ratio=spec.get("ratio"), refs=list(spec.get("refs") or ()),
                   uploads=list(spec.get("uploads") or ()), collection=spec.get("collection"),
                   created=time.time(), display_prompt=spec.get("display_prompt"), meta=spec.get("meta"),
                   animation=spec.get("animation"))

    def _fail(self, job: Job, e: GIAError) -> Job:
        p = error_payload(e)
        job.error = {"code": p["code"], "message": p["message"], "next_action": p["next_action"]}
        job.status = "error"
        job.finished = time.time()
        return job

    def _conversation(self, edit_of) -> tuple[str, str]:
        m = GEN_REF_RE.match(edit_of) if isinstance(edit_of, str) else None
        if not m:
            raise GIAError(INVALID_REF, f"bad edit_of {edit_of!r}; expected gen:g_<12 hex>#<index>")
        key = f"{m.group(1)}#{int(m.group(2) or 0)}"
        entry = self.manager._index().get(key)
        cid = getattr(getattr(entry, "record", None), "conversation_id", None)
        if entry is None or not cid or self.service.state.conversation(cid) is None:
            raise GIAError(INVALID_REF, f"image {key} cannot be edited: unknown, deleted or no conversation")
        return cid, f"gen:{key}"

    async def _run(self, spec: dict) -> Job:
        job = self._make_job(spec)
        job.status = "running"
        job.started = time.time()
        svc = self.service
        try:
            kw = dict(prompt=job.prompt, refs=job.refs, style=job.style, aspect_hint=job.ratio,
                      model=job.model, collection=job.collection, display_prompt=job.display_prompt)
            if spec.get("edit_of"):
                cid, job.edit_of = self._conversation(spec["edit_of"])
                sr = await svc.edit(cid, **kw)
            else:
                sr = await svc.generate(**kw, animation=job.animation)
        except GIAError as e:
            return self._fail(job, e)
        except Exception:
            log.exception("direct job %s failed with an unexpected error", job.id)
            return self._fail(job, GIAError(UPSTREAM_ERROR, "upstream generation failed"))
        res = sr.result
        if res.status == "error":
            return self._fail(job, GIAError(res.code or UPSTREAM_ERROR, res.gemini_text or "generation failed"))
        job.record_id = res.record_id
        job.images = svc.result_keys(res)
        if res.status == "generated_not_saved" and not job.images:
            return self._fail(job, GIAError(res.code or "generated_not_saved",
                                            res.save_error or "image was generated but not saved"))
        job.status = "done"
        job.finished = time.time()
        return job

    async def _finish(self, job: Job) -> None:
        try:
            await self.manager.on_job_finished(job)
        except Exception:
            log.exception("composition hook failed for job %s", job.id)

    async def submit(self, spec: dict) -> str:
        job = await self._run(spec)
        await self._finish(job)
        return job.id

    async def submit_many(self, specs: list[dict]) -> list[str]:
        ids, stop = [], None
        for spec in specs:
            if stop is not None:
                job = self._fail(self._make_job(spec), GIAError(stop["code"], stop["message"]))
            else:
                job = await self._run(spec)
                if job.status == "error" and job.error["code"] in QUEUE_STOP_CODES:
                    stop = job.error
            ids.append(job.id)
            await self._finish(job)
        return ids


# -- image helpers (blocking) -----------------------------------------------------
def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def _load_normalized(path: Path, size) -> Image.Image:
    try:
        with Image.open(path) as im:
            im.load()
            img = im.convert("RGB")
    except (OSError, ValueError) as e:
        raise GIAError("invalid_image", f"no se pudo abrir la imagen: {e.__class__.__name__}")
    return normalize_size(img, size)


def _save_png(img: Image.Image, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        img.save(tmp, format="PNG")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _render_layer(directory: Path, layer: dict, size, src: Path, master_path: Path | None,
                  key_color: str | None, tolerance: int, rev: int, offset=None, name: str | None = None) -> dict:
    """Process a layer's (or state's) source image and write its PNG; returns {file, bbox, offset}.
    `offset` None = search against `master_path` (the master, or the layer image for a state)."""
    img = _load_normalized(src, size)
    if layer["role"] == "background":
        out, bbox, off = img, [0, 0, size[0], size[1]], [0, 0]
    else:
        rgba, bbox = prepare_piece(img, key_color or "green", tolerance)
        if offset is not None:
            off = [int(offset[0]), int(offset[1])]
        elif master_path is not None:
            try:
                master = _load_normalized(master_path, size)
                off = list(search_offset(master, rgba, bbox))
            except GIAError:
                off = [0, 0]
        else:
            off = [0, 0]
        out, bbox = layer_png(rgba, bbox, off), list(bbox)
    name = name or f"{layer['id']}_{rev}.png"
    _save_png(out, directory / name)
    return {"file": name, "bbox": bbox, "offset": off}


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _apply_failure(ent: dict, code: str, message: str) -> None:
    """§5.3: a layer that already had a file goes back to done with a notice; otherwise it is an error.
    (A master has no file and no notice: it is simply an error.)"""
    ent["job"] = None
    ent["error"] = {"code": code, "message": message}
    if "pending_key_color" in ent:
        ent["pending_key_color"] = None
    ent["status"] = "done" if ent.get("file") else "error"


# -- manager (§5) ------------------------------------------------------------------
class CompositionManager:
    def __init__(self, service, store: CompositionStore, runner, *, recover_on_start: bool = True):
        self.service = service
        self.store = store
        self.runner = runner
        bind = getattr(runner, "bind", None)
        if bind is not None:
            bind(self)
        if recover_on_start:
            self.recover(live_job_ids=runner.live_job_ids())

    # -- lookups ---------------------------------------------------------------
    _index_cache = None

    @staticmethod
    def _stat_stamp(path):
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _index(self) -> dict:
        """image_index, cached by the state file and library mtimes (rebuilt only when either changes)."""
        s = self.service
        stamp = (self._stat_stamp(s.state.file), s.library.mtime())
        cached = self._index_cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        index = image_index(s.state, s.library, s.settings.output_root)
        self._index_cache = (stamp, index)
        return index

    async def _warm_index(self) -> None:
        """Build the index off the event loop so the sync lookups that follow hit the cache."""
        await asyncio.to_thread(self._index)

    def _image_info(self, key: str | None):
        if not key:
            return None
        return self._index().get(key[4:] if key.startswith("gen:") else key)

    def _require(self, cid: str) -> dict:
        comp = self.store.get(cid)
        if comp is None:
            raise GIAError(UNKNOWN_COMPOSITION, f"composición desconocida {cid!r}")
        return comp

    def resolve(self, ref: str) -> str:
        """Composition id from an id or an exact (case-insensitive) name; unknown_composition otherwise."""
        ref = (ref or "").strip()
        comps = self.store.all()
        found = next((c for c in comps if c["id"] == ref), None)
        if found is None:
            named = [c for c in comps if c["name"].casefold() == ref.casefold()]
            found = named[-1] if named else None
        if found is None:
            raise GIAError(UNKNOWN_COMPOSITION, f"composición desconocida {ref!r}")
        return found["id"]

    def resolve_layer(self, cid: str, ref: str) -> str:
        """Layer id from an id or an exact (case-insensitive) layer name; unknown_composition otherwise."""
        ref = (ref or "").strip()
        layers = self._require(cid)["layers"]
        found = next((x for x in layers if x["id"] == ref), None) \
            or next((x for x in layers if x["name"].casefold() == ref.casefold()), None)
        if found is None:
            raise GIAError(UNKNOWN_COMPOSITION, f"capa desconocida {ref!r}")
        return found["id"]

    @staticmethod
    def _layer(comp: dict, lid: str) -> dict:
        ly = next((x for x in comp["layers"] if x["id"] == lid), None)
        if ly is None:
            raise GIAError(UNKNOWN_COMPOSITION, f"capa desconocida {lid!r}")
        return ly

    def _collection(self, comp: dict) -> str | None:
        cid = comp.get("collection")
        if cid and self.service.library.get_collection(cid) is not None:
            return cid
        return None

    async def _call(self, fn, *args):
        r = fn(*args)
        return await r if inspect.isawaitable(r) else r

    # -- spec building ------------------------------------------------------------
    def _common(self, comp: dict, target: str, action: str, label: str) -> dict:
        return {"model": comp["model"], "ratio": comp["ratio"], "style": None,
                "collection": self._collection(comp), "display_prompt": f"{comp['name']} · {label}",
                "meta": {"composition": comp["id"], "target": target, "action": action}}

    def _master_spec(self, comp: dict) -> dict:
        refs, uploads = [], []
        for r in comp["refs"]:
            if isinstance(r, str):
                refs.append(r)
            elif "gen" in r:
                refs.append(r["gen"])
            else:
                uploads.append(r["upload"])
        spec = {"prompt": master_prompt(comp["prompt"], [p["name"] for p in comp["pieces"]]),
                "refs": refs, **self._common(comp, "master", "create", "imagen completa")}
        spec["style"] = comp["style"]
        if uploads:
            spec["uploads"] = uploads
        return spec

    def _layer_spec(self, comp: dict, ly: dict, action: str, key_color: str | None = None) -> dict:
        """Generation spec of a layer from the master image (§4)."""
        master_key = comp["master"]["image"]
        common = self._common(comp, ly["id"], action, ly["name"])
        names = [p["name"] for p in comp["pieces"]]
        if ly["role"] == "background":
            return self.service.change_spec(master_key, background_edit_prompt(names),
                                            background_ref_prompt(names), index=self._index(), **common)
        others = [n for n in names if n != ly["name"]]
        return {"prompt": piece_prompt(ly["name"], others, key_color or ly["key_color"] or "green"),
                "refs": [f"gen:{master_key}"], **common}

    # -- state helpers ----------------------------------------------------------------
    def _mark_busy(self, cid: str, *, master: bool = False, layer_ids=()) -> dict | None:
        def fn(c):
            c["owner_pid"] = os.getpid()
            if master:
                c["master"].update(status="queued", job=None, error=None)
            for ly in c["layers"]:
                if ly["id"] in layer_ids:
                    ly.update(status="queued", job=None, error=None)
        return self.store.update(cid, fn)

    def _set_jobs(self, cid: str, pairs: list[tuple[str | None, str]], master_job: str | None = None) -> None:
        """Attach job ids to entities still waiting for one (a DirectRunner job may already be finished)."""
        def fn(c):
            if master_job and c["master"]["status"] in BUSY and c["master"]["job"] is None:
                c["master"]["job"] = master_job
            for lid, jid in pairs:
                ly = next((x for x in c["layers"] if x["id"] == lid), None)
                if ly is not None and ly["status"] in BUSY and ly["job"] is None:
                    ly["job"] = jid
        self.store.update(cid, fn)

    def _fail_targets(self, cid: str, code: str, message: str, *, master: bool = False, layer_ids=()) -> None:
        def fn(c):
            if master:
                _apply_failure(c["master"], code, message)
            for ly in c["layers"]:
                if ly["id"] in layer_ids and ly["status"] in BUSY:
                    _apply_failure(ly, code, message)
        self.store.update(cid, fn)

    async def _launch_layers(self, cid: str, items: list[tuple[str, dict]]) -> bool:
        """Queue (layer_id, spec) pairs in one submit_many. On failure every layer takes the failure rule."""
        ids = [lid for lid, _ in items]
        self._mark_busy(cid, layer_ids=ids)
        try:
            job_ids = await self._call(self.runner.submit_many, [s for _, s in items])
        except GIAError as e:
            self._fail_targets(cid, e.code, str(e), layer_ids=ids)
            return False
        self._set_jobs(cid, list(zip(ids, job_ids)))
        return True

    async def _launch_master(self, cid: str, *, rollback_collection: str | None = None, rollback: bool = False) -> None:
        comp = self._mark_busy(cid, master=True)
        try:
            jid = await self._call(self.runner.submit, self._master_spec(comp))
        except GIAError as e:
            if rollback:
                self.store.remove(cid)
                if rollback_collection:
                    try:
                        self.service.library.delete_collection(rollback_collection)
                    except GIAError:
                        pass
            else:
                self.store.update(cid, lambda c: c["master"].update(status="error", job=None, error={
                    "code": e.code, "message": str(e)}))
            raise
        self._set_jobs(cid, [], master_job=jid)

    # -- create / retry (§5.1, §5.6) ------------------------------------------------------
    def _new_collection(self, name: str):
        lib = self.service.library
        taken = {c.name.casefold() for c in lib.collections()}
        base, n = f"Composición · {name}", 1
        cand = base
        while cand.casefold() in taken:
            n += 1
            cand = f"{base} ({n})"
        return lib.create_collection(cand)

    async def create(self, *, prompt, pieces, name=None, ratio=None, model=None, style=None, refs=None,
                     origin: str = "musa") -> dict:
        norm = validate_new(prompt=prompt, pieces=pieces, name=name, ratio=ratio, model=model,
                            style=style, refs=refs)
        if norm["style"] is not None and norm["style"] not in self.service.catalog().styles:
            raise GIAError(UNKNOWN_STYLE, f"unknown style {norm['style']!r}")
        self.store.check_room()
        check = getattr(self.runner, "check_capacity", None)
        if check is not None:
            check(2 + len(norm["pieces"]))
        coll = self._new_collection(norm["name"])
        try:
            comp = self.store.create(**norm, collection=coll.id, origin=origin, owner_pid=os.getpid())
        except Exception:
            try:
                self.service.library.delete_collection(coll.id)
            except GIAError:
                pass
            raise
        await self._launch_master(comp["id"], rollback_collection=coll.id, rollback=True)
        return self._require(comp["id"])

    async def retry(self, cid: str) -> dict:
        comp = self._require(cid)
        if comp["master"]["status"] != "error":
            raise _bad("solo se puede reintentar la imagen completa si está en error")
        check = getattr(self.runner, "check_capacity", None)
        if check is not None:
            check(1)
        await self._launch_master(cid)
        return self._require(cid)

    async def run_direct(self, *, prompt, pieces, name=None, refs=None, style=None, ratio=None,
                         model=None) -> dict:
        """MCP flow (DirectRunner): {'status': 'ok' | 'partial', 'composition': record}."""
        comp = await self.create(prompt=prompt, pieces=pieces, name=name, ratio=ratio, model=model,
                                 style=style, refs=refs, origin="mcp")
        ok = comp["master"]["status"] == "done" and all(
            ly["status"] == "done" and not ly.get("error") for ly in comp["layers"])
        return {"status": "ok" if ok else "partial", "composition": comp}

    # -- hook (§5.2, §5.3) ----------------------------------------------------------------
    @staticmethod
    def _job_failure(job: Job) -> tuple[str, str]:
        if job.status == "cancelled":
            return "cancelled", "el trabajo se canceló"
        err = job.error or {}
        return err.get("code") or UPSTREAM_ERROR, err.get("message") or "la generación falló"

    @staticmethod
    def _current(ent: dict, job: Job) -> bool:
        return ent["status"] in BUSY and ent["job"] in (None, job.id)

    async def on_job_finished(self, job: Job) -> None:
        meta = job.meta or {}
        cid, target = meta.get("composition"), meta.get("target")
        if not cid or not target:
            return
        comp = self.store.get(cid)
        if comp is None:
            return
        try:
            if target == "master":
                await self._master_finished(comp, job)
            else:
                await self._layer_finished(comp, target, job)
        except Exception:
            log.exception("composition %s: error handling job %s", cid, job.id)
            if target == "master":
                self._fail_targets(cid, UPSTREAM_ERROR, "error interno al procesar la imagen completa", master=True)
            else:
                self._fail_targets(cid, UPSTREAM_ERROR, "error interno al procesar la capa", layer_ids=[target])

    async def _master_finished(self, comp: dict, job: Job) -> None:
        cid, m = comp["id"], comp["master"]
        await self._warm_index()
        if not self._current(m, job):
            return
        if job.status != "done" or not job.images:
            code, msg = self._job_failure(job) if job.status != "done" else ("no_image", "el trabajo no devolvió imagen")
            self._fail_targets(cid, code, msg, master=True)
            return
        key = job.images[0]
        info = self._image_info(key)
        if info is None:
            self._fail_targets(cid, "invalid_image", "la imagen completa no está en la galería", master=True)
            return
        try:
            w, h = await asyncio.to_thread(_image_size, info.path)
        except (OSError, ValueError):
            self._fail_targets(cid, "invalid_image", "no se pudo leer la imagen completa", master=True)
            return

        def fn(c):
            c["master"].update(image=key, conversation_id=info.record.conversation_id, status="done",
                               job=None, error=None, width=w, height=h)
        comp = self.store.update(cid, fn)
        if comp is None:
            return
        items = [(ly["id"], self._layer_spec(comp, ly, "create")) for ly in comp["layers"]
                 if ly["status"] == "pending"]
        if items:
            await self._launch_layers(cid, items)

    async def _layer_finished(self, comp: dict, lid: str, job: Job) -> None:
        cid = comp["id"]
        ly = next((x for x in comp["layers"] if x["id"] == lid), None)
        if ly is None or not self._current(ly, job):
            return
        if job.status != "done" or not job.images:
            code, msg = self._job_failure(job) if job.status != "done" else ("no_image", "el trabajo no devolvió imagen")
            self._fail_targets(cid, code, msg, layer_ids=[lid])
            return
        action = (job.meta or {}).get("action")
        if action == "frames":
            await self._frames_finished(comp, ly, job)
        elif action == "state":
            await self._state_finished(comp, ly, job)
        else:
            await self._image_finished(comp, ly, job, action)

    async def _image_finished(self, comp: dict, ly: dict, job: Job, action: str | None) -> None:
        cid, lid = comp["id"], ly["id"]
        await self._warm_index()
        key = job.images[0]
        info = self._image_info(key)
        m = comp["master"]
        if info is None or not m.get("width"):
            self._fail_targets(cid, "invalid_image", "la imagen de la capa no está en la galería", layer_ids=[lid])
            return
        master_info = self._image_info(m["image"])
        rev = next_rev(ly["rev"])
        key_color = ly.get("pending_key_color") or ly["key_color"]
        try:
            res = await asyncio.to_thread(
                _render_layer, self.store.artifacts_dir(cid), ly, (m["width"], m["height"]), info.path,
                master_info.path if master_info is not None else None, key_color, ly["tolerance"], rev)
        except GIAError as e:
            self._fail_targets(cid, e.code, str(e), layer_ids=[lid])
            return
        except Exception as e:
            log.exception("layer %s of %s could not be processed", lid, cid)
            self._fail_targets(cid, "invalid_image", f"no se pudo procesar la capa: {e.__class__.__name__}",
                               layer_ids=[lid])
            return
        dropped = []

        def fn(c):
            cur = next(x for x in c["layers"] if x["id"] == lid)
            dropped.append(cur.get("file"))
            anim = animation_of(cur)
            if action in ("regenerate", "correct") and anim.get("type") == "states":
                # the states depended on the previous image (spec 3 §3)
                dropped.extend(st["file"] for st in anim["states"])
                cur["animation"] = dict(STATIC)
            cur.update(image=key, conversation_id=info.record.conversation_id, status="done", job=None,
                       error=None, key_color=key_color, pending_key_color=None, bbox=res["bbox"],
                       offset=res["offset"], rev=rev, file=res["file"])
            c["rev"] = next_rev(c["rev"])
        if self.store.update(cid, fn) is None:
            return
        self._drop_file(cid, dropped[0], res["file"])
        for f in dropped[1:]:
            self._drop_file(cid, f, None)

    async def _frames_finished(self, comp: dict, ly: dict, job: Job) -> None:
        cid, lid = comp["id"], ly["id"]
        key = job.images[0]
        ficha = self.service.animations.get(key)
        if ficha is None:
            warn = job.warning or {}
            self._fail_targets(cid, warn.get("code") or "invalid_animation",
                               warn.get("message") or "no se pudo detectar la hoja de frames", layer_ids=[lid])
            return

        def fn(c):
            cur = next(x for x in c["layers"] if x["id"] == lid)
            cur.update(animation={"type": "frames", "image": key}, status="done", job=None, error=None)
            c["rev"] = next_rev(c["rev"])
        self.store.update(cid, fn)

    async def _state_finished(self, comp: dict, ly: dict, job: Job) -> None:
        cid, lid = comp["id"], ly["id"]
        await self._warm_index()
        key = job.images[0]
        info = self._image_info(key)
        base = self._image_info(ly.get("image"))
        m = comp["master"]
        anim = animation_of(ly)
        if info is None or base is None or not m.get("width"):
            self._fail_targets(cid, "invalid_image", "la imagen del estado no está en la galería", layer_ids=[lid])
            return
        if anim.get("type") not in ("static", "motion", "states") or len(anim.get("states") or []) >= MAX_STATES:
            self._fail_targets(cid, INVALID_COMPOSITION, "la capa ya no admite más estados", layer_ids=[lid])
            return
        states = anim.get("states") or []
        rev = next_rev(max([ly["rev"], *[st["rev"] for st in states]]))
        try:
            res = await asyncio.to_thread(
                _render_layer, self.store.artifacts_dir(cid), ly, (m["width"], m["height"]), info.path,
                base.path, ly["key_color"], ly["tolerance"], rev, None, f"{lid}_s{len(states) + 1}_{rev}.png")
        except GIAError as e:
            self._fail_targets(cid, e.code, str(e), layer_ids=[lid])
            return
        except Exception as e:
            log.exception("state of layer %s of %s could not be processed", lid, cid)
            self._fail_targets(cid, "invalid_image", f"no se pudo procesar el estado: {e.__class__.__name__}",
                               layer_ids=[lid])
            return
        state = {"image": key, "conversation_id": info.record.conversation_id, "bbox": res["bbox"],
                 "offset": res["offset"], "file": res["file"], "rev": rev}
        refused = []

        def fn(c):
            cur = next(x for x in c["layers"] if x["id"] == lid)
            a = animation_of(cur)
            if a.get("type") == "states":
                new = {**a, "states": list(a["states"])}
            elif a.get("type") in ("static", "motion"):
                new = {"type": "states", "hold": DEFAULT_HOLD, "loop": "loop", "states": []}
            else:
                refused.append(True)
                return
            if len(new["states"]) >= MAX_STATES:
                refused.append(True)
                return
            new["states"].append(state)
            cur.update(animation=new, status="done", job=None, error=None)
            c["rev"] = next_rev(c["rev"])
        if self.store.update(cid, fn) is None:
            return
        if refused:
            self._drop_file(cid, res["file"], None)
            self._fail_targets(cid, INVALID_COMPOSITION, "la capa ya no admite más estados", layer_ids=[lid])

    def _drop_file(self, cid: str, old: str | None, keep: str | None) -> None:
        if old and old != keep:
            (self.store.root / cid / old).unlink(missing_ok=True)

    # -- layer actions (§5.4, §5.5, §5.7) ---------------------------------------------------
    def _check_actionable(self, comp: dict, ly: dict) -> None:
        if ly["status"] in BUSY:
            raise _bad("la capa ya tiene un trabajo en curso", layer=ly["id"])
        m = comp["master"]
        if m["status"] != "done" or self._image_info(m["image"]) is None:
            raise _bad("la imagen completa se borró; crea la composición de nuevo")

    async def _run_layer_job(self, cid: str, ly: dict, spec: dict, key_color: str | None = None) -> str:
        prev = {k: ly.get(k) for k in ("status", "job", "error", "pending_key_color")}

        def mark(c):
            cur = next(x for x in c["layers"] if x["id"] == ly["id"])
            if cur["status"] in BUSY:  # atomic check-and-set: only one job per layer
                raise _bad("la capa ya tiene un trabajo en curso", layer=ly["id"])
            cur.update(status="queued", job=None, error=None, pending_key_color=key_color)
            c["owner_pid"] = os.getpid()
        self.store.update(cid, mark)
        try:
            jid = await self._call(self.runner.submit, spec)
        except GIAError:
            def revert(c):
                next(x for x in c["layers"] if x["id"] == ly["id"]).update(prev)
            self.store.update(cid, revert)
            raise
        self._set_jobs(cid, [(ly["id"], jid)])
        return jid

    async def regenerate(self, cid: str, lid: str, key_color: str | None = None) -> str:
        """Regenerate a layer from the master (1 image). Returns the job id."""
        await self._warm_index()
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        if key_color is not None:
            if ly["role"] == "background" or key_color not in ("green", "magenta"):
                raise _bad("key_color solo vale green o magenta, y solo para piezas")
        self._check_actionable(comp, ly)
        pending = key_color if key_color and key_color != ly["key_color"] else None
        spec = self._layer_spec(comp, ly, "regenerate", key_color)
        return await self._run_layer_job(cid, ly, spec, pending)

    async def correct(self, cid: str, lid: str, prompt: str) -> str:
        """Correct a layer's image with the user's text (1 image). Returns the job id."""
        await self._warm_index()
        text = _clean_str(prompt, "prompt", 1, MAX_CORRECTION)
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        self._check_actionable(comp, ly)
        if not ly.get("file") or self._image_info(ly["image"]) is None:
            raise _bad("la capa no tiene imagen que corregir o su imagen se borró")
        common = self._common(comp, lid, "correct", ly["name"])
        spec = self.service.change_spec(
            ly["image"], correction_edit_prompt(text, ly["role"], ly["key_color"]),
            correction_ref_prompt(text, ly["role"], ly["key_color"]), index=self._index(), **common)
        return await self._run_layer_job(cid, ly, spec)

    # -- patches ---------------------------------------------------------------------------------
    def patch(self, cid: str, *, name=None, order=None) -> dict:
        comp = self._require(cid)
        new_name = _clean_str(name, "name", 1, MAX_NAME) if name is not None else None
        ids = None
        if order is not None:
            if not isinstance(order, (list, tuple)) or not all(isinstance(i, str) for i in order):
                raise _bad("order debe ser una lista de ids de capa")
            have = [ly["id"] for ly in comp["layers"]]
            if sorted(order) != sorted(have) or len(set(order)) != len(order):
                raise _bad("order debe contener exactamente las mismas capas")
            if order[0] != have[0]:
                raise _bad("el fondo debe ser la primera capa")
            ids = list(order)

        def fn(c):
            if new_name is not None:
                c["name"] = new_name
            if ids is not None and ids != [ly["id"] for ly in c["layers"]]:
                by = {ly["id"]: ly for ly in c["layers"]}
                c["layers"] = [by[i] for i in ids]
                c["rev"] = next_rev(c["rev"])
        return self.store.update(cid, fn)

    def patch_layer(self, cid: str, lid: str, *, visible=None, tolerance=None, offset=None,
                    animation=_MISSING) -> dict:
        """Blocking when `tolerance` is given (re-cuts the source images): call from a thread."""
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        bg = ly["role"] == "background"
        if visible is not None:
            if not isinstance(visible, bool):
                raise _bad("visible debe ser booleano")
            if bg and not visible:
                raise _bad("el fondo no se puede ocultar")
        if offset is not None:
            if bg:
                raise _bad("el fondo no se puede desplazar")
            if (not isinstance(offset, (list, tuple)) or len(offset) != 2
                    or any(isinstance(v, bool) or not isinstance(v, int) or not -200 <= v <= 200
                           for v in offset)):
                raise _bad("offset debe ser [dx, dy] con enteros entre -200 y 200")
        new_anim = None
        if animation is not _MISSING:
            if ly["status"] in BUSY:
                raise _bad("la capa tiene un trabajo en curso: espera a que termine", layer=lid)
            new_anim = validate_animation_patch(ly, animation)
        recut, recut_states = None, {}
        if tolerance is not None:
            if bg:
                raise _bad("el fondo no tiene tolerancia")
            if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 5 <= tolerance <= 80:
                raise _bad("tolerance debe ser un entero entre 5 y 80")
            if tolerance != ly["tolerance"]:
                if ly["status"] in BUSY:
                    raise _bad("la capa tiene un trabajo en curso", layer=lid)
                info = self._image_info(ly["image"]) if ly.get("image") else None
                m = comp["master"]
                if info is None or not m.get("width"):
                    raise _bad("la imagen fuente de la capa se borró: no se puede volver a recortar")
                size, directory = (m["width"], m["height"]), self.store.artifacts_dir(cid)
                rev = next_rev(ly["rev"])
                recut = _render_layer(directory, ly, size, info.path, None, ly["key_color"], tolerance, rev,
                                      offset=offset or ly["offset"])
                recut["rev"] = rev
                for k, st in enumerate(animation_of(ly).get("states") or [], 1):
                    sinfo = self._image_info(st["image"])
                    if sinfo is None:  # the state keeps its PNG
                        continue
                    srev = next_rev(st["rev"])
                    r = _render_layer(directory, ly, size, sinfo.path, None, ly["key_color"], tolerance, srev,
                                      offset=st["offset"], name=f"{lid}_s{k}_{srev}.png")
                    recut_states[st["file"]] = {"bbox": r["bbox"], "file": r["file"], "rev": srev}
        dropped = []

        def fn(c):
            cur = next(x for x in c["layers"] if x["id"] == lid)
            if new_anim is not None and cur["status"] in BUSY:
                raise _bad("la capa tiene un trabajo en curso: espera a que termine", layer=lid)
            changed = False
            if visible is not None and visible != cur["visible"]:
                cur["visible"], changed = visible, True
            if recut is not None:
                dropped.append(cur.get("file"))
                cur.update(tolerance=tolerance, file=recut["file"], bbox=recut["bbox"],
                           offset=recut["offset"], rev=recut["rev"])
                c["rev"] = next_rev(c["rev"])
            elif offset is not None and list(offset) != cur["offset"]:
                cur["offset"], changed = [int(offset[0]), int(offset[1])], True
            old = animation_of(cur)
            if recut_states and old.get("type") == "states":
                for st in old["states"]:
                    r = recut_states.get(st["file"])
                    if r:
                        dropped.append(st["file"])
                        st.update(r)
            if new_anim is not None and new_anim != old:
                if old.get("type") == "states" and new_anim.get("type") != "states":
                    dropped.extend(st["file"] for st in old["states"])
                cur["animation"] = new_anim
                c["rev"] = next_rev(c["rev"])
            if changed:
                cur["rev"] = next_rev(cur["rev"])
                c["rev"] = next_rev(c["rev"])
        out = self.store.update(cid, fn)
        keep = set()
        for out_ly in (out["layers"] if out else []):
            keep.add(out_ly.get("file"))
            keep.update(st["file"] for st in animation_of(out_ly).get("states") or [])
        for f in dropped:
            if f not in keep:
                self._drop_file(cid, f, None)
        return out

    # -- frames and states (spec 3 §3) ---------------------------------------------------------------
    def _check_piece_ready(self, ly: dict) -> None:
        if ly["role"] == "background":
            raise _bad("el fondo no admite frames ni estados")
        if ly["status"] in BUSY:
            raise _bad("la capa ya tiene un trabajo en curso", layer=ly["id"])
        if ly["status"] != "done" or not ly.get("file") or self._image_info(ly.get("image")) is None:
            raise _bad("la capa debe estar lista y conservar su imagen en la galería")

    async def frames(self, cid: str, lid: str, action: str, grid: str) -> str:
        """Generate a sprite sheet of the piece (1 image). Returns the job id."""
        await self._warm_index()
        text = _clean_str(action, "action", 1, 500)
        if grid not in GRIDS:
            raise _bad(f"grid debe ser uno de {', '.join(GRIDS)}", valid=sorted(GRIDS))
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        self._check_piece_ready(ly)
        if animation_of(ly).get("type") == "states":
            raise _bad("la capa tiene estados: quítalos antes de animarla con frames")
        spec = {"prompt": (f"{ly['name']} from the reference image, {text}. Keep the exact same character "
                           "design, colors and line style as the reference image."),
                "refs": [f"gen:{ly['image']}"],
                "animation": {"grid": grid, "mode": "sprite", "key_color": ly["key_color"] or "green"},
                **self._common(comp, lid, "frames", f"{ly['name']} · frames")}
        return await self._run_layer_job(cid, ly, spec)

    async def add_state(self, cid: str, lid: str, prompt: str) -> str:
        """Generate the next state of the piece from its last state image (1 image). Returns the job id."""
        await self._warm_index()
        text = _clean_str(prompt, "prompt", 1, MAX_CORRECTION)
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        self._check_piece_ready(ly)
        anim = animation_of(ly)
        if anim.get("type") == "frames":
            raise _bad("la capa tiene frames: quítalos antes de añadir estados")
        states = anim.get("states") or []
        if len(states) >= MAX_STATES:
            raise _bad(f"máximo {MAX_STATES} estados", max=MAX_STATES)
        src = states[-1]["image"] if states else ly["image"]
        if self._image_info(src) is None:
            raise _bad("la imagen del último estado se borró")
        kc = ly["key_color"]
        common = self._common(comp, lid, "state", f"{ly['name']} · estado {len(states) + 1}")
        spec = self.service.change_spec(src, correction_edit_prompt(text, "piece", kc),
                                        correction_ref_prompt(text, "piece", kc), index=self._index(), **common)
        return await self._run_layer_job(cid, ly, spec)

    def remove_state(self, cid: str, lid: str, k: int) -> dict:
        """Remove saved state k (>= 1); with none left the layer goes back to static."""
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise _bad("k debe ser un entero ≥ 1")
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        if k > len(animation_of(ly).get("states") or []):
            raise GIAError(UNKNOWN_COMPOSITION, f"la capa no tiene el estado {k}")
        gone = []

        def fn(c):
            cur = next(x for x in c["layers"] if x["id"] == lid)
            a = animation_of(cur)
            if a.get("type") != "states" or k > len(a["states"]):
                return
            new = {**a, "states": list(a["states"])}
            gone.append(new["states"].pop(k - 1)["file"])
            cur["animation"] = new if new["states"] else dict(STATIC)
            c["rev"] = next_rev(c["rev"])
        out = self.store.update(cid, fn)
        if not gone:
            raise GIAError(UNKNOWN_COMPOSITION, f"la capa no tiene el estado {k}")
        for f in gone:
            self._drop_file(cid, f, None)
        return out

    def state_path(self, cid: str, lid: str, k: int) -> Path:
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise _bad("k debe ser un entero ≥ 1")
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        states = animation_of(ly).get("states") or []
        if k > len(states):
            raise GIAError(UNKNOWN_COMPOSITION, f"la capa no tiene el estado {k}")
        path = self.store.root / cid / states[k - 1]["file"]
        if not path.is_file():
            raise _bad("el archivo del estado no existe")
        return path

    def delete(self, cid: str) -> bool:
        self._require(cid)
        return self.store.remove(cid)

    # -- views ------------------------------------------------------------------------------------
    def _ficha_of(self, key: str):
        return self.service.animations.get(key[4:] if key.startswith("gen:") else key)

    @staticmethod
    def _effective_rev(comp: dict, fichas: dict) -> int:
        """§3: max(composition rev, rev of every ficha used by a (non broken) frames layer)."""
        revs = [comp["rev"]]
        for ly in comp["layers"]:
            a = animation_of(ly)
            if a.get("type") == "frames" and not frames_view(a, fichas.get(a["image"])).get("broken"):
                revs.append(int(fichas[a["image"]].get("rev") or 0))
        return max(revs)

    @staticmethod
    def _animation_view(ly: dict, fichas: dict) -> dict:
        a = animation_of(ly)
        if a.get("type") == "frames":
            return frames_view(a, fichas.get(a["image"]))
        return copy.deepcopy(a)

    def summaries(self) -> list[dict]:
        fichas = self.service.animations.all()
        out = []
        for c in reversed(self.store.all()):
            layers = c["layers"]
            done = sum(1 for ly in layers if ly["status"] == "done")
            errors = sum(1 for ly in layers if ly["status"] == "error") + (c["master"]["status"] == "error")
            if done:
                cover = {"kind": "preview", "image": None}
            elif c["master"].get("image"):
                cover = {"kind": "master", "image": c["master"]["image"]}
            else:
                cover = {"kind": None, "image": None}
            out.append({"id": c["id"], "name": c["name"], "rev": self._effective_rev(c, fichas),
                        "progress": {"done": done, "total": len(layers), "errors": errors},
                        "cover": cover, "origin": c.get("origin"), "master_status": c["master"]["status"],
                        "layers": [{"id": ly["id"], "name": ly["name"], "role": ly["role"],
                                    "status": ly["status"], "animation": self._animation_view(ly, fichas)}
                                   for ly in layers],
                        "created": c["created"], "updated": c["updated"]})
        out.sort(key=lambda s: s["created"], reverse=True)
        return out

    def detail(self, cid: str, running_job_ids=None) -> dict:
        """The record plus width/height, master_exists and per layer `source_exists` (the webapp builds URLs).
        `rev` is the effective rev and each layer carries its `animation` read view (a frames layer with
        the ficha summary, or static + broken when its sheet is gone). Layers whose job is in
        `running_job_ids` are reported as 'running'."""
        comp = self._require(cid)
        index = self._index()
        fichas = self.service.animations.all()

        def exists(key):
            return bool(key) and (key[4:] if key.startswith("gen:") else key) in index
        comp["rev"] = self._effective_rev(comp, fichas)
        comp["width"], comp["height"] = comp["master"].get("width"), comp["master"].get("height")
        comp["master_exists"] = exists(comp["master"].get("image"))
        for ly in comp["layers"]:
            ly["source_exists"] = exists(ly.get("image"))
            ly["animation"] = self._animation_view(ly, fichas)
            if running_job_ids and ly["status"] == "queued" and ly.get("job") in running_job_ids:
                ly["status"] = "running"
        if running_job_ids and comp["master"]["status"] == "queued" and comp["master"].get("job") in running_job_ids:
            comp["master"]["status"] = "running"
        return comp

    def scene(self, cid: str, names) -> dict:
        """scene.json (spec 3 §5) with the `file` strings chosen by `names` (layer(ly), sheet(ly, key, ficha),
        state(ly, k, st)). Blocking when `names` resolves sheets."""
        comp = self._require(cid)
        if not comp["master"].get("width"):
            raise _bad("la composición aún no tiene imagen completa")
        return build_scene(comp, self._ficha_of, names)

    # -- artifacts (blocking: call from a thread) ---------------------------------------------------
    def _render_flat(self, comp: dict) -> Image.Image:
        """The scene at t = 0 over white (composite.png and the preview)."""
        names = _ExportNames(self, comp)
        scene = build_scene(comp, self._ficha_of, names)
        img = composite_at_t0(scene, names.open)
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.getchannel("A"))
        return flat

    def preview_path(self, cid: str) -> Path:
        """Flat preview PNG for the effective rev (regenerated if missing; older previews are removed)."""
        comp = self._require(cid)
        if not any(ly["status"] == "done" and ly.get("file") for ly in comp["layers"]):
            raise GIAError(UNKNOWN_COMPOSITION, "la composición aún no tiene nada que mostrar")
        rev = self._effective_rev(comp, self.service.animations.all())
        d = self.store.artifacts_dir(cid)
        dest = d / f"preview_{rev}.png"
        if not dest.exists():
            _save_png(self._render_flat(comp), dest)
            for old in d.glob("preview_*.png"):
                if old != dest:
                    old.unlink(missing_ok=True)
        return dest

    def layer_path(self, cid: str, lid: str) -> Path:
        comp = self._require(cid)
        ly = self._layer(comp, lid)
        if not ly.get("file"):
            raise _bad("la capa aún no tiene imagen", layer=lid)
        path = self.store.root / cid / ly["file"]
        if not path.is_file():
            raise _bad("el archivo de la capa no existe", layer=lid)
        return path

    def _tmp_download(self, suffix: str) -> tuple[int, Path]:
        tmp_dir = self.store.dir / "tmp_downloads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _TMP_ZIP_MAX_AGE_S
        for pattern in ("*.zip", "*.webp"):
            for old in tmp_dir.glob(pattern):
                try:
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass
        fd, name = tempfile.mkstemp(suffix=suffix, dir=tmp_dir)
        return fd, Path(name)

    def _exportable(self, cid: str) -> dict:
        comp = self._require(cid)
        if not any(ly["status"] == "done" and ly.get("file") for ly in comp["layers"]):
            raise _bad("la composición aún no tiene capas listas")
        if not comp["master"].get("width"):
            raise _bad("la composición aún no tiene imagen completa")
        return comp

    def export_zip(self, cid: str) -> tuple[Path, str]:
        """Zip (ZIP_STORED) with the layer PNGs (background.png, <slug>.png), frame sheets (<slug>-frames.png),
        states (<slug>-<k>.png), composite.png (t = 0), scene.json, musa-scene.js and index.html in
        <state_dir>/tmp_downloads (0600). The caller deletes the file after sending it."""
        comp = self._exportable(cid)
        names = _ExportNames(self, comp)
        scene = build_scene(comp, self._ficha_of, names)
        flat = composite_at_t0(scene, names.open)
        white = Image.new("RGB", flat.size, (255, 255, 255))
        white.paste(flat, mask=flat.getchannel("A"))
        buf = io.BytesIO()
        white.save(buf, format="PNG")
        player = (PACKAGE_ROOT / "web" / "musa-scene.js").read_bytes()
        fd, path = self._tmp_download(".zip")
        try:
            with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as zf:
                for arc, src in names.files.items():
                    zf.write(src, arc)
                zf.writestr("composite.png", buf.getvalue())
                zf.writestr("scene.json", json.dumps(scene, ensure_ascii=False, indent=1))
                zf.writestr("musa-scene.js", player)
                zf.writestr("index.html", scene_index_html(scene))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path, f"{slugify(comp['name'], 30)}-composicion.zip"

    def export_webp(self, cid: str) -> tuple[Path, str]:
        """Animated flattened WebP (spec 3 §6) in <state_dir>/tmp_downloads. Blocking (~seconds): call from a
        thread. The caller deletes the file after sending it."""
        comp = self._exportable(cid)
        names = _ExportNames(self, comp)
        scene = build_scene(comp, self._ficha_of, names)
        fd, path = self._tmp_download(".webp")
        os.close(fd)
        try:
            render_webp(scene, names.open, path)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path, f"scene-{slugify(comp['name'], 30)}.webp"

    # -- recovery (§3) ----------------------------------------------------------------------------------
    def recover(self, live_job_ids=None) -> int:
        """Close queued/running entries as `interrupted` when their owner process is gone; with
        `live_job_ids`, also those of this process (origin musa) whose job is not in the queue."""
        def dead(comp: dict, ent: dict) -> bool:
            if ent["status"] not in BUSY:
                return False
            pid = comp.get("owner_pid")
            if not _pid_alive(pid):
                return True
            return (live_job_ids is not None and comp.get("origin") == "musa" and pid == os.getpid()
                    and ent.get("job") not in live_job_ids)

        count = 0
        for comp in self.store.all():
            if not any(dead(comp, e) for e in [comp["master"], *comp["layers"]]):
                continue

            def fn(c):
                nonlocal count
                msg = "Musa o el servidor MCP se reiniciaron durante el trabajo"
                for ent in [c["master"], *c["layers"]]:
                    if dead(c, ent):
                        _apply_failure(ent, "interrupted", msg)
                        count += 1
            self.store.update(comp["id"], fn)
        return count


class _ExportNames:
    """File names of an exported scene (background.png, <slug>.png, <slug>-frames.png, <slug>-<k>.png, with
    -2/-3 on collisions). Records name -> source path in `files` and opens them for rendering."""

    def __init__(self, manager: CompositionManager, comp: dict):
        self.manager = manager
        self.dir = manager.store.root / comp["id"]
        self.used = {"background.png", "composite.png"}
        self.files: dict[str, Path] = {}
        self.stems: dict[str, str] = {}

    def _unique(self, stem: str) -> str:
        cand, n = stem, 1
        while f"{cand}.png" in self.used:
            n += 1
            cand = f"{stem}-{n}"
        self.used.add(f"{cand}.png")
        return cand

    def layer(self, ly: dict) -> str:
        if ly["role"] == "background":
            name = "background.png"
        else:
            stem = self._unique(slugify(ly["name"], 30))
            self.stems[ly["id"]] = stem
            name = f"{stem}.png"
        self.files[name] = self.dir / ly["file"]
        return name

    def sheet(self, ly: dict, key: str, ficha: dict) -> str:
        try:
            path = self.manager.service.animation_sheet(f"gen:{key}")
        except GIAError:
            raise
        except Exception as e:  # a ficha without rev / vanished cache: the layer is exported as static
            raise _bad(f"la hoja de frames no está disponible: {e.__class__.__name__}")
        name = self._unique(self.stems[ly["id"]] + "-frames") + ".png"
        self.files[name] = Path(path)
        return name

    def state(self, ly: dict, k: int, st: dict) -> str:
        name = self._unique(self.stems[ly["id"]] + "-" + str(k)) + ".png"
        self.files[name] = self.dir / st["file"]
        return name

    def open(self, name: str) -> Image.Image:
        with Image.open(self.files[name]) as im:
            im.load()
            return im.convert("RGBA")


def scene_index_html(scene: dict) -> str:
    """index.html of the web package: the scene inlined as JSON (<, >, & and U+2028/9 escaped), the
    composition name HTML-escaped in <title>; works from disk."""
    data = (json.dumps(scene, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return (f'<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{html.escape(scene["name"])}</title>\n</head>\n<body style="margin:0">\n'
            f'<div id="scene" style="max-width:{int(scene["width"])}px;margin:auto"></div>\n'
            f'<script type="application/json" id="scene-data">{data}</script>\n'
            f'<script src="musa-scene.js"></script>\n'
            f'<script>MusaScene.mountScene(document.getElementById("scene"), '
            f'JSON.parse(document.getElementById("scene-data").textContent));</script>\n'
            f'</body>\n</html>\n')


def public_view(d: dict) -> dict:
    """Drop the internal fields of a composition detail/record (owner_pid, master_exists, source_exists,
    sheet_rev) in place and return it: the shape both the web API and the MCP tools expose."""
    d.pop("owner_pid", None)
    d.pop("master_exists", None)
    for ly in d.get("layers", ()):
        ly.pop("source_exists", None)
        (ly.get("animation") or {}).pop("sheet_rev", None)
    return d
