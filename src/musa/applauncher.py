"""Launcher for Musa, the local web app: `app.json` bookkeeping plus the uvicorn server.

`app.json` lives in the state dir and records the running instance ({url, port, pid, started}).
It is written atomically with 0600 and removed on exit only if it still holds our pid.
"""
import asyncio
import json
import os
import secrets
import socket
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from . import winproc
from .config import Settings
from .library import LibraryStore
from .service import ImageService
from .style_store import UserStyleStore
from .webapp import create_app


def app_info_path(settings: Settings) -> Path:
    return settings.state_dir / "app.json"


def running_app(settings: Settings) -> dict | None:
    """Parsed app.json if its pid is alive and its port accepts TCP; otherwise None."""
    try:
        info = json.loads(app_info_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    pid, port, url = info.get("pid"), info.get("port"), info.get("url")
    if not isinstance(pid, int) or not isinstance(port, int) or not isinstance(url, str) or not url:
        return None
    if os.name == "nt":  # os.kill(pid, 0) would send CTRL_C_EVENT there
        if not winproc.pid_alive(pid):
            return None
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            pass
    except OSError:
        return None
    return info


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # a TIME_WAIT port is free for us
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _write_app_info(path: Path, info: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os.replace(tmp, path)


def _remove_app_info(path: Path) -> None:
    """Remove app.json only if it still belongs to this process."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(info, dict):
        return
    if info.get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


async def serve_app(settings: Settings, service: ImageService, library: LibraryStore,
                    user_styles: UserStyleStore, *, port: int, open_browser: bool) -> None:
    """Serve Musa on 127.0.0.1:port until cancelled. stdout stays untouched (MCP stdio owns it)."""
    key = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{port}/?key={key}"
    app = create_app(settings=settings, service=service, library=library,
                     user_styles=user_styles, key=key, port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                            access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    info_path = app_info_path(settings)
    _write_app_info(info_path, {"url": url, "port": port, "pid": os.getpid(),
                                "started": datetime.now(timezone.utc).isoformat()})
    print(f"Musa: {url}", file=sys.stderr)  # stderr only: with the MCP, stdout is JSON-RPC
    if open_browser:
        webbrowser.open(url)
    try:
        await server.serve()
    finally:
        ctx = getattr(app.state, "ctx", None)
        if ctx is not None:
            await ctx.aclose()
        _remove_app_info(info_path)
