import json
import os
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest

from gemini_image_api.compositions import (CompositionManager, CompositionStore, DirectRunner,
                                           background_edit_prompt, background_ref_prompt, master_prompt,
                                           piece_prompt)
from gemini_image_api.errors import GIAError
from gemini_image_api.jobs import Job
from gemini_image_api.animations import AnimationStore
from gemini_image_api.library import LibraryStore
from sprite_synth import composition_set

PIECES = ["Hermes", "Tarro"]


class FakeService:
    """Stands in for ImageService: generate/edit write real images from composition_set()."""

    def __init__(self, tmp_path):
        self.library = LibraryStore(tmp_path / "state")
        self.animations = AnimationStore(tmp_path / "state")
        self.dir = tmp_path / "imgs"
        self.dir.mkdir()
        self.master, self.plate, self.piece, self.bbox = composition_set()
        self.images, self.convs, self.heads, self.calls = {}, set(), set(), []
        self.fail = {}  # prompt substring -> error code raised by generate/edit
        self.n = 0
        self.state = SimpleNamespace(conversation=lambda cid: cid in self.convs)

    def catalog(self):
        return SimpleNamespace(styles={"ink": object()})

    def add(self, img, conv=None):
        self.n += 1
        key = f"g_{self.n:012x}#0"
        path = self.dir / f"{self.n}.png"
        img.save(path)
        conv = conv or f"c_{self.n}"
        self.convs.add(conv)
        self.images[key] = SimpleNamespace(path=path, record=SimpleNamespace(conversation_id=conv))
        self.heads.add(key)
        return key

    def conversation_head(self, key):
        return key in self.heads

    def change_spec(self, target_key, change_prompt, ref_prompt, *, index=None, **common):
        if self.conversation_head(target_key):
            return {"edit_of": f"gen:{target_key}", "prompt": change_prompt, **common}
        return {"refs": [f"gen:{target_key}"], "prompt": ref_prompt, **common}

    def _result(self, key):
        return SimpleNamespace(result=SimpleNamespace(status="ok", code=None, record_id=key, images=[key],
                                                      gemini_text=None, save_error=None), keys=[key])

    def result_keys(self, res):
        return [res.images[0]]

    def _maybe_fail(self, prompt):
        for sub, code in self.fail.items():
            if sub in prompt:
                raise GIAError(code, f"fake {code}")

    def _pick(self, prompt):
        if "Compose the scene" in prompt:
            return self.master
        return self.piece if "solid flat" in prompt else self.plate

    async def generate(self, *, prompt, refs=None, **kw):
        self.calls.append(("generate", prompt, list(refs or ()), kw))
        self._maybe_fail(prompt)
        return self._result(self.add(self._pick(prompt)))

    async def edit(self, conversation_id, *, prompt, refs=None, **kw):
        self.calls.append(("edit", prompt, conversation_id, kw))
        self._maybe_fail(prompt)
        old = next(k for k, e in self.images.items() if e.record.conversation_id == conversation_id)
        self.heads.discard(old)
        return self._result(self.add(self._pick(prompt), conv=conversation_id))


class FakeRunner:
    def __init__(self):
        self.specs, self.many, self.n, self.full = [], [], 0, False

    def live_job_ids(self):
        return set()

    def submit(self, spec):
        self.specs.append(spec)
        self.n += 1
        return f"j_{self.n}"

    def submit_many(self, specs):
        if self.full:
            raise GIAError("queue_full", "full")
        self.many.append(specs)
        ids = []
        for _ in specs:
            self.n += 1
            ids.append(f"j_{self.n}")
        return ids


class Mgr(CompositionManager):
    def _index(self):
        return self.service.images


@pytest.fixture
def svc(tmp_path):
    return FakeService(tmp_path)


@pytest.fixture
def store(tmp_path):
    return CompositionStore(tmp_path / "state")


def queue_mgr(svc, store):
    runner = FakeRunner()
    return Mgr(svc, store, runner), runner


def direct_mgr(svc, store):
    return Mgr(svc, store, DirectRunner(svc))


def job_for(store, spec, key, status="done", error=None):
    comp = store.get(spec["meta"]["composition"])
    tgt = spec["meta"]["target"]
    ent = comp["master"] if tgt == "master" else next(x for x in comp["layers"] if x["id"] == tgt)
    return Job(id=ent["job"], batch="b", prompt=spec["prompt"], style=None, model=None, ratio=None, refs=[],
               uploads=[], collection=None, status=status, images=[key] if key else [], error=error,
               meta=spec["meta"])


async def full_direct(svc, store, **kw):
    mgr = direct_mgr(svc, store)
    out = await mgr.run_direct(prompt="Hermes among shops", pieces=PIECES, name="Escena", **kw)
    return mgr, out


# -- store ------------------------------------------------------------------------
def test_store_validation(store):
    ok = dict(prompt="p", pieces=["a"])
    for bad in (dict(ok, pieces=[]), dict(ok, pieces=list("abcdefg")), dict(ok, pieces=["a", "A"]),
                dict(ok, pieces=["x" * 81]), dict(ok, name="n" * 81), dict(ok, prompt=" "),
                dict(ok, prompt="p" * 4001), dict(ok, refs=["a", "b", "c", "d"]), dict(ok, ratio="7:3"),
                dict(ok, refs=[{"bogus": "x"}])):
        with pytest.raises(GIAError) as e:
            store.create(**bad)
        assert e.value.code == "invalid_composition"
    c = store.create(prompt="  a scene  ", pieces=["a", "b"], refs=[{"gen": "gen:g_1#0"}])
    assert c["name"] == "a scene" and c["ratio"] == "4:3" and c["id"].startswith("cmp_")
    assert [ly["role"] for ly in c["layers"]] == ["background", "piece", "piece"]
    assert all(ly["status"] == "pending" for ly in c["layers"]) and c["layers"][0]["name"] == "Fondo"
    assert store.get(c["id"])["pieces"][0]["id"].startswith("pc_")


def test_store_limit_and_corrupt(store, tmp_path):
    for _ in range(100):
        store.create(prompt="p", pieces=["a"])
    with pytest.raises(GIAError) as e:
        store.create(prompt="p", pieces=["a"])
    assert e.value.code == "invalid_composition"
    store.file.write_text("{not json")
    assert store.all() == [] and list((tmp_path / "state").glob("compositions.json.corrupt-*"))


def test_prompts():
    mp = master_prompt("Scene", PIECES)
    assert mp.startswith("Scene\n\n") and "Hermes; Tarro" in mp
    assert "Hermes, Tarro" in background_edit_prompt(PIECES) and "Hermes, Tarro" in background_ref_prompt(PIECES)
    assert "including Tarro" in piece_prompt("Hermes", ["Tarro"], "green") and "chroma green #00FF00" in \
        piece_prompt("Hermes", ["Tarro"], "green")
    solo = piece_prompt("Hermes", [], "magenta")
    assert "including" not in solo and "magenta #FF00FF" in solo


# -- queue-style flow (fake runner, hook driven by hand) ----------------------------------
async def test_create_submits_master_and_collection_suffix(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena", style="ink",
                         refs=[{"gen": "gen:g_1#0"}, {"upload": "u_1"}])
    spec = runner.specs[0]
    assert spec["meta"] == {"composition": c["id"], "target": "master", "action": "create"}
    assert spec["display_prompt"] == "Escena · imagen completa" and spec["style"] == "ink"
    assert spec["refs"] == ["gen:g_1#0"] and spec["uploads"] == ["u_1"] and spec["ratio"] == "4:3"
    assert "Hermes; Tarro" in spec["prompt"]
    assert c["master"]["status"] == "queued" and c["master"]["job"] == "j_1" and c["owner_pid"] == os.getpid()
    assert all(ly["status"] == "pending" for ly in c["layers"])
    assert svc.library.get_collection(c["collection"]).name == "Composición · Escena"
    c2 = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    c3 = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    assert [svc.library.get_collection(x["collection"]).name for x in (c2, c3)] == [
        "Composición · Escena (2)", "Composición · Escena (3)"]
    with pytest.raises(GIAError) as e:
        await mgr.create(prompt="Scene", pieces=PIECES, style="nope")
    assert e.value.code == "unknown_style"


async def test_master_done_submits_background_edit_and_pieces(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    key = svc.add(svc.master)
    await mgr.on_job_finished(job_for(store, runner.specs[0], key))
    c = store.get(c["id"])
    assert c["master"]["status"] == "done" and c["master"]["image"] == key
    assert (c["master"]["width"], c["master"]["height"]) == (800, 600)
    specs = runner.many[0]
    assert len(specs) == 3
    bg = specs[0]
    assert bg["edit_of"] == f"gen:{key}" and bg["prompt"] == background_edit_prompt(PIECES)
    assert bg["display_prompt"] == "Escena · Fondo" and bg["style"] is None and bg["ratio"] == "4:3"
    assert bg["meta"] == {"composition": c["id"], "target": c["layers"][0]["id"], "action": "create"}
    for spec, name in zip(specs[1:], PIECES):
        assert spec["refs"] == [f"gen:{key}"] and f"Isolate only {name}" in spec["prompt"]
        assert spec["display_prompt"] == f"Escena · {name}"
    assert [ly["status"] for ly in c["layers"]] == ["queued"] * 3
    assert all(ly["job"] for ly in c["layers"])


async def test_background_uses_reference_when_master_not_head(svc, store):
    mgr, runner = queue_mgr(svc, store)
    await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    key = svc.add(svc.master)
    svc.heads.discard(key)
    await mgr.on_job_finished(job_for(store, runner.specs[0], key))
    bg = runner.many[0][0]
    assert "edit_of" not in bg and bg["refs"] == [f"gen:{key}"] and bg["prompt"] == background_ref_prompt(PIECES)


async def test_queue_full_at_followup(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    runner.full = True
    await mgr.on_job_finished(job_for(store, runner.specs[0], svc.add(svc.master)))
    c = store.get(c["id"])
    assert [ly["status"] for ly in c["layers"]] == ["error"] * 3
    assert {ly["error"]["code"] for ly in c["layers"]} == {"queue_full"}


async def test_master_failure_keeps_layers_pending_and_retry(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    await mgr.on_job_finished(job_for(store, runner.specs[0], None, "error", {"code": "content_blocked", "message": "no"}))
    c = store.get(c["id"])
    assert c["master"]["status"] == "error" and c["master"]["error"]["code"] == "content_blocked"
    assert all(ly["status"] == "pending" for ly in c["layers"])
    await mgr.retry(c["id"])
    assert len(runner.specs) == 2 and store.get(c["id"])["master"]["status"] == "queued"
    with pytest.raises(GIAError) as e:
        await mgr.retry(c["id"])
    assert e.value.code == "invalid_composition"


async def test_layer_done_sets_file_bbox_offset_rev(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    await mgr.on_job_finished(job_for(store, runner.specs[0], svc.add(svc.master)))
    c = store.get(c["id"])
    bg, hermes = c["layers"][0], c["layers"][1]
    await mgr.on_job_finished(job_for(store, runner.many[0][0], svc.add(svc.plate)))
    await mgr.on_job_finished(job_for(store, runner.many[0][1], svc.add(svc.piece)))
    c = store.get(c["id"])
    bg, hermes = c["layers"][0], c["layers"][1]
    assert bg["status"] == "done" and bg["bbox"] == [0, 0, 800, 600] and bg["offset"] == [0, 0] and bg["rev"]
    assert bg["file"] == f"{bg['id']}_{bg['rev']}.png" and bg["job"] is None and bg["conversation_id"]
    assert hermes["status"] == "done" and hermes["offset"] == [0, 0] and hermes["file"]
    assert all(abs(a - b) <= 3 for a, b in zip(hermes["bbox"], svc.bbox))
    assert (store.root / c["id"] / hermes["file"]).is_file()
    assert oct((store.root / c["id"]).stat().st_mode & 0o777) == "0o700"
    assert c["rev"]
    # a stale job (other id than the layer's) is ignored
    stale = job_for(store, runner.many[0][1], svc.add(svc.piece))
    stale.id = "j_other"
    before = store.get(c["id"])
    await mgr.on_job_finished(stale)
    assert store.get(c["id"])["layers"][1] == before["layers"][1]


async def _ready(svc, store):
    """Direct flow that completes everything: returns (mgr, composition)."""
    mgr, out = await full_direct(svc, store)
    assert out["status"] == "ok"
    return mgr, out["composition"]


async def test_failed_regenerate_keeps_done_layer_with_notice(svc, store):
    mgr, comp = await _ready(svc, store)
    hermes = comp["layers"][1]
    svc.fail["Isolate only Hermes"] = "content_blocked"
    await mgr.regenerate(comp["id"], hermes["id"], key_color="magenta")
    ly = store.get(comp["id"])["layers"][1]
    assert ly["status"] == "done" and ly["file"] == hermes["file"] and ly["image"] == hermes["image"]
    assert ly["error"]["code"] == "content_blocked" and ly["key_color"] == "green"
    assert ly["pending_key_color"] is None and ly["job"] is None
    # a layer that never had a file goes to error instead
    def clear(c):
        c["layers"][2].update(file=None, status="pending")
    store.update(comp["id"], clear)
    svc.fail["Isolate only Tarro"] = "usage_limit"
    await mgr.regenerate(comp["id"], comp["layers"][2]["id"])
    assert store.get(comp["id"])["layers"][2]["status"] == "error"


async def test_regenerate_key_color_applies_on_success(svc, store):
    mgr, comp = await _ready(svc, store)
    hermes = comp["layers"][1]
    await mgr.regenerate(comp["id"], hermes["id"], key_color="magenta")
    ly = store.get(comp["id"])["layers"][1]
    assert ly["key_color"] == "magenta" and ly["status"] == "done" and ly["error"] is None
    assert ly["rev"] > hermes["rev"] and ly["file"] != hermes["file"]
    assert not (store.root / comp["id"] / hermes["file"]).exists()
    spec = [c for c in svc.calls if "Isolate only Hermes" in c[1]][-1]
    assert "magenta #FF00FF" in spec[1] and spec[3]["display_prompt"] == "Escena · Hermes"
    with pytest.raises(GIAError):
        await mgr.regenerate(comp["id"], comp["layers"][0]["id"], key_color="green")


async def test_correct_edit_vs_reference_and_busy(svc, store):
    mgr, comp = await _ready(svc, store)
    hermes = comp["layers"][1]
    await mgr.correct(comp["id"], hermes["id"], "make the wings bigger")
    call = svc.calls[-1]
    assert call[0] == "edit" and call[1].startswith("make the wings bigger Keep the solid flat chroma green")
    # the layer image is no longer the head of its conversation -> reference generation
    await mgr.correct(comp["id"], hermes["id"], "tint it blue")
    ly = store.get(comp["id"])["layers"][1]
    assert svc.calls[-1][0] == "edit"  # new head after the edit
    svc.heads.discard(ly["image"])
    await mgr.correct(comp["id"], hermes["id"], "tint it red")
    call = svc.calls[-1]
    assert call[0] == "generate" and call[2] == [f"gen:{ly['image']}"]
    assert call[1].startswith("Same image as the reference, with this change: tint it red.")
    # background suffix
    await mgr.correct(comp["id"], comp["layers"][0]["id"], "more clouds")
    assert svc.calls[-1][1].endswith("Keep the same framing and the same positions of everything else.")
    # busy layer
    def busy(c):
        c["layers"][1].update(status="running", job="j_1")
    store.update(comp["id"], busy)
    for fn in (lambda: mgr.correct(comp["id"], hermes["id"], "x"), lambda: mgr.regenerate(comp["id"], hermes["id"])):
        with pytest.raises(GIAError) as e:
            await fn()
        assert e.value.code == "invalid_composition"
    with pytest.raises(GIAError):
        await mgr.correct(comp["id"], comp["layers"][2]["id"], "")


async def test_master_deleted_blocks_regenerate_and_correct(svc, store):
    mgr, comp = await _ready(svc, store)
    del svc.images[comp["master"]["image"]]
    for fn in (lambda: mgr.regenerate(comp["id"], comp["layers"][1]["id"]),
               lambda: mgr.correct(comp["id"], comp["layers"][1]["id"], "x")):
        with pytest.raises(GIAError) as e:
            await fn()
        assert e.value.code == "invalid_composition" and "se borró" in str(e.value)
    # tolerance needs the layer's source image
    del svc.images[comp["layers"][1]["image"]]
    with pytest.raises(GIAError) as e:
        mgr.patch_layer(comp["id"], comp["layers"][1]["id"], tolerance=40)
    assert e.value.code == "invalid_composition"


async def test_unknown_composition(svc, store):
    mgr, _ = queue_mgr(svc, store)
    with pytest.raises(GIAError) as e:
        await mgr.regenerate("cmp_nope", "ly_x")
    assert e.value.code == "unknown_composition"


# -- direct runner ----------------------------------------------------------------------------
async def test_direct_full_flow(svc, store):
    mgr, out = await full_direct(svc, store, style="ink")
    comp = out["composition"]
    assert out["status"] == "ok" and comp["origin"] == "mcp" and comp["master"]["status"] == "done"
    assert [ly["status"] for ly in comp["layers"]] == ["done"] * 3
    kinds = [c[0] for c in svc.calls]
    assert kinds == ["generate", "edit", "generate", "generate"]  # master, bg edit (head), 2 pieces
    assert svc.calls[0][3]["display_prompt"] == "Escena · imagen completa"
    assert svc.calls[2][2] == [f"gen:{comp['master']['image']}"]
    assert svc.calls[1][3]["collection"] == comp["collection"]


async def test_direct_stop_code_marks_rest_error_and_partial(svc, store):
    svc.fail["Isolate only Hermes"] = "usage_limit"
    mgr, out = await full_direct(svc, store)
    comp = out["composition"]
    assert out["status"] == "partial"
    assert [ly["status"] for ly in comp["layers"]] == ["done", "error", "error"]
    assert comp["layers"][1]["error"]["code"] == "usage_limit" and comp["layers"][2]["error"]["code"] == "usage_limit"
    assert len(svc.calls) == 3  # master, background, Hermes; Tarro never attempted


async def test_direct_other_error_continues_partial(svc, store):
    svc.fail["Isolate only Hermes"] = "content_blocked"
    mgr, out = await full_direct(svc, store)
    comp = out["composition"]
    assert out["status"] == "partial"
    assert [ly["status"] for ly in comp["layers"]] == ["done", "error", "done"]
    assert comp["layers"][1]["error"]["code"] == "content_blocked"


async def test_direct_master_failure(svc, store):
    svc.fail["Compose the scene"] = "usage_limit"
    mgr, out = await full_direct(svc, store)
    comp = out["composition"]
    assert out["status"] == "partial" and comp["master"]["status"] == "error"
    assert all(ly["status"] == "pending" for ly in comp["layers"])


# -- patches / views --------------------------------------------------------------------------------
async def test_patch_order_name_and_layer(svc, store):
    mgr, comp = await _ready(svc, store)
    cid = comp["id"]
    ids = [ly["id"] for ly in comp["layers"]]
    rev0 = comp["rev"]
    out = mgr.patch(cid, name="  Nuevo  ")
    assert out["name"] == "Nuevo" and out["rev"] == rev0
    out = mgr.patch(cid, order=[ids[0], ids[2], ids[1]])
    assert [ly["id"] for ly in out["layers"]] == [ids[0], ids[2], ids[1]] and out["rev"] > rev0
    for bad in ([ids[1], ids[0], ids[2]], ids[:2], [ids[0], ids[1], ids[1]]):
        with pytest.raises(GIAError) as e:
            mgr.patch(cid, order=bad)
        assert e.value.code == "invalid_composition"
    h = ids[1]
    out = mgr.patch_layer(cid, h, visible=False, offset=[3, -4])
    ly = next(x for x in out["layers"] if x["id"] == h)
    assert ly["visible"] is False and ly["offset"] == [3, -4] and ly["rev"] > comp["layers"][1]["rev"]
    for kw in (dict(offset=[201, 0]), dict(offset=[1]), dict(tolerance=4), dict(visible="no")):
        with pytest.raises(GIAError):
            mgr.patch_layer(cid, h, **kw)
    with pytest.raises(GIAError):
        mgr.patch_layer(cid, ids[0], visible=False)
    old = ly["file"]
    out = mgr.patch_layer(cid, h, tolerance=40)
    ly = next(x for x in out["layers"] if x["id"] == h)
    assert ly["tolerance"] == 40 and ly["file"] != old and ly["offset"] == [3, -4]
    assert not (store.root / cid / old).exists() and (store.root / cid / ly["file"]).is_file()


async def test_summaries_detail_preview(svc, store):
    mgr, comp = await _ready(svc, store)
    s = mgr.summaries()
    assert s[0]["id"] == comp["id"] and s[0]["progress"] == {"done": 3, "total": 3, "errors": 0}
    assert s[0]["cover"]["kind"] == "preview"
    d = mgr.detail(comp["id"])
    assert (d["width"], d["height"]) == (800, 600) and d["master_exists"]
    assert all(ly["source_exists"] for ly in d["layers"])
    p = mgr.preview_path(comp["id"])
    assert p.name == f"preview_{comp['rev']}.png"
    mgr.patch_layer(comp["id"], comp["layers"][1]["id"], visible=False)
    p2 = mgr.preview_path(comp["id"])
    assert p2 != p and not p.exists() and p2.is_file()


async def test_summary_cover_master_then_none(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    assert mgr.summaries()[0]["cover"] == {"kind": None, "image": None}
    key = svc.add(svc.master)
    await mgr.on_job_finished(job_for(store, runner.specs[0], key))
    assert mgr.summaries()[0]["cover"] == {"kind": "master", "image": key}


async def test_delete_removes_directory(svc, store):
    mgr, comp = await _ready(svc, store)
    assert (store.root / comp["id"]).is_dir()
    assert mgr.delete(comp["id"]) is True
    assert not (store.root / comp["id"]).exists() and store.get(comp["id"]) is None
    with pytest.raises(GIAError) as e:
        mgr.delete(comp["id"])
    assert e.value.code == "unknown_composition"


# -- export -------------------------------------------------------------------------------------------
async def test_export_zip(svc, store):
    mgr = direct_mgr(svc, store)
    out = await mgr.run_direct(prompt="Scene", pieces=["Cafe!", "Cafe?"], name="Mi escena")
    comp = out["composition"]
    path, filename = mgr.export_zip(comp["id"])
    try:
        assert path.parent == store.dir / "tmp_downloads" and filename.endswith(".zip")
        with zipfile.ZipFile(path) as zf:
            assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
            assert sorted(zf.namelist()) == ["background.png", "cafe-2.png", "cafe.png", "composite.png",
                                             "index.html", "musa-scene.js", "scene.json"]
            scene = json.loads(zf.read("scene.json"))
    finally:
        path.unlink()
    assert scene["version"] == 1 and scene["name"] == "Mi escena" and (scene["width"], scene["height"]) == (800, 600)
    bg, p1, p2 = scene["layers"]
    assert (bg["role"], bg["file"], bg["x"], bg["y"], bg["w"], bg["h"]) == ("background", "background.png", 0, 0, 800, 600)
    assert p1["file"] == "cafe.png" and p2["file"] == "cafe-2.png" and p1["visible"] is True
    assert (p1["x"], p1["y"]) == tuple(comp["layers"][1]["bbox"][:2]) and p1["w"] > 0 and p1["h"] > 0
    assert p1["id"] == comp["layers"][1]["id"] and p1["name"] == "Cafe!"


async def test_export_only_done_layers_and_empty(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    with pytest.raises(GIAError) as e:
        mgr.export_zip(c["id"])
    assert e.value.code == "invalid_composition"


# -- recovery --------------------------------------------------------------------------------------------
def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


async def test_recover_dead_owner(svc, store):
    mgr, comp = await _ready(svc, store)
    dead = _dead_pid()

    def fn(c):
        c["owner_pid"] = dead
        c["origin"] = "mcp"
        c["layers"][1].update(status="running", job="j_9")           # has file -> back to done + notice
        c["layers"][2].update(status="queued", job=None, file=None)  # no file -> error
        c["master"]["status"] = "running"
    store.update(comp["id"], fn)
    assert mgr.recover() == 3
    c = store.get(comp["id"])
    assert c["layers"][1]["status"] == "done" and c["layers"][1]["error"]["code"] == "interrupted"
    assert c["layers"][2]["status"] == "error" and c["layers"][2]["error"]["code"] == "interrupted"
    assert c["master"]["status"] == "error" and c["master"]["error"]["code"] == "interrupted"
    assert c["layers"][0]["status"] == "done" and c["layers"][0]["error"] is None


async def test_recover_own_pid_missing_from_queue(svc, store):
    mgr, comp = await _ready(svc, store)

    def fn(c):
        c["origin"], c["owner_pid"] = "musa", os.getpid()
        c["layers"][1].update(status="queued", job="j_1")
        c["layers"][2].update(status="queued", job="j_2")
    store.update(comp["id"], fn)
    assert mgr.recover() == 0                      # no queue info: own pid is alive
    assert mgr.recover(live_job_ids={"j_2"}) == 1  # j_1 is not in the queue
    c = store.get(comp["id"])
    assert c["layers"][1]["error"]["code"] == "interrupted" and c["layers"][2]["status"] == "queued"


async def test_manager_recovers_on_creation(svc, store):
    comp = store.create(prompt="p", pieces=["a"], origin="mcp", owner_pid=_dead_pid())
    store.update(comp["id"], lambda c: c["layers"][0].update(status="queued"))
    Mgr(svc, store, FakeRunner())
    assert store.get(comp["id"])["layers"][0]["error"]["code"] == "interrupted"


async def test_stale_job_for_frames_and_state_actions_changes_nothing(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    await mgr.on_job_finished(job_for(store, runner.specs[0], svc.add(svc.master)))
    for spec, key in ((runner.many[0][0], svc.plate), (runner.many[0][1], svc.piece)):
        await mgr.on_job_finished(job_for(store, spec, svc.add(key)))
    before = store.get(c["id"])
    hermes = before["layers"][1]
    store.update(c["id"], lambda x: x["layers"][1].update(status="queued", job="j_current"))
    snap = store.get(c["id"])
    for action in ("frames", "state", "regenerate"):
        spec = {"prompt": "p", "meta": {"composition": c["id"], "target": hermes["id"], "action": action}}
        stale = job_for(store, spec, svc.add(svc.piece))
        stale.id = "j_stale"
        await mgr.on_job_finished(stale)
        assert store.get(c["id"])["layers"] == snap["layers"], action


async def test_mark_is_atomic_and_busy_layer_rejects_second_job(svc, store):
    mgr, runner = queue_mgr(svc, store)
    c = await mgr.create(prompt="Scene", pieces=PIECES, name="Escena")
    await mgr.on_job_finished(job_for(store, runner.specs[0], svc.add(svc.master)))
    for spec, key in ((runner.many[0][0], svc.plate), (runner.many[0][1], svc.piece), (runner.many[0][2], svc.piece)):
        await mgr.on_job_finished(job_for(store, spec, svc.add(key)))
    lid = store.get(c["id"])["layers"][1]["id"]
    ly = store.get(c["id"])["layers"][1]
    await mgr.regenerate(c["id"], lid)
    n = len(runner.specs)
    with pytest.raises(GIAError) as e:
        await mgr._run_layer_job(c["id"], ly, {"prompt": "x", "meta": {}})   # a stale snapshot still cannot double-book
    assert e.value.code == "invalid_composition" and len(runner.specs) == n


async def test_index_is_cached_until_state_or_library_change(svc, store):
    import types
    calls = []
    state_file = svc.library.file   # any file whose mtime we control
    svc.state = types.SimpleNamespace(file=state_file, conversation=lambda cid: True)
    svc.settings = types.SimpleNamespace(output_root=svc.dir)
    from gemini_image_api import compositions as cm
    real = cm.image_index
    cm.image_index = lambda *a: calls.append(1) or {}
    try:
        mgr = CompositionManager(svc, store, FakeRunner())
        mgr._index(); mgr._index(); mgr._index()
        assert len(calls) == 1
        svc.library.create_collection("nuevo")      # library mtime changes
        mgr._index()
        assert len(calls) == 2
        await mgr._warm_index()
        assert len(calls) == 2
    finally:
        cm.image_index = real
