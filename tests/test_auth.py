import signal
import builtins
import sqlite3
import sys

from filelock import FileLock
import pytest

from musa import auth
from musa.auth import ensure_client, read_cookie_jar, save_cookie_jar_atomic
from musa.errors import GIAError

JAR = [{"name": "__Secure-1PSID", "value": "abc", "domain": ".google.com"},
       {"name": "SAPISID", "value": "x", "domain": ".google.com"},
       {"name": "OTRO", "value": "y", "domain": ".example.com"}]


def test_save_and_read_jar_atomic_0600(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    assert (settings.cookie_file.stat().st_mode & 0o777) == 0o600
    jar = read_cookie_jar(settings.cookie_file)
    assert {c["name"] for c in jar} == {"__Secure-1PSID", "SAPISID", "OTRO"}


def test_read_jar_empty(settings):
    assert read_cookie_jar(settings.cookie_file) == []


class FakeClient:
    # Mirrors real GeminiClient 2.1.1: only secure_1psid/secure_1psidts are read; list_models is sync.
    def __init__(self, secure_1psid=None, secure_1psidts=None, proxy=None, **kw):
        self.secure_1psid = secure_1psid
        self.secure_1psidts = secure_1psidts
        self.inited = False
        self.kw = {}
        self.kw_init = kw
    async def init(self, **kw):
        self.inited = True
        self.kw.update(kw)
    def list_models(self):
        return [{"model_name": "gemini-flash", "is_available": True},
                {"model_name": "gemini-pro", "is_available": True}]


class FakeClientGuest(FakeClient):
    def list_models(self): return [{"model_name": "gemini-flash-lite", "is_available": True}]


class FakeClientGuestMany(FakeClient):
    def list_models(self):
        return [{"model_name": "gemini-flash-lite", "is_available": True},
                {"model_name": "gemini-flash", "is_available": False},
                {"model_name": "gemini-pro", "is_available": False}]


class FakeClientAsyncModels(FakeClient):
    async def list_models(self): return FakeClient.list_models(self)


async def test_ensure_client_ok(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    c = await ensure_client(settings, client_cls=FakeClient)
    assert c.inited
    assert c.secure_1psid == "abc"


async def test_ensure_client_passes_psidts(settings):
    save_cookie_jar_atomic(JAR + [{"name": "__Secure-1PSIDTS", "value": "ts", "domain": ".google.com"}],
                           settings.cookie_file)
    c = await ensure_client(settings, client_cls=FakeClient)
    assert c.secure_1psidts == "ts"


async def test_ensure_client_async_list_models(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    assert (await ensure_client(settings, client_cls=FakeClientAsyncModels)).inited


async def test_ensure_client_guest_many_models_one_available(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    with pytest.raises(GIAError) as e:
        await ensure_client(settings, client_cls=FakeClientGuestMany)
    assert e.value.code == "auth_expired"


def test_read_jar_corrupt(settings):
    settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)
    settings.cookie_file.write_text("{not json")
    with pytest.raises(GIAError) as e:
        read_cookie_jar(settings.cookie_file)
    assert e.value.code == "auth_expired"


@pytest.mark.parametrize("content", ["{}", "[1, 2]", '[{"name": "x"}]'])
def test_read_jar_wrong_shape_is_auth_expired(settings, content):
    settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)
    settings.cookie_file.write_text(content)
    with pytest.raises(GIAError) as e:
        read_cookie_jar(settings.cookie_file)
    assert e.value.code == "auth_expired" and e.value.details["hint"] == "run: musa auth"


async def test_ensure_client_passes_auto_close_and_closes_on_guest(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    ok = await ensure_client(settings, client_cls=FakeClient, auto_close=True)
    assert ok.kw["auto_close"] is True and ok.kw["close_delay"] == 900
    closed = []
    class Guest(FakeClientGuest):
        async def close(self, *a): closed.append(True)
    with pytest.raises(GIAError):
        await ensure_client(settings, client_cls=Guest)
    assert closed == [True]


async def test_ensure_client_guest_mode_fails(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    with pytest.raises(GIAError) as e:
        await ensure_client(settings, client_cls=FakeClientGuest)
    assert e.value.code == "auth_expired"


async def test_ensure_client_no_cookies_fails(settings):
    with pytest.raises(GIAError) as e:
        await ensure_client(settings, client_cls=FakeClient)
    assert e.value.code == "auth_expired"


def _firefox_db(profile, rows):
    """Firefox-like cookies.sqlite in WAL mode. The open connection keeps the rows un-checkpointed
    in cookies.sqlite-wal, like a running Firefox does."""
    profile.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(profile / "cookies.sqlite")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT)")
    con.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)", rows)
    con.commit()
    return con


ROWS = [("__Secure-1PSID", "g.a000abc", ".google.com"), ("__Secure-1PSIDTS", "ts", ".google.com"),
        ("NID", "n", ".google.es"), ("OTHER", "o", "accounts.google.com")]


def test_read_firefox_cookies_reads_wal_and_filters_domain(tmp_path):
    con = _firefox_db(tmp_path / "p", ROWS)
    try:
        jar = auth.read_firefox_cookies(tmp_path / "p")
    finally:
        con.close()
    assert sorted(c["name"] for c in jar) == ["__Secure-1PSID", "__Secure-1PSIDTS"]
    assert all(c["domain"] == ".google.com" for c in jar)


def test_read_firefox_cookies_without_db(tmp_path):
    assert auth.read_firefox_cookies(tmp_path) == []


def _no_input(*a, **k):
    raise AssertionError("input() must not be called (stdio transport)")


def test_login_guided_waits_for_session_cookie(settings, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", _no_input)
    settings.firefox_path = sys.executable  # any existing file; the launcher is faked
    closed, launched, conns = [], [], []
    profile = settings.firefox_profile_dir

    def launcher(firefox, profile_dir):
        launched.append((firefox, profile_dir))
        conns.append(_firefox_db(profile_dir, [("NID", "n", ".google.es")]))

    sleeps = []

    def fake_sleep(s):  # the user "signs in" during the second poll interval
        sleeps.append(s)
        if len(sleeps) == 2:
            conns[0].executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)", ROWS[:2])
            conns[0].commit()

    monkeypatch.setattr(auth.time, "sleep", fake_sleep)
    try:
        jar = auth.login_guided(settings, launcher=launcher, closer=closed.append)
    finally:
        conns[0].close()
    assert launched == [(sys.executable, profile)]
    assert closed == [profile]
    assert {c["name"] for c in jar} == {"__Secure-1PSID", "__Secure-1PSIDTS"}
    assert read_cookie_jar(settings.cookie_file) == jar
    assert (settings.cookie_file.stat().st_mode & 0o777) == 0o600
    assert (profile.stat().st_mode & 0o777) == 0o700
    out = capsys.readouterr()
    assert out.out == "" and "Sign in" in out.err


def test_login_guided_timeout_keeps_existing_jar(settings, monkeypatch):
    monkeypatch.setattr(builtins, "input", _no_input)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(ticks))
    settings.firefox_path = sys.executable
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    closed = []
    with pytest.raises(GIAError) as e:
        auth.login_guided(settings, launcher=lambda f, p: None, closer=closed.append)
    assert e.value.code == "auth_expired" and "timeout" in str(e.value)
    assert closed == [settings.firefox_profile_dir]
    assert read_cookie_jar(settings.cookie_file) == JAR


def test_find_firefox_missing(settings):
    settings.firefox_path = "/nonexistent/firefox"
    with pytest.raises(GIAError) as e:
        auth.find_firefox(settings)
    assert e.value.code == "auth_failed" and "GIA_FIREFOX_PATH" in e.value.details["hint"]


def _chrome_cookies_db(profile, rows):
    db = profile / "Default" / "Cookies"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    con.executemany("INSERT INTO cookies VALUES (?, ?, x'00')", rows)
    con.commit()
    return con


def test_chatgpt_session_present_reads_a_copy_of_the_live_db(tmp_path):
    con = _chrome_cookies_db(tmp_path, [(".chatgpt.com", "_cfuvid")])
    try:
        assert auth.chatgpt_session_present(tmp_path) is False
        con.execute("INSERT INTO cookies VALUES ('chatgpt.com', '__Secure-next-auth.session-token.0', x'00')")
        con.commit()
        assert auth.chatgpt_session_present(tmp_path) is True  # connection still open: read from a copy
    finally:
        con.close()


def test_chatgpt_session_present_ignores_other_hosts_and_missing_db(tmp_path):
    assert auth.chatgpt_session_present(tmp_path) is False
    con = _chrome_cookies_db(tmp_path, [(".example.com", "__Secure-next-auth.session-token")])
    con.close()
    assert auth.chatgpt_session_present(tmp_path) is False


def test_chatgpt_session_present_survives_a_journal_vanishing_mid_copy(tmp_path, monkeypatch):
    con = _chrome_cookies_db(tmp_path, [(".chatgpt.com", "__Secure-next-auth.session-token")])
    con.close()
    (tmp_path / "Default" / "Cookies-journal").write_bytes(b"")
    real = auth.shutil.copyfile

    def flaky(src, dst):
        if str(src).endswith("-journal"):
            raise FileNotFoundError(src)
        return real(src, dst)
    monkeypatch.setattr(auth.shutil, "copyfile", flaky)
    assert auth.chatgpt_session_present(tmp_path) is True


def test_chatgpt_session_present_false_when_main_db_vanishes(tmp_path, monkeypatch):
    con = _chrome_cookies_db(tmp_path, [(".chatgpt.com", "__Secure-next-auth.session-token")])
    con.close()

    def gone(src, dst):
        raise FileNotFoundError(src)
    monkeypatch.setattr(auth.shutil, "copyfile", gone)
    assert auth.chatgpt_session_present(tmp_path) is False


def test_login_chatgpt_confirms_only_after_chrome_exits(settings, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", _no_input)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    settings.chrome_path = sys.executable  # any existing file; the launcher is faked
    events = []
    answers = iter([False, True, True])

    def check(profile):
        value = next(answers)
        events.append(("check", value))
        return value

    auth.login_chatgpt(settings, launcher=lambda c, p: events.append(("launch", c, p)),
                       closer=lambda p: events.append(("close", p)), session_check=check)
    profile = settings.chatgpt_profile_dir
    assert events == [("launch", sys.executable, profile), ("check", False), ("check", True),
                      ("close", profile), ("check", True)]
    assert (profile.stat().st_mode & 0o777) == 0o700
    out = capsys.readouterr()
    assert out.out == "" and "ChatGPT" in out.err


def test_login_chatgpt_cookie_missing_after_close(settings, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    settings.chrome_path = sys.executable
    answers = iter([True, False])
    with pytest.raises(GIAError) as e:
        auth.login_chatgpt(settings, launcher=lambda c, p: None, closer=lambda p: None,
                           session_check=lambda p: next(answers))
    assert e.value.code == "auth_expired"


def test_login_chatgpt_timeout_closes_chrome(settings, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(ticks))
    settings.chrome_path = sys.executable
    closed = []
    with pytest.raises(GIAError) as e:
        auth.login_chatgpt(settings, launcher=lambda c, p: None, closer=closed.append,
                           session_check=lambda p: False)
    assert e.value.code == "auth_expired" and "timeout" in str(e.value)
    assert closed == [settings.chatgpt_profile_dir]


def test_login_chatgpt_profile_busy(settings):
    settings.chrome_path = sys.executable
    settings.ensure_dirs()
    launched = []
    with FileLock(str(settings.chatgpt_lock_file)):
        with pytest.raises(GIAError) as e:
            auth.login_chatgpt(settings, launcher=lambda c, p: launched.append(p), closer=lambda p: None,
                               session_check=lambda p: True)
    assert e.value.code == "temporarily_blocked" and launched == []


def test_find_chrome_missing(settings):
    settings.chrome_path = "/nonexistent/chrome"
    with pytest.raises(GIAError) as e:
        auth.find_chrome(settings)
    assert e.value.code == "auth_failed" and "GIA_CHROME_PATH" in e.value.details["hint"]


def test_close_chrome_waits_until_every_process_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(auth.os, "name", "posix")  # POSIX branch; Windows: test_windows_support.py
    seen = iter([[11, 12], [11, 12], [12], []])
    kills = []
    monkeypatch.setattr(auth, "_profile_pids", lambda p: next(seen))
    monkeypatch.setattr(auth.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    auth._close_chrome(tmp_path)
    assert kills == [(11, signal.SIGTERM), (12, signal.SIGTERM)]


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX signals; Windows: test_windows_support.py")
def test_close_chrome_kills_after_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(auth.os, "name", "posix")
    kills = []
    monkeypatch.setattr(auth, "_profile_pids", lambda p: [11])
    monkeypatch.setattr(auth.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(ticks))
    auth._close_chrome(tmp_path, timeout_s=10)
    assert kills == [(11, signal.SIGTERM), (11, signal.SIGKILL)]
def test_login_guided_stale_psid_does_not_end_wait(settings, monkeypatch):
    """A half-finished earlier login leaves __Secure-1PSID in the profile: the wait must go on
    until the sign-in completes (__Secure-1PSIDTS), not return the stale cookie at once."""
    monkeypatch.setattr(builtins, "input", _no_input)
    settings.firefox_path = sys.executable
    profile = settings.firefox_profile_dir
    con = _firefox_db(profile, [("__Secure-1PSID", "stale", ".google.com")])
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:  # the user finishes signing in
            con.execute("DELETE FROM moz_cookies")
            con.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)",
                            [("__Secure-1PSID", "fresh", ".google.com"),
                             ("__Secure-1PSIDTS", "ts", ".google.com")])
            con.commit()

    monkeypatch.setattr(auth.time, "sleep", fake_sleep)
    try:
        jar = auth.login_guided(settings, launcher=lambda f, p: None, closer=lambda p: None)
    finally:
        con.close()
    assert len(sleeps) >= 3
    assert {c["name"]: c["value"] for c in jar}["__Secure-1PSID"] == "fresh"


def test_login_guided_new_psid_without_psidts_settles(settings, monkeypatch):
    monkeypatch.setattr(builtins, "input", _no_input)
    settings.firefox_path = sys.executable
    profile = settings.firefox_profile_dir
    conns = []
    clock = [0.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])

    def fake_sleep(s):
        clock[0] += s
        if not conns[0].execute("SELECT 1 FROM moz_cookies WHERE name='__Secure-1PSID'").fetchone():
            conns[0].execute("INSERT INTO moz_cookies VALUES ('__Secure-1PSID', 'new', '.google.com')")
            conns[0].commit()

    monkeypatch.setattr(auth.time, "sleep", fake_sleep)
    try:
        jar = auth.login_guided(settings, launcher=lambda f, p: conns.append(_firefox_db(p, [])),
                                closer=lambda p: None)
    finally:
        conns[0].close()
    assert [c["value"] for c in jar if c["name"] == "__Secure-1PSID"] == ["new"]
    assert clock[0] >= auth.LOGIN_SETTLE_S


def test_login_guided_clears_rotated_cache(settings, monkeypatch):
    monkeypatch.setattr(builtins, "input", _no_input)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    settings.firefox_path = sys.executable
    settings.ensure_dirs()
    old = settings.upstream_cache_dir / ".cached_cookies_old.json"
    old.write_text("[]")
    conns = []
    try:
        auth.login_guided(settings, launcher=lambda f, p: conns.append(_firefox_db(p, ROWS[:2])),
                          closer=lambda p: None)
    finally:
        conns[0].close()
    assert not old.exists()


async def test_ensure_client_uses_rotated_psidts_of_same_session_only(settings):
    settings.ensure_dirs()
    save_cookie_jar_atomic(JAR + [{"name": "__Secure-1PSIDTS", "value": "old", "domain": ".google.com"}],
                           settings.cookie_file)
    other = settings.upstream_cache_dir / ".cached_cookies_otra.json"
    other.write_text('[{"name": "__Secure-1PSIDTS", "value": "de-otra-cuenta"}]')
    c = await ensure_client(settings, client_cls=FakeClient)
    assert c.secure_1psidts == "old"
    (settings.upstream_cache_dir / ".cached_cookies_abc.json").write_text(
        '[{"name": "__Secure-1PSIDTS", "value": "rotada"}]')
    c = await ensure_client(settings, client_cls=FakeClient)
    assert c.secure_1psidts == "rotada"


def test_read_firefox_cookies_includes_gemini_host_cookies(tmp_path):
    """Gemini's host-only OSID cookies are required: without them every signed-in RPC is HTTP 400."""
    con = _firefox_db(tmp_path / "p", ROWS + [("__Secure-OSID", "o", "gemini.google.com"),
                                              ("OSID", "o2", "gemini.google.com")])
    try:
        jar = auth.read_firefox_cookies(tmp_path / "p")
    finally:
        con.close()
    assert {(c["name"], c["domain"]) for c in jar} >= {("__Secure-OSID", "gemini.google.com"),
                                                        ("OSID", "gemini.google.com")}


async def test_ensure_client_passes_jar_with_gemini_cookies(settings):
    jar = JAR + [{"name": "__Secure-OSID", "value": "o", "domain": "gemini.google.com"}]
    save_cookie_jar_atomic(jar, settings.cookie_file)
    c = await ensure_client(settings, client_cls=FakeClient)
    assert c.kw_init["cookie_jar"] == [j for j in jar if j["domain"] != ".example.com"]
