"""Single-worker generation queue for the web app (Musa).

One job = one ImageService.generate call. Variations (n) become n jobs sharing
a batch id. At most MAX_PENDING jobs queued+running; finished jobs are kept in
memory for RETAIN_S so the frontend can show their result or error.
The queue is in-memory only: it does not survive an app restart.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path


from .errors import (GENERATED_NOT_SAVED, GIAError, INVALID_ANIMATION, INVALID_ASPECT, INVALID_QUERY, INVALID_REF,
                     UNKNOWN_COLLECTION, UNKNOWN_MODEL, UNKNOWN_STYLE, UPSTREAM_ERROR, error_payload)
from .gallery import image_index
from .refs import GEN_REF_RE
from .service import ImageService
from .sprites import GRIDS, validate_animation_request
from .styles import ASPECT_HINTS, build_prompt
from .uploads import UploadStore

log = logging.getLogger(__name__)

QUEUE_STOP_CODES = {"usage_limit", "quota_guard", "temporarily_blocked", "auth_expired", "auth_failed"}
AUTO_MODEL = "auto"
# Static fallback, used only when the account's live model list is unavailable.
MODELS = [(AUTO_MODEL, "Auto"), ("gemini-flash", "Flash"), ("gemini-pro", "Pro")]
MODEL_LABELS = {"gemini-flash": "Flash", "gemini-flash-lite": "Flash Lite", "gemini-pro": "Pro",
                "gemini-carbon": "Carbon", "gemini-4-hydrogen": "Hydrogen"}


def model_entries(raw: list[dict] | None) -> list[dict]:
    """`/api/models` entries: "auto" first, then the account's available models.
    `raw` is backend.list_models() output; None (backend failure) -> the static fallback."""
    if raw is None:
        return [{"id": mid, "name": name, "description": ""} for mid, name in MODELS]
    out = [{"id": AUTO_MODEL, "name": "Auto", "description": ""}]
    seen = {AUTO_MODEL}
    for m in raw:
        mid = m.get("model_name")
        if not mid or mid in seen or not m.get("available", True):
            continue
        seen.add(mid)
        out.append({"id": mid, "name": MODEL_LABELS.get(mid, mid),
                    "description": m.get("display_name") or ""})
    return out


MAX_REFS_PER_JOB = 3
_COLLECTION_ID_RE = "col_[0-9a-f]{8}"
_SPEC_KEYS = frozenset({"prompt", "style", "model", "ratio", "n", "refs", "uploads", "collection", "template",
                        "template_vars", "edit_of", "animation", "meta", "display_prompt"})


@dataclass
class Job:
    id: str
    batch: str
    prompt: str
    style: str | None
    model: str | None            # resolved model; "auto" becomes None
    ratio: str | None
    refs: list[str]              # gen refs
    uploads: list[str]           # upload ids
    collection: str | None
    status: str = "queued"       # queued|running|done|error|cancelled
    created: float = 0.0
    started: float | None = None
    finished: float | None = None
    record_id: str | None = None
    images: list[str] = field(default_factory=list)   # "<record_id>#<index>" keys
    error: dict | None = None    # {code, message, next_action}
    warning: dict | None = None  # {code, message, next_action}, e.g. partial save (retry_download)
    template: str | None = None
    template_vars: dict | None = None
    edit_of: str | None = None   # "gen:<id>#<i>" the job edits (conversation resolved at submit)
    conversation_id: str | None = field(default=None, repr=False)
    display_prompt: str | None = None   # shown instead of `prompt` (gallery, job JSON); not sent to Gemini
    animation: dict | None = None
    meta: dict | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "batch": self.batch, "prompt": self.display_prompt or self.prompt,
                "style": self.style,
                "model": self.model, "ratio": self.ratio, "refs": list(self.refs),
                "uploads": list(self.uploads), "collection": self.collection, "status": self.status,
                "created": self.created, "started": self.started, "finished": self.finished,
                "record_id": self.record_id, "images": list(self.images), "error": self.error,
                "warning": self.warning,
                **({"template": self.template} if self.template else {}),
                **({"template_vars": dict(self.template_vars)} if self.template_vars else {}),
                **({"edit_of": self.edit_of} if self.edit_of else {}),
                **({"animation": dict(self.animation)} if self.animation else {}),
                **({"meta": dict(self.meta)} if self.meta else {})}


class JobQueue:
    MAX_PENDING = 20
    RETAIN_S = 600

    def __init__(self, service: ImageService, uploads: UploadStore, *,
                 on_change: Callable[[], None] | None = None,
                 now: Callable[[], float] | None = None,
                 on_finish: Callable[[Job], Awaitable[None]] | None = None):
        self.service = service
        self.uploads = uploads
        self.on_change = on_change
        self.on_finish = on_finish
        self._hooks: set[asyncio.Task] = set()
        self._now = now or time.time
        self._jobs: list[Job] = []          # creation order, including finished within RETAIN_S
        self._wakeup = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._model_ids: list[str] | None = None   # live ids from the app's /api/models cache
        self._default_resolved: str | None = None

    def has_models(self) -> bool:
        return self._model_ids is not None

    def set_models(self, entries: list[dict], default_resolved: str | None = None) -> None:
        """Install the model ids submit() validates against (the live list, or the static fallback)."""
        self._model_ids = [e["id"] for e in entries]
        self._default_resolved = default_resolved

    def model_ids(self) -> list[str]:
        return list(self._model_ids) if self._model_ids is not None else [m[0] for m in MODELS]

    # -- public API (submit/cancel are sync; called from handlers on the same loop) --

    def submit(self, *, prompt, style=None, model=None, ratio=None, n=1, refs=(), uploads=(),
               collection=None, template=None, template_vars=None, edit_of=None,
               animation=None, meta=None, display_prompt=None) -> list[Job]:
        conversation_id, edit_key = self._validate(
            prompt=prompt, style=style, model=model, ratio=ratio, n=n, refs=refs, uploads=uploads,
            collection=collection, template=template, template_vars=template_vars, edit_of=edit_of,
            animation=animation)
        self._prune()
        self.check_capacity(n)
        batch = f"b_{uuid.uuid4().hex[:8]}"
        jobs = [self._make_job(batch, conversation_id, edit_key, prompt=prompt, style=style, model=model,
                               ratio=ratio, refs=refs, uploads=uploads, collection=collection,
                               template=template, template_vars=template_vars, animation=animation,
                               meta=meta, display_prompt=display_prompt) for _ in range(n)]
        self._enqueue(jobs)
        return jobs

    def submit_many(self, specs: list[dict]) -> list[Job]:
        """Queue one job per spec (submit kwargs, n=1 implied) under a shared batch id.
        All specs are validated and capacity is checked once before anything is created."""
        validated = []
        for spec in specs:
            unknown = sorted(set(spec) - _SPEC_KEYS)
            if unknown:
                raise GIAError(INVALID_QUERY, f"unknown job spec key(s): {', '.join(map(str, unknown))}",
                               keys=unknown)
            if "n" in spec and (isinstance(spec["n"], bool) or spec["n"] != 1):
                raise GIAError(INVALID_QUERY, f"submit_many specs imply n=1, got n={spec['n']!r}")
            spec = {**spec, "n": 1}
            cid, edit_key = self._validate(
                prompt=spec.get("prompt"), style=spec.get("style"), model=spec.get("model"),
                ratio=spec.get("ratio"), n=1, refs=spec.get("refs", ()), uploads=spec.get("uploads", ()),
                collection=spec.get("collection"), template=spec.get("template"),
                template_vars=spec.get("template_vars"), edit_of=spec.get("edit_of"),
                animation=spec.get("animation"))
            validated.append((spec, cid, edit_key))
        self._prune()
        self.check_capacity(len(specs))
        batch = f"b_{uuid.uuid4().hex[:8]}"
        jobs = []
        for spec, cid, edit_key in validated:
            kw = {k: v for k, v in spec.items() if k not in ("n", "edit_of")}
            jobs.append(self._make_job(batch, cid, edit_key, **kw))
        self._enqueue(jobs)
        return jobs

    def check_capacity(self, n: int) -> None:
        pending = sum(1 for j in self._jobs if j.status in ("queued", "running"))
        if pending + n > self.MAX_PENDING:
            raise GIAError("queue_full",
                           f"{pending + n} pending jobs exceeds MAX_PENDING={self.MAX_PENDING}",
                           pending=pending, n=n, max_pending=self.MAX_PENDING)

    def _make_job(self, batch, conversation_id, edit_key, *, prompt, style=None, model=None, ratio=None,
                  refs=(), uploads=(), collection=None, template=None, template_vars=None,
                  animation=None, meta=None, display_prompt=None) -> Job:
        if animation is not None:  # normalized; the ratio is forced to the grid's aspect
            animation = validate_animation_request(animation)
            ratio = GRIDS[animation["grid"]][2]
        if template:  # the template replaces the user's prompt (as build_prompt does)
            t = self.service.catalog().templates[template]
            prompt = t.prompt.format(**{k: str(v) for k, v in (template_vars or {}).items()})
        job = Job(id=f"j_{uuid.uuid4().hex[:8]}", batch=batch, prompt=prompt, style=style,
                  model=None if model in (None, "auto") else model, ratio=ratio,
                  refs=list(refs), uploads=list(uploads), collection=collection,
                  template=template or None, template_vars=dict(template_vars) if template_vars else None,
                  edit_of=edit_key, conversation_id=conversation_id, created=self._now(),
                  animation=animation, meta=dict(meta) if meta is not None else None,
                  display_prompt=display_prompt)
        return job

    def _enqueue(self, jobs: list[Job]) -> None:
        self._jobs.extend(jobs)
        self._ensure_worker()
        self._wakeup.set()
        self._notify()

    def cancel(self, job_id: str) -> Job:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None or job.status in ("done", "error", "cancelled"):
            raise GIAError("unknown_job", f"unknown job {job_id!r}")
        if job.status == "running":
            raise GIAError("job_running", f"job {job_id!r} is already running and cannot be cancelled")
        job.status = "cancelled"
        job.finished = self._now()
        self._prune()
        self._notify()
        self._schedule_hook(job)
        return job

    def list(self) -> list[Job]:
        """Queued + running + finished within RETAIN_S, in creation order."""
        self._prune()
        return list(self._jobs)

    def _prune(self) -> None:
        """Drop finished jobs (done/error/cancelled) older than RETAIN_S."""
        now = self._now()
        self._jobs = [j for j in self._jobs
                      if j.status in ("queued", "running")
                      or (j.finished is not None and now - j.finished <= self.RETAIN_S)]

    def eta_s(self) -> float:
        """Median elapsed_s of the last 20 ok records; 30.0 when there is no data."""
        records = [r for r in self.service.state.visible_records() if r.status == "ok"]
        values = sorted(float(r.elapsed_s) for r in records[-20:])
        if not values:
            return 30.0
        mid = len(values) // 2
        if len(values) % 2:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2.0

    async def aclose(self) -> None:
        self._closed = True
        self._wakeup.set()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._hooks:
            await asyncio.gather(*list(self._hooks), return_exceptions=True)

    # -- validation --

    def _validate(self, *, prompt, style, model, ratio, n, refs, uploads, collection,
                  template=None, template_vars=None, edit_of=None, animation=None) -> tuple[str | None, str | None]:
        """Synchronous checks; returns (conversation id, normalized edit_of) — both None unless edit_of."""
        if prompt is not None and not isinstance(prompt, str):
            raise GIAError(INVALID_QUERY, "prompt must be a string")
        if animation is not None:
            if template or edit_of:
                raise GIAError(INVALID_ANIMATION, "animation cannot be combined with template or edit_of")
            validate_animation_request(animation)
        if template is not None and (not isinstance(template, str) or not template):
            raise GIAError(INVALID_QUERY, "template must be a non-empty string")
        if template_vars is not None and (not isinstance(template_vars, dict) or not template):
            raise GIAError(INVALID_QUERY, "template_vars must be an object and requires a template")
        if template:
            # unknown template -> unknown_style; missing/unformattable vars -> template_missing_var
            build_prompt(self.service.catalog(), prompt="x", template=template, template_vars=template_vars)
        elif not (prompt or "").strip():
            raise GIAError(INVALID_QUERY, "prompt must not be empty")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 8:
            raise GIAError(INVALID_QUERY, f"n must be an integer between 1 and 8, got {n!r}")
        if edit_of and n != 1:
            raise GIAError(INVALID_QUERY, f"edit_of requires n=1, got {n!r}")
        if ratio is not None and ratio not in ASPECT_HINTS:
            raise GIAError(INVALID_ASPECT, f"invalid ratio {ratio!r}", valid=sorted(ASPECT_HINTS))
        if model is not None:
            valid = self.model_ids()
            if model not in valid and model not in (self.service.settings.default_model, self._default_resolved):
                raise GIAError(UNKNOWN_MODEL, f"unknown model {model!r}", valid=valid)
        if len(refs) + len(uploads) > MAX_REFS_PER_JOB:
            raise GIAError(INVALID_REF,
                           f"maximum {MAX_REFS_PER_JOB} refs per job (uploads included), "
                           f"got {len(refs) + len(uploads)}",
                           max_refs=MAX_REFS_PER_JOB, got=len(refs) + len(uploads))
        if style is not None:
            catalog = self.service.catalog()
            if style not in catalog.styles:
                raise GIAError(UNKNOWN_STYLE, f"unknown style {style!r}",
                               valid=sorted(catalog.styles))
        if collection is not None and not collection.strip():
            raise GIAError(UNKNOWN_COLLECTION, "collection name is empty")
        if collection:
            needle = collection.strip()
            if re.fullmatch(_COLLECTION_ID_RE, needle) and \
                    self.service.library.get_collection(needle) is None:
                raise GIAError(UNKNOWN_COLLECTION, f"collection {needle!r} does not exist",
                               valid=[c.name for c in self.service.library.collections()])
        if not edit_of:
            return None, None
        m = GEN_REF_RE.match(edit_of) if isinstance(edit_of, str) else None
        if not m:
            raise GIAError(INVALID_REF, f"bad edit_of {edit_of!r}; expected gen:g_<12 hex>#<index>")
        key = f"{m.group(1)}#{int(m.group(2) or 0)}"
        svc = self.service
        entry = image_index(svc.state, svc.library, svc.settings.output_root).get(key)
        cid = getattr(getattr(entry, "record", None), "conversation_id", None)
        if entry is None or not cid or svc.state.conversation(cid) is None:
            raise GIAError(INVALID_REF, f"image {key} cannot be edited: unknown, deleted or no conversation")
        return cid, f"gen:{key}"

    # -- worker --

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    async def _work(self) -> None:
        while not self._closed:
            job = next((j for j in self._jobs if j.status == "queued"), None)
            if job is None:
                self._wakeup.clear()
                if next((j for j in self._jobs if j.status == "queued"), None) is None:
                    await self._wakeup.wait()
                continue
            await self._process(job)

    async def _process(self, job: Job) -> None:
        job.status = "running"
        job.started = self._now()
        self._notify()
        try:
            upload_paths = [self.uploads.path(u) for u in job.uploads]
            kw = dict(prompt=job.prompt, refs=job.refs, uploads=upload_paths, style=job.style,
                      template=job.template, template_vars=job.template_vars,
                      aspect_hint=job.ratio, model=job.model, collection=job.collection,
                      display_prompt=job.display_prompt)
            if job.conversation_id:
                sr = await self.service.edit(job.conversation_id, **kw)
            else:
                sr = await self.service.generate(**kw, animation=job.animation)
        except GIAError as e:
            self._fail(job, e)
            if e.code in QUEUE_STOP_CODES:
                self._drain_queue(e)
            return
        except Exception:
            log.exception("job %s failed with an unexpected error", job.id)
            self._fail(job, GIAError(UPSTREAM_ERROR, "upstream generation failed"))
            return
        res = sr.result
        if res.status == "error":
            # The real backend reports failures as an error GenerateResult, not an exception.
            e = GIAError(res.code or UPSTREAM_ERROR, res.gemini_text or "generation failed")
            self._fail(job, e)
            if e.code in QUEUE_STOP_CODES:
                self._drain_queue(e)
            return
        job.record_id = res.record_id
        job.images = self._image_keys(res)
        if res.status == "generated_not_saved" and not job.images:
            self._fail(job, GIAError(res.code or GENERATED_NOT_SAVED,
                                     res.save_error or "image was generated but not saved"))
            return
        if res.status != "ok":
            # Partial save: the job is done, but tell the user about retry_download.
            payload = error_payload(GIAError(res.code or GENERATED_NOT_SAVED,
                                             res.save_error or "some images were not saved"))
            job.warning = {"code": payload["code"], "message": payload["message"],
                           "next_action": payload["next_action"]}
        if sr.warning is not None and (job.warning is None or sr.warning.get("code") != "invalid_animation"):
            job.warning = sr.warning
        job.status = "done"
        job.finished = self._now()
        self._prune()
        self._notify()
        self._schedule_hook(job)

    def _image_keys(self, res) -> list[str]:
        """Image keys "<record_id>#<index>", mapping the index like service._assign_collection."""
        rec = self.service.state.generation(res.record_id) if res.record_id else None
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

    def _fail(self, job: Job, e: GIAError) -> None:
        payload = error_payload(e)
        job.error = {"code": payload["code"], "message": payload["message"],
                     "next_action": payload["next_action"]}
        job.status = "error"
        job.finished = self._now()
        self._prune()
        self._notify()
        self._schedule_hook(job)

    def _drain_queue(self, e: GIAError) -> None:
        """A queue-wide stop code fails every job still queued with the same error."""
        payload = error_payload(e)
        error = {"code": payload["code"], "message": payload["message"],
                 "next_action": payload["next_action"]}
        drained_jobs = []
        for j in self._jobs:
            if j.status == "queued":
                j.status = "error"
                j.error = dict(error)
                j.finished = self._now()
                drained_jobs.append(j)
        drained = bool(drained_jobs)
        if drained:
            self._prune()
            self._notify()
            for j in drained_jobs:
                self._schedule_hook(j)

    def _schedule_hook(self, job: Job) -> None:
        """Run on_finish for a job in its final state, as a task so the worker is never blocked."""
        if self.on_finish is None:
            return
        task = asyncio.get_running_loop().create_task(self._run_hook(job))
        self._hooks.add(task)
        task.add_done_callback(self._hooks.discard)

    async def _run_hook(self, job: Job) -> None:
        hook = self.on_finish
        if hook is None:
            return
        try:
            await hook(job)
        except Exception:
            log.exception("on_finish hook failed for job %s", job.id)
