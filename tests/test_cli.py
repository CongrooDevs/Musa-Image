import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from musa import cli
from musa.client import Backend, GenerateResult
from musa.errors import GIAError
from musa.state import GenerationRecord, StateStore
from musa.storage import SavedImage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
runner = CliRunner()


class FakeBackend(Backend):
    def __init__(self):
        self.calls = []
        self.gems = {}
        self.gem_seq = 0

    async def create_gem(self, name, instructions, description=""):
        self.gem_seq += 1
        gid = f"gem_{self.gem_seq:03d}"
        self.gems[gid] = {"name": name, "instructions": instructions}
        return gid

    async def update_gem(self, gem_id, name, instructions, description=""):
        self.gems[gem_id] = {"name": name, "instructions": instructions}

    async def delete_gem(self, gem_id):
        self.gems.pop(gem_id, None)

    async def gem_exists(self, gem_id):
        return gem_id in self.gems

    async def status(self): return {"valid": True, "images_today": 3}
    async def list_models(self): return [{"model_name": "gemini-flash", "available": True}]

    async def generate(self, prompt, refs, *, model, **kw):
        self.calls.append(("generate", prompt))
        return GenerateResult("ok", None, "c_cli1", [SavedImage(Path("/tmp/z.png"), 64, 64, len(PNG), "png", True, PNG)],
                              "ok", 2.0, ["c", "r", "rc"], record_id="g_000000000001")

    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        return GenerateResult("ok", None, conversation_id, [], "edited", 1.0, ["c", "r", "rc"])

    async def retry_download(self, conversation_id, image_index):
        return SavedImage(Path("/tmp/z.png"), 64, 64, len(PNG), "png", True, None)

    async def delete_conversation(self, conversation_id): pass


def _styles(tmp_path, desc="d"):
    p = tmp_path / "styles.yaml"
    p.write_text(yaml.safe_dump({"styles": {"anime": {"description": desc, "append": "anime"}}, "templates": {}}))
    return p


def test_gen(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    settings.output_root = tmp_path / "generated"
    fb = FakeBackend()
    cli.set_backend(fb, settings)
    r = runner.invoke(cli.app, ["gen", "a cat", "--style", "anime"])
    assert r.exit_code == 0, r.output
    assert "c_cli1" in r.output and "/tmp/z.png" in r.output
    assert "anime" in fb.calls[0][1]


def test_styles(settings, tmp_path):
    settings.styles_file = _styles(tmp_path, "anime style")
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["styles"])
    assert r.exit_code == 0 and "anime" in r.output


def test_auth_status(settings):
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["auth", "--status"])
    assert r.exit_code == 0 and "images_today" in r.output


def test_mcp_config():
    r = runner.invoke(cli.app, ["mcp-config"])
    assert r.exit_code == 0
    cfg = json.loads(r.output)
    assert "mcpServers" in cfg and "musa" in cfg["mcpServers"]


def test_edit(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["edit", "c_cli1", "add glasses"])
    assert r.exit_code == 0 and "c_cli1" in r.output


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    cli.set_backend(None, None)


def test_gen_error_result_exits_1(settings):
    class Err(FakeBackend):
        async def generate(self, prompt, refs, *, model, **kw):
            return GenerateResult("error", "no_image", None, [], "", 0.5, None)
    cli.set_backend(Err(), settings)
    r = runner.invoke(cli.app, ["gen", "a cat"])
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["status"] == "error" and out["code"] == "no_image"


def test_gen_giaerror_with_path_details(settings):
    class Boom(FakeBackend):
        async def generate(self, prompt, refs, *, model, **kw):
            raise GIAError("ref_not_allowed", "nope", allowed=[Path("/x")])
    cli.set_backend(Boom(), settings)
    r = runner.invoke(cli.app, ["gen", "a cat"])
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["code"] == "ref_not_allowed" and out["details"]["allowed"] == ["/x"]


def test_gen_without_style_ignores_broken_styles_file(settings, tmp_path):
    settings.styles_file = tmp_path / "missing.yaml"
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["gen", "a cat"])
    assert r.exit_code == 0, r.output


def test_each_command_closes_backend(settings):
    fb = FakeBackend(); closed = []
    async def aclose(): closed.append(True)
    fb.aclose = aclose
    cli.set_backend(fb, settings)
    runner.invoke(cli.app, ["auth", "--status"])
    assert closed == [True]


def test_history_lists_generations(settings):
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["history", "--limit", "5"])
    assert r.exit_code == 0 and json.loads(r.output) == []


def test_gen_copy_to_outside_is_path_not_allowed(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["gen", "x", "--copy-to", "/etc/"])
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "path_not_allowed"


def test_mcp_config_from_checkout_uses_project_and_absolute_uv(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda n: f"/abs/{n}")
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "musa"\n')
    cfg = cli.mcp_config_dict(repo)["mcpServers"]["musa"]
    assert cfg == {"command": "/abs/uv", "args": ["run", "--project", str(repo), "musa-mcp"]}


def test_mcp_config_installed_uses_musa_mcp(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda n: f"/abs/{n}")
    assert cli.mcp_config_dict(None)["mcpServers"]["musa"] == {"command": "/abs/musa-mcp"}


def _fake_checkout(tmp_path, pkg_parent="src", pyproject='[project]\nname = "musa"\n'):
    repo = tmp_path / "repo"
    pkg = repo / pkg_parent / "musa"
    pkg.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(pyproject)
    return repo, pkg / "cli.py"


def test_checkout_root_matches_src_layout_and_project_name(tmp_path):
    repo, f = _fake_checkout(tmp_path)
    assert cli._checkout_root(f) == repo.resolve()


def test_checkout_root_rejects_non_src_layout(tmp_path):
    _, f = _fake_checkout(tmp_path, pkg_parent="site-packages")
    assert cli._checkout_root(f) is None


def test_checkout_root_compares_project_name_exactly(tmp_path):
    # the old substring check accepted this: the string appears, but not as [project].name
    _, f = _fake_checkout(tmp_path, pyproject='[project]\nname = "other"\n[tool.x]\nname = "musa"\n')
    assert cli._checkout_root(f) is None
    _, f2 = _fake_checkout(tmp_path / "b", pyproject='[project]\nname = "musa-fork"\n')
    assert cli._checkout_root(f2) is None


def test_checkout_root_tolerates_non_table_project_and_bad_encoding(tmp_path):
    _, f = _fake_checkout(tmp_path / "a", pyproject='project = "x"\n')
    assert cli._checkout_root(f) is None
    repo, f2 = _fake_checkout(tmp_path / "b")
    (repo / "pyproject.toml").write_bytes(b'[project]\nname = "musa"\n# \xff\xfe\n')
    assert cli._checkout_root(f2) is None


def test_run_forgets_lazily_built_backend_and_recreates_on_demand(settings, monkeypatch):
    built, closed = [], []

    class B(FakeBackend):
        async def aclose(self):
            closed.append(self)

    def fake_build(s):
        built.append(B())
        return built[-1]
    monkeypatch.setattr(cli, "build_backend", fake_build)
    cli.set_backend(None, settings)
    cli._bs()

    async def noop():
        return 1
    assert cli._run(noop()) == 1
    assert cli._backend is None and closed == built[:1]
    cli._bs()
    assert len(built) == 2 and cli._backend is built[1]


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


def test_collections_empty(settings):
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["collections"])
    assert r.exit_code == 0 and json.loads(r.output) == []


def test_prompts_add_list_edit_use_rm(settings):
    cli.set_backend(FakeBackend(), settings)
    assert json.loads(runner.invoke(cli.app, ["prompts"]).output) == []
    r = runner.invoke(cli.app, ["prompts", "add", "Gato", "a cat"])
    assert r.exit_code == 0
    pid = json.loads(r.output)["id"]
    dup = runner.invoke(cli.app, ["prompts", "add", "gato", "x"])
    assert dup.exit_code == 1 and json.loads(dup.output)["code"] == "invalid_prompt"
    r = runner.invoke(cli.app, ["prompts", "edit", "GATO", "--text", "a big cat"])
    assert r.exit_code == 0 and json.loads(r.output)["text"] == "a big cat"
    r = runner.invoke(cli.app, ["prompts", "edit", pid, "--name", "Felino"])
    assert json.loads(r.output)["name"] == "Felino"
    r = runner.invoke(cli.app, ["prompts", "use", "felino"])
    assert r.exit_code == 0 and r.output == "a big cat\n"
    assert json.loads(runner.invoke(cli.app, ["prompts"]).output)[0]["uses"] == 1
    r = runner.invoke(cli.app, ["prompts", "rm", "Felino"])
    assert r.exit_code == 0 and json.loads(r.output) == {"deleted": pid}
    for args in (["rm", "Felino"], ["use", "Felino"], ["edit", "Felino", "--text", "x"]):
        r = runner.invoke(cli.app, ["prompts", *args])
        assert r.exit_code == 1 and json.loads(r.output)["code"] == "unknown_prompt"


def test_prompts_use_error_goes_to_stderr_only(settings):
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["prompts", "use", "nope"])
    assert r.exit_code == 1 and r.stdout == ""
    assert json.loads(r.stderr)["code"] == "unknown_prompt"


def test_prompts_text_roundtrip_and_dash_text(settings):
    cli.set_backend(FakeBackend(), settings)
    txt = "line1\nline2 {x} %s"
    assert runner.invoke(cli.app, ["prompts", "add", "n", txt]).exit_code == 0
    r = runner.invoke(cli.app, ["prompts", "use", "n"])
    assert r.stdout == txt + "\n"
    r = runner.invoke(cli.app, ["prompts", "add", "--", "d", "-v text"])
    assert r.exit_code == 0 and json.loads(r.output)["text"] == "-v text"


def test_collections_add(settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["collections", "add", "gen:g_000000000001", "X"])
    assert r.exit_code == 0
    assert json.loads(r.output)["collection"]["name"] == "X"


def test_collections_add_unknown_image_fails(settings):
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["collections", "add", "gen:g_000000000001", "X"])
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "invalid_ref"


def test_gen_with_collection(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    settings.output_root = tmp_path / "generated"
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["gen", "x", "--collection", "X"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["collection"]["name"] == "X"


def test_history_collection_filter(settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["collections", "add", "gen:g_000000000001", "X"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["history", "--collection", "X"])
    assert r.exit_code == 0 and [g["id"] for g in json.loads(r.output)] == ["g_000000000001"]
    r = runner.invoke(cli.app, ["history"])
    assert len(json.loads(r.output)) == 1


def test_history_model_filter(settings):
    cli.set_backend(FakeBackend(), settings)
    st = StateStore(settings.state_dir)
    for i, m in enumerate(("m-a", "m-b")):
        st.append(GenerationRecord(
            id=f"g_00000000000{i}", ts=f"2026-11-10T00:00:0{i}+00:00", type="generate", conversation_id="c",
            chat_metadata=["c"], prompt="p", style=None, style_text=None, aspect_hint=None, model=m,
            project=None, subdir=None, refs=[], images=[], status="ok", code=None, gemini_text="",
            elapsed_s=1.0, parent_id=None))
    r = runner.invoke(cli.app, ["history", "--model", "m-b"])
    assert r.exit_code == 0 and [g["id"] for g in json.loads(r.output)] == ["g_000000000001"]


def test_app_already_running_exits_0_without_serving(settings, monkeypatch):
    cli.set_backend(FakeBackend(), settings)
    monkeypatch.setattr(cli, "running_app", lambda s: {"url": "http://127.0.0.1:8765/?key=k"})
    def boom(*a, **kw):
        raise AssertionError("serve_app must not be called")
    monkeypatch.setattr(cli, "serve_app", boom)
    r = runner.invoke(cli.app, ["app", "--no-open"])
    assert r.exit_code == 0 and "already running" in r.output and "http://127.0.0.1:8765/?key=k" in r.output


def test_app_port_busy_exits_1_with_port_busy(settings, monkeypatch):
    cli.set_backend(FakeBackend(), settings)
    monkeypatch.setattr(cli, "running_app", lambda s: None)
    monkeypatch.setattr(cli, "port_free", lambda p: False)
    r = runner.invoke(cli.app, ["app", "--no-open"])
    assert r.exit_code == 1
    out = json.loads(r.output)
    assert out == {"status": "error", "code": "port_busy", "message": f"port {settings.app_port} is busy",
                   "next_action": "use --port"}


def test_app_uses_settings_app_port(settings, monkeypatch):
    cli.set_backend(FakeBackend(), settings)
    monkeypatch.setattr(cli, "running_app", lambda s: None)
    monkeypatch.setattr(cli, "port_free", lambda p: True)
    seen = {}
    async def fake_serve(s, service, library, user_styles, *, port, open_browser):
        seen["port"] = port
        seen["open_browser"] = open_browser
    monkeypatch.setattr(cli, "serve_app", fake_serve)
    r = runner.invoke(cli.app, ["app", "--port", "9999", "--no-open"])
    assert r.exit_code == 0, r.output
    assert seen == {"port": 9999, "open_browser": False}


def test_styles_gems_empty(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["styles", "gems"])
    assert r.exit_code == 0 and json.loads(r.output) == []


def test_styles_gem_on_off_and_instructions(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    fb = FakeBackend()
    cli.set_backend(fb, settings)
    r = runner.invoke(cli.app, ["styles", "gem", "anime", "on"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["style"] == "anime" and out["gem"]["custom"] is False
    assert out["gem"]["gem_id"] in fb.gems
    r = runner.invoke(cli.app, ["styles", "gems"])
    assert json.loads(r.output) == [{"style": "anime", "gem_id": out["gem"]["gem_id"],
                                     "name": out["gem"]["name"], "custom": False}]
    r = runner.invoke(cli.app, ["styles", "gem", "anime", "on", "--instructions", "always ink"])
    assert r.exit_code == 0 and json.loads(r.output)["gem"]["custom"] is True
    gid = json.loads(r.output)["gem"]["gem_id"]
    assert fb.gems[gid]["instructions"] == "always ink"
    r = runner.invoke(cli.app, ["styles", "gem", "anime", "off"])
    assert r.exit_code == 0 and json.loads(r.output)["gem"] is None
    assert fb.gems == {}


def test_styles_gem_bad_action_rejected(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["styles", "gem", "anime", "maybe"])
    assert r.exit_code != 0


def test_styles_gem_unknown_style_fails_with_payload(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["styles", "gem", "nope", "on"])
    assert r.exit_code == 1
    out = json.loads(r.output)
    assert out["status"] == "error" and out["code"] == "unknown_style" and "next_action" in out



def test_styles_add_show_edit_rm(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    img = tmp_path / "generated" / "c.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(PNG)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["styles", "add", "Neón", "--instructions", "neon glow", "--image", str(img)])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["id"] == "neon" and out["has_image"] is True
    r = runner.invoke(cli.app, ["styles", "show", "neon", "--prompt", "a cat"])
    assert r.exit_code == 0 and "neon glow" in json.loads(r.output)["final_prompt"]
    r = runner.invoke(cli.app, ["styles", "edit", "neon", "--instructions", "teal glow", "--remove-image"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["instructions"] == "teal glow" and json.loads(r.output)["has_image"] is False
    r = runner.invoke(cli.app, ["styles", "rm", "neon"])
    assert r.exit_code == 0 and json.loads(r.output) == {"deleted": "neon"}
    r = runner.invoke(cli.app, ["styles", "show", "neon"])
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "unknown_style"


def test_styles_builtin_rejected(settings, tmp_path):
    settings.styles_file = _styles(tmp_path)
    cli.set_backend(FakeBackend(), settings)
    for args in (["styles", "edit", "anime", "--name", "x"], ["styles", "rm", "anime"]):
        r = runner.invoke(cli.app, args)
        assert r.exit_code == 1 and json.loads(r.output)["code"] == "invalid_style"


def test_collections_rename_remove_rm(settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    cli.set_backend(FakeBackend(), settings)
    runner.invoke(cli.app, ["collections", "add", "gen:g_000000000001", "X"])
    r = runner.invoke(cli.app, ["collections", "rename", "X", "Y"])
    assert r.exit_code == 0 and json.loads(r.output)["name"] == "Y"
    r = runner.invoke(cli.app, ["collections", "remove", "gen:g_000000000001", "Y"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["collections", "rm", "Y"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["collections", "rm", "Y"])
    assert r.exit_code == 1 and json.loads(r.output)["status"] == "error"


def test_fav_and_rm_image(settings, tmp_path):
    _seed_saved_image(settings, tmp_path)
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["fav", "gen:g_000000000001"])
    assert r.exit_code == 0 and json.loads(r.output)["favorite"] is True
    r = runner.invoke(cli.app, ["fav", "gen:g_000000000001", "--off"])
    assert json.loads(r.output)["favorite"] is False
    r = runner.invoke(cli.app, ["rm", "gen:g_000000000001"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "generated" / "seed.png").exists()
    r = runner.invoke(cli.app, ["rm", "gen:g_000000000001"])
    assert r.exit_code == 1 and json.loads(r.output)["status"] == "error"


def test_styles_edit_validates_style_before_resolving_image(settings, tmp_path, monkeypatch):
    import musa.mcp_server as ms
    settings.styles_file = _styles(tmp_path)
    called = []

    async def fake_resolve(svc, image):
        called.append(image)
        return PNG, "x.png"
    monkeypatch.setattr(ms, "resolve_style_image", fake_resolve)
    cli.set_backend(FakeBackend(), settings)
    for sid, code in (("nope", "unknown_style"), ("anime", "invalid_style")):
        r = runner.invoke(cli.app, ["styles", "edit", sid, "--image", "https://example.com/a.png"])
        assert r.exit_code == 1 and json.loads(r.output)["code"] == code
    assert called == []


def test_auth_chatgpt_runs_the_chrome_login(settings, monkeypatch):
    calls = []
    monkeypatch.setattr("musa.auth.login_chatgpt", lambda s: calls.append(s))
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["auth", "--chatgpt"])
    assert r.exit_code == 0 and calls == [settings] and "ChatGPT" in r.output


def test_auth_chatgpt_status_prints_only_the_chatgpt_part(settings):
    class WithChatGPT(FakeBackend):
        async def status(self):
            return {"valid": True, "chatgpt": {"session": True, "image_gen": {"remaining": 2}}}
    cli.set_backend(WithChatGPT(), settings)
    r = runner.invoke(cli.app, ["auth", "--status", "--chatgpt"])
    assert r.exit_code == 0 and json.loads(r.output) == {"session": True, "image_gen": {"remaining": 2}}
