import json
from pathlib import Path

import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from musa.client import Backend, GenerateResult
from musa.errors import AUTH_EXPIRED, UNKNOWN_CONVERSATION, GIAError
from musa.mcp_server import create_server
from musa.state import GenerationRecord, StateStore
from musa.storage import SavedImage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeBackend(Backend):
    def __init__(self):
        self.calls = []
        self.gems = {}
        self.gem_seq = 0
        self.warning = None

    async def create_gem(self, name, instructions, description=""):
        self.gem_seq += 1
        gid = f"gem_{self.gem_seq:03d}"
        self.gems[gid] = {"name": name, "instructions": instructions, "description": description}
        return gid

    async def update_gem(self, gem_id, name, instructions, description=""):
        self.gems[gem_id] = {"name": name, "instructions": instructions, "description": description}

    async def delete_gem(self, gem_id):
        self.gems.pop(gem_id, None)

    async def gem_exists(self, gem_id):
        return gem_id in self.gems
    async def status(self):
        return {"valid": True, "models": ["gemini-flash"], "images_today": 0}

    async def list_models(self):
        return [{"model_name": "gemini-flash", "available": True}]

    async def generate(self, prompt, refs, *, model, progress=None, **kw):
        self.calls.append(("generate", prompt, model))
        if progress:
            await progress("generating", 0.3)
        img = SavedImage(path=Path("/tmp/x.png"), width=64, height=64, nbytes=len(PNG), format="png",
                         full_size=True, thumbnail=PNG)
        return GenerateResult("ok", None, "c_abc", [img], "done", 1.0, ["cid", "rid", "rcid"],
                              record_id="g_000000000001", warning=self.warning)

    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, **kw):
        self.calls.append(("edit", conversation_id, prompt))
        return GenerateResult("ok", None, conversation_id, [], "edited", 0.5, ["cid", "rid", "rcid"],
                              warning=self.warning)

    async def retry_download(self, conversation_id, image_index):
        return SavedImage(path=Path("/tmp/y.png"), width=64, height=64, nbytes=len(PNG), format="png",
                          full_size=False, thumbnail=None)

    async def delete_conversation(self, conversation_id):
        self.calls.append(("delete", conversation_id))


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def server(settings, tmp_path, backend):
    (tmp_path / "styles.yaml").write_text(yaml.safe_dump(
        {"styles": {"anime": {"description": "d", "append": "anime style"}}, "templates": {}}))
    settings.styles_file = tmp_path / "styles.yaml"
    settings.output_root = tmp_path / "generated"
    return create_server(backend, settings)


async def test_tools_list(server):
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"auth_status", "auth_login", "list_styles", "list_templates", "list_models",
            "generate_image", "edit_image", "retry_download", "delete_conversation",
            "list_generations"} <= names


async def test_generate_image_structure(server, backend):
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "a cat", "style": "anime", "model": "gemini-flash"})
    data = res.structured_content
    assert data["status"] == "ok" and data["conversation_id"] == "c_abc"
    assert data["images"][0]["path"] == "/tmp/x.png"
    assert "thumbnail_b64" not in data["images"][0]  # never base64 inside the JSON the agent reads
    images = [b for b in res.content if b.type == "image"]
    assert len(images) == 1 and images[0].mime_type == "image/jpeg"
    assert json.loads(res.content[0].text) == data
    assert "anime style" in backend.calls[0][1]


async def test_generate_image_no_thumbnail_when_disabled(server):
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "a cat", "return_image": False})
    assert not [b for b in res.content if b.type == "image"]


async def test_generate_image_unknown_style(server):
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "x", "style": "nope"})
    data = res.structured_content
    assert data["status"] == "error" and data["code"] == "unknown_style"


async def test_edit_image(server):
    async with Client(server) as c:
        res = await c.call_tool("edit_image", {"conversation_id": "c_abc", "prompt": "glasses"})
    assert res.structured_content["status"] == "ok"


async def test_retry_and_delete(server, backend):
    async with Client(server) as c:
        r = await c.call_tool("retry_download", {"conversation_id": "c_abc", "image_index": 0})
        d = await c.call_tool("delete_conversation", {"conversation_id": "c_abc"})
    assert r.structured_content["image"]["path"] == "/tmp/y.png"
    assert d.structured_content["status"] == "ok" and ("delete", "c_abc") in backend.calls


async def test_progress_forwarded_and_ctx_hidden(server):
    got = []

    async def handler(progress, total, message):
        got.append((progress, total, message))

    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert "ctx" not in tools["generate_image"].input_schema["properties"]
        await c.call_tool("generate_image", {"prompt": "x"}, progress_handler=handler)
    assert (0.3, 1.0, "generating") in got


async def test_list_models_ok(server):
    async with Client(server) as c:
        res = await c.call_tool("list_models", {})
    assert res.structured_content["status"] == "ok"
    assert res.structured_content["models"][0]["model_name"] == "gemini-flash"
    assert res.structured_content["resolved_default"] == {"requested": "gemini-flash", "resolved": "gemini-flash"}


async def test_list_models_resolved_default_uses_backend_alias(server, backend):
    async def resolve(name):
        return "gemini-4-flash"

    backend.resolve_model_name = resolve
    async with Client(server) as c:
        res = await c.call_tool("list_models", {})
    assert res.structured_content["resolved_default"] == {"requested": "gemini-flash",
                                                          "resolved": "gemini-4-flash"}


async def test_list_models_giaerror_is_structured(server, backend):
    async def boom():
        raise GIAError(AUTH_EXPIRED, "session expired")

    backend.list_models = boom
    async with Client(server) as c:
        res = await c.call_tool("list_models", {})
    assert not res.is_error
    assert res.structured_content["status"] == "error" and res.structured_content["code"] == "auth_expired"


async def test_edit_image_giaerror_is_structured(server, backend):
    async def boom(*a, **kw):
        raise GIAError(UNKNOWN_CONVERSATION, "no such conversation")

    backend.edit = boom
    async with Client(server) as c:
        res = await c.call_tool("edit_image", {"conversation_id": "nope", "prompt": "x"})
    assert not res.is_error
    assert res.structured_content["code"] == "unknown_conversation"


async def test_auth_login_generic_failure(server, monkeypatch):
    def boom(settings):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr("musa.auth.login_guided", boom)
    async with Client(server) as c:
        res = await c.call_tool("auth_login", {})
    assert res.structured_content["code"] == "auth_failed"
    assert "browser crashed" in res.structured_content["message"]


async def test_unreadable_styles_file_logs_and_falls_back(server, settings, caplog):
    settings.styles_file.write_text("styles: [unclosed")
    async with Client(server) as c:
        res = await c.call_tool("list_styles", {})
    assert res.structured_content == {}
    assert "cannot load styles catalog" in caplog.text


async def test_instructions_schema_and_annotations(server):
    async with Client(server) as c:
        instructions = c.instructions  # fastmcp 4.1.7: initialize_result is None in modern mode
        tools = {t.name: t for t in await c.list_tools()}
    assert "generate_image" in instructions and "gen:" in instructions and "quota_guard" in instructions
    gen = tools["generate_image"].input_schema["properties"]
    assert set(gen["aspect_hint"]["anyOf"][0]["enum"]) == {"1:1", "4:5", "3:4", "2:3", "9:16", "5:4", "4:3", "3:2", "16:9", "21:9"}

    assert gen["refs"]["description"] and "gen:" in gen["refs"]["description"]
    assert "copy_to" in gen and "template" in tools["edit_image"].input_schema["properties"]
    assert tools["list_styles"].annotations.read_only_hint is True
    assert tools["delete_conversation"].annotations.destructive_hint is True
    assert "list_generations" in tools

async def test_aspect_hint_schema_lists_ten_ratios(server):
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    expected = {"1:1", "4:5", "3:4", "2:3", "9:16", "5:4", "4:3", "3:2", "16:9", "21:9"}
    for name in ("generate_image", "edit_image"):
        prop = tools[name].input_schema["properties"]["aspect_hint"]
        assert set(prop["anyOf"][0]["enum"]) == expected


async def test_errors_carry_retryable_and_next_action(server):
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "x", "style": "nope"})
    d = res.structured_content
    assert d["code"] == "unknown_style" and d["retryable"] is False and "list_styles" in d["next_action"]


async def test_list_generations_tool(server):
    async with Client(server) as c:
        res = await c.call_tool("list_generations", {"limit": 5})
    assert res.structured_content == {"status": "ok", "generations": []}


def _seed_saved_image(settings, tmp_path):
    out = tmp_path / "generated"
    out.mkdir(parents=True, exist_ok=True)
    p = out / "seed.png"
    p.write_bytes(PNG)
    StateStore(settings.state_dir).append(GenerationRecord(
        id="g_000000000001", ts="2026-11-10T00:00:00+00:00", type="generate", conversation_id="c_seed",
        chat_metadata=["c"], prompt="seed", style=None, style_text=None, aspect_hint=None, model=None,
        project=None, subdir=None, refs=[],
        images=[{"path": str(p), "index": 0, "width": 64, "height": 64}],
        status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None))


async def test_list_collections_empty_and_readonly(server):
    async with Client(server) as c:
        res = await c.call_tool("list_collections", {})
        tools = {t.name: t for t in await c.list_tools()}
    assert res.structured_content == {"status": "ok", "collections": []}
    assert tools["list_collections"].annotations.read_only_hint is True


async def test_add_to_collection(server, settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    async with Client(server) as c:
        res = await c.call_tool("add_to_collection", {"image": "gen:g_000000000001", "collection": "X"})
    assert res.structured_content["status"] == "ok"
    assert res.structured_content["collection"]["name"] == "X"


async def test_add_to_collection_unknown_image(server):
    async with Client(server) as c:
        res = await c.call_tool("add_to_collection", {"image": "gen:g_000000000001", "collection": "X"})
    assert res.structured_content["code"] == "invalid_ref"


async def test_generate_image_with_collection(server):
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "x", "collection": "X"})
    assert res.structured_content["status"] == "ok"
    assert res.structured_content["collection"]["name"] == "X"


async def test_list_generations_collection_filter(server, settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    async with Client(server) as c:
        await c.call_tool("add_to_collection", {"image": "gen:g_000000000001", "collection": "X"})
        res = await c.call_tool("list_generations", {"collection": "X"})
        all_res = await c.call_tool("list_generations", {})
    assert [g["id"] for g in res.structured_content["generations"]] == ["g_000000000001"]
    assert len(all_res.structured_content["generations"]) == 1


async def test_list_generations_model_filter(server, settings):
    st = StateStore(settings.state_dir)
    for i, m in enumerate(("m-a", "m-b")):
        st.append(GenerationRecord(
            id=f"g_00000000000{i}", ts=f"2026-11-10T00:00:0{i}+00:00", type="generate", conversation_id="c",
            chat_metadata=["c"], prompt="p", style=None, style_text=None, aspect_hint=None, model=m,
            project=None, subdir=None, refs=[], images=[], status="ok", code=None, gemini_text="",
            elapsed_s=1.0, parent_id=None))
    async with Client(server) as c:
        res = await c.call_tool("list_generations", {"model": "m-a"})
        none = await c.call_tool("list_generations", {"model": "zzz"})
        props = (await c.list_tools())
    assert [g["id"] for g in res.structured_content["generations"]] == ["g_000000000000"]
    assert none.structured_content["generations"] == []
    assert "model" in {t.name: t for t in props}["list_generations"].input_schema["properties"]


async def test_list_generations_unknown_collection_is_structured(server):
    async with Client(server) as c:
        res = await c.call_tool("list_generations", {"collection": "nope"})
    d = res.structured_content
    assert d["status"] == "error" and d["code"] == "unknown_collection"


async def test_collection_schema_and_instructions(server):
    async with Client(server) as c:
        instructions = c.instructions
        tools = {t.name: t for t in await c.list_tools()}
    assert "collection" in instructions
    gen = tools["generate_image"].input_schema["properties"]
    assert gen["collection"]["description"]
    assert "collection" in tools["edit_image"].input_schema["properties"]
    assert "collection" in tools["list_generations"].input_schema["properties"]


async def test_auth_status_has_app_url_none(server, monkeypatch):
    import musa.mcp_server as ms
    monkeypatch.setattr(ms, "running_app", lambda s: None)
    async with Client(server) as c:
        res = await c.call_tool("auth_status", {})
    assert res.structured_content["app_url"] is None
    assert res.structured_content["valid"] is True


async def test_auth_status_has_app_url_when_running(server, monkeypatch):
    import musa.mcp_server as ms
    monkeypatch.setattr(ms, "running_app",
                        lambda s: {"url": "http://127.0.0.1:8765/?key=k", "port": 8765, "pid": 1, "started": "t"})
    async with Client(server) as c:
        res = await c.call_tool("auth_status", {})
    assert res.structured_content["app_url"] == "http://127.0.0.1:8765/?key=k"


async def test_tools_list_includes_style_gems(server):
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"list_style_gems", "style_gem"} <= names


async def test_list_style_gems_empty_and_readonly(server):
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        res = await c.call_tool("list_style_gems", {})
    assert res.structured_content == {"status": "ok", "gems": []}
    assert tools["list_style_gems"].annotations.read_only_hint is True


async def test_style_gem_on_creates_and_lists_then_off_deletes(server, backend):
    async with Client(server) as c:
        on = await c.call_tool("style_gem", {"style": "anime", "enabled": True})
        assert on.structured_content["status"] == "ok"
        gem = on.structured_content["gem"]
        assert on.structured_content["style"] == "anime"
        assert gem["gem_id"] in backend.gems and gem["custom"] is False
        assert gem["name"].startswith("Musa · ")
        lst = await c.call_tool("list_style_gems", {})
        assert lst.structured_content["gems"] == [
            {"style": "anime", "gem_id": gem["gem_id"], "name": gem["name"], "custom": False}]
        off = await c.call_tool("style_gem", {"style": "anime", "enabled": False})
    assert off.structured_content == {"status": "ok", "style": "anime", "gem": None}
    assert backend.gems == {}


async def test_style_gem_custom_instructions(server, backend):
    async with Client(server) as c:
        res = await c.call_tool("style_gem", {"style": "anime", "enabled": True,
                                              "instructions": "always watercolor"})
    assert res.structured_content["gem"]["custom"] is True
    gid = res.structured_content["gem"]["gem_id"]
    assert backend.gems[gid]["instructions"] == "always watercolor"


async def test_style_gem_unknown_style_returns_error_payload(server):
    async with Client(server) as c:
        res = await c.call_tool("style_gem", {"style": "nope", "enabled": True})
    d = res.structured_content
    assert d["status"] == "error" and d["code"] == "unknown_style"
    assert d["retryable"] is False and "next_action" in d


async def test_generate_image_returns_warning_when_present(server, backend):
    backend.warning = {"code": "gem_missing", "message": "gem gone", "next_action": "disable it"}
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "a cat", "style": "anime"})
    assert res.structured_content["status"] == "ok"
    assert res.structured_content["warning"] == backend.warning


async def test_edit_image_returns_warning_when_present(server, backend):
    backend.warning = {"code": "gem_missing", "message": "gem gone", "next_action": "disable it"}
    async with Client(server) as c:
        res = await c.call_tool("edit_image", {"conversation_id": "c_abc", "prompt": "glasses"})
    assert res.structured_content["warning"]["code"] == "gem_missing"


async def test_generate_image_has_no_warning_key_by_default(server):
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "a cat"})
    assert "warning" not in res.structured_content


async def test_style_gem_schema_and_instructions_mention_gems(server):
    from musa.mcp_server import INSTRUCTIONS
    assert "style_gem" in INSTRUCTIONS and "list_style_gems" in INSTRUCTIONS
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    props = tools["style_gem"].input_schema["properties"]
    assert set(props) == {"style", "enabled", "instructions"}
    assert props["enabled"]["type"] == "boolean"



NEW_TOOLS = {"create_style", "update_style", "delete_style", "describe_style", "rename_collection",
             "delete_collection", "remove_from_collection", "favorite_image", "get_image",
             "delete_image", "generate_batch", "list_prompts", "save_prompt", "update_prompt", "delete_prompt",
             "generate_animation", "set_animation", "export_animation", "set_layer_animation",
             "generate_composition", "list_compositions", "get_composition", "export_composition"}


async def test_tool_count_annotations_and_instructions(server):
    from musa.mcp_server import INSTRUCTIONS
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert len(tools) == 37 and NEW_TOOLS <= set(tools)
    for n in NEW_TOOLS:
        assert n in INSTRUCTIONS
    assert "Animations" in INSTRUCTIONS and "Compositions" in INSTRUCTIONS
    assert tools["set_animation"].annotations.idempotent_hint is True
    assert tools["export_animation"].annotations.read_only_hint in (None, False)
    assert tools["describe_style"].annotations.read_only_hint is True
    assert tools["get_image"].annotations.read_only_hint is True
    for n in ("delete_style", "delete_collection", "delete_image"):
        assert tools[n].annotations.destructive_hint is True
    for n in ("favorite_image", "rename_collection", "remove_from_collection"):
        assert tools[n].annotations.idempotent_hint is True
    assert tools["list_prompts"].annotations.read_only_hint is True
    for n in ("update_prompt", "delete_prompt"):
        assert tools[n].annotations.destructive_hint is True and tools[n].annotations.idempotent_hint is True
    assert "gen:" in tools["create_style"].input_schema["properties"]["image"]["description"]


async def test_prompts_save_list_update_delete(server):
    async with Client(server) as c:
        assert (await c.call_tool("list_prompts", {})).structured_content["prompts"] == []
        sv = (await c.call_tool("save_prompt", {"name": "Gato", "text": "a cat"})).structured_content
        pid = sv["prompt"]["id"]
        assert sv["status"] == "ok" and pid.startswith("pr_")
        dup = (await c.call_tool("save_prompt", {"name": "gato", "text": "other"})).structured_content
        assert dup["status"] == "error" and dup["code"] == "invalid_prompt"
        up = (await c.call_tool("update_prompt", {"prompt": "GATO", "text": "a big cat"})).structured_content
        assert up["prompt"]["text"] == "a big cat" and up["prompt"]["name"] == "Gato"
        up = (await c.call_tool("update_prompt", {"prompt": pid, "name": "Felino"})).structured_content
        assert up["prompt"]["name"] == "Felino" and up["prompt"]["text"] == "a big cat"
        lst = (await c.call_tool("list_prompts", {})).structured_content["prompts"]
        assert [p["name"] for p in lst] == ["Felino"]
        gone = (await c.call_tool("delete_prompt", {"prompt": "felino"})).structured_content
        assert gone == {"status": "ok", "deleted": pid}
        again = (await c.call_tool("delete_prompt", {"prompt": pid})).structured_content
        assert again["status"] == "error" and again["code"] == "unknown_prompt"
        bad = (await c.call_tool("update_prompt", {"prompt": "nope", "text": "x"})).structured_content
        assert bad["code"] == "unknown_prompt"


async def test_create_style_describe_update_delete(server, settings, tmp_path):
    img = tmp_path / "generated" / "cover.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(PNG)
    async with Client(server) as c:
        cr = await c.call_tool("create_style", {"name": "Neón Mío", "instructions": "neon glow",
                                                "description": "d", "image": str(img)})
        s = cr.structured_content["style"]
        assert cr.structured_content["status"] == "ok"
        assert s["id"] == "neon-mio" and s["has_image"] is True and s["source"] == "user"
        assert "neon-mio" in (await c.call_tool("list_styles", {})).structured_content
        d = await c.call_tool("describe_style", {"style": "neon-mio", "prompt": "a cat"})
        assert d.structured_content["status"] == "ok" and "neon glow" in d.structured_content["final_prompt"]
        up = await c.call_tool("update_style", {"style": "neon-mio", "name": "Neon 2", "instructions": "teal glow",
                                                "remove_image": True})
        assert up.structured_content["style"]["name"] == "Neon 2"
        assert up.structured_content["style"]["has_image"] is False
        d2 = await c.call_tool("describe_style", {"style": "neon-mio", "prompt": "a cat"})
        assert "teal glow" in d2.structured_content["final_prompt"]
        dl = await c.call_tool("delete_style", {"style": "neon-mio"})
        assert dl.structured_content == {"status": "ok", "deleted": "neon-mio"}
        assert "neon-mio" not in (await c.call_tool("list_styles", {})).structured_content


async def test_create_style_with_gen_ref_and_svg(server, settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    svg = tmp_path / "generated" / "s.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                   '<rect width="10" height="10" fill="red"/></svg>')
    async with Client(server) as c:
        a = await c.call_tool("create_style", {"name": "G", "instructions": "x", "image": "gen:g_000000000001"})
        b = await c.call_tool("create_style", {"name": "S", "instructions": "x", "image": str(svg)})
    assert a.structured_content["style"]["has_image"] is True
    assert b.structured_content["status"] == "ok" and b.structured_content["style"]["has_image"] is True
    ref = (settings.state_dir / "styles" / "s.png")
    assert ref.read_bytes().startswith(b"\x89PNG")


async def test_create_style_image_outside_allowed_dirs(server, tmp_path):
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)
    async with Client(server) as c:
        res = await c.call_tool("create_style", {"name": "X", "instructions": "x", "image": str(outside)})
    assert res.structured_content["code"] == "ref_not_allowed"


async def test_builtin_style_rejected_and_unknown(server):
    async with Client(server) as c:
        up = await c.call_tool("update_style", {"style": "anime", "name": "n"})
        dl = await c.call_tool("delete_style", {"style": "anime"})
        unk = await c.call_tool("delete_style", {"style": "nope"})
        dsc = await c.call_tool("describe_style", {"style": "nope"})
        empty = await c.call_tool("create_style", {"name": " ", "instructions": "x"})
    assert up.structured_content["code"] == "invalid_style"
    assert dl.structured_content["code"] == "invalid_style"
    assert unk.structured_content["code"] == "unknown_style"
    assert dsc.structured_content["code"] == "unknown_style"
    assert empty.structured_content["code"] == "invalid_style"


async def test_collection_rename_remove_delete(server, settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    async with Client(server) as c:
        await c.call_tool("add_to_collection", {"image": "gen:g_000000000001", "collection": "X"})
        rn = await c.call_tool("rename_collection", {"collection": "X", "name": "Y"})
        assert rn.structured_content["status"] == "ok" and rn.structured_content["name"] == "Y"
        rm = await c.call_tool("remove_from_collection", {"image": "gen:g_000000000001", "collection": "Y"})
        assert rm.structured_content["status"] == "ok"
        dl = await c.call_tool("delete_collection", {"collection": "Y"})
        assert dl.structured_content["status"] == "ok"
        gone = await c.call_tool("rename_collection", {"collection": "Y", "name": "Z"})
    assert gone.structured_content["status"] == "error"


async def test_favorite_get_delete_image(server, settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    async with Client(server) as c:
        fav = await c.call_tool("favorite_image", {"image": "gen:g_000000000001"})
        assert fav.structured_content["favorite"] is True
        got = await c.call_tool("get_image", {"image": "gen:g_000000000001"})
        g = got.structured_content
        assert g["status"] == "ok" and g["favorite"] is True and g["prompt"] == "seed"
        unfav = await c.call_tool("favorite_image", {"image": "gen:g_000000000001", "favorite": False})
        assert unfav.structured_content["favorite"] is False
        de = await c.call_tool("delete_image", {"image": "gen:g_000000000001"})
        assert de.structured_content["status"] == "ok"
        assert not (tmp_path / "generated" / "seed.png").exists()
        bad = await c.call_tool("get_image", {"image": "gen:g_ffffffffffff"})
    assert bad.structured_content["status"] == "error"


async def test_generate_batch(server, backend):
    async with Client(server) as c:
        res = await c.call_tool("generate_batch", {"prompts": ["a", "b"], "style": "anime"})
        tools = {t.name: t for t in await c.list_tools()}
        for bad in ([], ["x"] * 9):  # rejected by the input schema before the service runs
            with pytest.raises(ToolError):
                await c.call_tool("generate_batch", {"prompts": bad})
    props = tools["generate_batch"].input_schema["properties"]["prompts"]
    assert props["minItems"] == 1 and props["maxItems"] == 8
    d = res.structured_content
    assert d["status"] == "ok" and len(d["results"]) == 2 and d["stopped_at"] is None
    assert all("anime style" in c_[1] for c_ in backend.calls) and len(backend.calls) == 2


async def test_update_style_validates_before_resolving_image(server, monkeypatch):
    import musa.mcp_server as ms
    called = []

    async def fake_resolve(svc, image):
        called.append(image)
        return PNG, "x.png"
    monkeypatch.setattr(ms, "resolve_style_image", fake_resolve)
    async with Client(server) as c:
        a = await c.call_tool("update_style", {"style": "nope", "image": "https://example.com/a.png"})
        b = await c.call_tool("update_style", {"style": "anime", "image": "https://example.com/a.png"})
    assert a.structured_content["code"] == "unknown_style"
    assert b.structured_content["code"] == "invalid_style"
    assert called == []


class SheetBackend(FakeBackend):
    """generate/edit save a synthetic walk sheet under output_root and record it like the client."""

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def _save(self, kind, cid, prompt, kw):
        import io
        from datetime import datetime, timezone
        from sprite_synth import walk_sheet
        rid = StateStore.new_id("g")
        p = self.settings.output_root / f"{rid}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        walk_sheet(True).save(p, "JPEG")
        StateStore(self.settings.state_dir).append(GenerationRecord(
            id=rid, ts=datetime.now(timezone.utc).isoformat(), type=kind, conversation_id=cid, chat_metadata=["c"],
            prompt=prompt, style=None, style_text=None, aspect_hint=kw.get("aspect_hint"), model="gemini-flash",
            project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(p)}], status="ok", code=None,
            gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt=kw.get("slug_source")))
        img = SavedImage(path=p, width=1376, height=768, nbytes=p.stat().st_size, format="jpeg",
                         full_size=True, thumbnail=PNG)
        return GenerateResult("ok", None, cid, [img], "done", 1.0, ["c"], record_id=rid)

    async def generate(self, prompt, refs, *, model, progress=None, **kw):
        self.calls.append(("generate", prompt, model, kw))
        return self._save("generate", "c_anim", prompt, kw)

    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, **kw):
        self.calls.append(("edit", conversation_id, prompt, kw))
        return self._save("edit", conversation_id, prompt, kw)


@pytest.fixture
def sheet_server(settings, tmp_path):
    settings.styles_file = tmp_path / "styles.yaml"
    settings.output_root = tmp_path / "generated"
    backend = SheetBackend(settings)
    return create_server(backend, settings), backend


async def test_generate_animation_tool(sheet_server):
    server, backend = sheet_server
    async with Client(server) as c:
        res = await c.call_tool("generate_animation", {"prompt": "a robot walking", "grid": "4x2"})
        bad = await c.call_tool("generate_animation", {"prompt": "x", "grid": "9x9"})
    d = res.structured_content
    assert d["status"] == "ok" and d["conversation_id"] == "c_anim"
    (a,) = d["animations"]
    assert a["image"].startswith("gen:g_") and a["frames"] == 8 and a["status"] in ("ok", "review")
    assert a["frame_w"] > 0 and a["frame_h"] > 0
    call = backend.calls[0]
    assert "4 columns x 2 rows" in call[1] and call[3]["aspect_hint"] == "16:9"
    assert call[3]["slug_source"] == "a robot walking"
    assert len([b for b in res.content if b.type == "image"]) == 1
    assert bad.structured_content["status"] == "error" and bad.structured_content["code"] == "invalid_animation"
    assert len(backend.calls) == 1


async def test_set_animation_tool_convert_adjust_and_errors(sheet_server):
    server, backend = sheet_server
    async with Client(server) as c:
        g = (await c.call_tool("generate_image", {"prompt": "plain"})).structured_content
        ref = "gen:" + g["images"][0]["path"].rsplit("/", 1)[1].split(".")[0] + "#0"
        missing = (await c.call_tool("set_animation", {"image": ref, "fps": 12})).structured_content
        assert missing["code"] == "invalid_animation"
        ok = (await c.call_tool("set_animation", {"image": ref, "cols": 4, "rows": 2})).structured_content
        assert ok["status"] == "ok" and ok["animation"]["frames"] == 8 and ok["animation"]["fps"] == 10
        adj = (await c.call_tool("set_animation", {"image": ref, "fps": 20, "loop": "pingpong"})).structured_content
        assert adj["animation"]["fps"] == 20 and adj["animation"]["rev"] == ok["animation"]["rev"]
        bad = (await c.call_tool("set_animation", {"image": ref, "fps": 99})).structured_content
        assert bad["status"] == "error" and bad["code"] == "invalid_animation"
        unk = (await c.call_tool("set_animation", {"image": "gen:g_ffffffffffff", "cols": 2, "rows": 2})).structured_content
        assert unk["status"] == "error"


async def test_export_animation_tool_never_overwrites(sheet_server, settings, tmp_path):
    server, backend = sheet_server
    out = settings.output_root / "export"
    async with Client(server) as c:
        g = (await c.call_tool("generate_animation", {"prompt": "Robot Walk"})).structured_content
        ref = g["animations"][0]["image"]
        r1 = (await c.call_tool("export_animation", {"image": ref, "copy_to": str(out)})).structured_content
        r2 = (await c.call_tool("export_animation", {"image": ref, "copy_to": str(out)})).structured_content
        denied = (await c.call_tool("export_animation", {"image": ref, "copy_to": "/etc"})).structured_content
        noanim = (await c.call_tool("generate_image", {"prompt": "p"})).structured_content
        none = (await c.call_tool("export_animation", {
            "image": "gen:" + noanim["images"][0]["path"].rsplit("/", 1)[1].split(".")[0],
            "copy_to": str(out)})).structured_content
    assert r1["status"] == "ok" and Path(r1["png"]).name == "robot-walk.png"
    assert Path(r1["json"]).name == "robot-walk.json" and r1["animation"]["frames"] == 8
    meta = json.loads(Path(r1["json"]).read_text())
    from PIL import Image
    assert Image.open(r1["png"]).size == (meta["frames"] * meta["frameWidth"], meta["frameHeight"])
    assert meta["image"] == "robot-walk.png" and meta["transparent"] is True
    assert r2["status"] == "ok" and r2["png"] != r1["png"] and Path(r2["png"]).exists() and Path(r1["png"]).exists()
    assert json.loads(Path(r2["json"]).read_text())["image"] == Path(r2["png"]).name
    assert denied["status"] == "error" and denied["code"] == "path_not_allowed"
    assert none["status"] == "error" and none["code"] == "unknown_animation"


async def test_edit_image_inherits_animation(sheet_server):
    server, backend = sheet_server
    async with Client(server) as c:
        g = (await c.call_tool("generate_animation", {"prompt": "walk"})).structured_content
        e = (await c.call_tool("edit_image", {"conversation_id": g["conversation_id"],
                                              "prompt": "make it red"})).structured_content
    assert e["status"] == "ok"
    assert "Keep the exact same sprite sheet layout" in backend.calls[-1][2]
    assert backend.calls[-1][3]["slug_source"] == "make it red"


class CompBackend(SheetBackend):
    """Saves composition_set() images: master without refs, piece on 'solid flat', plate otherwise."""

    def __init__(self, settings):
        super().__init__(settings)
        from sprite_synth import composition_set
        self.master, self.plate, self.piece, _ = composition_set()
        self.fail_on: dict[str, str] = {}
        self.n = 0

    def _save(self, kind, cid, prompt, kw):
        from datetime import datetime, timezone
        for sub, code in self.fail_on.items():
            if sub in prompt:
                raise GIAError(code, "injected")
        img = self.master if "Compose the scene" in prompt else self.piece if "solid flat" in prompt else self.plate
        rid = StateStore.new_id("g")
        p = self.settings.output_root / f"{rid}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p)
        StateStore(self.settings.state_dir).append(GenerationRecord(
            id=rid, ts=datetime.now(timezone.utc).isoformat(), type=kind, conversation_id=cid, chat_metadata=["c"],
            prompt=prompt, style=None, style_text=None, aspect_hint=kw.get("aspect_hint"), model="gemini-flash",
            project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(p)}], status="ok", code=None,
            gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt=kw.get("slug_source")))
        res = SavedImage(path=p, width=img.width, height=img.height, nbytes=p.stat().st_size, format="png",
                         full_size=True, thumbnail=None)
        return GenerateResult("ok", None, cid, [res], "done", 1.0, ["c"], record_id=rid)

    async def generate(self, prompt, refs, *, model, progress=None, **kw):
        self.n += 1
        self.calls.append(("generate", prompt, model, kw))
        return self._save("generate", f"c_{self.n}", prompt, kw)


@pytest.fixture
def comp_server(settings, tmp_path):
    settings.styles_file = tmp_path / "styles.yaml"
    settings.output_root = tmp_path / "generated"
    backend = CompBackend(settings)
    return create_server(backend, settings), backend


async def test_generate_composition_tool_full_flow(comp_server):
    server, backend = comp_server
    async with Client(server) as c:
        res = await c.call_tool("generate_composition", {
            "prompt": "Hermes among shops", "pieces": ["Hermes", "Tarro"], "name": "Escena", "aspect_hint": "4:3"})
        d = res.structured_content
        assert d["status"] == "ok" and "quota" in d
        comp = d["composition"]
        assert comp["name"] == "Escena" and comp["origin"] == "mcp" and comp["master"]["status"] == "done"
        assert [ly["status"] for ly in comp["layers"]] == ["done"] * 3
        assert [x[0] for x in backend.calls] == ["generate", "edit", "generate", "generate"]
        assert backend.calls[0][3]["slug_source"] == "Escena · imagen completa"
        assert backend.calls[0][3]["aspect_hint"] == "4:3"
        lst = (await c.call_tool("list_compositions", {})).structured_content
        assert lst["status"] == "ok" and len(lst["compositions"]) == 1
        item = lst["compositions"][0]
        assert item["id"] == comp["id"] and item["progress"] == {"done": 3, "total": 3, "errors": 0}
        assert [(x["name"], x["role"], x["status"]) for x in item["layers"]] == [
            ("Fondo", "background", "done"), ("Hermes", "piece", "done"), ("Tarro", "piece", "done")]
        for ref in (comp["id"], "escena"):
            got = (await c.call_tool("get_composition", {"composition": ref})).structured_content
            assert got["status"] == "ok" and got["composition"]["id"] == comp["id"]
            assert (got["composition"]["width"], got["composition"]["height"]) == (800, 600)
        missing = (await c.call_tool("get_composition", {"composition": "cmp_nope"})).structured_content
        assert missing["status"] == "error" and missing["code"] == "unknown_composition"


async def test_generate_composition_validation_and_partial(comp_server):
    server, backend = comp_server
    async with Client(server) as c:
        bad = (await c.call_tool("generate_composition", {"prompt": "x", "pieces": ["a", "A"]})).structured_content
        assert bad["status"] == "error" and bad["code"] == "invalid_composition"
        refs = (await c.call_tool("generate_composition", {
            "prompt": "x", "pieces": ["a"], "refs": ["gen:g_1#0"] * 4})).structured_content
        assert refs["code"] == "invalid_composition"
        style = (await c.call_tool("generate_composition", {"prompt": "x", "pieces": ["a"], "style": "nope"})).structured_content
        assert style["code"] == "unknown_style"
        assert backend.calls == []
        backend.fail_on["Isolate only Hermes"] = "usage_limit"
        res = (await c.call_tool("generate_composition", {"prompt": "x", "pieces": ["Hermes", "Tarro"]})).structured_content
    assert res["status"] == "partial"
    layers = res["composition"]["layers"]
    assert [ly["status"] for ly in layers] == ["done", "error", "error"]
    assert layers[1]["error"]["code"] == layers[2]["error"]["code"] == "usage_limit"
    assert len([x for x in backend.calls if "Isolate only Tarro" in x[1]]) == 0


async def test_export_composition_tool_never_overwrites(comp_server, settings, tmp_path):
    import zipfile
    server, backend = comp_server
    dest = settings.output_root / "export"
    async with Client(server) as c:
        comp = (await c.call_tool("generate_composition", {
            "prompt": "x", "pieces": ["Hermes"], "name": "Escena"})).structured_content["composition"]
        r1 = (await c.call_tool("export_composition", {"composition": comp["id"], "copy_to": str(dest)})).structured_content
        r2 = (await c.call_tool("export_composition", {"composition": "Escena", "copy_to": str(dest)})).structured_content
        denied = (await c.call_tool("export_composition", {"composition": comp["id"], "copy_to": "/etc"})).structured_content
        none = (await c.call_tool("export_composition", {"composition": "zzz", "copy_to": str(dest)})).structured_content
    assert r1["status"] == "ok" and r2["status"] == "ok" and r1["zip"] != r2["zip"]
    with zipfile.ZipFile(r1["zip"]) as zf:
        assert sorted(zf.namelist()) == ["background.png", "composite.png", "hermes.png", "index.html",
                                         "musa-scene.js", "scene.json"]
        assert json.loads(zf.read("scene.json"))["name"] == "Escena"
    assert denied["status"] == "error" and denied["code"] == "path_not_allowed"
    assert none["status"] == "error" and none["code"] == "unknown_composition"
    assert not list((settings.state_dir / "tmp_downloads").glob("*.zip"))


async def test_set_layer_animation_and_export_webp(comp_server, settings):
    from PIL import Image
    server, backend = comp_server
    dest = settings.output_root / "export"
    async with Client(server) as c:
        comp = (await c.call_tool("generate_composition", {
            "prompt": "x", "pieces": ["Hermes", "Tarro"], "name": "Escena"})).structured_content["composition"]
        assert [ly["animation"] for ly in comp["layers"]] == [{"type": "static"}] * 3
        calls = len(backend.calls)
        ok = (await c.call_tool("set_layer_animation", {
            "composition": "Escena", "layer": "Hermes", "animation": {"type": "motion", "preset": "bob"}})).structured_content
        assert ok["status"] == "ok"
        assert ok["composition"]["layers"][1]["animation"] == {
            "type": "motion", "preset": "bob", "amplitude": 6, "period": 2.0, "phase": 0}
        lid = comp["layers"][2]["id"]
        fall = (await c.call_tool("set_layer_animation", {
            "composition": comp["id"], "layer": lid,
            "animation": {"type": "motion", "preset": "fall", "copies": 2}})).structured_content
        assert fall["composition"]["layers"][2]["animation"]["copies"] == 2
        cases = [({"type": "frames", "image": "g_x#0"}, "invalid_composition"), ({"type": "states"}, "invalid_composition"),
                 ({"type": "motion", "preset": "nope"}, "invalid_composition"), ({}, "invalid_composition")]
        for anim, code in cases:
            r = (await c.call_tool("set_layer_animation", {"composition": "Escena", "layer": "Hermes", "animation": anim})).structured_content
            assert r["status"] == "error" and r["code"] == code, anim
        bg = (await c.call_tool("set_layer_animation", {
            "composition": "Escena", "layer": "Fondo", "animation": {"type": "motion", "preset": "bob"}})).structured_content
        assert bg["code"] == "invalid_composition"
        nolayer = (await c.call_tool("set_layer_animation", {
            "composition": "Escena", "layer": "Nadie", "animation": {"type": "static"}})).structured_content
        assert nolayer["code"] == "unknown_composition"
        nocomp = (await c.call_tool("set_layer_animation", {
            "composition": "zzz", "layer": "Hermes", "animation": {"type": "static"}})).structured_content
        assert nocomp["code"] == "unknown_composition"
        assert len(backend.calls) == calls   # no quota spent
        got = (await c.call_tool("get_composition", {"composition": "Escena"})).structured_content["composition"]
        assert got["layers"][1]["animation"]["preset"] == "bob"
        lst = (await c.call_tool("list_compositions", {})).structured_content["compositions"][0]
        assert [ly["animation"]["type"] for ly in lst["layers"]] == ["static", "motion", "motion"]
        plain = (await c.call_tool("export_composition", {"composition": "Escena", "copy_to": str(dest)})).structured_content
        full = (await c.call_tool("export_composition", {
            "composition": "Escena", "copy_to": str(dest), "webp": True})).structured_content
    assert plain["status"] == "ok" and "webp" not in plain
    assert full["status"] == "ok" and Path(full["webp"]).name == "scene-escena.webp"
    with Image.open(full["webp"]) as im:
        assert 1 < im.n_frames <= 72 and im.width <= 800
    assert not list((settings.state_dir / "tmp_downloads").glob("*"))


async def test_mcp_composition_public_shape_has_no_internal_fields(comp_server):
    server, backend = comp_server
    async with Client(server) as c:
        gen = (await c.call_tool("generate_composition", {"prompt": "x", "pieces": ["Hermes"], "name": "E"})).structured_content
        got = (await c.call_tool("get_composition", {"composition": "E"})).structured_content["composition"]
        setd = (await c.call_tool("set_layer_animation", {
            "composition": "E", "layer": "Hermes", "animation": {"type": "static"}})).structured_content["composition"]
    for comp in (gen["composition"], got, setd):
        assert "owner_pid" not in comp and "master_exists" not in comp
        assert all("source_exists" not in ly for ly in comp["layers"])
    assert got["width"] == 800 and got["layers"][1]["animation"] == {"type": "static"}
