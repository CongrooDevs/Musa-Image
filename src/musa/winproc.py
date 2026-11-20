"""Windows process helpers (no pgrep / POSIX signals there). macOS and Linux never call these.

Every child runs with stdin=DEVNULL and no console window: under the MCP server stdin carries JSON-RPC.
"""
import os
import subprocess

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_WM_CLOSE = 0x0010

# The needle travels in an env var, so no quoting/escaping of the path is needed inside the script.
_FIND_PS = ("$n = $env:MUSA_PID_NEEDLE; "
            "Get-CimInstance Win32_Process -Filter \"Name='{image}'\" | "
            "Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains($n) }} | "
            "ForEach-Object {{ $_.ProcessId }}")


def find_pids(image: str, needle: str) -> list[int]:
    """PIDs of `image` (e.g. "chrome.exe") whose command line contains `needle`."""
    try:
        found = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _FIND_PS.format(image=image)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env={**os.environ, "MUSA_PID_NEEDLE": needle}, creationflags=_NO_WINDOW)
    except OSError:
        return []
    return [int(p) for p in found.stdout.split() if p.isdigit()]


def terminate(pid: int, force: bool = False) -> None:
    """Politely: WM_CLOSE to the process's visible windows, like clicking X (the browser flushes its profile;
    `taskkill` without /F is ignored by Chrome). With force: kill the whole process tree."""
    if not force:
        _post_close(pid)
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   stdin=subprocess.DEVNULL, capture_output=True, creationflags=_NO_WINDOW)


def _post_close(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.WinDLL("user32", use_last_error=True)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u32.IsWindowVisible(hwnd):
            u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        return True

    u32.EnumWindows(each, 0)


def pid_alive(pid: int) -> bool:
    """Windows replacement for os.kill(pid, 0), which there sends CTRL_C_EVENT instead of probing."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists, but belongs to someone else
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)
