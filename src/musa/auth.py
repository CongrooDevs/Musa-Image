import inspect
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import Settings
from .errors import AUTH_EXPIRED, AUTH_FAILED, GIAError


def read_cookie_jar(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GIAError(AUTH_EXPIRED, f"cookie file is corrupt: {e}", hint="run: musa auth") from e
    if not isinstance(data, list) or not all(isinstance(c, dict) and "name" in c and "value" in c for c in data):
        raise GIAError(AUTH_EXPIRED, "cookie file is corrupt: expected a list of {name, value, domain}",
                       hint="run: musa auth")
    return data


def save_cookie_jar_atomic(jar: list[dict], path: Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(jar, ensure_ascii=False))
    os.chmod(tmp, 0o600)  # in case a stale tmp existed with looser perms
    os.replace(tmp, p)


def _cookies_dict(jar: list[dict]) -> dict:
    return {c["name"]: c["value"] for c in jar if c.get("domain") == ".google.com"}


def _available_model_names(models) -> set[str]:
    names = set()
    for m in models:
        if isinstance(m, dict):
            name, avail = m.get("model_name", ""), m.get("is_available", True)
        else:
            name, avail = getattr(m, "model_name", str(m)), getattr(m, "is_available", True)
        if avail:
            names.add(name)
    return names


async def ensure_client(settings: Settings, client_cls, auto_close: bool = False):
    jar = read_cookie_jar(settings.cookie_file)
    cookies = _cookies_dict(jar)
    if not cookies.get("__Secure-1PSID"):
        raise GIAError(AUTH_EXPIRED, "no saved cookies found", hint="run: musa auth")
    if not cookies.get("__Secure-1PSIDTS") and settings.upstream_cache_dir.exists():
        cache_files = list(settings.upstream_cache_dir.glob("*.json")) + list(settings.upstream_cache_dir.glob(".*.json"))
        for cache_file in cache_files:
            try:
                cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached_data, list):
                    for c in cached_data:
                        if isinstance(c, dict) and c.get("name") == "__Secure-1PSIDTS" and c.get("value"):
                            cookies["__Secure-1PSIDTS"] = c["value"]
                            break
            except Exception:
                pass
    client = client_cls(
        secure_1psid=cookies["__Secure-1PSID"],
        secure_1psidts=cookies.get("__Secure-1PSIDTS"),
        cache_dir=settings.upstream_cache_dir,
    )
    await client.init(timeout=30, auto_close=auto_close, close_delay=900, auto_refresh=True)
    res = client.list_models()  # sync in the upstream client; tolerate awaitables
    if inspect.isawaitable(res):
        res = await res
    names = _available_model_names(res or [])
    if names == {"gemini-flash-lite"} or len(names) < 2:
        try:
            await client.close()
        except Exception:
            pass
        raise GIAError(
            AUTH_EXPIRED,
            "session is in guest mode (only flash-lite available)",
            hint="run: musa auth",
        )
    return client


LOGIN_TIMEOUT_S = 300
LOGIN_POLL_S = 2.0
GEMINI_URL = "https://gemini.google.com/app"
FIREFOX_CANDIDATES = ("/Applications/Firefox.app/Contents/MacOS/firefox", "firefox")


def find_firefox(settings: Settings) -> str:
    for candidate in ([settings.firefox_path] if settings.firefox_path else FIREFOX_CANDIDATES):
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    raise GIAError(AUTH_FAILED, "Firefox not found", hint="install Firefox or set GIA_FIREFOX_PATH")


def read_firefox_cookies(profile_dir: Path) -> list[dict]:
    """Read .google.com cookies from a Firefox profile, also while Firefox is running.

    Firefox keeps cookies.sqlite in WAL mode and locks it; copying the db plus its -wal file and
    opening the copy yields the current state without touching the live profile.
    """
    db = Path(profile_dir) / "cookies.sqlite"
    if not db.exists():
        return []
    with tempfile.TemporaryDirectory() as td:
        for suffix in ("", "-wal"):
            src = Path(f"{db}{suffix}")
            if src.exists():
                shutil.copyfile(src, Path(td) / f"cookies.sqlite{suffix}")
        con = sqlite3.connect(Path(td) / "cookies.sqlite")
        try:
            rows = con.execute(
                "SELECT name, value, host FROM moz_cookies WHERE host = '.google.com'").fetchall()
        except sqlite3.DatabaseError:
            return []  # profile still initialising
        finally:
            con.close()
    return [{"name": n, "value": v, "domain": h} for n, v, h in rows]


def _launch_firefox(firefox: str, profile_dir: Path) -> None:
    subprocess.Popen(
        [firefox, "-profile", str(profile_dir), "-no-remote", "-new-instance", GEMINI_URL],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def _close_firefox(profile_dir: Path) -> None:
    """Stop every Firefox bound to this profile. Firefox relaunches itself on a fresh profile, so
    the process we spawned exits immediately and the real browser is a detached child."""
    found = subprocess.run(["pgrep", "-f", "--", f"-profile {profile_dir}"], capture_output=True, text=True)
    for pid in found.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def _has_session(cookies: list[dict]) -> bool:
    return any(c["name"] == "__Secure-1PSID" for c in cookies)


def login_guided(settings: Settings, launcher=None, closer=None) -> list[dict]:
    """Guided login in the user's real Firefox with a dedicated profile under state_dir.

    Google refuses sign-in from automation-controlled browsers ("This browser or app may not be
    secure"), so Firefox is launched as a plain process and its cookies.sqlite is polled for
    __Secure-1PSID. The profile persists, so later logins are usually instant.
    stdio-safe: never reads stdin or writes stdout (MCP uses them for JSON-RPC).
    """
    settings.ensure_dirs()
    profile = settings.firefox_profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    (launcher or _launch_firefox)(find_firefox(settings), profile)
    print("Sign in to Gemini in the Firefox window; it closes by itself once the session "
          f"cookie is detected (timeout {LOGIN_TIMEOUT_S}s).", file=sys.stderr, flush=True)
    try:
        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while not _has_session(read_firefox_cookies(profile)):
            if time.monotonic() >= deadline:
                raise GIAError(AUTH_EXPIRED, "login timeout", hint="run: musa auth")
            time.sleep(LOGIN_POLL_S)
        time.sleep(LOGIN_POLL_S)  # let the companion cookies (__Secure-1PSIDTS, SIDCC...) land
        jar = read_firefox_cookies(profile)
    finally:
        (closer or _close_firefox)(profile)
    save_cookie_jar_atomic(jar, settings.cookie_file)
    return jar
