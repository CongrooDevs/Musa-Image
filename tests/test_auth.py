import builtins
import sqlite3
import sys

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
