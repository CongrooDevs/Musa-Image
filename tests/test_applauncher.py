import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import uvicorn

from gemini_image_api.applauncher import app_info_path, port_free, running_app, serve_app
from gemini_image_api.client import Backend
from gemini_image_api.library import LibraryStore
from gemini_image_api.service import ImageService
from gemini_image_api.state import StateStore
from gemini_image_api.style_store import UserStyleStore

REPO = Path(__file__).resolve().parents[1]


class FakeBackend(Backend):
    async def status(self): return {"valid": True}
    async def list_models(self): return []
    async def generate(self, prompt, refs, *, model, **kw): raise NotImplementedError
    async def edit(self, conversation_id, prompt, refs, *, model, **kw): raise NotImplementedError
    async def retry_download(self, conversation_id, image_index): raise NotImplementedError
    async def delete_conversation(self, conversation_id): pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_info(settings, **kw) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    app_info_path(settings).write_text(json.dumps(kw))


def _service(settings) -> ImageService:
    return ImageService(FakeBackend(), settings, StateStore(settings.state_dir),
                        LibraryStore(settings.state_dir), UserStyleStore(settings.state_dir))


def test_running_app_none_without_file(settings):
    assert running_app(settings) is None


def test_running_app_none_with_dead_pid(settings):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    _write_info(settings, url="http://127.0.0.1:1/?key=k", port=_free_port(), pid=p.pid, started="t")
    assert running_app(settings) is None


def test_running_app_none_with_unreadable_file(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    app_info_path(settings).write_text("{not json")
    assert running_app(settings) is None


@pytest.mark.parametrize("raw", ["[]", "null", '"x"'])
def test_running_app_none_when_app_json_is_not_an_object(settings, raw):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    app_info_path(settings).write_text(raw)
    assert running_app(settings) is None


def test_running_app_returns_info_with_live_pid_and_socket(settings):
    port = _free_port()
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    try:
        _write_info(settings, url=f"http://127.0.0.1:{port}/?key=k", port=port, pid=os.getpid(), started="t")
        info = running_app(settings)
        assert info is not None and info["port"] == port and info["url"].endswith("?key=k")
    finally:
        srv.close()


def test_running_app_none_when_socket_closed(settings):
    port = _free_port()
    _write_info(settings, url=f"http://127.0.0.1:{port}/?key=k", port=port, pid=os.getpid(), started="t")
    assert running_app(settings) is None  # pid alive, but nothing listens on the port


def test_port_free():
    port = _free_port()
    assert port_free(port) is True
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    try:
        assert port_free(port) is False
        srv.close()
        # tras cerrar con conexiones pendientes queda en TIME_WAIT; con SO_REUSEADDR sigue libre
        assert port_free(port) is True
    finally:
        try:
            srv.close()
        except OSError:
            pass


async def test_serve_app_writes_app_json_0600_and_removes_it(settings, monkeypatch):
    seen = {}

    async def fake_serve(self):
        p = app_info_path(settings)
        seen["exists"] = p.exists()
        seen["mode"] = stat.S_IMODE(p.stat().st_mode)
        seen["data"] = json.loads(p.read_text())

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    svc = _service(settings)
    await serve_app(settings, svc, svc.library, svc.styles, port=_free_port(), open_browser=False)
    assert seen["exists"] is True
    assert seen["mode"] == 0o600
    assert seen["data"]["pid"] == os.getpid() and seen["data"]["url"].startswith("http://127.0.0.1:")
    assert not app_info_path(settings).exists()


async def test_serve_app_does_not_remove_foreign_app_json(settings, monkeypatch):
    async def fake_serve(self):
        # another process overwrote app.json while we were serving
        _write_info(settings, url="http://127.0.0.1:1/?key=other", port=1, pid=os.getpid() + 1, started="t")

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    svc = _service(settings)
    await serve_app(settings, svc, svc.library, svc.styles, port=_free_port(), open_browser=False)
    info = json.loads(app_info_path(settings).read_text())
    assert info["pid"] == os.getpid() + 1  # not our pid -> kept


def _initialize_line() -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }).encode() + b"\n"


def _readline_with_timeout(stream, timeout_s: float) -> bytes:
    ready, _, _ = select.select([stream], [], [], timeout_s)
    assert ready, "no stdout line within the deadline"
    return stream.readline()


def test_gia_mcp_with_app_keeps_stdout_pure_jsonrpc(tmp_path):
    """gia-mcp with GIA_APP_WITH_MCP=true answers initialize on stdout as pure JSON-RPC.

    Uses a temp GIA_STATE_DIR (so no real cookies are read) and a free port; no network:
    initialize never touches the backend. The subprocess is always killed (whole process
    group: `uv run` spawns the python child).
    """
    state = tmp_path / "state"
    env = {**os.environ,
           "GIA_STATE_DIR": str(state),
           "GIA_OUTPUT_ROOT": str(tmp_path / "out"),
           "GIA_STYLES_FILE": str(tmp_path / "none.yaml"),
           "GIA_APP_WITH_MCP": "true",
           "GIA_APP_PORT": str(_free_port())}
    proc = subprocess.Popen(["uv", "run", "gia-mcp"], cwd=REPO, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        proc.stdin.write(_initialize_line())
        proc.stdin.close()
        msg = json.loads(_readline_with_timeout(proc.stdout, 20))
        assert msg["jsonrpc"] == "2.0" and msg["id"] == 1 and "result" in msg  # stdout line 1 is pure JSON-RPC
        deadline = time.monotonic() + 10
        while not (state / "app.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (state / "app.json").exists()  # the co-run Musa registered itself
        info = json.loads((state / "app.json").read_text())
        assert info["url"].startswith("http://127.0.0.1:") and info["port"] == int(env["GIA_APP_PORT"])
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
