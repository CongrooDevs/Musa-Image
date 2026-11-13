import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

from musa.client import Backend, GenerateResult
from musa.errors import GIAError
from musa.jobs import MODELS, JobQueue, model_entries
from musa.library import LibraryStore
from musa.service import ImageService
from musa.state import GenerationRecord, StateStore
from musa.storage import SavedImage
from musa.style_store import UserStyleStore
from musa.uploads import UploadStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeBackend(Backend):
    """Mimics the real backend: failures come back as GenerateResult(status=...),
    not as exceptions. raise_codes covers the GIAError path (e.g. quota_guard)."""

    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.error_results: list[str] = []   # returned as GenerateResult("error", code)
        self.raise_codes: list[str] = []     # raised as GIAError
        self.not_saved_count: int | None = None  # generated_not_saved with this many saved images
        self.warnings: list[dict] = []           # popped into GenerateResult.warning on each call
        self.boom = False                    # raise a non-GIAError
        self.running = 0
        self.max_running = 0
        self.img = None
        self.edit_calls = []

    async def quota(self):
        return None

    def _record(self, prompt, status, code, images):
        st = StateStore(self.settings.state_dir)
        rec = GenerationRecord(
            id=StateStore.new_id("g"), ts=datetime.now(timezone.utc).isoformat(),
            type="generate", conversation_id="c_1", chat_metadata=["c"], prompt=prompt,
            style=None, style_text=None, aspect_hint=None, model="gemini-flash",
            project=None, subdir=None, refs=[], images=images,
            status=status, code=code, gemini_text="done", elapsed_s=2.0, parent_id=None,
            user_prompt=getattr(self, "_slug", None) or prompt)
        st.append(rec)
        return rec

    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        self.edit_calls.append((conversation_id, prompt, model, kw))
        self._slug = kw.get("slug_source")
        rec = self._record(prompt, "ok", None, [{"index": 0, "path": str(self.img.path)}])
        return GenerateResult("ok", None, conversation_id, [self.img], "done", 2.0, ["c"], record_id=rec.id)

    async def generate(self, prompt, refs, *, model, **kw):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.calls.append((prompt, [str(r.path) for r in refs], model, kw))
        self._slug = kw.get("slug_source")
        try:
            await self.gate.wait()
            warning = self.warnings.pop(0) if self.warnings else None
            if self.boom:
                raise RuntimeError("kaput")
            if self.raise_codes:
                raise GIAError(self.raise_codes.pop(0), "backend raised")
            if self.error_results:
                code = self.error_results.pop(0)
                rec = self._record(prompt, "error", code, [])
                return GenerateResult("error", code, "c_1", [], f"backend failed: {code}",
                                      1.0, ["c"], record_id=rec.id)
            if self.not_saved_count is not None:
                images = ([{"index": 0, "path": str(self.img.path)}]
                          if self.not_saved_count > 0 else [])
                images += [{"index": i, "path": None} for i in range(len(images), 2)]
                rec = self._record(prompt, "generated_not_saved", "generated_not_saved", images)
                saved = [self.img] if self.not_saved_count > 0 else []
                return GenerateResult("generated_not_saved", "generated_not_saved", "c_1",
                                      saved, "done", 1.0, ["c"], save_error="OSError: boom",
                                      record_id=rec.id, warning=warning)
            rec = self._record(prompt, "ok", None, [{"index": 0, "path": str(self.img.path)}])
            return GenerateResult("ok", None, "c_1", [self.img], "done", 2.0, ["c"],
                                  record_id=rec.id, warning=warning)
        finally:
            self.running -= 1


async def wait_until(cond, timeout=5.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


@pytest.fixture
async def env(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings.styles_file = tmp_path / "styles.yaml"
    settings.styles_file.write_text(yaml.safe_dump(
        {"styles": {"anime": {"description": "d", "append": "anime style"}},
         "templates": {"t1": {"description": "tpl", "prompt": "a {thing} at dusk", "vars": ["thing"]},
                       "t0": {"description": "static", "prompt": "a calm lake", "vars": []}}}))
    img_path = settings.output_root / "a.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    img_path.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    backend = FakeBackend(settings)
    backend.img = SavedImage(img_path, 64, 64, 35, "jpeg", True, b"thumb")
    service = ImageService(backend, settings, StateStore(settings.state_dir),
                           library=LibraryStore(settings.state_dir),
                           styles=UserStyleStore(settings.state_dir))
    uploads = UploadStore(settings.state_dir, settings)
    changes: list[int] = []
    q = JobQueue(service, uploads, on_change=lambda: changes.append(1))
    yield SimpleNamespace(q=q, backend=backend, service=service, uploads=uploads,
                          changes=changes, settings=settings)
    await q.aclose()


def _record(state: StateStore, elapsed: float, n: int, path: str = "x.jpg") -> None:
    rec = GenerationRecord(
        id=f"g_{n:012x}", ts=datetime.now(timezone.utc).isoformat(),
        type="generate", conversation_id=f"c_{n}", chat_metadata=["c"], prompt="p",
        style=None, style_text=None, aspect_hint=None, model="gemini-flash",
        project=None, subdir=None, refs=[], images=[{"index": 0, "path": path}],
        status="ok", code=None, gemini_text="", elapsed_s=elapsed, parent_id=None)
    state.append(rec)


async def test_submit_creates_one_batch_and_runs_jobs_one_at_a_time(env):
    jobs = env.q.submit(prompt="a cat", n=3)
    assert len(jobs) == 3
    assert len({j.batch for j in jobs}) == 1
    assert all(j.status == "queued" for j in jobs)
    await wait_until(lambda: all(j.status == "done" for j in jobs))
    assert env.backend.max_running == 1
    for j in jobs:
        assert j.record_id and j.images == [f"{j.record_id}#0"]
        assert j.started is not None and j.finished is not None


async def test_done_job_gets_collection_assigned(env):
    jobs = env.q.submit(prompt="a cat", n=1, collection="Vacaciones")
    await wait_until(lambda: jobs[0].status == "done")
    job = jobs[0]
    coll = env.service.library.get_collection("Vacaciones")
    assert coll is not None
    assert env.service.library.collections_of(job.images[0]) == [coll.id]


async def test_cancel_queued_running_and_unknown(env):
    env.backend.gate.clear()
    jobs = env.q.submit(prompt="x", n=3)
    await wait_until(lambda: jobs[0].status == "running")
    with pytest.raises(GIAError) as e:
        env.q.cancel(jobs[0].id)
    assert e.value.code == "job_running"
    cancelled = env.q.cancel(jobs[1].id)
    assert cancelled.status == "cancelled"
    assert all(j.id != jobs[1].id or j.status != "queued" for j in env.q.list())
    with pytest.raises(GIAError) as e:
        env.q.cancel("j_doesnotexist")
    assert e.value.code == "unknown_job"
    env.backend.gate.set()
    await wait_until(lambda: all(j.status in ("done", "cancelled") for j in jobs))
    assert jobs[0].status == "done" and jobs[2].status == "done"


async def test_template_job_is_validated_sync_and_rendered(env):
    for kw, code in (({"template": "nope"}, "unknown_style"),
                     ({"template": "t1"}, "template_missing_var"),
                     ({"template": "t1", "template_vars": {"thing": "x"}, "n": 0}, "invalid_query"),
                     ({"template": "t1", "template_vars": "x"}, "invalid_query")):
        with pytest.raises(GIAError) as e:
            env.q.submit(prompt="", **kw)
        assert e.value.code == code, kw
    (job,) = env.q.submit(prompt="", template="t1", template_vars={"thing": "cat"})
    assert job.prompt == "a cat at dusk"
    await wait_until(lambda: job.status == "done")
    sent = env.backend.calls[-1][0]
    assert "a cat at dusk" in sent
    j = job.to_json()
    assert j["template"] == "t1" and j["template_vars"] == {"thing": "cat"} and "edit_of" not in j


async def test_edit_of_calls_service_edit_with_n_one(env):
    _record(env.service.state, 1.0, 1, path=str(env.backend.img.path))
    for kw, code in (({"edit_of": "bogus"}, "invalid_ref"),
                     ({"edit_of": "gen:g_0000000000ff#0"}, "invalid_ref"),
                     ({"edit_of": "gen:g_000000000001#0", "n": 2}, "invalid_query")):
        with pytest.raises(GIAError) as e:
            env.q.submit(prompt="more red", **kw)
        assert e.value.code == code, kw
    (job,) = env.q.submit(prompt="more red", edit_of="gen:g_000000000001#0")
    await wait_until(lambda: job.status == "done")
    assert [c[0] for c in env.backend.edit_calls] == ["c_1"]
    assert not env.backend.calls
    assert job.to_json()["edit_of"] == "gen:g_000000000001#0"
    assert "conversation_id" not in job.to_json()


async def test_returned_usage_limit_fails_job_and_drains_queue(env):
    env.backend.gate.clear()
    env.backend.error_results = ["usage_limit"]
    jobs = env.q.submit(prompt="x", n=3)
    env.backend.gate.set()
    await wait_until(lambda: all(j.status == "error" for j in jobs))
    assert env.backend.max_running == 1
    for j in jobs:
        assert j.error is not None
        assert set(j.error) == {"code", "message", "next_action"}
        assert j.error["code"] == "usage_limit"


async def test_raised_quota_guard_also_drains_queue(env):
    env.backend.gate.clear()
    env.backend.raise_codes = ["quota_guard"]
    jobs = env.q.submit(prompt="x", n=3)
    env.backend.gate.set()
    await wait_until(lambda: all(j.status == "error" for j in jobs))
    assert all(j.error["code"] == "quota_guard" for j in jobs)


async def test_returned_no_image_fails_only_that_job(env):
    env.backend.error_results = ["no_image"]
    jobs = env.q.submit(prompt="x", n=3)
    await wait_until(lambda: all(j.status in ("done", "error") for j in jobs))
    assert jobs[0].status == "error" and jobs[0].error["code"] == "no_image"
    assert jobs[1].status == "done" and jobs[2].status == "done"


async def test_generated_not_saved_partial_is_done_with_warning(env):
    env.backend.not_saved_count = 1
    jobs = env.q.submit(prompt="x", n=1)
    await wait_until(lambda: jobs[0].status == "done")
    job = jobs[0]
    assert job.images == [f"{job.record_id}#0"]
    assert job.error is None
    assert job.warning is not None
    assert set(job.warning) == {"code", "message", "next_action"}
    assert job.warning["code"] == "generated_not_saved"
    assert job.to_json()["warning"] == job.warning


async def test_generated_not_saved_without_images_is_error(env):
    env.backend.not_saved_count = 0
    jobs = env.q.submit(prompt="x", n=1)
    await wait_until(lambda: jobs[0].status == "error")
    assert jobs[0].error["code"] == "generated_not_saved"
    assert jobs[0].images == []


async def test_unexpected_exception_becomes_generic_upstream_error(env):
    env.backend.boom = True
    jobs = env.q.submit(prompt="x", n=1)
    await wait_until(lambda: jobs[0].status == "error")
    assert jobs[0].error["code"] == "upstream_error"
    assert "kaput" not in jobs[0].error["message"]


async def test_job_21_pending_is_queue_full(env):
    env.backend.gate.clear()
    for _ in range(JobQueue.MAX_PENDING):
        env.q.submit(prompt="x", n=1)
    with pytest.raises(GIAError) as e:
        env.q.submit(prompt="x", n=1)
    assert e.value.code == "queue_full"
    env.backend.gate.set()
    await wait_until(lambda: all(j.status == "done" for j in env.q.list()))


async def test_model_validation_uses_live_list_then_static_fallback(env):
    q = env.q
    q.submit(prompt="x", model="gemini-flash")  # no live list yet: static fallback
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", model="gemini-carbon")
    assert e.value.code == "unknown_model" and e.value.details["valid"] == [m[0] for m in MODELS]
    q.set_models(model_entries([{"model_name": "gemini-carbon", "available": True},
                                {"model_name": "gemini-pro", "available": False}]))
    (job,) = q.submit(prompt="x", model="gemini-carbon")
    assert job.model == "gemini-carbon"
    (auto,) = q.submit(prompt="x", model="auto")
    assert auto.model is None
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", model="gemini-pro")
    assert e.value.code == "unknown_model" and e.value.details["valid"] == ["auto", "gemini-carbon"]
    q.submit(prompt="x", model="gemini-flash")  # settings.default_model is always accepted
    await wait_until(lambda: all(j.status == "done" for j in q.list()))


def test_model_entries_shape_and_fallback():
    assert model_entries(None) == [{"id": "auto", "name": "Auto", "description": ""},
                                   {"id": "gemini-flash", "name": "Flash", "description": ""},
                                   {"id": "gemini-pro", "name": "Pro", "description": ""}]
    got = model_entries([{"model_name": "gemini-flash-lite", "available": True, "display_name": "Rápido"},
                         {"model_name": "gemini-flash-lite", "available": True},
                         {"model_name": "zzz", "available": True}, {"model_name": "off", "available": False}])
    assert got == [{"id": "auto", "name": "Auto", "description": ""},
                   {"id": "gemini-flash-lite", "name": "Flash Lite", "description": "Rápido"},
                   {"id": "zzz", "name": "zzz", "description": ""}]


async def test_submit_validates_synchronously(env):
    q = env.q
    with pytest.raises(GIAError):
        q.submit(prompt="   ")
    with pytest.raises(GIAError):
        q.submit(prompt="x", n=0)
    with pytest.raises(GIAError):
        q.submit(prompt="x", n=9)
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", ratio="7:5")
    assert e.value.code == "invalid_aspect"
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", model="gpt-image-1")
    assert e.value.code == "unknown_model"
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", refs=["gen:g_0123456789ab"] * 4)
    assert e.value.code == "invalid_ref"
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", style="nope")
    assert e.value.code == "unknown_style"
    with pytest.raises(GIAError) as e:
        q.submit(prompt="x", collection="col_00000000")
    assert e.value.code == "unknown_collection"
    # a plain name is accepted and the service creates it
    jobs = q.submit(prompt="x", n=1, collection="Nueva coleccion")
    await wait_until(lambda: jobs[0].status == "done")
    assert env.service.library.get_collection("Nueva coleccion") is not None


async def test_finished_jobs_disappear_after_retain_s(env):
    now = [1000.0]
    q = JobQueue(env.service, env.uploads, now=lambda: now[0])
    try:
        jobs = q.submit(prompt="x", n=1)
        await wait_until(lambda: jobs[0].status == "done")
        assert len(q.list()) == 1
        now[0] += JobQueue.RETAIN_S + 1
        assert q.list() == []
    finally:
        await q.aclose()


async def test_prune_shrinks_internal_job_list(env):
    now = [1000.0]
    q = JobQueue(env.service, env.uploads, now=lambda: now[0])
    try:
        jobs = q.submit(prompt="x", n=2)
        await wait_until(lambda: all(j.status == "done" for j in jobs))
        assert len(q._jobs) == 2
        now[0] += JobQueue.RETAIN_S + 1
        q.list()  # triggers _prune
        assert q._jobs == []
        assert q.list() == []
    finally:
        await q.aclose()


async def test_on_change_called_on_every_transition(env):
    jobs = env.q.submit(prompt="x", n=2)
    await wait_until(lambda: all(j.status == "done" for j in jobs))
    # 1 for submit + (queued->running + running->done) per job
    assert len(env.changes) == 1 + 2 * 2


async def test_eta_s_is_median_or_default(env):
    assert env.q.eta_s() == 30.0
    state = env.service.state
    for i, elapsed in enumerate([40.0, 10.0, 30.0, 20.0]):
        _record(state, elapsed, i)
    assert env.q.eta_s() == 25.0


async def test_uploads_resolved_and_passed_to_service(env):
    uid = env.uploads.save(PNG)
    jobs = env.q.submit(prompt="x", n=1, uploads=[uid])
    await wait_until(lambda: jobs[0].status == "done")
    assert jobs[0].status == "done"
    assert env.backend.calls[0][1] == [str(env.uploads.path(uid))]
    bad = env.q.submit(prompt="x", n=1, uploads=["u_0123456789ab"])
    await wait_until(lambda: bad[0].status == "error")
    assert bad[0].error["code"] == "unknown_upload"


async def test_model_auto_maps_to_none_and_ratio_becomes_aspect_hint(env):
    jobs = env.q.submit(prompt="x", n=1, model="auto", ratio="16:9")
    await wait_until(lambda: jobs[0].status == "done")
    assert jobs[0].model is None
    prompt_sent, _refs, model_sent, _kw = env.backend.calls[0]
    assert "16:9" in prompt_sent
    assert model_sent == env.settings.default_model  # None -> default in the service


async def test_job_to_json_shape(env):
    jobs = env.q.submit(prompt="x", n=1)
    await wait_until(lambda: jobs[0].status == "done")
    j = jobs[0].to_json()
    assert set(j) == {"id", "batch", "prompt", "style", "model", "ratio", "refs", "uploads",
                      "collection", "status", "created", "started", "finished", "record_id",
                      "images", "error", "warning"}
    assert j["status"] == "done" and j["images"] == [f"{j['record_id']}#0"]
    assert j["warning"] is None


async def test_sr_warning_lands_on_done_job(env):
    w = {"code": "gem_missing", "message": "gem gone", "next_action": "turn the Gem off"}
    env.backend.warnings.append(w)
    jobs = env.q.submit(prompt="x", n=1)
    await wait_until(lambda: jobs[0].status == "done")
    assert jobs[0].warning == w
    assert jobs[0].to_json()["warning"] == w


async def test_sr_warning_overwrites_partial_save_warning(env):
    env.backend.not_saved_count = 1
    w = {"code": "gem_missing", "message": "gem gone", "next_action": "turn the Gem off"}
    env.backend.warnings.append(w)
    jobs = env.q.submit(prompt="x", n=1)
    await wait_until(lambda: jobs[0].status == "done")
    assert jobs[0].warning == w  # sr.warning wins over the generated_not_saved warning



async def test_edit_of_rejects_deleted_image_deleted_conversation_and_bad_index(env):
    state = env.service.state
    img = str(env.backend.img.path)
    _record(state, 1.0, 1, path=img)
    _record(state, 1.0, 2, path=img)
    env.service.library.mark_deleted("g_000000000001#0")
    state.append(GenerationRecord(
        id="g_0000000000d2", ts=datetime.now(timezone.utc).isoformat(), type="delete", conversation_id="c_2",
        chat_metadata=[], prompt="", style=None, style_text=None, aspect_hint=None, model=None, project=None,
        subdir=None, refs=[], images=[], status="ok", code=None, gemini_text="", elapsed_s=0.0, parent_id=None))
    _record(state, 1.0, 3, path=img)
    for ref in ("gen:g_000000000001#0",   # image deleted
                "gen:g_000000000002#0",   # conversation deleted
                "gen:g_000000000003#5"):  # index does not exist
        with pytest.raises(GIAError) as e:
            env.q.submit(prompt="x", edit_of=ref)
        assert e.value.code == "invalid_ref", ref
    (job,) = env.q.submit(prompt="x", edit_of="gen:g_000000000003")  # no '#': index 0, normalized
    assert job.edit_of == "gen:g_000000000003#0"


async def test_template_job_prompt_is_always_rendered(env):
    (job,) = env.q.submit(prompt="ignored user text", template="t1", template_vars={"thing": "dog"})
    assert job.prompt == "a dog at dusk"


async def test_non_string_prompt_is_invalid_query(env):
    for bad in (5, ["a"], {"a": 1}):
        with pytest.raises(GIAError) as e:
            env.q.submit(prompt=bad)
        assert e.value.code == "invalid_query"


async def test_template_without_vars_and_without_template_vars(env):
    (job,) = env.q.submit(prompt="", template="t0")
    assert job.prompt == "a calm lake"
    await wait_until(lambda: job.status == "done")
    assert "a calm lake" in env.backend.calls[-1][0]


# -- phase 1 infrastructure: display_prompt, submit_many, on_finish, warning precedence --

async def test_display_prompt_reaches_backend_slug_and_job_json(env):
    (job,) = env.q.submit(prompt="long internal prompt", display_prompt="Un gato")
    assert job.to_json()["prompt"] == "Un gato"
    await wait_until(lambda: job.status == "done")
    assert env.backend.calls[-1][3]["slug_source"] == "Un gato"
    (plain,) = env.q.submit(prompt="plain")
    assert plain.to_json()["prompt"] == "plain"
    await wait_until(lambda: plain.status == "done")
    assert env.backend.calls[-1][3]["slug_source"] == "plain"


async def test_display_prompt_reaches_edit_slug(env):
    _record(env.service.state, 1.0, 1, path=str(env.backend.img.path))
    (job,) = env.q.submit(prompt="more red", edit_of="gen:g_000000000001#0", display_prompt="Rojo")
    await wait_until(lambda: job.status == "done")
    assert env.backend.edit_calls[-1][3]["slug_source"] == "Rojo"


async def test_animation_and_meta_in_json_only_when_set(env):
    (job,) = env.q.submit(prompt="x", animation={"grid": "2x2"}, meta={"composition": "c1"})
    j = job.to_json()
    assert j["animation"] == {"grid": "2x2", "mode": "sprite", "key_color": "green"}
    assert j["meta"] == {"composition": "c1"}
    (plain,) = env.q.submit(prompt="y")
    assert "animation" not in plain.to_json() and "meta" not in plain.to_json()


async def test_submit_many_shares_batch_and_runs(env):
    jobs = env.q.submit_many([{"prompt": "a", "meta": {"k": 1}}, {"prompt": "b", "display_prompt": "B"}])
    assert len(jobs) == 2 and len({j.batch for j in jobs}) == 1
    assert jobs[0].meta == {"k": 1} and jobs[1].to_json()["prompt"] == "B"
    await wait_until(lambda: all(j.status == "done" for j in jobs))


async def test_submit_n_gives_each_job_its_own_meta_and_animation(env):
    env.backend.gate.clear()
    jobs = env.q.submit(prompt="x", n=2, meta={"k": [1]}, animation={"grid": "2x2"})
    assert jobs[0].meta == jobs[1].meta == {"k": [1]}
    assert jobs[0].meta is not jobs[1].meta and jobs[0].animation is not jobs[1].animation
    jobs[0].meta["k2"] = 1
    assert "k2" not in jobs[1].meta
    env.backend.gate.set()
    many = env.q.submit_many([{"prompt": "a", "meta": {"k": 1}}])
    assert many[0].meta == {"k": 1}


async def test_submit_many_rejects_unknown_keys_and_n(env):
    for spec in ({"prompt": "a", "bogus": 1}, {"prompt": "a", "n": 2}, {"prompt": "a", "n": True}):
        with pytest.raises(GIAError) as e:
            env.q.submit_many([{"prompt": "ok"}, spec])
        assert e.value.code == "invalid_query"
    assert env.q.list() == []
    jobs = env.q.submit_many([{"prompt": "a", "n": 1}])
    assert len(jobs) == 1


async def test_record_user_prompt_is_display_prompt(env):
    (job,) = env.q.submit(prompt="long internal prompt", display_prompt="Un gato")
    await wait_until(lambda: job.status == "done")
    assert env.service.state.generation(job.record_id).user_prompt == "Un gato"


async def test_animation_job_forces_ratio_and_normalizes(env):
    (job,) = env.q.submit(prompt="walk", ratio="1:1", animation={"grid": "4x2", "mode": "scene"})
    assert job.ratio == "16:9"
    assert job.animation == {"grid": "4x2", "mode": "scene", "key_color": None}
    await wait_until(lambda: job.status in ("done", "error"))
    assert env.backend.calls[-1][3]["aspect_hint"] == "16:9"
    assert "4 columns x 2 rows" in env.backend.calls[-1][0]


async def test_animation_validation_errors(env):
    cases = [dict(animation={"grid": "9x9"}), dict(animation={"grid": "4x2", "x": 1}),
             dict(animation="4x2"), dict(animation={"grid": "4x2"}, template="t0"),
             dict(animation={"grid": "4x2"}, edit_of="gen:g_000000000001#0")]
    _record(env.service.state, 1.0, 1, path=str(env.backend.img.path))
    for kw in cases:
        with pytest.raises(GIAError) as e:
            env.q.submit(prompt="x", **kw)
        assert e.value.code == "invalid_animation", kw
    assert env.q.list() == []

async def test_submit_many_validation_error_creates_nothing(env):
    with pytest.raises(GIAError) as e:
        env.q.submit_many([{"prompt": "ok"}, {"prompt": "bad", "style": "nope"}])
    assert e.value.code == "unknown_style"
    assert env.q.list() == []


async def test_submit_many_queue_full_creates_nothing(env):
    env.backend.gate.clear()
    for n in (6, 6, 6, 1):
        env.q.submit(prompt="x", n=n)
    before = len(env.q.list())
    with pytest.raises(GIAError) as e:
        env.q.submit_many([{"prompt": "a"}, {"prompt": "b"}])
    assert e.value.code == "queue_full"
    assert len(env.q.list()) == before
    env.backend.gate.set()


async def test_on_finish_called_once_for_done_and_error(env):
    seen = []

    async def hook(job):
        seen.append((job.id, job.status))

    env.q.on_finish = hook
    env.backend.error_results = ["no_image"]
    bad, good = env.q.submit(prompt="a", n=2)
    await wait_until(lambda: good.status == "done")
    await env.q.aclose()
    assert sorted(seen) == sorted([(bad.id, "error"), (good.id, "done")])


async def test_on_finish_called_for_cancel_and_drain(env):
    seen = []

    async def hook(job):
        seen.append((job.id, job.status))

    env.q.on_finish = hook
    env.backend.gate.clear()
    jobs = env.q.submit(prompt="x", n=4)
    await wait_until(lambda: jobs[0].status == "running")
    env.q.cancel(jobs[1].id)
    env.backend.raise_codes = ["usage_limit"]
    env.backend.gate.set()
    await wait_until(lambda: all(j.status in ("error", "cancelled") for j in jobs))
    await env.q.aclose()
    assert sorted(seen) == sorted([(jobs[0].id, "error"), (jobs[1].id, "cancelled"),
                                   (jobs[2].id, "error"), (jobs[3].id, "error")])


async def test_raising_on_finish_does_not_stop_worker(env):
    calls = []

    async def hook(job):
        calls.append(job.id)
        raise RuntimeError("hook boom")

    env.q.on_finish = hook
    first, second = env.q.submit(prompt="a", n=2)
    await wait_until(lambda: second.status == "done")
    await env.q.aclose()
    assert first.status == "done" and calls == [first.id, second.id]


async def test_invalid_animation_warning_does_not_override_existing(env):
    env.backend.not_saved_count = 1
    env.backend.warnings = [{"code": "invalid_animation", "message": "m", "next_action": "n"}]
    (job,) = env.q.submit(prompt="a")
    await wait_until(lambda: job.status == "done")
    assert job.warning["code"] != "invalid_animation"


async def test_invalid_animation_warning_kept_when_no_other_warning(env):
    env.backend.warnings = [{"code": "invalid_animation", "message": "m", "next_action": "n"}]
    (job,) = env.q.submit(prompt="a")
    await wait_until(lambda: job.status == "done")
    assert job.warning["code"] == "invalid_animation"


async def test_other_warning_overrides_existing(env):
    env.backend.not_saved_count = 1
    env.backend.warnings = [{"code": "gem_missing", "message": "m", "next_action": "n"}]
    (job,) = env.q.submit(prompt="a")
    await wait_until(lambda: job.status == "done")
    assert job.warning["code"] == "gem_missing"


def test_model_entries_label_chatgpt():
    got = model_entries([{"model_name": "chatgpt-web", "display_name": "ChatGPT (web)", "available": True}])
    assert got[1] == {"id": "chatgpt-web", "name": "ChatGPT (web)", "description": "ChatGPT (web)"}
