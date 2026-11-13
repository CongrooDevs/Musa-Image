import pytest, yaml
from datetime import datetime, timezone
from pathlib import Path
from gemini_image_api.client import Backend, GenerateResult
from gemini_image_api.errors import GIAError, UPSTREAM_ERROR

from gemini_image_api.library import LibraryStore
from gemini_image_api.service import ImageService, to_payload
from gemini_image_api.state import StateStore, GenerationRecord
from gemini_image_api.storage import SavedImage
from gemini_image_api.style_store import UserStyleStore

class FakeBackend(Backend):
    def __init__(self, quota=None, save_error=None):
        self.calls, self._quota, self._save_error = [], quota, save_error
        self.settings = None
        self.last_record_id = None
        self.gems = {}
        self.gem_calls = []
        self.warning = None
        self.gem_net_error = False
    async def quota(self): return self._quota
    async def generate(self, prompt, refs, *, model, **kw):
        self.calls.append(("generate", prompt, [str(r.path) for r in refs], model, kw))
        return self._result()
    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        self.calls.append(("edit", conversation_id, prompt, model, kw))
        return self._result()
    def _result(self):
        st = StateStore(self.settings.state_dir)
        saved = None if self._save_error else str(self.img.path)
        rec = GenerationRecord(id=StateStore.new_id("g"), ts=datetime.now(timezone.utc).isoformat(),
                               type="generate", conversation_id="c_1", chat_metadata=["c"], prompt="p",
                               style=None, style_text=None, aspect_hint=None, model="gemini-flash",
                               project=None, subdir=None, refs=[], images=[{"index": 0, "path": saved}],
                               status="ok", code=None, gemini_text="done", elapsed_s=1.0, parent_id=None,
                               user_prompt="x")
        st.append(rec)
        self.last_record_id = rec.id
        if self._save_error:
            return GenerateResult("generated_not_saved", "generated_not_saved", "c_1", [], "", 1.0, ["c"],
                                  save_error=self._save_error, record_id=rec.id, warning=self.warning)
        return GenerateResult("ok", None, "c_1", [self.img], "done", 1.0, ["c"], record_id=rec.id,
                              warning=self.warning)
    async def create_gem(self, name, instructions, description=""):
        self.gem_calls.append(("create", name, instructions, description))
        gid = f"gem_{len(self.gems) + 1:03d}"
        self.gems[gid] = {"name": name, "instructions": instructions, "description": description}
        return gid
    async def update_gem(self, gem_id, name, instructions, description=""):
        self.gem_calls.append(("update", gem_id, name, instructions, description))
        if gem_id not in self.gems:
            raise GIAError(UPSTREAM_ERROR, f"gem {gem_id} does not exist")
        self.gems[gem_id].update(name=name, instructions=instructions, description=description)
    async def delete_gem(self, gem_id):
        self.gem_calls.append(("delete", gem_id))
        if self.gem_net_error:
            raise GIAError(UPSTREAM_ERROR, "network down")
        if gem_id not in self.gems:
            raise GIAError(UPSTREAM_ERROR, f"gem {gem_id} does not exist")
        del self.gems[gem_id]
    async def gem_exists(self, gem_id):
        if self.gem_net_error:
            raise GIAError(UPSTREAM_ERROR, "network down")
        return gem_id in self.gems

@pytest.fixture
def svc(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings.styles_file = tmp_path / "styles.yaml"
    settings.styles_file.write_text(yaml.safe_dump({"styles": {"anime": {"description": "d", "append": "anime style"}},
                                                    "templates": {"t": {"prompt": "photo of {x}", "vars": ["x"]}}}))
    img_path = settings.output_root / "a.jpg"; img_path.parent.mkdir(parents=True, exist_ok=True)
    img_path.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    def make(**kw):
        b = FakeBackend(**kw)
        b.img = SavedImage(img_path, 64, 64, 35, "jpeg", True, b"thumb")
        b.settings = settings
        return ImageService(b, settings, StateStore(settings.state_dir),
                            library=LibraryStore(settings.state_dir),
                            styles=UserStyleStore(settings.state_dir)), b
    return make

async def test_generate_builds_prompt_and_reports_quota(svc):
    s, b = svc(quota={"tier": "PRO", "window_5h_pct": 4, "weekly_pct": 2, "resets_at_5h": "r5", "resets_at_weekly": "rw"})
    sr = await s.generate(prompt="a cat", style="anime", aspect_hint="16:9")
    assert "anime style" in b.calls[0][1] and "16:9" in b.calls[0][1]
    p = to_payload(sr)
    assert p["status"] == "ok" and p["quota"]["window_5h_pct"] == 4 and p["images"][0]["path"].endswith("a.jpg")

async def test_quota_guard_blocks_before_calling_gemini(svc, settings):
    settings.quota_stop_pct = 90
    s, b = svc(quota={"tier": "PRO", "window_5h_pct": 91, "weekly_pct": 10, "resets_at_5h": "r5", "resets_at_weekly": "rw"})
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x")
    assert e.value.code == "quota_guard" and e.value.details["resets_at"] == "r5" and b.calls == []

async def test_quota_guard_weekly_and_disabled_and_unknown(svc, settings):
    s, b = svc(quota={"tier": "PRO", "window_5h_pct": 1, "weekly_pct": 95, "resets_at_5h": "r5", "resets_at_weekly": "rw"})
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x")
    assert e.value.details["resets_at"] == "rw"
    settings.quota_stop_pct = 0
    await s.generate(prompt="x")                 # disabled -> goes through
    settings.quota_stop_pct = 90
    s2, b2 = svc(quota=None)
    await s2.generate(prompt="x")                # unknown quota -> no guard
    assert len(b.calls) == 1 and len(b2.calls) == 1

async def test_copy_to_validated_before_call_and_applied_after(svc, tmp_path):
    s, b = svc()
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x", copy_to="/etc/")
    assert e.value.code == "path_not_allowed" and b.calls == []
    sr = await s.generate(prompt="x", copy_to="assets/")
    p = to_payload(sr)
    assert p["images"][0]["copied_to"] == str((tmp_path / "assets" / "a.jpg").resolve())

async def test_gen_ref_resolved_from_history(svc, settings):
    s, b = svc()
    st = StateStore(settings.state_dir)
    from gemini_image_api.state import GenerationRecord
    st.append(GenerationRecord(id="g_0123456789ab", ts="2026-11-09T00:00:00+00:00", type="generate",
                               conversation_id="c_9", chat_metadata=["c"], prompt="p", style=None,
                               style_text=None, aspect_hint=None, model=None, project=None, subdir=None,
                               refs=[], images=[{"index": 0, "path": str(b.img.path)}], status="ok",
                               code=None, gemini_text="", elapsed_s=1.0, parent_id=None))
    await s.generate(prompt="x", refs=["gen:g_0123456789ab"])
    assert b.calls[0][2] == [str(b.img.path.resolve())]

async def test_edit_accepts_template_and_save_error_reason(svc):
    s, b = svc(save_error="OSError: [Errno 30] Read-only file system")
    sr = await s.edit("c_1", prompt="", template="t", template_vars={"x": "a dog"})
    assert b.calls[0][2].endswith("photo of a dog")
    p = to_payload(sr)
    assert p["status"] == "generated_not_saved" and "Read-only" in p["details"]["reason"]

def test_prompts_resolve_by_id_or_name_and_unknown(svc):
    s, _ = svc()
    p = s.save_prompt("Gato", "a cat")
    assert s.update_prompt("gato", text="a big cat")["text"] == "a big cat"
    assert s.update_prompt(p["id"], name="Felino")["name"] == "Felino"
    assert s.use_prompt("FELINO")["uses"] == 1
    assert [x["name"] for x in s.list_prompts()] == ["Felino"]
    assert s.delete_prompt("felino") == {"deleted": p["id"]}
    assert s.list_prompts() == []
    for call in (lambda: s.use_prompt("Felino"), lambda: s.delete_prompt(p["id"]),
                 lambda: s.update_prompt("nope", text="x")):
        with pytest.raises(GIAError) as e:
            call()
        assert e.value.code == "unknown_prompt"

def test_prompt_id_wins_over_name_and_text_is_verbatim(svc):
    s, _ = svc()
    a = s.save_prompt("first", "text a")
    with pytest.raises(GIAError) as ex:  # id-shaped names are rejected, so an id is never ambiguous
        s.save_prompt(a["id"], "text b")
    assert ex.value.code == "invalid_prompt"
    assert s.use_prompt(a["id"])["text"] == "text a"
    txt = "l1\nl2 {x} %s"
    p = s.save_prompt("raw", txt)
    assert s.use_prompt("raw")["text"] == txt and s.use_prompt(p["id"])["text"] == txt
    with pytest.raises(GIAError) as e:
        s.use_prompt("zzz")
    assert e.value.details["prompt"] == "zzz"

def test_list_generations_payload(svc, settings):
    s, _ = svc()
    assert s.list_generations() == []

def test_list_generations_returns_display_prompt(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    from gemini_image_api.state import GenerationRecord
    st.append(GenerationRecord(id="g_0123456789ab", ts="2026-11-09T00:00:00+00:00", type="generate",
                               conversation_id="c_9", chat_metadata=["c"],
                               prompt="Generate an image (do not search the web): x", user_prompt="x",
                               style=None, style_text=None, aspect_hint=None, model=None, project=None,
                               subdir=None, refs=[], images=[], status="ok", code=None, gemini_text="",
                               elapsed_s=1.0, parent_id=None))
    assert s.list_generations()[0]["prompt"] == "x"

def _style_with_ref(settings, tmp_path):
    img = tmp_path / "ref.jpg"; img.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    settings.styles_file = tmp_path / "s.yaml"
    settings.styles_file.write_text(yaml.safe_dump(
        {"styles": {"conref": {"name": "ConRef", "append": "x", "ref": "ref.jpg"}}}))
    return "conref", str(img)


async def test_style_with_ref_is_sent_first_as_trusted_ref(svc, settings, tmp_path):
    s, b = svc()
    sid, ref = _style_with_ref(settings, tmp_path)
    await s.generate(prompt="x", style=sid, refs=[])
    assert b.calls[0][2][0] == ref

async def test_style_ref_counts_toward_max_refs(svc, settings, tmp_path):
    s, b = svc(); settings.max_refs = 1
    sid, _ = _style_with_ref(settings, tmp_path)
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x", style=sid, refs=[str(b.img.path)])
    assert e.value.code == "invalid_ref" and b.calls == []

async def test_style_with_missing_ref_file_is_invalid_style(svc, settings, tmp_path):
    s, b = svc()
    settings.styles_file.write_text(yaml.safe_dump(
        {"styles": {"roto": {"name": "Roto", "append": "x", "ref": "no-existe.jpg"}}}))
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x", style="roto")
    assert e.value.code == "invalid_style" and b.calls == []

UP_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

def _upload(settings, name="u_0123456789ab.png"):
    up = settings.state_dir / "uploads"; up.mkdir(parents=True, exist_ok=True)
    f = up / name; f.write_bytes(UP_PNG)
    return f

async def test_uploads_sent_after_style_ref_before_user_refs(svc, settings, tmp_path):
    s, b = svc()
    sid, ref = _style_with_ref(settings, tmp_path)
    u = _upload(settings)
    await s.generate(prompt="x", style=sid, uploads=[u], refs=[str(b.img.path)])
    paths = b.calls[0][2]
    assert paths[0] == ref
    assert paths[1] == str(u.resolve())
    assert paths[2] == str(b.img.path.resolve())

async def test_uploads_count_toward_max_refs(svc, settings):
    s, b = svc(); settings.max_refs = 1
    u = _upload(settings)
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x", uploads=[u], refs=[str(b.img.path)])
    assert e.value.code == "invalid_ref" and b.calls == []

async def test_upload_outside_uploads_dir_is_invalid_ref(svc, settings, tmp_path):
    s, b = svc()
    f = tmp_path / "foto.png"; f.write_bytes(UP_PNG)
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x", uploads=[f])
    assert e.value.code == "invalid_ref" and b.calls == []

async def test_edit_rejects_upload_outside_uploads_dir(svc, settings, tmp_path):
    s, b = svc()
    f = tmp_path / "foto.png"; f.write_bytes(UP_PNG)
    with pytest.raises(GIAError) as e:
        await s.edit("c_1", prompt="x", uploads=[f])
    assert e.value.code == "invalid_ref" and b.calls == []

async def test_collection_is_created_and_assigned(svc):
    s, b = svc()
    sr = await s.generate(prompt="x", collection="Campaña")
    assert sr.collection["name"] == "Campaña"
    assert s.library.collections_of(f"{b.last_record_id}#0") == [sr.collection["id"]]
    assert to_payload(sr)["collection"] == sr.collection

async def test_collection_not_assigned_without_saved_images(svc):
    s, b = svc(save_error="boom")
    sr = await s.generate(prompt="x", collection="Campaña")
    assert sr.collection is None and "collection" not in to_payload(sr)
    assert s.library.collections() == []

def test_list_generations_hides_deleted_and_filters_by_collection(svc, settings):
    s, b = svc()
    st = StateStore(settings.state_dir)
    img = settings.output_root / "g.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    rec = GenerationRecord(id="g_0123456789ab", ts="2026-11-09T00:00:00+00:00", type="generate",
                           conversation_id="c_9", chat_metadata=["c"], prompt="p", style=None,
                           style_text=None, aspect_hint=None, model=None, project=None, subdir=None,
                           refs=[], images=[{"index": 0, "path": str(img)}, {"index": 1, "path": str(img)}],
                           status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None)
    st.append(rec)
    s.library.mark_deleted("g_0123456789ab#1")
    assert [e["index"] for e in s.list_generations()[0]["images"]] == [0]
    s.library.mark_deleted("g_0123456789ab#0")
    assert s.list_generations() == []
    st.append(GenerationRecord(id="g_0123456789ac", ts="2026-11-09T00:01:00+00:00", type="generate",
                               conversation_id="c_9", chat_metadata=["c"], prompt="q", style=None,
                               style_text=None, aspect_hint=None, model=None, project=None, subdir=None,
                               refs=[], images=[{"index": 0, "path": str(img)}], status="ok", code=None,
                               gemini_text="", elapsed_s=1.0, parent_id=None))
    coll = s.library.create_collection("A")
    s.library.add("g_0123456789ac#0", coll.id)
    assert [r["id"] for r in s.list_generations(collection=coll.id)] == ["g_0123456789ac"]
    assert [r["id"] for r in s.list_generations(collection="A")] == ["g_0123456789ac"]

def test_add_to_collection_and_list_collections(svc, settings):
    s, b = svc()
    st = StateStore(settings.state_dir)
    img = settings.output_root / "k.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    st.append(GenerationRecord(id="g_0123456789ab", ts="2026-11-09T00:00:00+00:00", type="generate",
                               conversation_id="c_9", chat_metadata=["c"], prompt="p", style=None,
                               style_text=None, aspect_hint=None, model=None, project=None, subdir=None,
                               refs=[], images=[{"index": 0, "path": str(img)}], status="ok", code=None,
                               gemini_text="", elapsed_s=1.0, parent_id=None))
    out = s.add_to_collection("gen:g_0123456789ab#0", "Nueva")
    assert out["key"] == "g_0123456789ab#0" and out["collection"]["name"] == "Nueva"
    assert out["collections"] == [out["collection"]["id"]]
    colls = s.list_collections()
    assert len(colls) == 1 and colls[0]["name"] == "Nueva" and colls[0]["count"] == 1
    for bad in ("nope", "gen:g_ffffffffffff"):
        with pytest.raises(GIAError) as e:
            s.add_to_collection(bad, "Nueva")
        assert e.value.code == "invalid_ref"

def test_list_generations_keeps_error_and_not_saved_records(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    st.append(GenerationRecord(id="g_0000000000e1", ts="2026-11-09T00:00:00+00:00", type="generate",
                               conversation_id="c_e", chat_metadata=["c"], prompt="p", style=None,
                               style_text=None, aspect_hint=None, model=None, project=None, subdir=None,
                               refs=[], images=[], status="error", code="no_image", gemini_text="",
                               elapsed_s=0.5, parent_id=None))
    st.append(GenerationRecord(id="g_0000000000e2", ts="2026-11-09T00:01:00+00:00", type="generate",
                               conversation_id="c_n", chat_metadata=["c"], prompt="q", style=None,
                               style_text=None, aspect_hint=None, model=None, project=None, subdir=None,
                               refs=[], images=[{"index": 0, "path": None}], status="generated_not_saved",
                               code="generated_not_saved", gemini_text="", elapsed_s=1.0, parent_id=None))
    gens = {g["id"]: g for g in s.list_generations()}
    assert gens["g_0000000000e1"]["images"] == []
    assert gens["g_0000000000e2"]["images"] == [{"index": 0, "path": None}]

async def test_empty_collection_rejected_before_backend(svc):
    s, b = svc()
    with pytest.raises(GIAError) as e:
        await s.generate(prompt="x", collection="   ")
    assert e.value.code == "invalid_collection" and b.calls == []

def _seed_gen(settings, st, gid, ts, prompt, n=1, model=None, thumbs=False, name=None):
    paths = []
    for i in range(n):
        p = settings.output_root / (name or f"{gid}_{i}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 16)
        paths.append(p)
    st.append(GenerationRecord(id=gid, ts=ts, type="generate", conversation_id="c_9", chat_metadata=["c"],
                               prompt=prompt, style=None, style_text=None, aspect_hint=None, model=model,
                               project=None, subdir=None, refs=[],
                               images=[{"index": i, "path": str(p)} for i, p in enumerate(paths)],
                               status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None))
    return paths

def test_list_generations_model_filter_and_paging(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    for i in range(5):
        _seed_gen(settings, st, f"g_00000000000{i}", f"2026-11-09T00:00:0{i}+00:00", "p", model="m-a" if i % 2 == 0 else "m-b")
    assert [g["id"] for g in s.list_generations(model="m-a")] == ["g_000000000004", "g_000000000002", "g_000000000000"]
    assert [g["id"] for g in s.list_generations(model="m-a", limit=1, offset=1)] == ["g_000000000002"]
    assert s.list_generations(model="nope") == []
    assert len(s.list_generations()) == 5

def test_batch_partial_success_and_both_key_forms(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p", n=2)
    out = s.batch_images("favorite", ["gen:g_0123456789ab#0", "g_0123456789ab#1", "gen:g_ffffffffffff#0"])
    assert [r["ok"] for r in out["results"]] == [True, True, False]
    assert out["results"][2]["error"]["code"] == "invalid_ref" and out["results"][2]["key"] == "gen:g_ffffffffffff#0"
    assert s.library.is_favorite("g_0123456789ab#0") and s.library.is_favorite("g_0123456789ab#1")
    s.batch_images("unfavorite", ["g_0123456789ab#0"])
    assert not s.library.is_favorite("g_0123456789ab#0")

def test_batch_collections_require_collection_and_work(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    for action in ("add_to_collection", "remove_from_collection"):
        for coll in (None, "  "):
            with pytest.raises(GIAError) as e:
                s.batch_images(action, ["g_0123456789ab#0"], coll)
            assert e.value.code == "invalid_query"
    assert s.batch_images("add_to_collection", ["g_0123456789ab#0"], "X")["results"][0]["ok"]
    assert s.library.collections_of("g_0123456789ab#0")
    assert s.batch_images("remove_from_collection", ["g_0123456789ab#0"], "X")["results"][0]["ok"]
    assert s.library.collections_of("g_0123456789ab#0") == []

def test_batch_rejects_bad_input(svc):
    s, _ = svc()
    cases = [("explode", ["g_0123456789ab#0"]), ("favorite", []), ("favorite", "g_0123456789ab#0"),
             ("favorite", [f"g_{i:012x}#0" for i in range(201)]), ("favorite", ["../etc/passwd"]),
             ("favorite", ["g_0123456789ab#0", "gen:g_0123456789ab"])]
    for action, keys in cases:
        with pytest.raises(GIAError) as e:
            s.batch_images(action, keys)
        assert e.value.code == "invalid_query", (action, keys)

def test_batch_delete_removes_file_and_thumb(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    paths = _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p", n=2)
    thumbs = settings.state_dir / "thumbs"; thumbs.mkdir(parents=True, exist_ok=True)
    (thumbs / "g_0123456789ab_0.jpg").write_bytes(b"t")
    out = s.batch_images("delete", ["g_0123456789ab#0", "g_0123456789ab#5"])
    assert [r["ok"] for r in out["results"]] == [True, False]
    assert not paths[0].exists() and paths[1].exists()
    assert not (thumbs / "g_0123456789ab_0.jpg").exists()
    assert "g_0123456789ab#0" in s.library.deleted()

def test_zip_contents_and_names(svc, settings):
    import zipfile
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T10:00:00+00:00", "../../etc/passwd; rm -rf /\x00 \"x\"", n=2)
    _seed_gen(settings, st, "g_0123456789ac", "2026-11-10T10:00:00+00:00", "a cat")
    path, fname = s.zip_images(["g_0123456789ab#0", "gen:g_0123456789ab#1", "gen:g_0123456789ac#0"])
    try:
        assert fname.endswith(".zip") and "/" not in fname
        assert path.parent == settings.state_dir / "tmp_downloads" and (path.stat().st_mode & 0o777) == 0o600
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
            assert zf.read(names[2]) == b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    finally:
        path.unlink()
    assert len(set(names)) == 3
    import re
    assert all(re.fullmatch(r"[0-9a-z-]+_[0-9a-z-]+_\d+\.png", n) for n in names), names
    assert names[0].startswith("2026-11-09_") and names[2].startswith("2026-11-10_a-cat_3")

def test_zip_limits(svc, settings, monkeypatch):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T10:00:00+00:00", "p", n=101)
    keys = [f"g_0123456789ab#{i}" for i in range(101)]
    with pytest.raises(GIAError) as e:
        s.zip_images(keys)
    assert e.value.code == "invalid_query" and "100" in str(e.value)
    monkeypatch.setattr("gemini_image_api.service.MAX_ZIP_BYTES", 20)
    with pytest.raises(GIAError) as e:
        s.zip_images(keys[:2])
    assert e.value.code == "invalid_query" and "bytes" in str(e.value)
    assert not list((settings.state_dir / "tmp_downloads").glob("*")) if (settings.state_dir / "tmp_downloads").exists() else True

def test_zip_excludes_symlink_outside_output_root(svc, settings, tmp_path):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    secret = tmp_path / "secret.png"; secret.write_bytes(b"\x89PNG\r\n\x1a\nSECRET")
    link = settings.output_root / "link.png"; link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    st.append(GenerationRecord(id="g_0123456789ab", ts="2026-11-09T00:00:00+00:00", type="generate",
                               conversation_id="c", chat_metadata=["c"], prompt="p", style=None, style_text=None,
                               aspect_hint=None, model=None, project=None, subdir=None, refs=[],
                               images=[{"index": 0, "path": str(link)}], status="ok", code=None,
                               gemini_text="", elapsed_s=1.0, parent_id=None))
    with pytest.raises(GIAError) as e:
        s.zip_images(["g_0123456789ab#0"])
    assert e.value.code == "invalid_ref" and e.value.details["missing"] == ["g_0123456789ab#0"]

def test_zip_fails_on_deleted_or_unknown_key_without_partial_zip(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p", n=2)
    s.delete_image("gen:g_0123456789ab#1")
    with pytest.raises(GIAError) as e:
        s.zip_images(["g_0123456789ab#0", "g_0123456789ab#1", "g_ffffffffffff#0"])
    assert e.value.code == "invalid_ref"
    assert e.value.details["missing"] == ["g_0123456789ab#1", "g_ffffffffffff#0"]
    assert not (settings.state_dir / "tmp_downloads").exists()

def test_zip_removes_stale_temp_files(svc, settings):
    import os, time
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    d = settings.state_dir / "tmp_downloads"; d.mkdir(parents=True)
    old = d / "old.zip"; old.write_bytes(b"x")
    os.utime(old, (time.time() - 7200,) * 2)
    path, _ = s.zip_images(["g_0123456789ab#0"])
    path.unlink()
    assert not old.exists()


async def test_set_style_gem_enable_creates_gem_with_template(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    assert out["style"] == "anime" and out["gem"]["custom"] is False
    assert out["gem"]["gem_id"] in b.gems and out["gem"]["name"] == "Musa · anime"
    assert b.gem_calls[0][0] == "create"
    assert 'always works in the "anime" style' in b.gems[out["gem"]["gem_id"]]["instructions"]
    entry = s.gems.get("anime")
    assert entry["gem_id"] == out["gem"]["gem_id"] and entry["custom"] is False


async def test_set_style_gem_enable_with_instructions_marks_custom(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True, instructions="custom rules")
    assert out["gem"]["custom"] is True
    assert b.gems[out["gem"]["gem_id"]]["instructions"] == "custom rules"
    out2 = await s.set_style_gem("anime", True, instructions="other rules")
    assert out2["gem"]["gem_id"] == out["gem"]["gem_id"]
    assert ("update", out["gem"]["gem_id"]) in [(c[0], c[1]) for c in b.gem_calls]
    assert s.gems.get("anime")["instructions"] == "other rules"


async def test_set_style_gem_empty_instructions_restores_template(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True, instructions="custom rules")
    out2 = await s.set_style_gem("anime", True, instructions="")
    assert out2["gem"]["custom"] is False
    assert 'always works in the "anime" style' in s.gems.get("anime")["instructions"]
    assert b.gems[out["gem"]["gem_id"]]["instructions"] == s.gems.get("anime")["instructions"]


async def test_set_style_gem_disable_deletes_gem_and_entry(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    gem_id = out["gem"]["gem_id"]
    out2 = await s.set_style_gem("anime", False)
    assert out2 == {"style": "anime", "gem": None}
    assert gem_id not in b.gems and s.gems.get("anime") is None
    assert ("delete", gem_id) in b.gem_calls


async def test_set_style_gem_disable_when_gem_already_gone(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    del b.gems[out["gem"]["gem_id"]]  # gem vanished upstream; delete_gem will fail
    out2 = await s.set_style_gem("anime", False)
    assert out2["gem"] is None and s.gems.get("anime") is None


async def test_set_style_gem_disable_network_error_keeps_entry(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    b.gem_net_error = True
    with pytest.raises(GIAError) as e:
        await s.set_style_gem("anime", False)
    assert e.value.code == "upstream_error"
    assert s.gems.get("anime") is not None and out["gem"]["gem_id"] in b.gems


async def test_set_style_gem_unknown_style(svc):
    s, b = svc()
    with pytest.raises(GIAError) as e:
        await s.set_style_gem("nope", True)
    assert e.value.code == "unknown_style" and b.gem_calls == []


async def test_style_gem_instructions_and_list(svc):
    s, b = svc()
    d = s.style_gem_instructions("anime")
    assert d["custom"] is False and "anime style" in d["instructions"]
    assert s.list_style_gems() == []
    out = await s.set_style_gem("anime", True, instructions="mine")
    d = s.style_gem_instructions("anime")
    assert d == {"instructions": "mine", "custom": True}
    assert s.list_style_gems() == [{"style": "anime", "gem_id": out["gem"]["gem_id"],
                                    "name": "Musa · anime", "custom": True}]
    with pytest.raises(GIAError) as e:
        s.style_gem_instructions("nope")
    assert e.value.code == "unknown_style"


async def test_run_passes_gem_id_for_generate_and_edit(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    await s.generate(prompt="a cat", style="anime")
    assert b.calls[-1][4].get("gem_id") == out["gem"]["gem_id"]
    # style text (append) is still sent in the prompt
    assert "anime style" in b.calls[-1][1]
    await s.edit("c_1", prompt="more", style="anime")
    assert b.calls[-1][4].get("gem_id") == out["gem"]["gem_id"]


async def test_run_without_gem_passes_none(svc):
    s, b = svc()
    await s.generate(prompt="a cat", style="anime")
    assert b.calls[-1][4].get("gem_id") is None


async def test_gem_missing_warning_removes_entry_and_travels(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    b.warning = {"code": "gem_missing", "gem_id": out["gem"]["gem_id"], "message": "gone", "next_action": "re-create"}
    sr = await s.generate(prompt="a cat", style="anime")
    assert s.gems.get("anime") is None
    assert sr.warning["code"] == "gem_missing"
    assert sr.warning["gem_id"] == out["gem"]["gem_id"]
    assert to_payload(sr)["warning"] == sr.warning


async def test_gem_missing_warning_with_stale_gem_id_keeps_entry(svc):
    s, b = svc()
    out = await s.set_style_gem("anime", True)
    live = out["gem"]["gem_id"]
    # conversation reused a dead gem X while gems.json holds the live one Y
    b.warning = {"code": "gem_missing", "gem_id": "gem_dead", "message": "gone", "next_action": "re-create"}
    sr = await s.edit("c_1", prompt="more", style="anime")
    assert sr.warning["code"] == "gem_missing" and sr.warning["gem_id"] == "gem_dead"
    assert s.gems.get("anime") is not None and s.gems.get("anime")["gem_id"] == live


async def test_no_warning_omitted_from_payload(svc):
    s, b = svc()
    sr = await s.generate(prompt="a cat")
    assert sr.warning is None and "warning" not in to_payload(sr)


async def test_delete_user_style_removes_gem_and_style(svc, settings):
    s, b = svc()
    st = s.styles.create(name="My Style", description="d", append="a")
    out = await s.set_style_gem(st.id, True)
    await s.delete_user_style(st.id)
    assert out["gem"]["gem_id"] not in b.gems
    assert s.gems.get(st.id) is None
    with pytest.raises(GIAError):
        s.styles.delete(st.id)


async def test_delete_user_style_without_gem(svc):
    s, b = svc()
    st = s.styles.create(name="Plain", description="d")
    await s.delete_user_style(st.id)
    assert b.gem_calls == []
    with pytest.raises(GIAError):
        s.styles.delete(st.id)



# -- collections / favourites / images / batch / style editing ---------------------------------
def _seed(settings, gid="g_0123456789ab", name="k.png", style="anime"):
    img = settings.output_root / name
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    StateStore(settings.state_dir).append(GenerationRecord(
        id=gid, ts="2026-11-09T00:00:00+00:00", type="generate", conversation_id="c_9", chat_metadata=["c"],
        prompt="p", style=style, style_text=None, aspect_hint=None, model="gemini-flash", project=None,
        subdir=None, refs=[], images=[{"index": 0, "path": str(img), "width": 8, "height": 6}], status="ok",
        code=None, gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt="a cat"))
    return img


def test_rename_and_delete_collection(svc, settings):
    s, _ = svc()
    _seed(settings)
    ref = "gen:g_0123456789ab#0"
    cid = s.add_to_collection(ref, "Old")["collection"]["id"]
    out = s.rename_collection("old", "New", description="desc")
    assert out == {"id": cid, "name": "New", "description": "desc", "count": 1}
    with pytest.raises(GIAError) as e:
        s.rename_collection("nope", "X")
    assert e.value.code == "unknown_collection"
    s.add_to_collection(ref, "Other")
    with pytest.raises(GIAError) as e:
        s.rename_collection("New", "other")
    assert e.value.code == "invalid_collection"
    out = s.delete_collection("New")
    assert out["deleted"] is True and out["images_affected"] == 1
    assert [c["name"] for c in s.list_collections()] == ["Other"]
    with pytest.raises(GIAError) as e:
        s.delete_collection(cid)
    assert e.value.code == "unknown_collection"


def test_remove_from_collection(svc, settings):
    s, _ = svc()
    _seed(settings)
    ref = "gen:g_0123456789ab"
    s.add_to_collection(ref, "A")
    out = s.remove_from_collection(ref, "A")
    assert out["key"] == "g_0123456789ab#0" and out["collections"] == []
    assert s.remove_from_collection(ref, "A")["collections"] == []  # idempotent
    with pytest.raises(GIAError) as e:
        s.remove_from_collection(ref, "ghost")
    assert e.value.code == "unknown_collection"
    with pytest.raises(GIAError) as e:
        s.remove_from_collection("gen:g_ffffffffffff", "A")
    assert e.value.code == "invalid_ref"


def test_favorite_and_get_image(svc, settings):
    s, _ = svc()
    img = _seed(settings)
    ref = "gen:g_0123456789ab#0"
    assert s.set_favorite(ref) == {"key": "g_0123456789ab#0", "favorite": True}
    s.add_to_collection(ref, "A")
    info = s.get_image(ref)
    assert info == {"ref": ref, "path": str(img), "prompt": "a cat", "style": "anime", "model": "gemini-flash",
                    "ts": "2026-11-09T00:00:00+00:00", "width": 8, "height": 6, "favorite": True,
                    "collections": info["collections"], "conversation_id": "c_9"}
    assert len(info["collections"]) == 1
    assert s.set_favorite(ref, False)["favorite"] is False
    assert s.get_image("gen:g_0123456789ab")["favorite"] is False
    for bad in ("nope", "gen:g_ffffffffffff"):
        with pytest.raises(GIAError) as e:
            s.get_image(bad)
        assert e.value.code == "invalid_ref"


def test_delete_image_unlinks_file_and_thumb_and_hides(svc, settings):
    s, _ = svc()
    img = _seed(settings)
    thumb = settings.state_dir / "thumbs" / "g_0123456789ab_0.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"t")
    ref = "gen:g_0123456789ab#0"
    s.set_favorite(ref)
    s.add_to_collection(ref, "A")
    assert s.delete_image(ref) == {"key": "g_0123456789ab#0", "deleted": True}
    assert not img.exists() and not thumb.exists()
    assert s.library.is_deleted("g_0123456789ab#0") and not s.library.is_favorite("g_0123456789ab#0")
    with pytest.raises(GIAError) as e:
        s.get_image(ref)
    assert e.value.code == "invalid_ref"
    assert s.list_collections()[0]["count"] == 0


async def test_generate_batch_runs_sequentially_with_shared_options(svc):
    s, b = svc()
    out = await s.generate_batch(["a", "b", "c"], style="anime", collection="Lote", aspect_hint="16:9")
    assert out["status"] == "ok" and out["stopped_at"] is None and len(out["results"]) == 3
    assert [c[1].count("anime style") for c in b.calls] == [1, 1, 1]
    assert all("16:9" in c[1] for c in b.calls)
    assert all(r["collection"]["name"] == "Lote" for r in out["results"])
    colls = s.list_collections()
    assert len(colls) == 1 and colls[0]["count"] == 3


async def test_generate_batch_limits(svc):
    s, b = svc()
    for bad in ([], ["x"] * 9):
        with pytest.raises(GIAError):
            await s.generate_batch(bad)
    assert b.calls == []
    assert (await s.generate_batch(["x"] * 8))["status"] == "ok"


async def test_generate_batch_stops_on_quota_guard(svc, settings):
    settings.quota_stop_pct = 90
    s, b = svc(quota={"tier": "PRO", "window_5h_pct": 95, "weekly_pct": 1, "resets_at_5h": "r", "resets_at_weekly": "w"})
    out = await s.generate_batch(["a", "b"])
    assert out["status"] == "stopped" and out["stopped_at"] == 0
    assert len(out["results"]) == 1 and out["results"][0]["code"] == "quota_guard" and b.calls == []


async def test_generate_batch_stops_on_auth_and_continues_on_other_errors(svc):
    s, b = svc()
    outcomes = iter([GIAError("no_image", "nothing"), GIAError("auth_expired", "gone"), None])
    real = s.generate

    async def flaky(**kw):
        o = next(outcomes)
        if o:
            raise o
        return await real(**kw)
    s.generate = flaky
    out = await s.generate_batch(["a", "b", "c"])
    assert [r["code"] for r in out["results"]] == ["no_image", "auth_expired"]
    assert out["status"] == "stopped" and out["stopped_at"] == 1

    outcomes = iter([GIAError("no_image", "nothing"), None])
    out = await s.generate_batch(["a", "b"])
    assert out["status"] == "partial" and out["stopped_at"] is None
    assert [r["status"] for r in out["results"]] == ["error", "ok"]


async def test_update_user_style_updates_fields_and_gem(svc):
    s, b = svc()
    st = s.styles.create(name="Mine", description="d", append="a")
    await s.set_style_gem(st.id, True)
    new = await s.update_user_style(st.id, name="Mine 2", description="d2", append="new rules")
    assert new.id == st.id and new.name == "Mine 2"
    op = b.gem_calls[-1]
    assert op[0] == "update" and "Mine 2" in op[2]
    assert "new rules" in op[3] and op[4] == "d2"
    assert "new rules" in s.gems.get(st.id)["instructions"] and s.gems.get(st.id)["custom"] is False


async def test_update_user_style_keeps_custom_gem_instructions(svc):
    s, b = svc()
    st = s.styles.create(name="Mine", description="d", append="a")
    await s.set_style_gem(st.id, True, instructions="custom rules")
    await s.update_user_style(st.id, append="other", name="Renamed")
    op = b.gem_calls[-1]
    assert op[3] == "custom rules" and "Renamed" in op[2]
    assert s.gems.get(st.id)["custom"] is True


async def test_update_user_style_without_gem_and_errors(svc):
    s, b = svc()
    st = s.styles.create(name="Plain", description="d")
    await s.update_user_style(st.id, description="x")
    assert b.gem_calls == []
    with pytest.raises(GIAError) as e:
        await s.update_user_style("nope", description="x")
    assert e.value.code == "unknown_style"
    with pytest.raises(GIAError) as e:
        await s.update_user_style("anime", description="x")  # builtin
    assert e.value.code == "invalid_style"


def test_describe_style(svc):
    s, _ = svc()
    d = s.describe_style("anime")
    assert d["id"] == "anime" and d["append"] == "anime style" and d["gem"] is None and d["has_ref"] is False
    assert "anime style" in d["final_prompt"] and d["style_ref"] is None
    assert "a dog" in s.describe_style("anime", prompt="a dog")["final_prompt"]
    with pytest.raises(GIAError) as e:
        s.describe_style("nope")
    assert e.value.code == "unknown_style"


async def test_update_user_style_gem_gone_upstream_drops_entry(svc):
    s, b = svc()
    st = s.styles.create(name="Mine", description="d", append="a")
    gid = (await s.set_style_gem(st.id, True))["gem"]["gem_id"]
    del b.gems[gid]  # update_gem now fails and gem_exists is False
    new = await s.update_user_style(st.id, description="d2")
    assert new.description == "d2" and s.gems.get(st.id) is None


async def test_update_user_style_gem_sync_failure_keeps_style_and_raises(svc):
    s, b = svc()
    st = s.styles.create(name="Mine", description="d", append="a")
    await s.set_style_gem(st.id, True)

    async def boom(*a, **k):
        raise GIAError(UPSTREAM_ERROR, "network down")
    b.update_gem = boom
    with pytest.raises(GIAError) as e:
        await s.update_user_style(st.id, append="new")
    assert e.value.code == "upstream_error" and "style saved; Gem sync failed" in str(e.value)
    assert e.value.details == {"style": st.id, "saved": True}
    assert s.catalog().styles[st.id].append == "new" and s.gems.get(st.id) is not None


async def test_update_user_style_family_only_does_not_touch_gem(svc):
    s, b = svc()
    st = s.styles.create(name="Mine", description="d", append="a")
    await s.set_style_gem(st.id, True)
    n = len(b.gem_calls)
    await s.update_user_style(st.id, family="Painting")
    assert len(b.gem_calls) == n


async def test_generate_batch_stops_on_backend_usage_limit_result(svc):
    s, b = svc()

    async def limited(prompt, refs, *, model, **kw):
        return GenerateResult("error", "usage_limit", "c_1", [], "", 1.0, None)
    b.generate = limited
    out = await s.generate_batch(["a", "b"])
    assert out["status"] == "stopped" and out["stopped_at"] == 0 and len(out["results"]) == 1
    assert out["results"][0]["code"] == "usage_limit"


async def test_generate_batch_stops_on_temporarily_blocked_and_survives_unexpected_errors(svc):
    s, b = svc()
    outcomes = iter([RuntimeError("boom"), GIAError("temporarily_blocked", "wait"), None])
    real = s.generate

    async def flaky(**kw):
        o = next(outcomes)
        if o:
            raise o
        return await real(**kw)
    s.generate = flaky
    out = await s.generate_batch(["a", "b", "c"])
    assert [r["code"] for r in out["results"]] == ["upstream_error", "temporarily_blocked"]
    assert out["status"] == "stopped" and out["stopped_at"] == 1


def test_delete_collection_keeps_images_accessible(svc, settings):
    s, _ = svc()
    _seed(settings)
    ref = "gen:g_0123456789ab#0"
    s.add_to_collection(ref, "A")
    s.delete_collection("A")
    info = s.get_image(ref)
    assert info["collections"] == [] and Path(info["path"]).exists()


def _real_png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


_REF_PHRASE = "style reference"


async def test_update_user_style_remove_image_resyncs_gem_without_reference(svc):
    s, b = svc()
    st = s.styles.create(name="Pic", description="d", append="a", image=(_real_png(), "image/png"))
    await s.set_style_gem(st.id, True)
    assert _REF_PHRASE in b.gem_calls[-1][2]
    await s.update_user_style(st.id, remove_image=True)
    op = b.gem_calls[-1]
    assert op[0] == "update" and _REF_PHRASE not in op[3]
    assert _REF_PHRASE not in s.gems.get(st.id)["instructions"]


async def test_update_user_style_new_image_resyncs_gem_with_reference(svc):
    s, b = svc()
    st = s.styles.create(name="Plain", description="d", append="a")
    await s.set_style_gem(st.id, True)
    assert _REF_PHRASE not in b.gem_calls[-1][2]
    await s.update_user_style(st.id, image=(_real_png(), "image/png"))
    op = b.gem_calls[-1]
    assert op[0] == "update" and _REF_PHRASE in op[3]
    assert _REF_PHRASE in s.gems.get(st.id)["instructions"]


def test_list_generations_model_over_100_records_no_dupes_or_gaps(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    for i in range(150):
        _seed_gen(settings, st, f"g_{i:012x}", f"2026-11-09T{i // 60:02d}:{i % 60:02d}:00+00:00", "p",
                  model="m-a" if i % 2 == 0 else "m-b")
    ids = []
    for off in (0, 25, 50):
        ids += [g["id"] for g in s.list_generations(model="m-a", limit=25, offset=off)]
    assert len(ids) == 75 == len(set(ids))
    assert ids == [f"g_{i:012x}" for i in range(148, -1, -2)]


def test_list_generations_model_and_collection_combined(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    for i, m in enumerate(("m-a", "m-b", "m-a")):
        _seed_gen(settings, st, f"g_00000000000{i}", f"2026-11-09T00:00:0{i}+00:00", "p", model=m)
    cid = s.library.resolve_collection("C", create=True).id
    for i in (0, 1):
        s.library.add(f"g_00000000000{i}#0", cid)
    assert [g["id"] for g in s.list_generations(model="m-a", collection="C")] == ["g_000000000000"]


def test_list_generations_collection_members_older_than_limit(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    for i in range(30):
        _seed_gen(settings, st, f"g_{i:012x}", f"2026-11-09T00:00:{i:02d}+00:00", "p")
    s.library.add("g_000000000000#0", s.library.resolve_collection("C", create=True).id)
    assert [g["id"] for g in s.list_generations(collection="C", limit=5)] == ["g_000000000000"]
    s.library.mark_deleted("g_00000000001d#0")  # newest record hidden: must not eat a page slot
    assert len(s.list_generations(limit=29)) == 29


def test_batch_unknown_collection_is_one_global_error(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    with pytest.raises(GIAError) as e:
        s.batch_images("remove_from_collection", ["g_0123456789ab#0", "g_0123456789ab#1"], "nope")
    assert e.value.code == "unknown_collection"
    assert s.library.collections() == []
    out = s.batch_images("add_to_collection", ["g_ffffffffffff#0"], "Fresh")  # no valid key: no collection created
    assert out["results"][0]["ok"] is False and s.library.collections() == []


def test_batch_favorite_is_idempotent(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    for _ in range(2):
        assert s.batch_images("favorite", ["g_0123456789ab#0"])["results"] == [{"key": "g_0123456789ab#0", "ok": True}]
    assert s.library.is_favorite("g_0123456789ab#0")


def test_batch_reads_history_once(svc, settings, monkeypatch):
    import gemini_image_api.service as svc_mod
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p", n=5)
    calls = []
    real = svc_mod.image_index
    monkeypatch.setattr(svc_mod, "image_index", lambda *a, **k: calls.append(1) or real(*a, **k))
    out = s.batch_images("delete", [f"g_0123456789ab#{i}" for i in range(5)])
    assert all(r["ok"] for r in out["results"]) and len(calls) == 1


def test_batch_and_zip_reject_huge_index(svc):
    s, _ = svc()
    key = "g_0123456789ab#" + "1" * 5000
    for fn in (lambda: s.batch_images("favorite", [key]), lambda: s.zip_images([key])):
        with pytest.raises(GIAError) as e:
            fn()
        assert e.value.code == "invalid_query"


def test_zip_rejects_normalized_duplicates(svc):
    s, _ = svc()
    with pytest.raises(GIAError) as e:
        s.zip_images(["g_0123456789ab#0", "gen:g_0123456789ab"])
    assert e.value.code == "invalid_query"


def test_zip_file_vanishing_midway_is_invalid_ref_and_cleans_up(svc, settings, monkeypatch):
    import zipfile
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    def boom(self, filename, *a, **k):
        raise FileNotFoundError(2, "gone", str(filename))
    monkeypatch.setattr(zipfile.ZipFile, "write", boom)
    with pytest.raises(GIAError) as e:
        s.zip_images(["g_0123456789ab#0"])
    assert e.value.code == "invalid_ref" and e.value.details["missing"] == ["g_0123456789ab#0"]
    assert list((settings.state_dir / "tmp_downloads").glob("*")) == []


def test_zip_write_failure_is_not_invalid_ref(svc, settings, monkeypatch):
    import errno, zipfile
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    def full(self, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(zipfile.ZipFile, "write", full)
    with pytest.raises(OSError) as e:
        s.zip_images(["g_0123456789ab#0"])
    assert not isinstance(e.value, GIAError) and e.value.errno == errno.ENOSPC
    assert list((settings.state_dir / "tmp_downloads").glob("*")) == []


# -- phase 1 infrastructure: display_prompt, conversation_head, change_spec --

async def test_display_prompt_is_slug_source_for_generate_and_edit(svc):
    s, b = svc()
    await s.generate(prompt="long prompt", display_prompt="Un gato")
    await s.edit("c_1", prompt="long edit", display_prompt="Un perro")
    await s.generate(prompt="plain")
    assert [c[-1]["slug_source"] for c in b.calls] == ["Un gato", "Un perro", "plain"]


def _conv_rec(gid, ts, cid="c_h", type="generate", status="ok", parent_id=None, images=1, path=None):
    return GenerationRecord(id=gid, ts=ts, type=type, conversation_id=cid, chat_metadata=["c"], prompt="p",
                            style=None, style_text=None, aspect_hint=None, model=None, project=None,
                            subdir=None, refs=[],
                            images=[{"index": i, "path": path} for i in range(images)],
                            status=status, code=None, gemini_text="", elapsed_s=1.0, parent_id=parent_id)


def _two_in_conversation(settings, st):
    p1 = settings.output_root / "h1.png"
    p2 = settings.output_root / "h2.png"
    p1.parent.mkdir(parents=True, exist_ok=True)
    for p in (p1, p2):
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    st.append(_conv_rec("g_00000000aaa1", "2026-11-09T00:00:00+00:00", path=str(p1)))
    st.append(_conv_rec("g_00000000aaa2", "2026-11-09T00:01:00+00:00", type="edit", path=str(p2)))


def test_conversation_head_true_only_for_latest_record(svc, settings):
    s, _ = svc()
    _two_in_conversation(settings, StateStore(settings.state_dir))
    assert s.conversation_head("g_00000000aaa2#0") is True
    assert s.conversation_head("gen:g_00000000aaa2#0") is True
    assert s.conversation_head("g_00000000aaa1#0") is False
    assert s.conversation_head("g_00000000bbbb#0") is False  # not in the gallery index


def test_conversation_head_ignores_failed_records_and_retry_counts_as_parent(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    _two_in_conversation(settings, st)
    st.append(_conv_rec("g_00000000aaa3", "2026-11-09T00:02:00+00:00", type="edit", status="error", images=0))
    assert s.conversation_head("g_00000000aaa2#0") is True  # error record is not a head candidate
    p = settings.output_root / "h4.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 16)
    # a newer retry_download of the head's parent record merges into its key and does not steal the head
    st.append(_conv_rec("g_00000000aaa4", "2026-11-09T00:03:00+00:00", type="retry_download",
                        parent_id="g_00000000aaa2", path=str(p)))
    assert s.conversation_head("g_00000000aaa2#0") is True
    # a retry of the older record counts as that (non-head) record
    p5 = settings.output_root / "h5.png"
    p5.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x02" * 16)
    rec = _conv_rec("g_00000000aaa5", "2026-11-09T00:04:00+00:00", type="retry_download",
                    parent_id="g_00000000aaa1", path=str(p5))
    rec.images = [{"index": 1, "path": str(p5)}]
    st.append(rec)
    assert s.conversation_head("g_00000000aaa1#1") is False


def test_change_spec_edit_when_head_else_reference(svc, settings):
    s, _ = svc()
    _two_in_conversation(settings, StateStore(settings.state_dir))
    common = {"model": "gemini-pro", "ratio": "1:1", "meta": {"k": 1}}
    assert s.change_spec("g_00000000aaa2#0", "change", "ref", **common) == {
        "edit_of": "gen:g_00000000aaa2#0", "prompt": "change", **common}
    assert s.change_spec("gen:g_00000000aaa1#0", "change", "ref", **common) == {
        "refs": ["gen:g_00000000aaa1#0"], "prompt": "ref", **common}


def test_delete_image_via_purge_removes_file_thumb_and_marks(svc, settings):
    s, _ = svc()
    st = StateStore(settings.state_dir)
    (path,) = _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p")
    thumb = settings.state_dir / "thumbs" / "g_0123456789ab_0.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"t")
    assert s.delete_image("gen:g_0123456789ab#0") == {"key": "g_0123456789ab#0", "deleted": True}
    assert not path.exists() and not thumb.exists()
    assert s.library.is_deleted("g_0123456789ab#0")


# -- animations ---------------------------------------------------------------------------
def _sheet_svc(svc, settings):
    from sprite_synth import walk_sheet
    s, b = svc()
    p = settings.output_root / "sheet.jpg"
    walk_sheet(True).save(p, "JPEG")
    b.img = SavedImage(p, 1376, 768, p.stat().st_size, "jpeg", True, None)
    return s, b


def _key(b, i=0):
    return f"{b.last_record_id}#{i}"


async def test_generate_animation_sends_sheet_prompt_and_stores_ficha(svc, settings):
    from PIL import Image
    from gemini_image_api.sprites import cache_path
    s, b = _sheet_svc(svc, settings)
    sr = await s.generate(prompt="a robot walking", aspect_hint="1:1", display_prompt="Robot",
                          animation={"grid": "4x2", "mode": "sprite"})
    call = b.calls[0]
    assert "4 columns x 2 rows" in call[1] and "a robot walking" in call[1]
    assert call[4]["aspect_hint"] == "16:9" and call[4]["slug_source"] == "Robot"
    assert sr.warning is None
    f = s.animations.get(_key(b))
    assert (f["cols"], f["rows"], f["frames"]) == (4, 2, 8)
    assert (f["mode"], f["key_color"], f["tolerance"], f["fps"], f["loop"]) == ("sprite", "green", 25, 10, "loop")
    assert f["detected_by"] == "lanes" and len(f["cells"]) == 8
    sheet = cache_path(settings.state_dir, _key(b), f["rev"])
    assert Image.open(sheet).size == (8 * f["frame_w"], f["frame_h"])


async def test_generate_animation_defaults_without_user_display_prompt(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="a robot", animation={"grid": "2x2", "mode": "scene"})
    assert b.calls[0][4]["slug_source"] == "a robot" and b.calls[0][4]["aspect_hint"] == "1:1"
    f = s.animations.get(_key(b))
    assert f["mode"] == "scene" and f["key_color"] is None and f["frames"] == 4


async def test_generate_animation_rejects_template_and_bad_request(svc, settings):
    s, b = _sheet_svc(svc, settings)
    for kw in ({"template": "t", "template_vars": {"x": "a"}, "animation": {"grid": "4x2"}},
               {"animation": {"grid": "5x5"}}, {"animation": {"grid": "4x2", "mode": "x"}}):
        with pytest.raises(GIAError) as e:
            await s.generate(prompt="p", **kw)
        assert e.value.code == "invalid_animation"
    assert b.calls == []


async def test_generate_animation_processing_failure_warns_without_ficha(svc, settings):
    s, b = svc()  # a.jpg is not a decodable image
    sr = await s.generate(prompt="p", animation={"grid": "4x2"})
    assert sr.warning["code"] == "invalid_animation" and "next_action" in sr.warning
    assert s.animations.get(_key(b)) is None
    b.warning = {"code": "gem_missing", "message": "m", "next_action": "n", "gem_id": "x"}
    sr = await s.generate(prompt="p", animation={"grid": "4x2"})
    assert sr.warning["code"] == "gem_missing"


async def test_edit_inherits_animation_from_head(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="walk", animation={"grid": "4x2", "mode": "sprite", "key_color": "magenta"})
    old_key = _key(b)
    old = s.set_animation(f"gen:{old_key}", fps=14, loop="pingpong", tolerance=30)
    await s.edit("c_1", prompt="make it blue")
    sent = b.calls[-1]
    assert sent[2].startswith("make it blue") or "make it blue" in sent[2]
    assert "Keep the exact same sprite sheet layout" in sent[2] and "4x2 grid" in sent[2]
    assert sent[4]["slug_source"] == "make it blue"
    new = s.animations.get(_key(b))
    assert _key(b) != old_key
    assert (new["cols"], new["rows"], new["mode"], new["key_color"], new["tolerance"], new["fps"],
            new["loop"]) == (4, 2, "sprite", "magenta", 30, 14, "pingpong")
    assert new["cells"] and new["rev"] != old["rev"]


async def test_edit_without_animation_adds_no_suffix_or_ficha(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="plain")
    await s.edit("c_1", prompt="change")
    assert "Keep the exact same" not in b.calls[-1][2]
    assert s.animations.all() == {}


async def test_set_animation_convert_adjust_and_rev_rules(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="plain")
    ref, key = f"gen:{_key(b)}", _key(b)
    with pytest.raises(GIAError) as e:
        s.set_animation(ref, fps=12)
    assert e.value.code == "invalid_animation"
    f = s.set_animation(ref, cols=4, rows=2)
    assert (f["fps"], f["tolerance"], f["loop"], f["mode"], f["key_color"]) == (10, 25, "loop", "sprite", "green")
    assert f["frames"] == 8 and s.get_animation(ref)["rev"] == f["rev"]
    f2 = s.set_animation(ref, fps=20, loop="pingpong")
    assert f2["rev"] == f["rev"] and f2["fps"] == 20 and f2["cells"] == f["cells"]
    f3 = s.set_animation(ref, tolerance=40)
    assert f3["rev"] != f2["rev"] and f3["fps"] == 20
    f4 = s.set_animation(ref, mode="scene")
    assert f4["key_color"] is None and f4["rev"] != f3["rev"]
    f5 = s.set_animation(ref, redetect=True)
    assert f5["rev"] == f4["rev"] or f5["cells"]  # re-detect keeps data valid
    f6 = s.set_animation(ref, cols=2, rows=2)
    assert f6["frames"] == 4 and f6["rev"] != f4["rev"]
    assert s.animations.get(key)["frames"] == 4


async def test_set_animation_validation_and_errors(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="plain")
    ref = f"gen:{_key(b)}"
    bad = [dict(cols=1, rows=1), dict(cols=9, rows=1), dict(cols=4, rows=2, fps=1), dict(cols=4, rows=2, fps=31),
           dict(cols=4, rows=2, tolerance=4), dict(cols=4, rows=2, mode="x"),
           dict(cols=4, rows=2, key_color="red"), dict(cols=4, rows=2, loop="x"), dict(cols=7, rows=6)]
    for kw in bad:
        with pytest.raises(GIAError) as e:
            s.set_animation(ref, **kw)
        assert e.value.code == "invalid_animation", kw
    assert s.animations.get(_key(b)) is None
    with pytest.raises(GIAError) as e:
        s.get_animation(ref)
    assert e.value.code == "unknown_animation"
    with pytest.raises(GIAError) as e:
        s.delete_animation(ref)
    assert e.value.code == "unknown_animation"
    with pytest.raises(GIAError) as e:
        s.animation_sheet(ref)
    assert e.value.code == "unknown_animation"
    with pytest.raises(GIAError) as e:
        s.set_animation("gen:g_ffffffffffff#0", cols=4, rows=2)
    assert e.value.code == "invalid_ref"


async def test_set_animation_undecodable_image_is_invalid_image(svc, settings):
    s, b = svc()  # a.jpg holds garbage
    await s.generate(prompt="plain")
    with pytest.raises(GIAError) as e:
        s.set_animation(f"gen:{_key(b)}", cols=4, rows=2)
    assert e.value.code == "invalid_image"
    assert s.animations.get(_key(b)) is None


async def test_animation_sheet_regenerates_and_export_matches(svc, settings):
    from PIL import Image
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="a robot walking", animation={"grid": "4x2"})
    ref = f"gen:{_key(b)}"
    path = s.animation_sheet(ref)
    size = Image.open(path).size
    path.unlink()
    again = s.animation_sheet(ref)
    assert again == path and Image.open(again).size == size
    f = s.get_animation(ref)
    assert s.animations.get(_key(b))["rev"] == f["rev"]
    png, meta, base = s.animation_export(ref)
    assert png == path and base == "x"  # FakeBackend stores user_prompt "x"
    assert meta == {"image": f"{base}.png", "frameWidth": f["frame_w"], "frameHeight": f["frame_h"],
                    "frames": 8, "columns": 8, "rows": 1, "fps": 10, "loop": "loop", "transparent": True}


async def test_delete_animation_keeps_image_and_removes_cache(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="p", animation={"grid": "2x2"})
    ref = f"gen:{_key(b)}"
    path = s.animation_sheet(ref)
    assert s.delete_animation(ref) == {"key": _key(b), "deleted": True}
    assert not path.exists() and s.animations.get(_key(b)) is None
    assert s.get_image(ref)["path"]


async def test_delete_image_purges_animation(svc, settings):
    s, b = _sheet_svc(svc, settings)
    await s.generate(prompt="p", animation={"grid": "2x2"})
    ref = f"gen:{_key(b)}"
    path = s.animation_sheet(ref)
    s.delete_image(ref)
    assert not path.exists() and s.animations.get(_key(b)) is None


def test_batch_delete_purges_file_thumb_mark_and_animation(svc, settings):
    from PIL import Image
    from gemini_image_api.sprites import write_sheet
    s, _ = svc()
    st = StateStore(settings.state_dir)
    paths = _seed_gen(settings, st, "g_0123456789ab", "2026-11-09T00:00:00+00:00", "p", n=2)
    thumbs = settings.state_dir / "thumbs"; thumbs.mkdir(parents=True, exist_ok=True)
    for i in (0, 1):
        (thumbs / f"g_0123456789ab_{i}.jpg").write_bytes(b"t")
        f = s.animations.put(f"g_0123456789ab#{i}", {"cols": 2, "rows": 1, "mode": "sprite", "fps": 10})
        write_sheet(settings.state_dir, f"g_0123456789ab#{i}", f["rev"], Image.new("RGBA", (4, 4)))
    out = s.batch_images("delete", ["g_0123456789ab#0"])
    assert out["results"][0]["ok"]
    assert not paths[0].exists() and paths[1].exists()
    assert not (thumbs / "g_0123456789ab_0.jpg").exists() and (thumbs / "g_0123456789ab_1.jpg").exists()
    assert "g_0123456789ab#0" in s.library.deleted()
    assert s.animations.get("g_0123456789ab#0") is None and s.animations.get("g_0123456789ab#1") is not None
    cache = settings.state_dir / "anim_cache"
    assert not list(cache.glob("g_0123456789ab_0_*.png")) and list(cache.glob("g_0123456789ab_1_*.png"))


def test_service_owns_a_composition_store(svc, settings):
    from gemini_image_api.compositions import CompositionStore
    s, _ = svc()
    assert isinstance(s.compositions, CompositionStore) and s.compositions.dir == settings.state_dir
    custom = CompositionStore(settings.state_dir)
    s2 = type(s)(s.backend, settings, s.state, compositions=custom)
    assert s2.compositions is custom
