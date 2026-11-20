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

from filelock import FileLock, Timeout as LockTimeout

from . import winproc
from .config import Settings
from .errors import AUTH_EXPIRED, AUTH_FAILED, TEMPORARILY_BLOCKED, GIAError


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


def _rotated_psidts(settings: Settings, psid: str) -> str | None:
    """__Secure-1PSIDTS rotated in the background for THIS session (the cache file is keyed by
    the PSID value; files left over from another account or an older login are ignored)."""
    cache_file = settings.upstream_cache_dir / f".cached_cookies_{psid}.json"
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, list):
        for c in data:
            if isinstance(c, dict) and c.get("name") == "__Secure-1PSIDTS" and c.get("value"):
                return c["value"]
    return None


def clear_rotated_cookies(settings: Settings) -> None:
    if settings.upstream_cache_dir.exists():
        for f in settings.upstream_cache_dir.iterdir():
            if f.is_file() and f.suffix in (".json", ".tmp"):
                f.unlink(missing_ok=True)


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
    rotated = _rotated_psidts(settings, cookies["__Secure-1PSID"])
    if rotated:  # the background rotation is newer than whatever the login saved
        cookies["__Secure-1PSIDTS"] = rotated
    client = client_cls(
        secure_1psid=cookies["__Secure-1PSID"],
        secure_1psidts=cookies.get("__Secure-1PSIDTS"),
        cache_dir=settings.upstream_cache_dir,
        cookie_jar=[c for c in jar if c.get("domain") in COOKIE_HOSTS],
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
            "Google treats the session as signed out (no account models)",
            hint="run: musa auth, sign in and wait until Gemini loads",
        )
    return client


LOGIN_TIMEOUT_S = 300
LOGIN_POLL_S = 2.0
GEMINI_URL = "https://gemini.google.com/app"
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
FIREFOX_CANDIDATES = ("/Applications/Firefox.app/Contents/MacOS/firefox", "firefox",
                      r"C:\Program Files\Mozilla Firefox\firefox.exe",
                      r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
                      *([str(Path(_LOCALAPPDATA) / "Mozilla Firefox" / "firefox.exe")] if _LOCALAPPDATA else []))


def find_firefox(settings: Settings) -> str:
    for candidate in ([settings.firefox_path] if settings.firefox_path else FIREFOX_CANDIDATES):
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    raise GIAError(AUTH_FAILED, "Firefox not found", hint="install Firefox or set GIA_FIREFOX_PATH")


# gemini.google.com holds host-only OSID/__Secure-OSID: without them Gemini answers every
# signed-in batchexecute call (models, quota) with HTTP 400.
COOKIE_HOSTS = (".google.com", "gemini.google.com")


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
                "SELECT name, value, host FROM moz_cookies WHERE host IN (?, ?)", COOKIE_HOSTS).fetchall()
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
    if os.name == "nt":
        for pid in winproc.find_pids("firefox.exe", str(profile_dir)):
            winproc.terminate(pid, force=True)  # cookies were already read; no session-restore prompt matters
        return
    found = subprocess.run(["pgrep", "-f", "--", f"-profile {profile_dir}"], capture_output=True, text=True)
    for pid in found.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


LOGIN_SETTLE_S = 10.0  # extra wait once a NEW __Secure-1PSID shows up without __Secure-1PSIDTS


def _cookie(cookies: list[dict], name: str) -> str | None:
    return next((c["value"] for c in cookies if c["name"] == name), None)


def _has_session(cookies: list[dict]) -> bool:
    """A finished Google sign-in: __Secure-1PSID plus __Secure-1PSIDTS. With only __Secure-1PSID
    the redirect chain may still be running, and Google treats the session as signed out."""
    return bool(_cookie(cookies, "__Secure-1PSID") and _cookie(cookies, "__Secure-1PSIDTS"))


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
    # A stale __Secure-1PSID from an earlier (or half-finished) login must not end the wait at once.
    old_psid = _cookie(read_firefox_cookies(profile), "__Secure-1PSID")
    (launcher or _launch_firefox)(find_firefox(settings), profile)
    print("Sign in to Gemini in the Firefox window and wait until Gemini loads; it closes by itself "
          f"once the session is complete (timeout {LOGIN_TIMEOUT_S}s).", file=sys.stderr, flush=True)
    try:
        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        new_psid_since = None
        while True:
            jar = read_firefox_cookies(profile)
            if _has_session(jar):
                break
            psid = _cookie(jar, "__Secure-1PSID")
            if psid and psid != old_psid:
                new_psid_since = new_psid_since or time.monotonic()
                if time.monotonic() - new_psid_since >= LOGIN_SETTLE_S:
                    break  # fresh sign-in; Google did not issue __Secure-1PSIDTS to this browser
            if time.monotonic() >= deadline:
                raise GIAError(AUTH_EXPIRED, "login timeout: the Google session never completed",
                               hint="run: musa auth, sign in and wait until Gemini loads")
            time.sleep(LOGIN_POLL_S)
        time.sleep(LOGIN_POLL_S)  # let the companion cookies (SIDCC...) land
        jar = read_firefox_cookies(profile)
    finally:
        (closer or _close_firefox)(profile)
    clear_rotated_cookies(settings)  # rotations of the previous session are now stale
    save_cookie_jar_atomic(jar, settings.cookie_file)
    return jar


# -- ChatGPT (web) -------------------------------------------------------------
CHROME_CANDIDATES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "google-chrome", "google-chrome-stable",
                     r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                     *([str(Path(_LOCALAPPDATA) / "Google" / "Chrome" / "Application" / "chrome.exe")]
                       if _LOCALAPPDATA else []))
CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login"
CHATGPT_SESSION_COOKIE = "__Secure-next-auth.session-token"
CHROME_CLOSE_TIMEOUT_S = 10.0


def find_chrome(settings: Settings) -> str:
    for candidate in ([settings.chrome_path] if settings.chrome_path else CHROME_CANDIDATES):
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    raise GIAError(AUTH_FAILED, "Google Chrome not found", hint="install Google Chrome or set GIA_CHROME_PATH")


def chatgpt_session_present(profile_dir: Path) -> bool:
    """True when the profile's cookie store holds a ChatGPT session cookie (name only: values are encrypted).

    Chrome may hold the live db open, so the db and its journal are copied and the copy is read.
    Chrome on Windows keeps it in Default/Network/Cookies and locks it while running (the copy fails until exit)."""
    db = Path(profile_dir) / "Default" / "Network" / "Cookies"
    if not db.exists():
        db = Path(profile_dir) / "Default" / "Cookies"
    if not db.exists():
        return False
    with tempfile.TemporaryDirectory() as td:
        try:
            for suffix in ("", "-journal", "-wal"):
                src = Path(f"{db}{suffix}")
                try:
                    shutil.copyfile(src, Path(td) / f"Cookies{suffix}")
                except FileNotFoundError:
                    if not suffix:
                        return False
        except OSError:
            return False
        con = sqlite3.connect(Path(td) / "Cookies")
        try:
            row = con.execute("SELECT 1 FROM cookies WHERE host_key LIKE '%chatgpt.com' AND name LIKE ? LIMIT 1",
                              (CHATGPT_SESSION_COOKIE + "%",)).fetchone()
        except sqlite3.DatabaseError:
            return False  # profile still initialising
        finally:
            con.close()
    return row is not None


def _launch_chrome(chrome: str, profile_dir: Path) -> None:
    """Plain Chrome process: no automation switches and no debugging port, so sign-in works as usual."""
    subprocess.Popen(
        [chrome, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check", CHATGPT_LOGIN_URL],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def _profile_pids(profile_dir: Path) -> list[int]:
    if os.name == "nt":
        return winproc.find_pids("chrome.exe", str(profile_dir))
    found = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={profile_dir}"], capture_output=True, text=True)
    return [int(p) for p in found.stdout.split()]


def _close_chrome(profile_dir: Path, timeout_s: float = CHROME_CLOSE_TIMEOUT_S) -> None:
    """SIGTERM every Chrome process bound to this profile and wait until all exit (SIGKILL after timeout_s),
    so the cookie store is flushed before anyone reads it or opens the profile again."""
    def signal_all(force: bool) -> None:
        for pid in _profile_pids(profile_dir):
            if os.name == "nt":  # no SIGKILL there; taskkill without /F asks Chrome to close cleanly
                winproc.terminate(pid, force=force)
                continue
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal_all(force=False)
    deadline = time.monotonic() + timeout_s
    while _profile_pids(profile_dir):
        if time.monotonic() >= deadline:
            signal_all(force=True)
            return
        time.sleep(0.2)


def login_chatgpt(settings: Settings, launcher=None, closer=None, session_check=None) -> None:
    """Guided ChatGPT sign-in in a plain Google Chrome on the dedicated profile Musa later drives headless.

    Holds the profile lock (never waits for it) so no headless Chrome opens the profile meanwhile; confirms the
    session cookie again after Chrome has exited. stdio-safe: never reads stdin or writes stdout."""
    settings.ensure_dirs()
    profile = settings.chatgpt_profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    check = session_check or chatgpt_session_present
    lock = FileLock(str(settings.chatgpt_lock_file))
    try:
        lock.acquire(timeout=0)
    except LockTimeout:
        raise GIAError(TEMPORARILY_BLOCKED, "the ChatGPT Chrome profile is in use by another Musa process",
                       hint="retry when the current ChatGPT job ends") from None
    try:
        (launcher or _launch_chrome)(find_chrome(settings), profile)
        # Windows: Chrome locks its cookie db while open, so the session shows up once the user closes it.
        wait_for_exit = os.name == "nt"
        if wait_for_exit:
            print("Sign in to ChatGPT in the Chrome window, then close Chrome "
                  f"(timeout {LOGIN_TIMEOUT_S}s).", file=sys.stderr, flush=True)
        else:
            print("Sign in to ChatGPT in the Chrome window; it closes by itself once the session cookie is "
                  f"detected (timeout {LOGIN_TIMEOUT_S}s).", file=sys.stderr, flush=True)
        try:
            deadline = time.monotonic() + LOGIN_TIMEOUT_S
            chrome_seen = False
            while not check(profile):
                if time.monotonic() >= deadline:
                    raise GIAError(AUTH_EXPIRED, "login timeout", hint="run: musa auth --chatgpt")
                if wait_for_exit:
                    running = bool(_profile_pids(profile))
                    if chrome_seen and not running:
                        break  # closed by the user: confirmed below
                    chrome_seen = chrome_seen or running
                time.sleep(LOGIN_POLL_S)
        finally:
            (closer or _close_chrome)(profile)
        if not check(profile):
            raise GIAError(AUTH_EXPIRED, "ChatGPT session cookie not found after Chrome closed",
                           hint="run: musa auth --chatgpt")
    finally:
        lock.release()
