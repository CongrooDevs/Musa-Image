# Proveedor ChatGPT (web) — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objetivo:** Musa genera y edita imágenes con la sesión web de ChatGPT del usuario, seleccionando el modelo `chatgpt-web`.

**Arquitectura:**
- Un módulo nuevo, `src/musa/chatgpt_web.py`, contiene tres piezas:
  - `ChatGPTBrowser`: Chrome headless lanzado por Playwright con comunicación por pipe, perfil dedicado, lock de archivo y cierre por inactividad.
  - `ChatGPTPage`: adaptador sobre la página de Playwright.
  - `ChatGPTWebBackend`: implementa el protocolo `Backend`.
- `client.RoutedBackend` envuelve el backend de Gemini (sin cambios) y el de ChatGPT. Las generaciones nuevas se reparten por modelo y las conversaciones existentes por `rec.model`.
- El inicio de sesión abre una Chrome normal y no automatizada (`auth.login_chatgpt`).

**Stack:** Python 3.11, `playwright` (solo el paquete; usa la Google Chrome instalada), `filelock`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-11-13-chatgpt-web-provider-design.md`.

## Restricciones globales

- Id del modelo: `"chatgpt-web"`. Etiqueta: `"ChatGPT (web)"`. Las dos constantes viven en `client.py`: `CHATGPT_MODEL` y `CHATGPT_LABEL`.
- Al lanzar Chrome con Playwright, **siempre** se pasa `ignore_default_args=["--enable-automation", "--use-mock-keychain"]` y `args=["--disable-blink-features=AutomationControlled"]`, con `headless=True`. Nunca se usa `--remote-debugging-port` ni `connect_over_cdp`.
- El User-Agent nunca lleva una versión fija: se deriva de `Browser.getVersion`.
- El perfil vive en `settings.chatgpt_profile_dir = state_dir / "chatgpt-chrome-profile"` (0700). El lock es `settings.chatgpt_lock_file = state_dir / "chatgpt-profile.lock"`, compartido por el navegador y el inicio de sesión.
- La base `Cookies` del perfil se lee siempre **a partir de una copia** en un directorio temporal.
- El `accessToken` nunca sale de la página: todas las llamadas a la API van dentro de `page.evaluate`, y nada lo registra ni lo devuelve.
- `playwright` se importa solo dentro de `chatgpt_web._start_playwright()`. Ningún test importa `playwright`.
- **Sin códigos de error nuevos:** `tests/test_errors.py` exige exactamente 29.
- El aviso de un Gem con ChatGPT usa el código **`gem_ignored`**, nunca `gem_missing`. Motivo: `src/musa/service.py:263-266` compara `warning.get("code") == GEM_MISSING` (la constante, importada de `client` en la línea 18) y ejecuta `self.gems.remove(style)` cuando el `gem_id` coincide. Usar `gem_missing` borraría el Gem del estilo en Gemini cada vez que se use ese estilo con ChatGPT.
- No se supone ningún límite fijo de cuota de ChatGPT: solo `remaining == 0` → `usage_limit`.
- Antes del plan, la suite base da **826 passed** (`uv run pytest -q`). Al terminar cada tarea, toda la suite debe pasar.

## Mapa de archivos

| Archivo | Cambio |
|---|---|
| `pyproject.toml`, `uv.lock` | Dependencia `playwright>=1.50` |
| `src/musa/config.py` | `chrome_path`, `chatgpt_idle_close_s`, `chatgpt_profile_dir`, `chatgpt_lock_file` |
| `src/musa/auth.py` | `find_chrome`, `chatgpt_session_present`, `_launch_chrome`, `_profile_pids`, `_close_chrome`, `login_chatgpt` |
| `src/musa/chatgpt_web.py` (nuevo) | Funciones puras, `ChatGPTBrowser`, `ChatGPTPage`, `ChatGPTWebBackend` |
| `src/musa/client.py` | `CHATGPT_MODEL`, `CHATGPT_LABEL`, `append_record`, `RoutedBackend`, `build_backend` |
| `src/musa/jobs.py` | Etiqueta en `MODEL_LABELS` |
| `src/musa/cli.py` | `musa auth --chatgpt` |
| `src/musa/mcp_server.py` | `auth_login(provider)`, `INSTRUCTIONS` |
| `src/musa/errors.py` | Textos de `NEXT_ACTION` neutrales respecto al proveedor |
| `src/musa/web/app.js` | Textos de `ERR_ES` neutrales y `gem_ignored` |
| `README.md` | Sección «ChatGPT (web)» y tabla de la CLI |
| `tests/test_auth.py`, `tests/test_config.py`, `tests/test_chatgpt_web.py` (nuevo), `tests/test_chatgpt_browser.py` (nuevo), `tests/test_chatgpt_backend.py` (nuevo), `tests/test_router.py` (nuevo), `tests/test_client.py`, `tests/test_jobs.py`, `tests/test_cli.py`, `tests/test_mcp_server.py`, `tests/test_errors.py` | Tests |

---

### Tarea 1: Configuración, dependencia e inicio de sesión de ChatGPT

**Archivos:**
- Modificar: `pyproject.toml`, `uv.lock` (con `uv add`)
- Modificar: `src/musa/config.py:30-33` (campos) y `:46-76` (propiedades, `ensure_dirs`)
- Modificar: `src/musa/auth.py` (imports y bloque nuevo al final)
- Test: `tests/test_config.py`, `tests/test_auth.py`

**Interfaces:**
- Produce:
  - `Settings.chrome_path: str | None`
  - `Settings.chatgpt_idle_close_s: float`
  - `Settings.chatgpt_profile_dir -> Path`
  - `Settings.chatgpt_lock_file -> Path`
  - `auth.find_chrome(settings) -> str`
  - `auth.chatgpt_session_present(profile_dir: Path) -> bool`
  - `auth.login_chatgpt(settings, launcher=None, closer=None, session_check=None) -> None`
  - `auth.CHATGPT_SESSION_COOKIE = "__Secure-next-auth.session-token"`

- [ ] **Paso 1: Añadir la dependencia**

Run: `uv add "playwright>=1.50"`
Expected: `pyproject.toml` incluye `"playwright>=1.50"` en `dependencies` y `uv.lock` se actualiza. No ejecutes `playwright install`: se usa la Chrome del sistema.

- [ ] **Paso 2: Escribir los tests de configuración (fallan)**

Añadir al final de `tests/test_config.py`:

```python
def test_chatgpt_settings(settings: Settings, monkeypatch):
    assert settings.chrome_path is None
    assert settings.chatgpt_idle_close_s == 120.0
    assert settings.chatgpt_profile_dir == settings.state_dir / "chatgpt-chrome-profile"
    assert settings.chatgpt_lock_file == settings.state_dir / "chatgpt-profile.lock"
    monkeypatch.setenv("GIA_CHROME_PATH", "/opt/chrome")
    assert Settings().chrome_path == "/opt/chrome"


def test_ensure_dirs_creates_private_chatgpt_profile(settings: Settings):
    settings.ensure_dirs()
    assert (settings.chatgpt_profile_dir.stat().st_mode & 0o777) == 0o700
```

- [ ] **Paso 3: Escribir los tests de inicio de sesión (fallan)**

Añadir a `tests/test_auth.py`. Los imports `builtins`, `sqlite3`, `sys`, `pytest`, `auth` y `GIAError` ya existen; añade `import signal` y `from filelock import FileLock` arriba.

```python
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
    seen = iter([[11, 12], [11, 12], [12], []])
    kills = []
    monkeypatch.setattr(auth, "_profile_pids", lambda p: next(seen))
    monkeypatch.setattr(auth.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    auth._close_chrome(tmp_path)
    assert kills == [(11, signal.SIGTERM), (12, signal.SIGTERM)]


def test_close_chrome_kills_after_timeout(monkeypatch, tmp_path):
    kills = []
    monkeypatch.setattr(auth, "_profile_pids", lambda p: [11])
    monkeypatch.setattr(auth.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(ticks))
    auth._close_chrome(tmp_path, timeout_s=10)
    assert kills == [(11, signal.SIGTERM), (11, signal.SIGKILL)]
```

- [ ] **Paso 4: Ejecutarlos y ver que fallan**

Run: `uv run pytest tests/test_config.py tests/test_auth.py -q`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'chrome_path'` / `module 'musa.auth' has no attribute 'chatgpt_session_present'`).

- [ ] **Paso 5: Implementar la configuración**

En `src/musa/config.py`, después de `temp_chats: bool = True  …` (línea 33), añadir:

```python
    chrome_path: str | None = None       # Google Chrome binary for the ChatGPT (web) provider (GIA_CHROME_PATH)
    chatgpt_idle_close_s: float = 120.0  # close the headless Chrome after this many idle seconds (proposal, not measured)
```

Después de la propiedad `firefox_profile_dir` (línea 55), añadir:

```python
    @property
    def chatgpt_profile_dir(self) -> Path: return self.state_dir / "chatgpt-chrome-profile"
    @property
    def chatgpt_lock_file(self) -> Path: return self.state_dir / "chatgpt-profile.lock"
```

En `ensure_dirs`, cambiar la tupla del bucle por:

```python
        for d in (self.state_dir, self.cookie_dir, self.upstream_cache_dir, self.state_dir / "logs",
                  self.chatgpt_profile_dir):
```

- [ ] **Paso 6: Implementar el inicio de sesión de ChatGPT**

En `src/musa/auth.py`, sustituir la línea de imports de errores por:

```python
from filelock import FileLock, Timeout as LockTimeout

from .config import Settings
from .errors import AUTH_EXPIRED, AUTH_FAILED, TEMPORARILY_BLOCKED, GIAError
```

Añadir al final del archivo:

```python
# -- ChatGPT (web) -------------------------------------------------------------
CHROME_CANDIDATES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "google-chrome", "google-chrome-stable")
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

    Chrome may hold the live db open, so the db and its journal are copied and the copy is read."""
    db = Path(profile_dir) / "Default" / "Cookies"
    if not db.exists():
        return False
    with tempfile.TemporaryDirectory() as td:
        for suffix in ("", "-journal", "-wal"):
            src = Path(f"{db}{suffix}")
            if src.exists():
                shutil.copyfile(src, Path(td) / f"Cookies{suffix}")
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
    found = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={profile_dir}"], capture_output=True, text=True)
    return [int(p) for p in found.stdout.split()]


def _close_chrome(profile_dir: Path, timeout_s: float = CHROME_CLOSE_TIMEOUT_S) -> None:
    """SIGTERM every Chrome process bound to this profile and wait until all exit (SIGKILL after timeout_s),
    so the cookie store is flushed before anyone reads it or opens the profile again."""
    def signal_all(sig) -> None:
        for pid in _profile_pids(profile_dir):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    signal_all(signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while _profile_pids(profile_dir):
        if time.monotonic() >= deadline:
            signal_all(signal.SIGKILL)
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
        print("Sign in to ChatGPT in the Chrome window; it closes by itself once the session cookie is "
              f"detected (timeout {LOGIN_TIMEOUT_S}s).", file=sys.stderr, flush=True)
        try:
            deadline = time.monotonic() + LOGIN_TIMEOUT_S
            while not check(profile):
                if time.monotonic() >= deadline:
                    raise GIAError(AUTH_EXPIRED, "login timeout", hint="run: musa auth --chatgpt")
                time.sleep(LOGIN_POLL_S)
        finally:
            (closer or _close_chrome)(profile)
        if not check(profile):
            raise GIAError(AUTH_EXPIRED, "ChatGPT session cookie not found after Chrome closed",
                           hint="run: musa auth --chatgpt")
    finally:
        lock.release()
```

- [ ] **Paso 7: Ejecutar los tests**

Run: `uv run pytest tests/test_config.py tests/test_auth.py -q`
Expected: PASS.

- [ ] **Paso 8: Ejecutar la suite completa**

Run: `uv run pytest -q`
Expected: PASS (826 + 11).

- [ ] **Paso 9: Commit**

```bash
git add pyproject.toml uv.lock src/musa/config.py src/musa/auth.py tests/test_config.py tests/test_auth.py
git commit -m "ChatGPT: configuración, dependencia playwright e inicio de sesión en Chrome"
```

---

### Tarea 2: Funciones puras del protocolo de chatgpt.com

**Archivos:**
- Crear: `src/musa/chatgpt_web.py`
- Test: `tests/test_chatgpt_web.py`

**Interfaces:**
- Produce (módulo `musa.chatgpt_web`):
  - `BASE_URL = "https://chatgpt.com"`
  - `UPLOAD_PATH = "/backend-api/files/process_upload_stream"`
  - `@dataclass(frozen=True) OutputImage(message_id: str, file_id: str, width: int | None, height: int | None)`
  - `message_ids(conversation: dict) -> set[str]`
  - `new_messages(conversation: dict, known: set[str]) -> list[dict]` (orden por `create_time`)
  - `output_images(messages: list[dict]) -> list[OutputImage]`
  - `has_tool_message(messages: list[dict]) -> bool`
  - `assistant_text(messages: list[dict]) -> str | None`
  - `image_gen_quota(init_json: dict | None) -> dict | None` (`{"remaining", "reset_after"}`)
  - `conversation_id_from_url(url: str) -> str | None`
  - `is_blocking_response(method: str, url: str, status: int) -> bool`

- [ ] **Paso 1: Escribir los tests (fallan)**

Crear `tests/test_chatgpt_web.py`:

```python
from musa import chatgpt_web as cw

CID = "6af76eed-1aec-83e9-b323-d348dba09bfa"


def _msg(mid, role, t, status="finished_successfully", parts=None, ctype="text"):
    return {"id": mid, "author": {"role": role}, "create_time": t, "status": status,
            "content": {"content_type": ctype, "parts": parts or []}}


def _img(fid):
    return {"content_type": "image_asset_pointer", "asset_pointer": f"sediment://{fid}", "width": 10, "height": 8}


def _conv(*msgs):
    mapping = {m["id"]: {"message": m} for m in msgs}
    mapping["root"] = {"message": None}
    return {"mapping": mapping}


def test_output_images_only_from_finished_tool_messages():
    conv = _conv(_msg("u1", "user", 1, parts=[_img("file_upload"), "hola"], ctype="multimodal_text"),
                 _msg("a1", "assistant", 2, parts=["Generando"]),
                 _msg("t1", "tool", 3, status="in_progress", parts=[_img("file_pending")], ctype="multimodal_text"),
                 _msg("t2", "tool", 4, parts=[_img("file_out")], ctype="multimodal_text"))
    imgs = cw.output_images(cw.new_messages(conv, set()))
    assert imgs == [cw.OutputImage("t2", "file_out", 10, 8)]


def test_new_messages_skips_known_and_orders_by_time():
    conv = _conv(_msg("b", "assistant", 5), _msg("a", "user", 1), _msg("c", "tool", 9))
    assert [m["id"] for m in cw.new_messages(conv, {"a"})] == ["b", "c"]
    assert cw.message_ids(conv) == {"a", "b", "c"}


def test_assistant_text_and_tool_detection():
    fresh = [_msg("a1", "assistant", 1, parts=["No puedo ", {"x": 1}, "hacer eso."]),
             _msg("a2", "assistant", 2, status="in_progress", parts=["..."])]
    assert cw.assistant_text(fresh) == "No puedo hacer eso."
    assert cw.assistant_text([_msg("u", "user", 1, parts=["x"])]) is None
    assert cw.has_tool_message(fresh) is False
    assert cw.has_tool_message([_msg("t", "tool", 1, status="in_progress")]) is True


def test_image_gen_quota():
    data = {"limits_progress": [{"feature_name": "file_upload", "remaining": 9},
                                {"feature_name": "image_gen", "remaining": 3, "reset_after": "2026-11-14T20:21:58Z"}]}
    assert cw.image_gen_quota(data) == {"remaining": 3, "reset_after": "2026-11-14T20:21:58Z"}
    assert cw.image_gen_quota({"limits_progress": []}) is None
    assert cw.image_gen_quota(None) is None


def test_conversation_id_from_url():
    assert cw.conversation_id_from_url(f"https://chatgpt.com/c/{CID}") == CID
    assert cw.conversation_id_from_url(f"https://chatgpt.com/c/{CID}?model=x") == CID
    assert cw.conversation_id_from_url("https://chatgpt.com/") is None


def test_is_blocking_response_only_on_posts_of_the_send_path():
    assert cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/f/conversation", 403)
    assert cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/sentinel/chat-requirements/prepare", 404)
    assert not cw.is_blocking_response("GET", f"https://chatgpt.com/backend-api/conversation/{CID}", 404)
    assert not cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/files", 403)
    assert not cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/f/conversation", 200)
```

- [ ] **Paso 2: Ejecutarlos y ver que fallan**

Run: `uv run pytest tests/test_chatgpt_web.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'musa.chatgpt_web'` o `ImportError`).

- [ ] **Paso 3: Implementar**

Crear `src/musa/chatgpt_web.py`:

```python
"""ChatGPT (web) provider: drives chatgpt.com in the user's Google Chrome (dedicated profile, headless).

The only module that imports playwright, lazily. Spec: docs/superpowers/specs/2026-11-13-chatgpt-web-provider-design.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BASE_URL = "https://chatgpt.com"
UPLOAD_PATH = "/backend-api/files/process_upload_stream"
_CONV_URL_RE = re.compile(r"/c/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


@dataclass(frozen=True)
class OutputImage:
    message_id: str
    file_id: str
    width: int | None
    height: int | None


def _messages(conversation: dict) -> list[dict]:
    """Every message of a conversation's mapping tree, oldest first."""
    msgs = [n["message"] for n in (conversation.get("mapping") or {}).values() if n.get("message")]
    return sorted(msgs, key=lambda m: m.get("create_time") or 0)


def _role(message: dict) -> str | None:
    return (message.get("author") or {}).get("role")


def message_ids(conversation: dict) -> set[str]:
    return {m["id"] for m in _messages(conversation) if m.get("id")}


def new_messages(conversation: dict, known: set[str]) -> list[dict]:
    return [m for m in _messages(conversation) if m.get("id") not in known]


def output_images(messages: list[dict]) -> list[OutputImage]:
    """Generated images only: image_asset_pointer parts of finished tool messages (user uploads are excluded)."""
    out = []
    for m in messages:
        if _role(m) != "tool" or m.get("status") != "finished_successfully":
            continue
        for part in (m.get("content") or {}).get("parts") or []:
            if (isinstance(part, dict) and part.get("content_type") == "image_asset_pointer"
                    and "://" in str(part.get("asset_pointer", ""))):
                pointer = part["asset_pointer"]
                out.append(OutputImage(m["id"], pointer.split("://", 1)[1], part.get("width"), part.get("height")))
    return out


def has_tool_message(messages: list[dict]) -> bool:
    return any(_role(m) == "tool" for m in messages)


def assistant_text(messages: list[dict]) -> str | None:
    """Text of the last finished assistant message, or None when there is none."""
    for m in reversed(messages):
        if _role(m) == "assistant" and m.get("status") == "finished_successfully":
            parts = (m.get("content") or {}).get("parts") or []
            return "".join(p for p in parts if isinstance(p, str)).strip()
    return None


def image_gen_quota(init_json: dict | None) -> dict | None:
    for item in (init_json or {}).get("limits_progress") or []:
        if item.get("feature_name") == "image_gen":
            return {"remaining": item.get("remaining"), "reset_after": item.get("reset_after")}
    return None


def conversation_id_from_url(url: str) -> str | None:
    m = _CONV_URL_RE.search(url or "")
    return m.group(1) if m else None


def is_blocking_response(method: str, url: str, status: int) -> bool:
    """403/404 on a POST of the send path: Cloudflare/sentinel refused the turn ('Unusual activity' or a
    disguised 'Model not found'). Musa's own polling is GET and never counts."""
    path = (url or "").split("?", 1)[0]
    return (method == "POST" and status in (403, 404) and "/backend-api/" in path
            and ("conversation" in path or "sentinel" in path))
```

- [ ] **Paso 4: Ejecutar los tests**

Run: `uv run pytest tests/test_chatgpt_web.py -q`
Expected: PASS (6 tests).

- [ ] **Paso 5: Commit**

```bash
git add src/musa/chatgpt_web.py tests/test_chatgpt_web.py
git commit -m "ChatGPT: funciones puras de conversación, cuota y bloqueo"
```

---

### Tarea 3: `ChatGPTBrowser` y `ChatGPTPage`

**Archivos:**
- Modificar: `src/musa/chatgpt_web.py`
- Test: `tests/test_chatgpt_browser.py`

**Interfaces:**
- Consume: `Settings.chatgpt_profile_dir`, `Settings.chatgpt_lock_file`, `Settings.chrome_path`, `Settings.chatgpt_idle_close_s`, `Settings.gen_timeout_s` (Tarea 1); `UPLOAD_PATH`, `image_gen_quota`, `is_blocking_response` (Tarea 2).
- Produce:
  - `ua_override(version_info: dict) -> dict` (parámetros de `Network.setUserAgentOverride`)
  - `ChatGPTBrowser(settings, start_playwright=None)` con `launch_kwargs() -> dict`, `page()` (async context manager → página de Playwright) y `aclose()`.
  - `ChatGPTPage(page, upload_timeout_s=90.0, clock=time.monotonic, sleep=asyncio.sleep)` con:
    - propiedad `url`
    - `open(url)`
    - `session() -> dict` (`{"logged_in": bool, "plan": str | None}`)
    - `api(path, method="GET", body=None) -> tuple[int, dict | None]`
    - `image_quota() -> dict | None`
    - `attach(paths: list[Path])`
    - `send(prompt: str)`
    - `download(conversation_id, file_id) -> bytes`
    - `watch_blocking()` (context manager que entrega `list[tuple[str, int]]`)
  - Constantes `JS_SESSION`, `JS_API`, `JS_BYTES`, `JS_TZ` (identidad usada por los fakes de los tests).

- [ ] **Paso 1: Escribir los tests (fallan)**

Crear `tests/test_chatgpt_browser.py`:

```python
import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from musa import chatgpt_web as cw
from musa.errors import GIAError

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "HeadlessChrome/155.0.8059.86 Safari/537.36")


class FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getVersion":
            return {"product": "HeadlessChrome/155.0.8059.86", "userAgent": UA}
        return {}


class FakeContext:
    def __init__(self):
        self.pages = [SimpleNamespace(name="page0")]
        self.cdp = FakeCDP()
        self.closed = False

    async def new_page(self):
        return SimpleNamespace(name="new")

    async def new_cdp_session(self, page):
        return self.cdp

    async def close(self):
        self.closed = True


class FakePW:
    def __init__(self, fail=None):
        self.launches, self.contexts, self.stopped, self.fail = [], [], False, fail
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    async def _launch(self, **kw):
        if self.fail:
            raise self.fail
        self.launches.append(kw)
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def stop(self):
        self.stopped = True


def _browser(settings, pw):
    async def start():
        return pw
    return cw.ChatGPTBrowser(settings, start_playwright=start)


def _lock_free(settings) -> bool:
    lock = FileLock(str(settings.chatgpt_lock_file))
    try:
        lock.acquire(timeout=0)
    except Exception:
        return False
    lock.release()
    return True


async def test_launch_kwargs_drop_automation_and_mock_keychain(settings):
    pw = FakePW()
    b = _browser(settings, pw)
    async with b.page():
        pass
    (kw,) = pw.launches
    assert kw["user_data_dir"] == str(settings.chatgpt_profile_dir)
    assert kw["headless"] is True and kw["channel"] == "chrome"
    assert set(kw["ignore_default_args"]) == {"--enable-automation", "--use-mock-keychain"}
    assert kw["args"] == ["--disable-blink-features=AutomationControlled"]
    assert "executable_path" not in kw
    await b.aclose()


async def test_chrome_path_replaces_channel(settings):
    settings.chrome_path = "/opt/chrome"
    kw = cw.ChatGPTBrowser(settings).launch_kwargs()
    assert kw["executable_path"] == "/opt/chrome" and "channel" not in kw


async def test_user_agent_comes_from_the_real_binary(settings):
    pw = FakePW()
    b = _browser(settings, pw)
    async with b.page():
        pass
    method, params = pw.contexts[0].cdp.sent[-1]
    assert method == "Network.setUserAgentOverride"
    assert "Headless" not in params["userAgent"] and "Chrome/155.0.8059.86" in params["userAgent"]
    meta = params["userAgentMetadata"]
    assert {"brand": "Google Chrome", "version": "155"} in meta["brands"]
    assert all("Headless" not in b["brand"] for b in meta["brands"])
    assert meta["fullVersion"] == "155.0.8059.86" and meta["mobile"] is False
    await b.aclose()


async def test_reuses_one_browser_and_closes_when_idle(settings):
    settings.chatgpt_idle_close_s = 0.05
    pw = FakePW()
    b = _browser(settings, pw)
    async with b.page() as p1:
        pass
    async with b.page() as p2:
        assert p1 is p2
    assert len(pw.launches) == 1 and not _lock_free(settings)
    await asyncio.sleep(0.2)
    assert pw.contexts[0].closed and pw.stopped and _lock_free(settings)


async def test_profile_busy_is_temporarily_blocked(settings):
    settings.gen_timeout_s = 0.1
    settings.ensure_dirs()
    b = _browser(settings, FakePW())
    with FileLock(str(settings.chatgpt_lock_file)):
        with pytest.raises(GIAError) as e:
            async with b.page():
                pass
    assert e.value.code == "temporarily_blocked"


async def test_launch_failure_is_auth_failed_and_releases_lock(settings):
    b = _browser(settings, FakePW(fail=RuntimeError("no chrome")))
    with pytest.raises(GIAError) as e:
        async with b.page():
            pass
    assert e.value.code == "auth_failed" and "GIA_CHROME_PATH" in e.value.details["hint"]
    assert _lock_free(settings)


async def test_unexpected_error_tears_down_but_giaerror_keeps_browser(settings):
    pw = FakePW()
    b = _browser(settings, pw)
    with pytest.raises(GIAError):
        async with b.page():
            raise GIAError("timeout", "slow")
    assert not pw.contexts[0].closed
    with pytest.raises(RuntimeError):
        async with b.page():
            raise RuntimeError("target closed")
    assert pw.contexts[0].closed and _lock_free(settings)
    await b.aclose()


# --- ChatGPTPage over a fake Playwright page ---------------------------------
class FakeResponse:
    def __init__(self, url, status=200, method="POST"):
        self.url, self.status = url, status
        self.request = SimpleNamespace(method=method)

    async def finished(self):
        return None


class FakeInput:
    def __init__(self, page, accept):
        self.page, self.accept = page, accept

    async def get_attribute(self, name):
        return self.accept if name == "accept" else None

    async def set_input_files(self, files):
        self.page.uploaded.append((self.accept, files))
        for r in self.page.on_upload(files):
            self.page.emit(r)


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    async def wait_for(self, **kw):
        return None

    async def click(self):
        self.page.events.append(("click", self.selector))

    async def count(self):
        return len(self.page.file_inputs)

    def nth(self, i):
        return FakeInput(self.page, self.page.file_inputs[i])


class FakePage:
    def __init__(self):
        self.url = "https://chatgpt.com/"
        self.events, self.uploaded, self.handlers, self.goto_errors = [], [], [], []
        self.file_inputs = ["image/*", "image/*,video/*", None]
        self.on_upload = lambda files: []
        self.api_calls = []
        self.api_handler = lambda path, method, body: (200, {})
        self.keyboard = SimpleNamespace(type=self._type, press=self._press)

    async def _type(self, text, delay=0):
        self.events.append(("type", text))

    async def _press(self, key):
        self.events.append(("press", key))

    async def goto(self, url, wait_until=None):
        self.events.append(("goto", url))
        if self.goto_errors:
            raise self.goto_errors.pop(0)
        self.url = url

    def locator(self, selector):
        return FakeLocator(self, selector)

    def on(self, event, fn):
        self.handlers.append(fn)

    def remove_listener(self, event, fn):
        self.handlers.remove(fn)

    def emit(self, resp):
        for fn in list(self.handlers):
            fn(resp)

    async def evaluate(self, script, arg=None):
        if script is cw.JS_API:
            path, method, body = arg
            self.api_calls.append((path, method, body))
            status, data = self.api_handler(path, method, body)
            return {"status": status, "json": data}
        if script is cw.JS_BYTES:
            return {"status": 200, "b64": base64.b64encode(b"IMG:" + arg.encode()).decode()}
        if script is cw.JS_TZ:
            return ["America/Bogota", 300]
        if script is cw.JS_SESSION:
            return {"logged_in": True, "plan": "free"}
        raise AssertionError(f"unexpected script {script[:40]!r}")


def _cpage(fake, **kw):
    async def no_sleep(s):
        await asyncio.sleep(0)
    return cw.ChatGPTPage(fake, sleep=no_sleep, **kw)


async def test_open_retries_err_aborted_once():
    fake = FakePage()
    fake.goto_errors = [RuntimeError("Page.goto: net::ERR_ABORTED at https://chatgpt.com/")]
    await _cpage(fake).open("https://chatgpt.com/")
    assert [e for e in fake.events if e[0] == "goto"] == [("goto", "https://chatgpt.com/")] * 2
    fake.goto_errors = [RuntimeError("net::ERR_ABORTED"), RuntimeError("net::ERR_ABORTED")]
    with pytest.raises(RuntimeError):
        await _cpage(fake).open("https://chatgpt.com/")


async def test_send_keeps_line_breaks_inside_the_message():
    fake = FakePage()
    await _cpage(fake).send("linea uno\n\nlinea dos")
    keys = [e for e in fake.events if e[0] in ("type", "press")]
    assert keys == [("type", "linea uno"), ("press", "Shift+Enter"), ("press", "Shift+Enter"),
                    ("type", "linea dos"), ("press", "Enter")]


async def test_attach_waits_for_one_finished_upload_per_file(tmp_path):
    fake = FakePage()
    up = "https://chatgpt.com" + cw.UPLOAD_PATH
    fake.on_upload = lambda files: [FakeResponse(up, method="GET"), FakeResponse(up, status=500),
                                    *[FakeResponse(up) for _ in files]]
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    await _cpage(fake).attach(paths)
    assert fake.uploaded == [("image/*", [str(p) for p in paths])]
    assert fake.handlers == []


async def test_attach_times_out_without_upload_signal(tmp_path):
    fake = FakePage()
    with pytest.raises(GIAError) as e:
        await _cpage(fake, upload_timeout_s=0.05, clock=iter_clock()).attach([tmp_path / "a.png"])
    assert e.value.code == "upstream_error" and e.value.details == {"expected": 1, "finished": 0}
    assert fake.handlers == []


def iter_clock():
    ticks = iter(x * 0.02 for x in range(10_000))
    return lambda: next(ticks)


async def test_attach_without_refs_does_nothing():
    fake = FakePage()
    await _cpage(fake).attach([])
    assert fake.uploaded == []


async def test_download_follows_the_signed_url():
    fake = FakePage()
    fake.api_handler = lambda path, method, body: (200, {"download_url": "https://chatgpt.com/backend-api/estuary/content?id=f"})
    data = await _cpage(fake).download("conv", "file_1")
    assert data == b"IMG:https://chatgpt.com/backend-api/estuary/content?id=f"
    assert fake.api_calls == [("/backend-api/files/download/file_1?conversation_id=conv&inline=false", "GET", None)]


async def test_download_without_url_is_upstream_error():
    fake = FakePage()
    fake.api_handler = lambda path, method, body: (404, None)
    with pytest.raises(GIAError) as e:
        await _cpage(fake).download("conv", "file_1")
    assert e.value.code == "upstream_error"


async def test_image_quota_sends_the_page_timezone():
    fake = FakePage()
    fake.api_handler = lambda path, method, body: (200, {"limits_progress": [
        {"feature_name": "image_gen", "remaining": 2, "reset_after": "R"}]})
    assert await _cpage(fake).image_quota() == {"remaining": 2, "reset_after": "R"}
    path, method, body = fake.api_calls[0]
    assert (path, method) == ("/backend-api/conversation/init", "POST")
    assert body["timezone"] == "America/Bogota" and body["timezone_offset_min"] == 300


async def test_watch_blocking_collects_only_blocking_posts():
    fake = FakePage()
    page = _cpage(fake)
    with page.watch_blocking() as blocked:
        fake.emit(FakeResponse("https://chatgpt.com/backend-api/f/conversation", 403))
        fake.emit(FakeResponse("https://chatgpt.com/backend-api/conversation/x", 404, method="GET"))
    assert blocked == [("https://chatgpt.com/backend-api/f/conversation", 403)]
    assert fake.handlers == []
```

- [ ] **Paso 2: Ejecutarlos y ver que fallan**

Run: `uv run pytest tests/test_chatgpt_browser.py -q`
Expected: FAIL (`AttributeError: module 'musa.chatgpt_web' has no attribute 'ChatGPTBrowser'`).

- [ ] **Paso 3: Implementar**

En `src/musa/chatgpt_web.py`, sustituir el bloque de imports por:

```python
from __future__ import annotations

import asyncio
import base64
import os
import platform
import re
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from filelock import AsyncFileLock, Timeout as LockTimeout

from .errors import AUTH_FAILED, TEMPORARILY_BLOCKED, UPSTREAM_ERROR, GIAError
```

Y añadir al final del archivo:

```python
# -- browser -------------------------------------------------------------------
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# --use-mock-keychain makes Chrome unable to decrypt the cookies saved with the macOS keychain (it then drops
# them: the session is lost); --enable-automation sets navigator.webdriver and the automation infobar.
DROPPED_DEFAULT_ARGS = ["--enable-automation", "--use-mock-keychain"]
UPLOAD_TIMEOUT_S = 90.0
TYPE_DELAY_MS = 10
COMPOSER = "#prompt-textarea, div[contenteditable='true']"


async def _start_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise GIAError(AUTH_FAILED, "playwright is not installed", hint="run: uv sync") from e
    return await async_playwright().start()


def _client_hint_platform() -> tuple[str, str]:
    plat = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
    arch = "arm" if platform.machine().lower() in ("arm64", "aarch64") else "x86"
    return plat, arch


def ua_override(version_info: dict) -> dict:
    """Network.setUserAgentOverride params from Browser.getVersion: the installed binary's real UA and client
    hints, without the 'HeadlessChrome' marker."""
    full = version_info["product"].split("/", 1)[1]
    major = full.split(".", 1)[0]
    plat, arch = _client_hint_platform()
    return {"userAgent": version_info["userAgent"].replace("HeadlessChrome", "Chrome"),
            "userAgentMetadata": {
                "brands": [{"brand": "Google Chrome", "version": major}, {"brand": "Chromium", "version": major},
                           {"brand": "Not_A Brand", "version": "24"}],
                "fullVersion": full, "platform": plat, "platformVersion": "", "architecture": arch,
                "model": "", "mobile": False}}


class ChatGPTBrowser:
    """One headless Chrome on the dedicated profile, launched on demand over Playwright's pipe (no TCP port).

    The profile file lock is held while Chrome runs (Chrome refuses a profile open twice, and `musa auth
    --chatgpt` must not race it); the browser closes after `chatgpt_idle_close_s` without use."""

    def __init__(self, settings, start_playwright=None):
        self.settings = settings
        self._start = start_playwright or _start_playwright
        self._lock = asyncio.Lock()
        self._flock = AsyncFileLock(str(settings.chatgpt_lock_file))
        self._pw = self._ctx = self._page = None
        self._idle = None

    def launch_kwargs(self) -> dict:
        kw = {"user_data_dir": str(self.settings.chatgpt_profile_dir), "headless": True,
              "ignore_default_args": list(DROPPED_DEFAULT_ARGS), "args": list(LAUNCH_ARGS)}
        if self.settings.chrome_path:
            kw["executable_path"] = self.settings.chrome_path
        else:
            kw["channel"] = "chrome"
        return kw

    @asynccontextmanager
    async def page(self):
        """Exclusive use of the logged-in page; an unexpected error tears the browser down (relaunch next time)."""
        async with self._lock:
            self._cancel_idle()
            try:
                yield await self._ensure()
            except GIAError:
                raise
            except BaseException:
                await self._teardown()
                raise
            finally:
                if self._ctx is not None:
                    self._arm_idle()

    async def aclose(self) -> None:
        self._cancel_idle()
        async with self._lock:
            await self._teardown()

    async def _ensure(self):
        if self._page is not None:
            return self._page
        profile = self.settings.chatgpt_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        os.chmod(profile, 0o700)
        try:
            await self._flock.acquire(timeout=self.settings.gen_timeout_s)
        except LockTimeout:
            raise GIAError(TEMPORARILY_BLOCKED, "the ChatGPT Chrome profile is in use by another Musa process",
                           hint="retry when the other ChatGPT job or `musa auth --chatgpt` ends") from None
        try:
            self._pw = await self._start()
            self._ctx = await self._pw.chromium.launch_persistent_context(**self.launch_kwargs())
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            cdp = await self._ctx.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride", ua_override(await cdp.send("Browser.getVersion")))
            self._page = page
            return page
        except GIAError:
            await self._teardown()
            raise
        except Exception as e:
            await self._teardown()
            raise GIAError(AUTH_FAILED, f"could not start Google Chrome: {e}",
                           hint="install Google Chrome or set GIA_CHROME_PATH") from e

    async def _teardown(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._pw = self._ctx = self._page = None
        for close in (ctx.close if ctx else None, pw.stop if pw else None):
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        if self._flock.is_locked:
            await self._flock.release(force=True)

    def _arm_idle(self) -> None:
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(self.settings.chatgpt_idle_close_s,
                                     lambda: loop.create_task(self._close_if_idle()))

    def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    async def _close_if_idle(self) -> None:
        async with self._lock:
            self._idle = None
            await self._teardown()


# -- page adapter ----------------------------------------------------------------
JS_SESSION = """async () => {
  const r = await fetch('/api/auth/session', {credentials: 'include'});
  if (!r.ok) return {logged_in: false, plan: null, status: r.status};
  const s = await r.json();
  return {logged_in: !!s.accessToken, plan: (s.account || {}).planType || null};
}"""
# The bearer token is read and used inside the page; it never crosses into Python.
JS_API = """async ([path, method, body]) => {
  const s = await (await fetch('/api/auth/session', {credentials: 'include'})).json();
  const r = await fetch(path, {method, credentials: 'include',
    headers: {'Authorization': 'Bearer ' + (s.accessToken || ''), 'Content-Type': 'application/json'},
    body: body === null || body === undefined ? undefined : JSON.stringify(body)});
  const t = await r.text();
  let json = null; try { json = JSON.parse(t); } catch (e) {}
  return {status: r.status, json};
}"""
JS_BYTES = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, b64: btoa(s)};
}"""
JS_TZ = "() => [Intl.DateTimeFormat().resolvedOptions().timeZone, new Date().getTimezoneOffset()]"


class ChatGPTPage:
    """Thin adapter over a Playwright page: the backend reaches chatgpt.com only through these methods."""

    def __init__(self, page, upload_timeout_s: float = UPLOAD_TIMEOUT_S, clock=time.monotonic, sleep=asyncio.sleep):
        self._page = page
        self._upload_timeout_s = upload_timeout_s
        self._clock, self._sleep = clock, sleep

    @property
    def url(self) -> str:
        return self._page.url

    async def open(self, url: str) -> None:
        for attempt in (0, 1):
            try:
                await self._page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as e:
                if attempt or "ERR_ABORTED" not in str(e):
                    raise

    async def session(self) -> dict:
        return await self._page.evaluate(JS_SESSION)

    async def api(self, path: str, method: str = "GET", body=None) -> tuple[int, dict | None]:
        r = await self._page.evaluate(JS_API, [path, method, body])
        return r["status"], r.get("json")

    async def image_quota(self) -> dict | None:
        tz, offset = await self._page.evaluate(JS_TZ)
        status, data = await self.api("/backend-api/conversation/init", "POST", {
            "conversation_id": None, "conversation_origin": None, "gizmo_id": None,
            "requested_default_model": None, "system_hints": None,
            "timezone": tz, "timezone_offset_min": offset})
        return image_gen_quota(data) if status == 200 else None

    async def download(self, conversation_id: str, file_id: str) -> bytes:
        status, meta = await self.api(
            f"/backend-api/files/download/{file_id}?conversation_id={conversation_id}&inline=false")
        url = (meta or {}).get("download_url")
        if status != 200 or not url:
            raise GIAError(UPSTREAM_ERROR, f"ChatGPT gave no download link for {file_id} (HTTP {status})")
        got = await self._page.evaluate(JS_BYTES, url)
        if got["status"] != 200:
            raise GIAError(UPSTREAM_ERROR, f"ChatGPT image download failed (HTTP {got['status']})")
        return base64.b64decode(got["b64"])

    async def attach(self, paths: list[Path]) -> None:
        """Attach every reference in one go, then wait for one finished 200 POST to process_upload_stream per
        file. The send button is not used: it is only reachable through its localized aria-label."""
        if not paths:
            return
        finished: list[asyncio.Future] = []

        def on_response(resp) -> None:
            if (resp.request.method == "POST" and resp.status == 200
                    and resp.url.split("?", 1)[0].endswith(UPLOAD_PATH)):
                finished.append(asyncio.ensure_future(resp.finished()))

        self._page.on("response", on_response)
        try:
            inputs = self._page.locator("input[type='file']")
            target = None
            for i in range(await inputs.count()):
                if "image" in (await inputs.nth(i).get_attribute("accept") or ""):
                    target = inputs.nth(i)
                    break
            if target is None:
                raise GIAError(UPSTREAM_ERROR, "the ChatGPT page has no image upload input")
            await target.set_input_files([str(p) for p in paths])
            deadline = self._clock() + self._upload_timeout_s
            while True:
                done = [f for f in finished if f.done() and f.exception() is None and f.result() is None]
                if len(done) >= len(paths):
                    return
                if self._clock() >= deadline:
                    raise GIAError(UPSTREAM_ERROR, "the reference upload did not finish",
                                   expected=len(paths), finished=len(done))
                await self._sleep(0.25)
        finally:
            self._page.remove_listener("response", on_response)

    async def send(self, prompt: str) -> None:
        """Type the prompt (line breaks as Shift+Enter, which keeps them inside the message) and send with Enter."""
        box = self._page.locator(COMPOSER).first
        await box.wait_for(state="visible", timeout=30_000)
        await box.click()
        lines = prompt.split("\n")
        for i, line in enumerate(lines):
            if line:
                await self._page.keyboard.type(line, delay=TYPE_DELAY_MS)
            if i < len(lines) - 1:
                await self._page.keyboard.press("Shift+Enter")
        await self._page.keyboard.press("Enter")

    @contextmanager
    def watch_blocking(self):
        """Collect (url, status) of responses that mean ChatGPT refused the turn while the block runs."""
        blocked: list[tuple[str, int]] = []

        def on_response(resp) -> None:
            if is_blocking_response(resp.request.method, resp.url, resp.status):
                blocked.append((resp.url.split("?", 1)[0], resp.status))

        self._page.on("response", on_response)
        try:
            yield blocked
        finally:
            self._page.remove_listener("response", on_response)
```

- [ ] **Paso 4: Ejecutar los tests**

Run: `uv run pytest tests/test_chatgpt_browser.py tests/test_chatgpt_web.py -q`
Expected: PASS.

Nota sobre `test_attach_times_out_without_upload_signal`: el reloj inyectado avanza 0,02 por llamada, así que el límite de 0,05 se alcanza tras pocas iteraciones y el test no espera tiempo real.

- [ ] **Paso 5: Commit**

```bash
git add src/musa/chatgpt_web.py tests/test_chatgpt_browser.py
git commit -m "ChatGPT: navegador headless por pipe y adaptador de página"
```

---

### Tarea 4: `ChatGPTWebBackend`

**Archivos:**
- Modificar: `src/musa/client.py` (constantes y `append_record`; `GeminiWebBackend._append` pasa a usarlo)
- Modificar: `src/musa/chatgpt_web.py` (backend)
- Test: `tests/test_chatgpt_backend.py`

**Interfaces:**
- Consume: `ChatGPTBrowser`, `ChatGPTPage` y las funciones puras (Tareas 2-3); `auth.chatgpt_session_present` (Tarea 1).
- Produce:
  - `client.CHATGPT_MODEL = "chatgpt-web"`, `client.CHATGPT_LABEL = "ChatGPT (web)"`, `client.GEM_IGNORED = "gem_ignored"`
  - `client.append_record(state: StateStore, **kw) -> GenerationRecord`
  - `chatgpt_web.ChatGPTWebBackend(settings, state, browser=None, page_factory=ChatGPTPage, clock=time.monotonic, sleep=asyncio.sleep)` con `status`, `list_models`, `resolve_model_name`, `generate`, `edit`, `retry_download`, `delete_conversation` y `aclose`, con las mismas firmas que `GeminiWebBackend`.
  - `status()` → `{"session": bool, "image_gen": {"remaining", "reset_after", "checked_at"} | None}`, leído de `state_dir/chatgpt_quota.json`.

- [ ] **Paso 1: Escribir los tests (fallan)**

Crear `tests/test_chatgpt_backend.py`:

```python
import io
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from PIL import Image

from musa import chatgpt_web as cw
from musa.client import CHATGPT_MODEL
from musa.errors import GIAError
from musa.refs import ResolvedRef
from musa.state import StateStore

CID = "6af76eed-1aec-83e9-b323-d348dba09bfa"


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 12), (200, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def _msg(mid, role, t, status="finished_successfully", parts=None):
    return {"id": mid, "author": {"role": role}, "create_time": t, "status": status,
            "content": {"content_type": "multimodal_text", "parts": parts or []}}


def _img(fid):
    return {"content_type": "image_asset_pointer", "asset_pointer": f"sediment://{fid}", "width": 16, "height": 12}


def _conv(*msgs):
    return {"mapping": {m["id"]: {"message": m} for m in msgs}}


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeBrowser:
    def __init__(self):
        self.uses, self.closed = 0, False

    @asynccontextmanager
    async def page(self):
        self.uses += 1
        yield "raw-page"

    async def aclose(self):
        self.closed = True


class FakeChatGPT:
    """Stands in for ChatGPTPage: scripted conversation polls, quota, uploads and downloads."""

    def __init__(self, *, logged_in=True, quota=(3, "2026-11-14T20:21:58Z"), polls=None, before=None,
                 blocked=None, download_fail=False):
        self.url = "https://chatgpt.com/"
        self.logged_in, self.quota = logged_in, quota
        self.polls = list(polls or [])
        self.before = before
        self.blocked = blocked or []
        self.download_fail = download_fail
        self.calls = []

    async def open(self, url):
        self.calls.append(("open", url))

    async def session(self):
        if self.logged_in == "cloudflare":
            return {"logged_in": False, "plan": None, "status": 403}
        return {"logged_in": self.logged_in, "plan": "free"}

    async def image_quota(self):
        if self.quota is None:
            return None
        return {"remaining": self.quota[0], "reset_after": self.quota[1]}

    async def api(self, path, method="GET", body=None):
        self.calls.append(("api", method, path, body))
        if method == "PATCH":
            return 200, {"success": True}
        if path == f"/backend-api/conversation/{CID}":
            if self.before is not None and not any(c[0] == "send" for c in self.calls):
                return 200, self.before
            conv = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return 200, conv
        raise AssertionError(path)

    async def attach(self, paths):
        self.calls.append(("attach", [Path(p).name for p in paths]))

    async def send(self, prompt):
        self.calls.append(("send", prompt))
        self.url = f"https://chatgpt.com/c/{CID}"

    async def download(self, conversation_id, file_id):
        self.calls.append(("download", conversation_id, file_id))
        if self.download_fail:
            raise GIAError("upstream_error", "link expired")
        return png()

    @contextmanager
    def watch_blocking(self):
        yield self.blocked


@pytest.fixture
def make(settings, tmp_path):
    settings.output_root = tmp_path / "out"

    def build(fake):
        ft = FakeTime()
        state = StateStore(settings.state_dir)
        b = cw.ChatGPTWebBackend(settings, state, browser=FakeBrowser(), page_factory=lambda raw: fake,
                                 clock=ft.clock, sleep=ft.sleep)
        return b, state, ft
    return build


def _done(*parts_ids):
    return _conv(_msg("u1", "user", 1, parts=["x"]),
                 *[_msg(f"t{i}", "tool", 2 + i, parts=[_img(fid)]) for i, fid in enumerate(parts_ids)])


async def test_generate_saves_image_and_records_chatgpt_conversation(make, settings, tmp_path):
    fake = FakeChatGPT(polls=[_conv(_msg("u1", "user", 1)), _done("file_out")])
    b, state, _ = make(fake)
    ref = ResolvedRef(path=tmp_path / "ref.png", source="local", sha256="abc")
    res = await b.generate("Generate an image (do not search the web): un gato", [ref], model=CHATGPT_MODEL,
                           slug_source="un gato")
    assert res.status == "ok" and res.code is None and len(res.images) == 1
    assert res.images[0].path.exists() and res.chat_metadata == [CID]
    rec = state.conversation(res.conversation_id)
    assert rec.model == CHATGPT_MODEL and rec.chat_metadata == [CID] and rec.type == "generate"
    assert rec.images[0]["image_id"] == "file_out" and rec.refs[0]["sha256"] == "abc"
    assert ("attach", ["ref.png"]) in fake.calls and ("open", "https://chatgpt.com/") in fake.calls
    st = await b.status()
    assert st["image_gen"]["remaining"] == 3 and st["image_gen"]["reset_after"] == "2026-11-14T20:21:58Z"


async def test_edit_reopens_the_conversation_and_takes_only_new_images(make):
    old_user, old_tool = _msg("u1", "user", 1, parts=["x"]), _msg("t0", "tool", 2, parts=[_img("file_old")])
    before = _conv(old_user, old_tool)
    after = _conv(old_user, old_tool, _msg("u2", "user", 10), _msg("t9", "tool", 11, parts=[_img("file_new")]))
    b, state, _ = make(FakeChatGPT(polls=[before]))
    first = await b.generate("p", [], model=CHATGPT_MODEL)
    fake = FakeChatGPT(polls=[after], before=before)
    b.page_factory = lambda raw: fake
    res = await b.edit(first.conversation_id, "hazla verde", [], model="gemini-flash")
    assert res.status == "ok"
    rec = state.conversation(first.conversation_id)
    assert rec.type == "edit" and rec.parent_id == first.record_id and rec.images[0]["image_id"] == "file_new"
    assert ("open", f"https://chatgpt.com/c/{CID}") in fake.calls


async def test_edit_unknown_conversation(make):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    with pytest.raises(GIAError) as e:
        await b.edit("c_missing", "x", [], model=CHATGPT_MODEL)
    assert e.value.code == "unknown_conversation"


async def test_not_logged_in_is_auth_expired_without_record(make, settings):
    b, state, _ = make(FakeChatGPT(logged_in=False, polls=[_done("f")]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "error" and res.code == "auth_expired"
    assert state.load_all() == []


async def test_cloudflare_on_the_session_check_is_temporarily_blocked(make, settings):
    b, state, _ = make(FakeChatGPT(logged_in="cloudflare", polls=[_done("f")]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "temporarily_blocked" and state.load_all() == []


async def test_exhausted_quota_is_usage_limit(make):
    fake = FakeChatGPT(quota=(0, "R"), polls=[_done("f")])
    b, _, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "usage_limit" and not any(c[0] == "send" for c in fake.calls)


async def test_blocked_send_is_temporarily_blocked(make):
    fake = FakeChatGPT(blocked=[("https://chatgpt.com/backend-api/f/conversation", 403)], polls=[_done("f")])
    b, _, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "temporarily_blocked"


async def test_text_reply_without_tool_becomes_no_image_after_30s(make):
    refusal = _conv(_msg("u1", "user", 1), _msg("a1", "assistant", 2, parts=["No puedo generar eso."]))
    b, state, ft = make(FakeChatGPT(polls=[refusal]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "error" and res.code == "no_image" and res.gemini_text == "No puedo generar eso."
    assert ft.t >= cw.NO_IMAGE_STABLE_S
    assert state.load_all()[-1].code == "no_image"


async def test_pending_tool_message_is_not_no_image(make):
    pending = _conv(_msg("u1", "user", 1), _msg("a1", "assistant", 2, parts=["Creando imagen"]),
                    _msg("t1", "tool", 3, status="in_progress"))
    polls = [pending] * 12 + [_done("file_late")]
    b, _, _ = make(FakeChatGPT(polls=polls))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "ok"


async def test_no_image_until_timeout_is_timeout(make, settings):
    settings.gen_timeout_s = 20
    b, _, _ = make(FakeChatGPT(polls=[_conv(_msg("u1", "user", 1))]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "timeout"


async def test_download_failure_then_retry_download(make):
    fake = FakeChatGPT(polls=[_done("file_out")], download_fail=True)
    b, state, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "generated_not_saved" and res.code == "generated_not_saved"
    assert state.conversation(res.conversation_id).images[0]["path"] is None
    fake.download_fail = False
    saved = await b.retry_download(res.conversation_id, 0)
    assert saved.path.exists()
    rec = state.conversation(res.conversation_id)
    assert rec.type == "retry_download" and rec.status == "ok" and rec.images[0]["path"] == str(saved.path)
    assert ("download", CID, "file_out") in fake.calls


async def test_gem_is_ignored_with_its_own_warning_code(make):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    res = await b.generate("p", [], model=CHATGPT_MODEL, gem_id="gem_1")
    assert res.status == "ok" and res.warning["code"] == "gem_ignored" and res.warning["gem_id"] == "gem_1"


async def test_delete_conversation_hides_it(make):
    fake = FakeChatGPT(polls=[_done("f")])
    b, state, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    await b.delete_conversation(res.conversation_id)
    assert ("api", "PATCH", f"/backend-api/conversation/{CID}", {"is_visible": False}) in fake.calls
    assert state.conversation(res.conversation_id) is None


async def test_list_models_reflects_the_session_cookie(make, monkeypatch):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    monkeypatch.setattr("musa.auth.chatgpt_session_present", lambda p: True)
    assert await b.list_models() == [{"model_name": "chatgpt-web", "display_name": "ChatGPT (web)", "available": True}]
    monkeypatch.setattr("musa.auth.chatgpt_session_present", lambda p: False)
    assert (await b.list_models())[0]["available"] is False
    assert (await b.status())["session"] is False
    assert await b.resolve_model_name("chatgpt-web") == "chatgpt-web"


async def test_aclose_closes_the_browser(make):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    await b.aclose()
    assert b.browser.closed
```

- [ ] **Paso 2: Ejecutarlos y ver que fallan**

Run: `uv run pytest tests/test_chatgpt_backend.py -q`
Expected: FAIL (`ImportError: cannot import name 'CHATGPT_MODEL' from 'musa.client'`).

- [ ] **Paso 3: Constantes y `append_record` en `client.py`**

En `src/musa/client.py`, después de `GEM_CACHE_TTL_S = 60.0` (línea 54), añadir:

```python
# gem_ignored: ChatGPT has no Gems. A different code from gem_missing on purpose: ImageService._run
# (service.py:263-266) removes the style's Gem entry when a result carries GEM_MISSING with its gem_id.
GEM_IGNORED = "gem_ignored"
CHATGPT_MODEL = "chatgpt-web"
CHATGPT_LABEL = "ChatGPT (web)"
```

Después de `def _now()` (línea 127-128), añadir:

```python
def append_record(state: StateStore, **kw) -> GenerationRecord:
    """Append one generations.jsonl record with the defaults every backend shares."""
    base = dict(id=StateStore.new_id("g"), ts=_now(), style=None, style_text=None, aspect_hint=None,
                model=None, project=None, subdir=None, refs=[], images=[], status="ok", code=None,
                gemini_text="", elapsed_s=0.0, parent_id=None, chat_metadata=None, prompt="",
                user_prompt=None, gem_id=None)
    base.update(kw)
    rec = GenerationRecord(**base)
    state.append(rec)
    return rec
```

Sustituir el cuerpo de `GeminiWebBackend._append` (líneas 421-429) por:

```python
    def _append(self, **kw) -> GenerationRecord:
        return append_record(self.state, **kw)
```

- [ ] **Paso 4: Implementar el backend**

En `src/musa/chatgpt_web.py`, ampliar los imports:

```python
from . import auth as _auth
from .client import (CHATGPT_LABEL, CHATGPT_MODEL, GEM_IGNORED, Backend, GenerateResult, _map_exc, _now,
                     append_record)
from .errors import (AUTH_EXPIRED, AUTH_FAILED, GENERATED_NOT_SAVED, NO_IMAGE, TEMPORARILY_BLOCKED, TIMEOUT,
                     UNKNOWN_CONVERSATION, UPSTREAM_ERROR, USAGE_LIMIT, GIAError)
from .state import StateStore
from .storage import SavedImage, save_image
from .styles import GENERATE_PREFIX
```

Y añadir `import json` a los imports estándar. Al final del archivo, añadir:

```python
# -- backend ---------------------------------------------------------------------
POLL_S = 4.0
NO_IMAGE_STABLE_S = 30.0
CONV_ID_TIMEOUT_S = 60.0


def _gem_warning(gem_id: str | None) -> dict | None:
    if not gem_id:
        return None
    return {"code": GEM_IGNORED, "gem_id": gem_id,
            "message": "ChatGPT does not use Gems; the style was applied as text only.",
            "next_action": "Nothing to do; pick a Gemini model to use the style's Gem."}


def _raise_if_blocked(blocked: list[tuple[str, int]]) -> None:
    if blocked:
        url, status = blocked[0]
        raise GIAError(TEMPORARILY_BLOCKED, f"ChatGPT refused the request (HTTP {status} on {url})")


class ChatGPTWebBackend(Backend):
    """Image generation through the user's chatgpt.com session (model 'chatgpt-web')."""

    def __init__(self, settings, state: StateStore, browser: ChatGPTBrowser | None = None,
                 page_factory=ChatGPTPage, clock=time.monotonic, sleep=asyncio.sleep):
        self.settings, self.state = settings, state
        self.browser = browser or ChatGPTBrowser(settings)
        self.page_factory = page_factory
        self._clock, self._sleep = clock, sleep

    # -- session / quota -------------------------------------------------------------
    @property
    def _quota_file(self) -> Path:
        return Path(self.settings.state_dir) / "chatgpt_quota.json"

    def _cached_quota(self) -> dict | None:
        try:
            return json.loads(self._quota_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _store_quota(self, quota: dict | None) -> None:
        if not quota:
            return
        tmp = self._quota_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({**quota, "checked_at": _now()}), encoding="utf-8")
        os.replace(tmp, self._quota_file)

    def _session_present(self) -> bool:
        return _auth.chatgpt_session_present(self.settings.chatgpt_profile_dir)

    async def status(self) -> dict:
        """Never launches Chrome: session from the cookie store, quota as of the last ChatGPT call."""
        return {"session": self._session_present(), "image_gen": self._cached_quota()}

    async def list_models(self) -> list[dict]:
        return [{"model_name": CHATGPT_MODEL, "display_name": CHATGPT_LABEL, "available": self._session_present()}]

    async def resolve_model_name(self, name: str) -> str:
        return name

    async def aclose(self) -> None:
        await self.browser.aclose()

    # -- generate / edit -----------------------------------------------------------------
    async def generate(self, prompt, refs, *, model, progress=None, project=None, subdir=None, style=None,
                       style_text=None, aspect_hint=None, slug_source=None, gem_id=None) -> GenerateResult:
        return await self._turn("generate", StateStore.new_id("c"), None, prompt, refs, progress=progress,
                                project=project, subdir=subdir, style=style, style_text=style_text,
                                aspect_hint=aspect_hint, slug_source=slug_source, gem_id=gem_id)

    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, project=None, subdir=None,
                   style=None, style_text=None, aspect_hint=None, slug_source=None, gem_id=None) -> GenerateResult:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} not found", hint="call generate first")
        return await self._turn("edit", conversation_id, rec, prompt, refs, progress=progress,
                                project=project if project is not None else rec.project,
                                subdir=subdir if subdir is not None else rec.subdir, style=style,
                                style_text=style_text, aspect_hint=aspect_hint, slug_source=slug_source, gem_id=gem_id)

    async def _turn(self, kind, conv, rec, prompt, refs, *, progress, project, subdir, style, style_text,
                    aspect_hint, slug_source, gem_id) -> GenerateResult:
        t0 = self._clock()
        warning = _gem_warning(gem_id)
        refs_meta = [{"source": r.source, "path": str(r.path), "sha256": r.sha256} for r in refs]
        chat_id = rec.chat_metadata[0] if rec else None
        saved: list[SavedImage] = []
        entries: list[dict] = []
        errors: list[str] = []
        try:
            async with self.browser.page() as raw:
                page = self.page_factory(raw)
                await page.open(f"{BASE_URL}/c/{chat_id}" if chat_id else f"{BASE_URL}/")
                sess = await page.session()
                if sess.get("status") in (403, 429):  # Cloudflare challenge / rate limit, not a lost session
                    raise GIAError(TEMPORARILY_BLOCKED, f"ChatGPT refused the session check (HTTP {sess['status']})")
                if not sess.get("logged_in"):
                    raise GIAError(AUTH_EXPIRED, "no ChatGPT session in Musa's Chrome profile",
                                   hint="run: musa auth --chatgpt")
                quota = await page.image_quota()
                self._store_quota(quota)
                if quota and quota.get("remaining") == 0:
                    raise GIAError(USAGE_LIMIT, "ChatGPT image quota exhausted", resets_at=quota.get("reset_after"))
                known: set[str] = set()
                if chat_id:
                    status, data = await page.api(f"/backend-api/conversation/{chat_id}")
                    if status != 200 or not data:
                        raise GIAError(UPSTREAM_ERROR, f"could not read the ChatGPT conversation (HTTP {status})")
                    known = message_ids(data)
                if progress:
                    await progress("generating", 0.3)
                with page.watch_blocking() as blocked:
                    await page.attach([r.path for r in refs])
                    await page.send(prompt)
                    chat_id = chat_id or await self._await_conversation_id(page, blocked)
                    images, text = await self._await_images(page, chat_id, known, blocked)
                for i, img in enumerate(images):
                    if progress:
                        await progress("downloading", 0.6 + 0.4 * i / len(images))
                    entry = {"index": i, "path": None, "cid": "", "rid": "", "rcid": "", "image_id": img.file_id,
                             "url": ""}
                    try:
                        s = self._save(await page.download(chat_id, img.file_id), prompt=prompt, conv=conv,
                                       refs_meta=refs_meta, project=project, subdir=subdir, index=i,
                                       slug_source=slug_source)
                        saved.append(s)
                        entry.update(path=str(s.path), width=s.width, height=s.height, bytes=s.nbytes,
                                     format=s.format, full_size=s.full_size)
                    except Exception as e:
                        errors.append(f"{type(e).__name__}: {e}")  # kept with path=None for retry_download
                    entries.append(entry)
                self._store_quota(await page.image_quota())
        except Exception as e:
            err = _map_exc(e)
            return GenerateResult("error", err.code, conv, [], str(err), self._clock() - t0,
                                  list(rec.chat_metadata) if rec else None, warning=warning)
        elapsed = self._clock() - t0
        if not entries:
            status, code = "error", NO_IMAGE
        elif len(saved) < len(entries):
            status, code = "generated_not_saved", GENERATED_NOT_SAVED
        else:
            status, code = "ok", None
        appended = append_record(self.state, type=kind, conversation_id=conv, chat_metadata=[chat_id], prompt=prompt,
                                 style=style, style_text=style_text, aspect_hint=aspect_hint, model=CHATGPT_MODEL,
                                 project=project, subdir=subdir, refs=refs_meta, images=entries, status=status,
                                 code=code, gemini_text=text or "", elapsed_s=elapsed,
                                 parent_id=rec.id if rec else None, user_prompt=slug_source)
        return GenerateResult(status, code, conv, saved, text or "", elapsed, [chat_id],
                              save_error=errors[0] if errors else None, record_id=appended.id, warning=warning)

    async def _await_conversation_id(self, page, blocked) -> str:
        deadline = self._clock() + CONV_ID_TIMEOUT_S
        while True:
            _raise_if_blocked(blocked)
            cid = conversation_id_from_url(page.url)
            if cid:
                return cid
            if self._clock() >= deadline:
                raise GIAError(UPSTREAM_ERROR, "ChatGPT did not open a conversation after sending the prompt")
            await self._sleep(1.0)

    async def _await_images(self, page, chat_id, known, blocked) -> tuple[list[OutputImage], str | None]:
        """Poll the conversation until new output images appear. A finished assistant reply with no tool message
        that stays unchanged for NO_IMAGE_STABLE_S is a refusal: the image tool is asynchronous and may still
        arrive after the assistant ends its turn."""
        deadline = self._clock() + self.settings.gen_timeout_s
        last_sig, stable_since = None, None
        while True:
            _raise_if_blocked(blocked)
            status, data = await page.api(f"/backend-api/conversation/{chat_id}")
            if status == 200 and data:
                fresh = new_messages(data, known)
                images = output_images(fresh)
                if images:
                    return images, assistant_text(fresh)
                text = assistant_text(fresh)
                if text is not None and not has_tool_message(fresh):
                    sig = tuple((m.get("id"), m.get("status")) for m in fresh)
                    if sig != last_sig:
                        last_sig, stable_since = sig, self._clock()
                    elif self._clock() - stable_since >= NO_IMAGE_STABLE_S:
                        return [], text
                else:
                    last_sig = stable_since = None
            if self._clock() >= deadline:
                raise GIAError(TIMEOUT, "ChatGPT returned no image before gen_timeout_s")
            await self._sleep(POLL_S)

    def _save(self, data, *, prompt, conv, refs_meta, project, subdir, index, slug_source) -> SavedImage:
        meta = {"prompt": prompt, "model": CHATGPT_MODEL, "conversation_id": conv,
                "ref_sha256": [r["sha256"] for r in refs_meta]}
        return save_image(data, output_root=self.settings.output_root, project=project, subdir=subdir,
                          prompt=slug_source or prompt.removeprefix(GENERATE_PREFIX), index=index, meta=meta,
                          write_sidecar=self.settings.write_sidecar, thumb_size=self.settings.thumb_size)

    # -- retry / delete -----------------------------------------------------------------
    async def retry_download(self, conversation_id, image_index) -> SavedImage:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} not found")
        entry = next((e for e in rec.images if e.get("index") == image_index), None)
        if entry is None or not entry.get("image_id"):
            raise GIAError(UPSTREAM_ERROR, f"no re-downloadable image {image_index} in {conversation_id}")
        try:
            async with self.browser.page() as raw:
                page = self.page_factory(raw)
                await page.open(f"{BASE_URL}/")
                data = await page.download(rec.chat_metadata[0], entry["image_id"])
        except Exception as e:
            raise _map_exc(e) from e
        saved = self._save(data, prompt=rec.prompt, conv=conversation_id, refs_meta=rec.refs or [],
                           project=rec.project, subdir=rec.subdir, index=image_index, slug_source=rec.user_prompt)
        images = [dict(e) for e in rec.images]
        for e in images:
            if e.get("index") == image_index:
                e.update(path=str(saved.path), width=saved.width, height=saved.height, bytes=saved.nbytes,
                         format=saved.format, full_size=saved.full_size)
        ok = all(e.get("path") for e in images)
        append_record(self.state, type="retry_download", conversation_id=conversation_id,
                      chat_metadata=rec.chat_metadata, prompt=rec.prompt, style=rec.style, style_text=rec.style_text,
                      aspect_hint=rec.aspect_hint, model=rec.model, project=rec.project, subdir=rec.subdir,
                      refs=rec.refs or [], images=images, status="ok" if ok else rec.status,
                      code=None if ok else rec.code, gemini_text=rec.gemini_text, parent_id=rec.id,
                      user_prompt=rec.user_prompt)
        return saved

    async def delete_conversation(self, conversation_id) -> None:
        """Hide the conversation from the ChatGPT sidebar (is_visible=false) and mark it deleted locally."""
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} not found")
        try:
            async with self.browser.page() as raw:
                page = self.page_factory(raw)
                await page.open(f"{BASE_URL}/")
                status, _ = await page.api(f"/backend-api/conversation/{rec.chat_metadata[0]}", "PATCH",
                                           {"is_visible": False})
        except Exception as e:
            raise _map_exc(e) from e
        if status != 200:
            raise GIAError(UPSTREAM_ERROR, f"ChatGPT did not hide the conversation (HTTP {status})")
        append_record(self.state, type="delete", conversation_id=conversation_id, chat_metadata=None, parent_id=rec.id)
```

Nota: `test_list_models_reflects_the_session_cookie` aplica el monkeypatch sobre `musa.auth.chatgpt_session_present`. El backend lo llama como `_auth.chatgpt_session_present(...)` (atributo del módulo), así que el parche funciona.

- [ ] **Paso 5: Ejecutar los tests**

Run: `uv run pytest tests/test_chatgpt_backend.py tests/test_chatgpt_browser.py tests/test_chatgpt_web.py tests/test_client.py -q`
Expected: PASS. `test_client.py` sigue pasando con `_append` delegando en `append_record`.

- [ ] **Paso 6: Suite completa y commit**

Run: `uv run pytest -q` → PASS.

```bash
git add src/musa/client.py src/musa/chatgpt_web.py tests/test_chatgpt_backend.py
git commit -m "ChatGPT: backend de generación, edición, descarga y borrado"
```

---

### Tarea 5: `RoutedBackend`, `build_backend` y etiqueta de modelo

**Archivos:**
- Modificar: `src/musa/client.py` (`RoutedBackend` antes de `build_backend`; `build_backend:565-571`)
- Modificar: `src/musa/jobs.py:35-36` (`MODEL_LABELS`)
- Test: `tests/test_router.py` (nuevo), `tests/test_client.py:403-405`, `tests/test_jobs.py`

**Interfaces:**
- Consume: `CHATGPT_MODEL` (Tarea 4) y `ChatGPTWebBackend(settings, state)` (Tarea 4).
- Produce: `client.RoutedBackend(gemini: Backend, chatgpt: Backend, state: StateStore)` con los atributos públicos `gemini`, `chatgpt` y `state`. `build_backend(...)` devuelve un `RoutedBackend`.

- [ ] **Paso 1: Escribir los tests (fallan)**

Crear `tests/test_router.py`:

```python
import pytest

from musa.client import CHATGPT_MODEL, Backend, GenerateResult, RoutedBackend, append_record
from musa.state import StateStore


class Side(Backend):
    def __init__(self, name):
        self.name, self.calls, self.closed = name, [], False

    async def status(self):
        return {"valid": True, "who": self.name}

    async def list_models(self):
        return [{"model_name": f"{self.name}-m", "available": True}]

    async def resolve_model_name(self, name):
        return f"{self.name}:{name}"

    async def generate(self, prompt, refs, *, model, **kw):
        self.calls.append(("generate", model))
        return GenerateResult("ok", None, "c_x", [], self.name, 0.0, None)

    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        self.calls.append(("edit", conversation_id))
        return GenerateResult("ok", None, conversation_id, [], self.name, 0.0, None)

    async def retry_download(self, conversation_id, image_index):
        self.calls.append(("retry", conversation_id))

    async def delete_conversation(self, conversation_id):
        self.calls.append(("delete", conversation_id))

    async def create_gem(self, name, instructions, description=""):
        self.calls.append(("create_gem", name))
        return "gem_1"

    async def gem_exists(self, gem_id):
        return True

    async def quota(self):
        return {"window_5h_pct": 1, "who": self.name}

    async def aclose(self):
        self.closed = True


@pytest.fixture
def routed(settings):
    state = StateStore(settings.state_dir)
    append_record(state, type="generate", conversation_id="c_gpt", chat_metadata=["x"], model=CHATGPT_MODEL)
    append_record(state, type="generate", conversation_id="c_gem", chat_metadata=["a", "b", "c"], model="gemini-flash")
    g, c = Side("gemini"), Side("chatgpt")
    return RoutedBackend(g, c, state), g, c


async def test_generate_routes_by_model(routed):
    r, g, c = routed
    assert (await r.generate("p", [], model=CHATGPT_MODEL)).gemini_text == "chatgpt"
    assert (await r.generate("p", [], model="gemini-flash")).gemini_text == "gemini"
    assert (await r.generate("p", [], model=None)).gemini_text == "gemini"


async def test_conversation_operations_follow_the_recorded_model(routed):
    r, g, c = routed
    await r.edit("c_gpt", "x", [], model="gemini-flash")
    await r.retry_download("c_gpt", 0)
    await r.delete_conversation("c_gpt")
    await r.edit("c_gem", "x", [], model="gemini-flash")
    await r.edit("c_unknown", "x", [], model="gemini-flash")
    assert c.calls == [("edit", "c_gpt"), ("retry", "c_gpt"), ("delete", "c_gpt")]
    assert g.calls == [("edit", "c_gem"), ("edit", "c_unknown")]


async def test_models_status_quota_and_gems(routed):
    r, g, c = routed
    assert [m["model_name"] for m in await r.list_models()] == ["gemini-m", "chatgpt-m"]
    assert await r.resolve_model_name(CHATGPT_MODEL) == CHATGPT_MODEL
    assert await r.resolve_model_name("gemini-flash") == "gemini:gemini-flash"
    st = await r.status()
    assert st["who"] == "gemini" and st["chatgpt"]["who"] == "chatgpt"
    assert (await r.quota())["who"] == "gemini"
    assert await r.create_gem("n", "i") == "gem_1" and g.calls == [("create_gem", "n")]
    assert await r.gem_exists("gem_1") is True


async def test_status_survives_a_chatgpt_failure(routed):
    r, g, c = routed

    async def boom():
        raise RuntimeError("cookie db locked")
    c.status = boom
    st = await r.status()
    assert st["chatgpt"]["session"] is False and "cookie db locked" in st["chatgpt"]["error"]


async def test_aclose_closes_both(routed):
    r, g, c = routed
    await r.aclose()
    assert g.closed and c.closed
```

En `tests/test_client.py`, sustituir `test_build_backend` (líneas 403-405) por:

```python
async def test_build_backend(settings, monkeypatch):
    monkeypatch.setattr(client_mod, "_default_client_cls", FakeClient)
    b = build_backend(settings)
    assert isinstance(b, client_mod.RoutedBackend) and isinstance(b.gemini, GeminiWebBackend)
    assert type(b.chatgpt).__name__ == "ChatGPTWebBackend"
```

En `tests/test_jobs.py`, añadir:

```python
def test_model_entries_label_chatgpt():
    got = model_entries([{"model_name": "chatgpt-web", "display_name": "ChatGPT (web)", "available": True}])
    assert got[1] == {"id": "chatgpt-web", "name": "ChatGPT (web)", "description": "ChatGPT (web)"}
```

- [ ] **Paso 2: Ejecutarlos y ver que fallan**

Run: `uv run pytest tests/test_router.py tests/test_client.py::test_build_backend tests/test_jobs.py::test_model_entries_label_chatgpt -q`
Expected: FAIL (`ImportError: cannot import name 'RoutedBackend'`).

- [ ] **Paso 3: Implementar**

En `src/musa/client.py`, justo antes de `def build_backend`, añadir:

```python
class RoutedBackend(Backend):
    """Gemini plus ChatGPT (web). New generations route by model; anything about an existing conversation
    routes by the model it was generated with. Gems and the quota guard are Gemini's."""

    def __init__(self, gemini: Backend, chatgpt: Backend, state: StateStore):
        self.gemini, self.chatgpt, self.state = gemini, chatgpt, state

    def _for(self, conversation_id) -> Backend:
        rec = self.state.conversation(conversation_id)
        return self.chatgpt if rec is not None and rec.model == CHATGPT_MODEL else self.gemini

    async def status(self) -> dict:
        st = await self.gemini.status()
        try:
            st["chatgpt"] = await self.chatgpt.status()
        except Exception as e:
            st["chatgpt"] = {"session": False, "image_gen": None, "error": f"{type(e).__name__}: {e}"}
        return st

    async def list_models(self) -> list[dict]:
        return [*await self.gemini.list_models(), *await self.chatgpt.list_models()]

    async def resolve_model_name(self, name: str) -> str:
        return name if name == CHATGPT_MODEL else await self.gemini.resolve_model_name(name)

    async def generate(self, prompt, refs, *, model, progress=None, **kw) -> GenerateResult:
        target = self.chatgpt if model == CHATGPT_MODEL else self.gemini
        return await target.generate(prompt, refs, model=model, progress=progress, **kw)

    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, **kw) -> GenerateResult:
        return await self._for(conversation_id).edit(conversation_id, prompt, refs, model=model, progress=progress, **kw)

    async def retry_download(self, conversation_id, image_index) -> SavedImage:
        return await self._for(conversation_id).retry_download(conversation_id, image_index)

    async def delete_conversation(self, conversation_id) -> None:
        await self._for(conversation_id).delete_conversation(conversation_id)

    async def create_gem(self, name: str, instructions: str, description: str = "") -> str:
        return await self.gemini.create_gem(name, instructions, description=description)

    async def update_gem(self, gem_id: str, name: str, instructions: str, description: str = "") -> None:
        await self.gemini.update_gem(gem_id, name, instructions, description=description)

    async def delete_gem(self, gem_id: str) -> None:
        await self.gemini.delete_gem(gem_id)

    async def gem_exists(self, gem_id: str) -> bool:
        return await self.gemini.gem_exists(gem_id)

    async def quota(self) -> dict | None:
        return await self.gemini.quota()

    async def aclose(self) -> None:
        try:
            await self.chatgpt.aclose()
        finally:
            await self.gemini.aclose()
```

Sustituir la última línea de `build_backend` (`return GeminiWebBackend(...)`) por:

```python
    from .chatgpt_web import ChatGPTWebBackend  # local import: chatgpt_web imports this module
    return RoutedBackend(GeminiWebBackend(settings, state, client_cls=client_cls, auto_close=auto_close),
                         ChatGPTWebBackend(settings, state), state)
```

En `src/musa/jobs.py`, cambiar `MODEL_LABELS` por:

```python
MODEL_LABELS = {"gemini-flash": "Flash", "gemini-flash-lite": "Flash Lite", "gemini-pro": "Pro",
                "gemini-carbon": "Carbon", "gemini-4-hydrogen": "Hydrogen", "chatgpt-web": "ChatGPT (web)"}
```

- [ ] **Paso 4: Ejecutar la suite completa**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Paso 5: Commit**

```bash
git add src/musa/client.py src/musa/jobs.py tests/test_router.py tests/test_client.py tests/test_jobs.py
git commit -m "ChatGPT: enrutador de backends por modelo y conversación"
```

---

### Tarea 6: CLI, MCP, textos de error, frontend y README

**Archivos:**
- Modificar: `src/musa/cli.py:135-150` (`auth`)
- Modificar: `src/musa/mcp_server.py:7` (import `Literal`), `:32-71` (`INSTRUCTIONS`), `:161-171` (`auth_login`)
- Modificar: `src/musa/errors.py:34-49` (`NEXT_ACTION`)
- Modificar: `src/musa/web/app.js:95-108` (`ERR_ES`)
- Modificar: `README.md`
- Test: `tests/test_cli.py`, `tests/test_mcp_server.py`, `tests/test_errors.py`

**Interfaces:**
- Consume: `auth.login_chatgpt(settings)` (Tarea 1) y `RoutedBackend.chatgpt` (Tarea 5).

- [ ] **Paso 1: Escribir los tests (fallan)**

En `tests/test_cli.py`, añadir:

```python
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
```

En `tests/test_mcp_server.py`, añadir:

```python
async def test_auth_login_chatgpt_closes_the_browser_first(server, backend, monkeypatch):
    order = []

    class ChatGPTSide:
        async def aclose(self):
            order.append("aclose")

    backend.chatgpt = ChatGPTSide()  # same object the server holds; auth_login looks it up at call time
    monkeypatch.setattr("musa.auth.login_chatgpt", lambda s: order.append("login"))
    async with Client(server) as c:
        res = await c.call_tool("auth_login", {"provider": "chatgpt"})
    assert res.structured_content == {"status": "ok", "provider": "chatgpt"}
    assert order == ["aclose", "login"]
```

En `tests/test_errors.py`, añadir:

```python
def test_next_actions_cover_both_providers():
    assert "auth --chatgpt" in NEXT_ACTION[errors.AUTH_EXPIRED]
    assert "GIA_CHROME_PATH" in NEXT_ACTION[errors.AUTH_FAILED]
    assert "Gemini" not in NEXT_ACTION[errors.NO_IMAGE]
```

- [ ] **Paso 2: Ejecutarlos y ver que fallan**

Run: `uv run pytest tests/test_cli.py tests/test_mcp_server.py tests/test_errors.py -q -k "chatgpt or providers"`
Expected: FAIL (`No such option: --chatgpt`; `auth_login` sin el parámetro `provider`; los textos no cambiados).

- [ ] **Paso 3: CLI**

En `src/musa/cli.py`, sustituir la función `auth` (líneas 135-150) por:

```python
@app.command()
def auth(status: bool = typer.Option(False, "--status", help="Show session status instead of logging in"),
         chatgpt: bool = typer.Option(False, "--chatgpt", help="ChatGPT (Google Chrome) instead of Gemini (Firefox)")):
    """Guided login (Firefox for Gemini; Google Chrome with --chatgpt) or session check."""
    b, s = _bs()
    if status:
        try:
            st = _run(b.status())
        except GIAError as e:
            _fail(e)
        _dump(st.get("chatgpt") if chatgpt else st)
        return
    if chatgpt:
        from . import auth as auth_mod
        try:
            auth_mod.login_chatgpt(s)
        except GIAError as e:
            _fail(e)
        typer.echo(f"OK: ChatGPT session saved in {s.chatgpt_profile_dir}")
        return
    from .auth import login_guided
    try:
        jar = login_guided(s)
    except GIAError as e:
        _fail(e)
    typer.echo(f"OK: {len(jar)} cookies saved to {s.cookie_file}")
```

- [ ] **Paso 4: MCP**

En `src/musa/mcp_server.py`, cambiar `from typing import Annotated` por `from typing import Annotated, Literal`.

Sustituir `auth_login` (líneas 161-171) por:

```python
    @mcp.tool(annotations={"openWorldHint": True})
    async def auth_login(provider: Annotated[Literal["gemini", "chatgpt"], Field(
            description="gemini: Firefox sign-in to Gemini; chatgpt: Google Chrome sign-in to ChatGPT (model chatgpt-web)")] = "gemini") -> dict:
        """Open a dedicated browser profile to sign in (Gemini: Firefox; ChatGPT: Google Chrome); closes itself once logged in."""
        from . import auth as auth_mod
        try:
            if provider == "chatgpt":
                chatgpt = getattr(backend, "chatgpt", None)
                if chatgpt is not None:
                    await chatgpt.aclose()  # release the profile held by the idle headless Chrome
                await asyncio.to_thread(auth_mod.login_chatgpt, settings)
                return {"status": "ok", "provider": "chatgpt"}
            jar = await asyncio.to_thread(auth_mod.login_guided, settings)
        except GIAError as e:
            return error_payload(e)
        except Exception as e:
            return error_payload(GIAError(AUTH_FAILED, str(e)))
        return {"status": "ok", "cookies": len(jar)}
```

En `INSTRUCTIONS`, justo antes del `"""` de cierre (línea 71), añadir:

```
ChatGPT (web): pass model="chatgpt-web" to generate_image/generate_batch/generate_animation to use the user's ChatGPT web
session instead (headless Google Chrome, dedicated profile; sign in with auth_login(provider="chatgpt")). edit_image,
retry_download and delete_conversation follow the conversation's provider automatically. ChatGPT has no Gems: a style's
Gem is ignored there (warning gem_ignored). auth_status.chatgpt = {session, image_gen: {remaining, reset_after,
checked_at}} as of the last ChatGPT call. Automating chatgpt.com may break OpenAI's Terms of Use; the user accepted it.
```

- [ ] **Paso 5: Textos de error**

En `src/musa/errors.py`, sustituir estas entradas de `NEXT_ACTION`:

```python
    AUTH_EXPIRED: "Session expired: call auth_login (or run `musa auth`); for model chatgpt-web use auth_login(provider=\"chatgpt\") / `musa auth --chatgpt`. Then retry.",
    USAGE_LIMIT: "Usage limit reached (Gemini or ChatGPT): stop generating and wait for the quota window to reset (details.resets_at when known).",
    TEMPORARILY_BLOCKED: "Temporarily blocked (Google, or ChatGPT/Cloudflare), or the ChatGPT browser profile is busy: wait several minutes before retrying; do not loop.",
    NO_IMAGE: "The model answered without an image: read gemini_text and rephrase the prompt.",
    AUTH_FAILED: "Guided login or browser start failed: check details/message (Gemini needs Firefox, GIA_FIREFOX_PATH; ChatGPT needs Google Chrome, GIA_CHROME_PATH, and the playwright package).",
    UPSTREAM_ERROR: "Unclassified provider error: retry once later; if it persists, check auth_status.",
```

- [ ] **Paso 6: Frontend**

En `src/musa/web/app.js`, dentro de `ERR_ES`, sustituir las entradas `auth_expired`, `auth_failed`, `usage_limit`, `temporarily_blocked`, `no_image` y `timeout`, y añadir `gem_ignored` después de `gem_missing`:

```js
  auth_expired: "La sesión ha caducado. Ejecuta «musa auth» (Gemini) o «musa auth --chatgpt» (ChatGPT) en la terminal y vuelve a intentarlo.",
  auth_failed: "No se pudo iniciar sesión o abrir el navegador. Gemini necesita Firefox y ChatGPT, Google Chrome; ejecuta «musa auth» de nuevo.",
  usage_limit: "Has alcanzado el límite de uso del proveedor. Espera a que se renueve la cuota.",
  temporarily_blocked: "El proveedor ha bloqueado la petición temporalmente (o ChatGPT está ocupado en otro proceso). Espera unos minutos antes de reintentar.",
  gem_ignored: "ChatGPT no usa Gems; el estilo se aplicó solo como texto.",
  no_image: "El modelo no devolvió ninguna imagen. Reformula el prompt e inténtalo de nuevo.",
  timeout: "El proveedor tardó demasiado en responder. Inténtalo de nuevo; si se repite, simplifica el prompt o usa menos referencias.",
```

- [ ] **Paso 7: README**

En `README.md`:
1. En la tabla «Comandos CLI», sustituir la fila de `musa auth` por:
   `| \`musa auth [--status] [--chatgpt]\` | Login guiado (Firefox para Gemini; Google Chrome con \`--chatgpt\`) o comprobación de sesión (\`--status --chatgpt\` muestra solo ChatGPT) |`
2. En «Configuración», línea 397, añadir `GIA_CHROME_PATH` (binario de Google Chrome para ChatGPT) y `GIA_CHATGPT_IDLE_CLOSE_S` (segundos sin uso antes de cerrar Chrome; 120).
3. Insertar, antes de `## Registrar el MCP en tu agente`, esta sección:

```markdown
## ChatGPT (web)

Musa también genera y edita con **tu sesión web de ChatGPT** (vale una cuenta Free): elige el modelo **«ChatGPT (web)»** (`chatgpt-web`) en el selector, en `musa gen --model chatgpt-web` o en `generate_image(model="chatgpt-web")`. Las ediciones, `retry_download` y `delete_conversation` siguen solas al proveedor de su conversación.

> ⚠️ **Términos de uso:** los Términos de OpenAI prohíben extraer contenido generado de forma automática o programática. Este canal automatiza chatgpt.com y descarga las imágenes. El riesgo sobre tu cuenta es tuyo.

- **Requisito:** Google Chrome (en macOS, `/Applications/Google Chrome.app`; en otro sitio, `GIA_CHROME_PATH`).
- **Inicio de sesión:** `musa auth --chatgpt` (o `auth_login(provider="chatgpt")`) abre Chrome **sin automatizar** con un perfil propio en `<state_dir>/chatgpt-chrome-profile`. Inicia sesión y Chrome se cierra solo al detectar la cookie.
- **Generación:** Musa abre ese perfil en Chrome **headless**, sin puerto de depuración, y lo cierra tras 2 min sin uso. Solo un proceso puede usar el perfil a la vez: si Musa y musa-mcp coinciden, el segundo espera.
- **Historial:** las conversaciones quedan en tu barra lateral de ChatGPT. `musa delete` / `delete_conversation` las oculta.
- **Cuota:** `musa auth --status --chatgpt` muestra `image_gen.remaining` y `reset_after` según la última llamada. Musa no supone ningún límite fijo.
- **Gems:** ChatGPT no los usa. El estilo se aplica como texto (aviso `gem_ignored`).
```

- [ ] **Paso 8: Ejecutar la suite completa**

Run: `uv run pytest -q`
Expected: PASS. Si algún test de `tests/test_webapp.py` o `tests/js` fija un texto de `ERR_ES` cambiado, actualízalo al texto nuevo: es un test de redacción.

- [ ] **Paso 9: Commit**

```bash
git add src/musa/cli.py src/musa/mcp_server.py src/musa/errors.py src/musa/web/app.js README.md tests/test_cli.py tests/test_mcp_server.py tests/test_errors.py
git commit -m "ChatGPT: musa auth --chatgpt, auth_login(provider), textos y README"
```

---

### Tarea 7: Verificación real y limpieza (la hace el controlador, no un subagente)

**Archivos:** ninguno de código. Al terminar, si algo queda pendiente, actualizar `README.md` («Known deviations») y la spec.

- [ ] **Paso 1: Borrar los archivos de la prueba previa**

Run: `rm -rf /tmp/musa_spike_chatgpt.py /tmp/musa-spike`

- [ ] **Paso 2: Leer la sesión y la cuota (no gasta cuota)**

Run: `uv run musa auth --status --chatgpt`
Expected: `"session": true`. Si `image_gen` es `null` (todavía no hubo ninguna llamada), sigue: el paso 4 la registra.

- [ ] **Paso 3: Client hints en headless (no gasta cuota)**

Run, desde un script desechable en `/tmp`:

```python
import asyncio
from musa.config import load_settings
from musa.chatgpt_web import ChatGPTBrowser

async def main():
    b = ChatGPTBrowser(load_settings())
    async with b.page() as page:
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        print(await page.evaluate("[navigator.userAgent, navigator.userAgentData && navigator.userAgentData.brands, navigator.webdriver]"))
    await b.aclose()

asyncio.run(main())
```

Expected: el UA y las marcas sin `Headless`, y `webdriver` en `false`. Si aparece `HeadlessChrome`, se corrige `ua_override` antes de seguir.

- [ ] **Paso 4: Generar, editar y ocultar (gasta 2 imágenes)**

```bash
uv run musa gen --model chatgpt-web "una manzana roja sobre una mesa blanca, estilo simple"
uv run musa edit <conversation_id> "haz la manzana verde, mismo encuadre"
uv run musa delete <conversation_id>
```

Expected:
- `gen` y `edit` → `status: ok` y la imagen guardada.
- `delete` → ok, y la conversación ya no aparece en la barra lateral. Para comprobarlo, `GET /backend-api/conversations?offset=0&limit=28` dentro de la página ya no la lista (mismo script del paso 3 con `ChatGPTPage(page).api(...)`).

- [ ] **Paso 5: Prueba con 2 referencias (gasta 1 imagen; solo si `remaining >= 1`)**

```bash
uv run musa gen --model chatgpt-web -r generated/20261109-222024_un-gato-astronauta_0.jpg -r generated/20261109-223215_una-taza-de-caf-humeante-sobre-una-mesa_0.jpg "el gato de la primera imagen tomando la taza de la segunda, acuarela"
```

Expected: `status: ok`. Si falla con `upstream_error` «the reference upload did not finish», hay que revisar la señal (1 respuesta por archivo) y corregir `attach`. Si no queda cuota, se deja **pendiente hasta `reset_after`** y se anota en «Known deviations».

- [ ] **Paso 6: Que no quede nada abierto**

Run: `pgrep -fl chatgpt-chrome-profile || echo "chrome closed"; lsof -nP -iTCP -sTCP:LISTEN | grep -i chrome || echo "no chrome ports"`
Expected: `chrome closed` y `no chrome ports`.

- [ ] **Paso 7: Commit de documentación (si hubo pendientes)**

```bash
git add README.md docs/superpowers/specs/2026-11-13-chatgpt-web-provider-design.md
git commit -m "ChatGPT: verificación real y pendientes"
```
