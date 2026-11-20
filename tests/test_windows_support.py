"""Windows code paths (taskkill/PowerShell instead of pgrep/signals). Platform-faking tests run everywhere."""
import os
import subprocess
import sys

import pytest

from musa import auth, compositions, winproc


def test_close_firefox_on_windows_uses_taskkill(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(auth.os, "name", "nt")
    monkeypatch.setattr(winproc, "find_pids", lambda image, needle: calls.append(("find", image, needle)) or [7, 8])
    monkeypatch.setattr(winproc, "terminate", lambda pid, force=False: calls.append(("kill", pid, force)))
    auth._close_firefox(tmp_path)
    assert calls == [("find", "firefox.exe", str(tmp_path)), ("kill", 7, True), ("kill", 8, True)]


def test_close_chrome_on_windows_closes_politely_then_forces(monkeypatch, tmp_path):
    kills = []
    monkeypatch.setattr(auth.os, "name", "nt")
    monkeypatch.setattr(auth, "_profile_pids", lambda p: [11])
    monkeypatch.setattr(winproc, "terminate", lambda pid, force=False: kills.append((pid, force)))
    monkeypatch.setattr(auth.os, "kill", lambda *a: pytest.fail("os.kill must not be used on Windows"))
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(ticks))
    auth._close_chrome(tmp_path, timeout_s=10)
    assert kills == [(11, False), (11, True)]


def test_profile_pids_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(auth.os, "name", "nt")
    monkeypatch.setattr(winproc, "find_pids", lambda image, needle: [3] if (image, needle) == ("chrome.exe", str(tmp_path)) else [])
    assert auth._profile_pids(tmp_path) == [3]


def test_find_pids_passes_needle_by_env_and_parses(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="12\r\n34\r\n\r\n", stderr="")
    monkeypatch.setattr(winproc.subprocess, "run", fake_run)
    needle = r"C:\Users\o'brien\state\chatgpt-chrome-profile"
    assert winproc.find_pids("chrome.exe", needle) == [12, 34]
    assert seen["cmd"][0] == "powershell" and "Name='chrome.exe'" in seen["cmd"][-1]
    assert needle not in seen["cmd"][-1] and seen["kw"]["env"]["MUSA_PID_NEEDLE"] == needle
    assert seen["kw"]["stdin"] is subprocess.DEVNULL  # MCP stdio: never share stdin


def test_find_pids_without_powershell(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(winproc.subprocess, "run", boom)
    assert winproc.find_pids("chrome.exe", "x") == []


def test_terminate_command(monkeypatch):
    cmds = []
    monkeypatch.setattr(winproc.subprocess, "run", lambda cmd, **kw: cmds.append(cmd))
    monkeypatch.setattr(winproc, "_post_close", lambda pid: cmds.append(("WM_CLOSE", pid)))
    winproc.terminate(5)
    winproc.terminate(5, force=True)
    assert cmds == [("WM_CLOSE", 5), ["taskkill", "/PID", "5", "/T", "/F"]]


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 API")
def test_pid_alive_real_windows():
    assert winproc.pid_alive(os.getpid()) is True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert winproc.pid_alive(p.pid) is False


def test_compositions_pid_alive_on_windows_never_calls_os_kill(monkeypatch):
    monkeypatch.setattr(compositions.os, "name", "nt")
    monkeypatch.setattr(compositions.os, "kill", lambda *a: pytest.fail("os.kill(pid, 0) sends CTRL_C_EVENT on Windows"))
    monkeypatch.setattr(winproc, "pid_alive", lambda pid: pid == 42)
    assert compositions._pid_alive(42) is True and compositions._pid_alive(43) is False


def test_user_styles_yaml_read_as_utf8(tmp_path):
    from musa.styles import StyleCatalog
    f = tmp_path / "styles.yaml"
    f.write_text("styles:\n  neon:\n    description: neón\n", encoding="utf-8")
    assert StyleCatalog.load(f, source="user").styles["neon"].description == "neón"


def test_chatgpt_session_present_reads_windows_network_cookies(tmp_path):
    import sqlite3
    db = tmp_path / "Default" / "Network" / "Cookies"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    con.execute("INSERT INTO cookies VALUES ('chatgpt.com', '__Secure-next-auth.session-token', x'00')")
    con.commit()
    con.close()
    assert auth.chatgpt_session_present(tmp_path) is True


def test_login_chatgpt_on_windows_waits_for_user_to_close_chrome(settings, monkeypatch, capsys):
    """Chrome locks its cookie db on Windows, so the cookie can only be read once the user closes it."""
    monkeypatch.setattr(auth.os, "name", "nt")
    monkeypatch.setattr(auth.os, "chmod", lambda *a: None)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    settings.chrome_path = sys.executable
    pids = iter([[], [5], [5], []])  # not started yet, running, running, closed by the user
    monkeypatch.setattr(auth, "_profile_pids", lambda p: next(pids))
    checks = iter([False, False, False, False, True])  # locked while running; present after exit
    closed = []
    auth.login_chatgpt(settings, launcher=lambda c, p: None, closer=closed.append,
                       session_check=lambda p: next(checks))
    assert closed == [settings.chatgpt_profile_dir]
    assert "close Chrome" in capsys.readouterr().err


def test_login_chatgpt_on_windows_closed_without_signing_in(settings, monkeypatch):
    monkeypatch.setattr(auth.os, "name", "nt")
    monkeypatch.setattr(auth.os, "chmod", lambda *a: None)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    settings.chrome_path = sys.executable
    pids = iter([[5], []])
    monkeypatch.setattr(auth, "_profile_pids", lambda p: next(pids))
    with pytest.raises(auth.GIAError) as e:
        auth.login_chatgpt(settings, launcher=lambda c, p: None, closer=lambda p: None,
                           session_check=lambda p: False)
    assert e.value.code == "auth_expired" and "not found" in str(e.value)
