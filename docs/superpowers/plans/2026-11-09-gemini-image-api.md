# gemini-image-api Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir `gemini-image-api`: un MCP server (FastMCP, stdio) + CLI (typer) en Python que usa la sesión web de Gemini vía `gemini-webapi==2.1.1` para generar/editar imágenes con refs, estilos y descarga automática, sin API key de pago — según el spec aprobado en `docs/superpowers/specs/2026-11-09-gemini-image-api-design.md`.

**Architecture:** Pipeline por capas: `mcp_server.py`/`cli.py` (interfaces) → `client.py` (único módulo que importa `gemini-webapi`; interfaz `Backend` para poder cambiar de backend) → soportes puros y testeables (`config`, `errors`, `styles`, `refs`, `storage`, `state`, `auth`). Estado único persistido en `~/.gemini-image-api/generations.jsonl`; cookies en `~/.gemini-image-api/cookies/` (0600).

**Tech Stack:** Python >=3.11, uv (pyproject + uv.lock), gemini-webapi==2.1.1 (pin exacto), fastmcp, typer, httpx, pydantic + pydantic-settings, pyyaml, pillow, filelock; opcional auth: playwright; tests: pytest + pytest-asyncio + respx (mock de httpx). Licencia AGPL-3.0.

## Global Constraints

- `gemini-webapi==2.1.1` con pin exacto; el ÚNICO archivo que hace `import gemini_webapi` (o `from gemini_webapi import ...`) es `src/gemini_image_api/client.py`. Verificable con: `grep -rn "gemini_webapi" src/ --include="*.py" | grep -v client.py` → vacío.
- En tests NUNCA se hace red real a Google: `gemini_webapi` se mockea (inyección vía parámetro `client_cls`/factory) y httpx vía `respx`.
- Un commit por task, mensajes convencionales (`feat:`, `test:`, `docs:`).
- Estado en `~/.gemini-image-api/` con dir 0700 y cookies 0600, escrituras atómicas (temp + `os.replace`); `generations.jsonl` append-only bajo `filelock`.
- Logs a stderr/archivo, NUNCA stdout en el servidor MCP stdio; filtro que oculta `__Secure-*`, `SAPISID`, `Cookie`.
- Código e identifiers en inglés; README y docs en español.
- Verificación de APIs externas antes de usarlas (versiones instaladas pueden diferir): `uv run python -c "import fastmcp, inspect; print(inspect.signature(fastmcp.FastMCP.tool))"` y `uv run python -c "import gemini_webapi, inspect; from gemini_webapi import GeminiClient; print(inspect.signature(GeminiClient.__init__))"`.

---

## Verified upstream APIs (installed 2026-11-09, gemini-webapi 2.1.1, fastmcp 4.1.7)

Verificadas con `inspect` sobre las versiones instaladas por `uv sync`. Toda task que toque estas APIs DEBE usar estas firmas exactas:

- `GeminiClient(secure_1psid: str|None=None, secure_1psidts: str|None=None, proxy=None, **kwargs)`
- `await client.init(timeout=450, auto_close=False, close_delay=450, auto_refresh=True, refresh_interval=600, watchdog_timeout=120, impersonate="chrome145")`
- `await client.generate_content(prompt, files: list[str|Path|bytes|BytesIO]|None=None, model=None, gem=None, chat: ChatSession|None=None, temporary=False, deep_research=False, extended_thinking=False, **kwargs) -> ModelOutput`
- `ModelOutput` (pydantic) tiene PROPERTIES: `.text`, `.images`, `.videos`, `.media`, `.chosen`, `.rcid` (fields son `metadata, candidates, chosen`)
- `await client.list_models() -> list[AvailableModel] | None`; `AvailableModel` tiene `model_name`, `display_name`, `is_available`
- `client.start_chat(**kwargs) -> ChatSession` (pasa kwargs al constructor); `ChatSession` tiene propiedad `.metadata` y `await chat.send_message(prompt, files=None, temporary=False, ...) -> ModelOutput`
- `await client.read_chat(cid: str, limit=10) -> ChatHistory | None`; `await client.delete_chat(cid)`
- Excepciones reales en `gemini_webapi.exceptions`: `APIError, AuthError, GeminiError, ImageGenerationError, ModelInvalidError, TemporarilyBlockedError, TimeoutError, UsageLimitExceededError`
- `GeneratedImage` fields: `url, title, alt, proxy, client, client_ref, cid, rid, rcid, image_id`; `WebImage` NO tiene cid/rid/rcid → filtrar con `isinstance(im, GeneratedImage)`
- `await image.save(path="temp", filename=None, verbose=False, client=None, **kwargs) -> str` — `full_size: bool = True` va por **kwargs** (llega a `_perform_save(..., full_size=True)`); devuelve la ruta absoluta (str)
- fastmcp 4.1.7: `FastMCP(name=...)`; decorator `@mcp.tool()`; las tools pueden declarar `ctx: Context` (de `fastmcp.server.context`) y usar `await ctx.report_progress(progress: float, total: float|None=None, message: str|None=None)`; `mcp.run()` sin argumentos = stdio
- Tests MCP in-memory: `async with fastmcp.Client(server) as c: tools = await c.list_tools(); res = await c.call_tool("name", {...})`; el resultado es `mcp.types.CallToolResult` con fields `meta, content, structured_content, is_error` → leer datos en `res.structured_content` (NO `.data`)
- Devolver imagen en una tool: `mcp.types.ImageContent(data=<base64 str>, mimeType="image/png")` (fastmcp 4 NO exporta `fastmcp.Image`)

## File Structure

| Archivo | Responsabilidad única |
|---|---|
| `pyproject.toml` | Proyecto uv, deps (pin `gemini-webapi==2.1.1`), entry points `gia`/`gia-mcp`, hatchling src-layout, pytest config |
| `styles.yaml` | Catálogo de ejemplo: `styles:` y `templates:` |
| `README.md` | Setup, disclaimer ToS + cuenta secundaria, config MCP para agentes, comandos E2E manual |
| `src/gemini_image_api/__init__.py` | `__version__ = "0.1.0"` |
| `src/gemini_image_api/errors.py` | `GIAError(code, message, **details)` + constantes de códigos |
| `src/gemini_image_api/config.py` | `Settings` (pydantic-settings, env `GIA_*`, yaml en `~/.config/gemini-image-api/config.yaml`) + `load_settings()` |
| `src/gemini_image_api/styles.py` | Carga `styles.yaml`, `build_prompt(...)` (style/style_text/template/vars/aspect hint) |
| `src/gemini_image_api/refs.py` | Validación de refs locales (allowlist, magic bytes, tamaño) + descarga de URLs anti-SSRF |
| `src/gemini_image_api/storage.py` | Guardar imagen (nombre único, ext por magic bytes, miniatura, tEXt PNG, sidecar opcional), `safe_output_dir` |
| `src/gemini_image_api/state.py` | `StateStore` sobre `generations.jsonl` (append bajo filelock, índice de conversaciones, conteo diario) |
| `src/gemini_image_api/auth.py` | Sesión híbrida: cargar jar → `ensure_client` (init + guest check) → login guiado Playwright Firefox → export manual |
| `src/gemini_image_api/client.py` | **Único import de gemini_webapi.** `Backend` (Protocol), `GeminiWebBackend`, `GenerateResult`, mapeo de errores upstream → códigos propios, locks, timeouts |
| `src/gemini_image_api/mcp_server.py` | `create_server(backend, settings) -> FastMCP` con las 9 tools; `main()` para stdio |
| `src/gemini_image_api/cli.py` | `typer` app: `auth, styles, models, gen, edit, retry-download, delete, mcp-config, serve` |
| `tests/test_*.py` | Un archivo por módulo; `tests/conftest.py` con fixtures tmp settings/state |

Interfaces compartidas (usar EXACTAMENTE estos nombres/firmas en todas las tasks):

```python
# errors.py
codes = dict(auth_expired="auth_expired", usage_limit="usage_limit", temporarily_blocked="temporarily_blocked",
             no_image="no_image", timeout="timeout", generated_not_saved="generated_not_saved", content_blocked="content_blocked",
             invalid_ref="invalid_ref", ref_not_allowed="ref_not_allowed", ssrf_blocked="ssrf_blocked",
             unknown_style="unknown_style", template_missing_var="template_missing_var",
             unknown_conversation="unknown_conversation", unknown_model="unknown_model")
class GIAError(Exception):
    def __init__(self, code: str, message: str, **details): ...   # self.code, self.details

# refs.py
@dataclass(frozen=True)
class ResolvedRef: path: Path; source: str; sha256: str
async def resolve_refs(refs: list[str], settings: "Settings", tmpdir: Path) -> list[ResolvedRef]

# storage.py
@dataclass(frozen=True)
class SavedImage: path: Path; width: int; height: int; nbytes: int; format: str; full_size: bool; thumbnail: bytes | None
def save_image(data: bytes, *, output_root: Path, project: str | None, subdir: str | None, prompt: str,
               index: int, meta: dict, write_sidecar: bool, thumb_size: int) -> SavedImage

# state.py
@dataclass
class GenerationRecord:
    id: str; ts: str; type: str; conversation_id: str; chat_metadata: list | None; prompt: str
    style: str | None; style_text: str | None; aspect_hint: str | None; model: str | None
    project: str | None; subdir: str | None; refs: list[dict]; images: list[dict]
    status: str; code: str | None; gemini_text: str; elapsed_s: float; parent_id: str | None
    def to_line(self) -> str; @classmethod def from_line(cls, line: str) -> "GenerationRecord"
class StateStore:
    def __init__(self, state_dir: Path): ...
    def append(self, record: GenerationRecord) -> None
    def load_all(self) -> list[GenerationRecord]
    def conversation(self, cid: str) -> GenerationRecord | None   # última entrada con chat_metadata
    def count_images_today(self) -> int
    @staticmethod def new_id(prefix: str) -> str                     # f"{prefix}_{uuid4().hex[:12]}"

# client.py
@dataclass
class GenerateResult:
    status: str            # "ok" | "generated_not_saved" | "error"
    code: str | None
    conversation_id: str | None
    images: list[SavedImage]
    gemini_text: str
    elapsed_s: float
    chat_metadata: list | None
class Backend(Protocol):
    async def status(self) -> dict: ...
    async def list_models(self) -> list[dict]: ...
    async def generate(self, prompt: str, refs: list[ResolvedRef], *, model: str, progress=None) -> GenerateResult: ...
    async def edit(self, conversation_id: str, prompt: str, refs: list[ResolvedRef], *, model: str, progress=None) -> GenerateResult: ...
    async def retry_download(self, conversation_id: str, image_index: int) -> SavedImage: ...
    async def delete_conversation(self, conversation_id: str) -> None: ...
def build_backend(settings: "Settings | None" = None, state: "StateStore | None" = None, client_cls=None) -> Backend
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `src/gemini_image_api/__init__.py`, `styles.yaml`, `tests/conftest.py`

**Interfaces:**
- Produces: paquete instalable `gemini-image-api` importable como `gemini_image_api`, entornos reproducibles con `uv sync`, pytest configurado (asyncio_mode=auto).

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "gemini-image-api"
version = "0.1.0"
description = "MCP server + CLI para generar/editar imagenes con tu sesion web de Gemini (sin API key de pago)"
requires-python = ">=3.11"
license = { text = "AGPL-3.0" }
dependencies = [
  "gemini-webapi==2.1.1",
  "fastmcp>=2.10",
  "typer>=0.12",
  "httpx>=0.27",
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
  "pyyaml>=6.0",
  "pillow>=10.3",
  "filelock>=3.14",
]
[project.optional-dependencies]
auth = ["playwright>=1.45"]
test = ["pytest>=8", "pytest-asyncio>=0.23", "respx>=0.21"]
[project.scripts]
gia = "gemini_image_api.cli:main"
gia-mcp = "gemini_image_api.mcp_server:main"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/gemini_image_api"]
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create `src/gemini_image_api/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 3: Create `styles.yaml` (ejemplos)**

```yaml
styles:
  fotorealista:
    description: "Foto realista, luz cinematografica"
    append: "photorealistic, 8k, cinematic lighting, sharp focus, DSLR look"
  anime:
    description: "Estilo anime japones"
    append: "anime style, cel shading, vibrant colors, studio quality"
  isometrico:
    description: "3D isometrico"
    append: "isometric 3D render, clean lines, soft shadows, octane render"
templates:
  anuncio_producto:
    description: "Foto de producto para anuncio"
    prompt: "Professional product photo of {producto} on a {fondo} background, {estilo} lighting, commercial photography"
    vars: [producto, fondo, estilo]
```

- [ ] **Step 4: Create `tests/conftest.py`**

```python
import pytest
from pathlib import Path
from gemini_image_api.config import Settings

@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("GIA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GIA_OUTPUT_ROOT", str(tmp_path / "generated"))
    monkeypatch.setenv("GIA_STYLES_FILE", str(tmp_path / "styles.yaml"))
    return Settings()
```

- [ ] **Step 5: Verify install and collection**

Run: `uv sync --extra test && uv run python -c "import gemini_image_api; print(gemini_image_api.__version__)"` → Expected: `0.1.0`
Run: `uv run pytest --collect-only -q` → Expected: `no tests ran` (exit 5) sin errores de import.

- [ ] **Step 6: Commit**

Run: `git add pyproject.toml src tests styles.yaml uv.lock && git commit -m "feat: scaffolding del proyecto (uv, deps con pin gemini-webapi==2.1.1, pytest)"`

---

### Task 2: `errors.py` — errores tipados con códigos

**Files:**
- Create: `src/gemini_image_api/errors.py`, `tests/test_errors.py`

**Interfaces:**
- Consumes: nada.
- Produces: `GIAError` + constantes de códigos usadas por todos los demás módulos y por el contrato MCP (`code` en la respuesta estructurada).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_errors.py
from gemini_image_api.errors import GIAError, AUTH_EXPIRED, UNKNOWN_STYLE

def test_error_carries_code_and_details():
    err = GIAError(AUTH_EXPIRED, "sesion expirada", hint="run gia auth")
    assert err.code == "auth_expired"
    assert err.details == {"hint": "run gia auth"}
    assert "sesion expirada" in str(err)

def test_unknown_style_code():
    err = GIAError(UNKNOWN_STYLE, "estilo no existe", valid=["anime", "anime2"])
    assert err.code == "unknown_style" and err.details["valid"] == ["anime", "anime2"]

def test_all_codes_are_unique_strings():
    from gemini_image_api import errors
    vals = [v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str)]
    assert len(vals) == len(set(vals)) and len(vals) >= 13
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_errors.py -v` → Expected: FAIL `ModuleNotFoundError: gemini_image_api.errors`

- [ ] **Step 3: Implement `errors.py`**

```python
AUTH_EXPIRED = "auth_expired"
USAGE_LIMIT = "usage_limit"
TEMPORARILY_BLOCKED = "temporarily_blocked"
NO_IMAGE = "no_image"
TIMEOUT = "timeout"
GENERATED_NOT_SAVED = "generated_not_saved"
CONTENT_BLOCKED = "content_blocked"
INVALID_REF = "invalid_ref"
REF_NOT_ALLOWED = "ref_not_allowed"
SSRF_BLOCKED = "ssrf_blocked"
UNKNOWN_STYLE = "unknown_style"
TEMPLATE_MISSING_VAR = "template_missing_var"
UNKNOWN_CONVERSATION = "unknown_conversation"
UNKNOWN_MODEL = "unknown_model"

class GIAError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_errors.py -v` → Expected: 3 passed

- [ ] **Step 5: Commit**

Run: `git add src/gemini_image_api/errors.py tests/test_errors.py && git commit -m "feat: errores tipados GIAError con codigos del contrato MCP"`

---

### Task 3: `config.py` — settings con env `GIA_*` y yaml de usuario

**Files:**
- Create: `src/gemini_image_api/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings) y `load_settings() -> Settings` cacheada. Usada por refs, storage, state, auth, client, mcp_server, cli.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path
from gemini_image_api.config import Settings, load_settings

def test_defaults(settings: Settings):
    assert settings.gen_timeout_s == 300.0
    assert settings.download_timeout_s == 60.0
    assert settings.max_refs == 10
    assert settings.max_ref_bytes == 20 * 1024 * 1024
    assert settings.write_sidecar is False
    assert settings.default_model == "gemini-flash"
    assert settings.thumb_size == 512
    assert settings.state_dir.name == "state"

def test_allowed_ref_dirs_default(settings: Settings):
    dirs = settings.allowed_ref_dirs_list()
    assert Path.cwd() in dirs and settings.output_root.resolve() in [d.resolve() for d in dirs]

def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GIA_GEN_TIMEOUT_S", "120")
    monkeypatch.setenv("GIA_ALLOWED_REF_DIRS", f"{tmp_path}/a:{tmp_path}/b")
    s = Settings()
    assert s.gen_timeout_s == 120.0
    assert s.allowed_ref_dirs_list() == [tmp_path / "a", tmp_path / "b"]

def test_ensure_dirs_permissions(settings: Settings):
    settings.ensure_dirs()
    assert (settings.state_dir / "cookies").is_dir()
    assert (settings.state_dir.stat().st_mode & 0o777) == 0o700

def test_load_settings_cached(monkeypatch, settings):
    monkeypatch.setattr("gemini_image_api.config._cache", None)
    a, b = load_settings(), load_settings()
    assert a is b
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_config.py -v` → FAIL ModuleNotFoundError

- [ ] **Step 3: Implement `config.py`**

```python
from functools import lru_cache
from pathlib import Path
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GIA_",
        yaml_file=str(Path.home() / ".config/gemini-image-api/config.yaml"),
        extra="ignore",
    )
    state_dir: Path = Path.home() / ".gemini-image-api"
    output_root: Path = Path.cwd() / "generated"
    styles_file: Path | None = None
    gen_timeout_s: float = 300.0
    download_timeout_s: float = 60.0
    max_refs: int = 10
    max_ref_bytes: int = 20 * 1024 * 1024
    allowed_ref_dirs: str = ""          # paths separados por ":"; vacio = cwd + output_root
    write_sidecar: bool = False
    daily_limit: int | None = None
    min_delay_s: float = 2.0
    default_model: str = "gemini-flash"
    thumb_size: int = 512

    @property
    def cookie_dir(self) -> Path: return self.state_dir / "cookies"
    @property
    def cookie_file(self) -> Path: return self.cookie_dir / "cookies.json"
    @property
    def generations_file(self) -> Path: return self.state_dir / "generations.jsonl"
    @property
    def lock_file(self) -> Path: return self.state_dir / "state.lock"

    def allowed_ref_dirs_list(self) -> list[Path]:
        if self.allowed_ref_dirs.strip():
            return [Path(p).expanduser().resolve() for p in self.allowed_ref_dirs.split(":") if p.strip()]
        return [Path.cwd().resolve(), self.output_root.expanduser().resolve()]

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.cookie_dir, self.state_dir / "logs"):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)

@lru_cache(maxsize=1)
def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_config.py -v` → 5 passed. Nota: `test_load_settings_cached` usa `monkeypatch.setattr("gemini_image_api.config._cache", None)` — con `lru_cache` no hay `_cache`; cámbialo a `load_settings.cache_clear()`.

- [ ] **Step 5: Commit** — `git add src/gemini_image_api/config.py tests/test_config.py && git commit -m "feat: Settings con env GIA_*, yaml de usuario y permisos 0700"`

---

### Task 4: `styles.py` — catálogo + build_prompt

**Files:**
- Create: `src/gemini_image_api/styles.py`, `tests/test_styles.py`

**Interfaces:**
- Consumes: `Settings` (para localizar `styles.yaml`), `errors.GIAError`.
- Produces: `StyleCatalog.load(path)`, `build_prompt(catalog, *, prompt, style, style_text, template, template_vars, aspect_hint) -> str`, `ASPECT_HINTS`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_styles.py
import pytest, yaml
from pathlib import Path
from gemini_image_api.styles import StyleCatalog, build_prompt, ASPECT_HINTS
from gemini_image_api.errors import GIAError

@pytest.fixture
def catalog(tmp_path: Path) -> StyleCatalog:
    p = tmp_path / "styles.yaml"
    p.write_text(yaml.safe_dump({
        "styles": {"anime": {"description": "d", "append": "anime style, cel shading"}},
        "templates": {"anuncio": {"description": "d", "prompt": "foto de {producto}", "vars": ["producto"]}},
    }))
    return StyleCatalog.load(p)

def test_build_prompt_con_style(catalog):
    out = build_prompt(catalog, prompt="un gato", style="anime")
    assert "un gato" in out and "anime style, cel shading" in out

def test_build_prompt_style_desconocido_lista_validos(catalog):
    with pytest.raises(GIAError) as e:
        build_prompt(catalog, prompt="x", style="nope")
    assert e.value.code == "unknown_style" and e.value.details["valid"] == ["anime"]

def test_build_prompt_template_y_vars(catalog):
    out = build_prompt(catalog, prompt="", template="anuncio", template_vars={"producto": "zapato"})
    assert "foto de zapato" in out

def test_build_prompt_template_var_faltante(catalog):
    with pytest.raises(GIAError) as e:
        build_prompt(catalog, prompt="", template="anuncio", template_vars={})
    assert e.value.code == "template_missing_var" and "producto" in e.value.details["missing"]

def test_aspect_hint(catalog):
    assert "16:9" in build_prompt(catalog, prompt="x", aspect_hint="16:9")
    assert set(ASPECT_HINTS) == {"1:1", "16:9", "9:16", "4:3", "3:4"}
```

- [ ] **Step 2:** Run → FAIL ModuleNotFoundError
- [ ] **Step 3: Implement `styles.py`**

```python
import re, yaml
from dataclasses import dataclass, field
from pathlib import Path
from .errors import GIAError, UNKNOWN_STYLE, TEMPLATE_MISSING_VAR

ASPECT_HINTS = {
    "1:1": "square 1:1 aspect ratio",
    "16:9": "wide landscape 16:9 aspect ratio",
    "9:16": "tall portrait 9:16 aspect ratio",
    "4:3": "4:3 aspect ratio",
    "3:4": "3:4 portrait aspect ratio",
}
GENERATE_PREFIX = "Generate an image (do not search the web): "

@dataclass(frozen=True)
class Style:
    name: str; description: str; append: str

@dataclass(frozen=True)
class Template:
    name: str; description: str; prompt: str; vars: list[str]

@dataclass
class StyleCatalog:
    styles: dict[str, Style] = field(default_factory=dict)
    templates: dict[str, Template] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "StyleCatalog":
        data = yaml.safe_load(Path(path).read_text()) or {}
        styles = {n: Style(n, s.get("description", ""), s.get("append", "")) for n, s in (data.get("styles") or {}).items()}
        templates = {n: Template(n, t.get("description", ""), t.get("prompt", ""), list(t.get("vars") or []))
                     for n, t in (data.get("templates") or {}).items()}
        return cls(styles=styles, templates=templates)

def build_prompt(catalog: StyleCatalog, *, prompt: str, style: str | None = None,
                 style_text: str | None = None, template: str | None = None,
                 template_vars: dict | None = None, aspect_hint: str | None = None) -> str:
    parts = [GENERATE_PREFIX]
    if template:
        t = catalog.templates.get(template)
        if t is None:
            raise GIAError(UNKNOWN_STYLE, f"template {template!r} no existe", valid=sorted(catalog.templates))
        missing = [v for v in t.vars if not (template_vars or {}).get(v)]
        if missing:
            raise GIAError(TEMPLATE_MISSING_VAR, f"faltan vars {missing}", missing=missing, required=t.vars)
        prompt = t.prompt.format(**{k: str(v) for k, v in (template_vars or {}).items()})
    parts.append(prompt.strip())
    if style:
        s = catalog.styles.get(style)
        if s is None:
            raise GIAError(UNKNOWN_STYLE, f"estilo {style!r} no existe", valid=sorted(catalog.styles))
        parts.append(s.append)
    if style_text:
        parts.append(style_text.strip())
    if aspect_hint:
        if aspect_hint not in ASPECT_HINTS:
            raise GIAError("invalid_ref", f"aspect_hint {aspect_hint!r} invalido", valid=sorted(ASPECT_HINTS))
        parts.append(ASPECT_HINTS[aspect_hint])
    return " ".join(p for p in parts if p)
```

- [ ] **Step 4:** `uv run pytest tests/test_styles.py -v` → 5 passed
- [ ] **Step 5:** Commit `feat: catalogo de estilos/plantillas y build_prompt con aspect hint`

---

### Task 5: `refs.py` — validación local + descarga URL anti-SSRF

**Files:**
- Create: `src/gemini_image_api/refs.py`, `tests/test_refs.py`

**Interfaces:**
- Consumes: `Settings`, `errors`.
- Produces: `ResolvedRef`, `sniff_image_format`, `validate_local_ref`, `download_ref_url`, `resolve_refs`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_refs.py
import pytest, respx, httpx
from pathlib import Path
from gemini_image_api.refs import resolve_refs, sniff_image_format, validate_local_ref
from gemini_image_api.errors import GIAError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

def test_sniff():
    assert sniff_image_format(PNG) == "png"
    assert sniff_image_format(JPEG) == "jpeg"
    assert sniff_image_format(b"GIF89a" + b"\x00" * 10) == "gif"
    assert sniff_image_format(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "webp"
    assert sniff_image_format(b"not an image") is None

def test_local_ref_ok(settings, tmp_path):
    f = tmp_path / "foto.png"; f.write_bytes(PNG)
    # tmp_path no esta en allowed por defecto -> anyadir
    settings.allowed_ref_dirs = str(tmp_path)
    r = validate_local_ref(f, settings)
    assert r == f.resolve()

def test_local_ref_fuera_de_allowlist(settings, tmp_path):
    f = tmp_path / "foto.png"; f.write_bytes(PNG)
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "ref_not_allowed"

def test_local_ref_no_imagen(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    f = tmp_path / "x.txt"; f.write_bytes(b"hola")
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "invalid_ref"

def test_local_ref_state_dir_prohibido(settings, tmp_path):
    f = settings.state_dir / "cookies" / "cookies.json"
    f.parent.mkdir(parents=True); f.write_bytes(b"{}")
    settings.allowed_ref_dirs = str(settings.state_dir)
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "ref_not_allowed"

@respx.mock
async def test_resolve_refs_url_descarga_a_temp(settings, tmp_path, monkeypatch):
    # forzar DNS a 93.184.216.34 (publica de ejemplo)
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    respx.get("https://example.com/foto.png").mock(return_value=httpx.Response(200, content=PNG))
    out = await resolve_refs(["https://example.com/foto.png"], settings, tmp_path)
    assert len(out) == 1 and out[0].path.suffix == ".png" and out[0].path.read_bytes() == PNG
    assert len(out[0].sha256) == 64

@respx.mock
async def test_resolve_refs_url_ssrf_bloqueada(settings, tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))])
    respx.get("http://169.254.169.254/latest/meta-data").mock(return_value=httpx.Response(200, content=PNG))
    with pytest.raises(GIAError) as e:
        await resolve_refs(["http://169.254.169.254/latest/meta-data"], settings, tmp_path)
    assert e.value.code == "ssrf_blocked"

async def test_resolve_refs_max_count(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    files = []
    for i in range(12):
        f = tmp_path / f"f{i}.png"; f.write_bytes(PNG); files.append(str(f))
    with pytest.raises(GIAError) as e:
        await resolve_refs(files, settings, tmp_path)
    assert e.value.code == "invalid_ref" and "max_refs" in str(e.value.details)
```

- [ ] **Step 2:** Run → FAIL ModuleNotFoundError
- [ ] **Step 3: Implement `refs.py`**

```python
import hashlib, ipaddress, socket, tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .config import Settings
from .errors import GIAError, INVALID_REF, REF_NOT_ALLOWED, SSRF_BLOCKED

MAGIC = [(b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif")]

def sniff_image_format(data: bytes) -> str | None:
    for m in MAGIC:
        if data.startswith(m[0]):
            return m[1]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None

def validate_local_ref(path: Path, settings: Settings) -> Path:
    p = Path(path).expanduser()
    if p.is_symlink() or not p.exists() or not p.is_file():
        raise GIAError(REF_NOT_ALLOWED, f"ref no accesible: {path}")
    rp = p.resolve()
    if settings.state_dir.resolve() in rp.parents:
        raise GIAError(REF_NOT_ALLOWED, "no se permiten refs dentro del state dir (cookies)")
    if not any(rp.is_relative_to(d) for d in settings.allowed_ref_dirs_list()):
        raise GIAError(REF_NOT_ALLOWED, f"{rp} fuera de allowed_ref_dirs", allowed=settings.allowed_ref_dirs_list())
    size = rp.stat().st_size
    if size > settings.max_ref_bytes:
        raise GIAError(INVALID_REF, f"ref supera {settings.max_ref_bytes} bytes", path=str(rp))
    data = rp.read_bytes()
    if sniff_image_format(data) is None:
        raise GIAError(INVALID_REF, "el archivo no es imagen (png/jpeg/webp/gif)", path=str(rp))
    return rp

def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified

async def download_ref_url(url: str, tmpdir: Path, settings: Settings) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise GIAError(INVALID_REF, f"URL invalida: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise GIAError(INVALID_REF, f"DNS fallo para {parsed.hostname}: {e}")
    ips = {ipaddress.ip_address(i[4][0]) for i in infos}
    if any(_blocked(ip) for ip in ips):
        raise GIAError(SSRF_BLOCKED, f"{parsed.hostname} resuelve a IP bloqueada", ips=[str(i) for i in ips])
    tmpdir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=3, timeout=settings.download_timeout_s) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise GIAError(INVALID_REF, f"descarga {url} devolvio {resp.status_code}")
            data = b""
            async for chunk in resp.aiter_bytes(65536):
                data += chunk
                if len(data) > settings.max_ref_bytes:
                    raise GIAError(INVALID_REF, f"descarga de {url} supera max_ref_bytes")
    fmt = sniff_image_format(data)
    if fmt is None:
        raise GIAError(INVALID_REF, f"la URL {url} no devolvio una imagen")
    ext = "jpg" if fmt == "jpeg" else fmt
    out = tmpdir / f"ref_{hashlib.sha256(data).hexdigest()[:16]}.{ext}"
    out.write_bytes(data)
    return out

@dataclass(frozen=True)
class ResolvedRef:
    path: Path; source: str; sha256: str

async def resolve_refs(refs: list[str], settings: Settings, tmpdir: Path) -> list[ResolvedRef]:
    if len(refs) > settings.max_refs:
        raise GIAError(INVALID_REF, f"maximo {settings.max_refs} refs por llamada", max_refs=settings.max_refs, got=len(refs))
    out: list[ResolvedRef] = []
    for r in refs:
        if r.startswith("http://") or r.startswith("https://"):
            p = await download_ref_url(r, tmpdir, settings)
            source = r
        else:
            p = validate_local_ref(Path(r), settings)
            source = str(p)
        data = p.read_bytes()
        out.append(ResolvedRef(path=p, source=source, sha256=hashlib.sha256(data).hexdigest()))
    return out
```

- [ ] **Step 4:** `uv run pytest tests/test_refs.py -v` → 7 passed (ajusta si respx requiere decorator distinto en la versión instalada; verifica con `uv run python -c "import respx; print(respx.__version__)"`).
- [ ] **Step 5:** Commit `feat: resolve_refs con allowlist, magic bytes, limite de tamano y anti-SSRF`

---

### Task 6: `storage.py` — guardar imagen con nombre único, miniatura y metadata

**Files:**
- Create: `src/gemini_image_api/storage.py`, `tests/test_storage.py`

**Interfaces:**
- Consumes: `Settings` (no directamente; recibe params), `errors`.
- Produces: `SavedImage`, `save_image(...)`, `slugify`, `safe_output_dir`, `make_thumbnail_png`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_storage.py
import io, pytest
from pathlib import Path
from PIL import Image
from gemini_image_api.storage import save_image, slugify, safe_output_dir
from gemini_image_api.errors import GIAError

def png_bytes(w=64, h=48, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO(); Image.new("RGB", (w, h), color).save(buf, "PNG"); return buf.getvalue()

def test_slugify():
    assert slugify("Hola Mundo! 2026") == "hola-mundo-2026"
    assert len(slugify("a" * 200)) <= 40

def test_safe_output_dir_rechaza_escape(tmp_path):
    root = tmp_path / "generated"
    with pytest.raises(GIAError):
        safe_output_dir(root, "../..", None)
    with pytest.raises(GIAError):
        safe_output_dir(root, None, "/etc")
    p = safe_output_dir(root, "clienteA", "sesion1")
    assert p == (root / "clienteA" / "sesion1").resolve() and p.is_dir()

def test_save_image_png(tmp_path):
    data = png_bytes(1408, 768)
    img = save_image(data, output_root=tmp_path / "generated", project=None, subdir=None,
                     prompt="un gato astronauta", index=0, meta={"prompt": "un gato astronauta"},
                     write_sidecar=False, thumb_size=512)
    assert img.path.exists() and img.path.suffix == ".png"
    assert "un-gato-astronauta_0" in img.path.name
    assert img.width == 1408 and img.height == 768 and img.format == "png" and img.full_size is True
    assert img.thumbnail is not None
    with Image.open(io.BytesIO(img.thumbnail)) as t:
        assert max(t.size) <= 512
    # metadata tEXt dentro del PNG
    with Image.open(img.path) as im:
        assert im.text.get("prompt") == "un gato astronauta"
    assert not img.path.with_suffix(".json").exists()  # sidecar off

def test_save_image_sidecar_opcional(tmp_path):
    img = save_image(png_bytes(), output_root=tmp_path / "g", project=None, subdir=None,
                     prompt="x", index=1, meta={"a": 1}, write_sidecar=True, thumb_size=512)
    side = img.path.with_suffix(".json")
    assert side.exists() and '"a": 1' in side.read_text()

def test_save_image_dos_en_el_mismo_segundo_no_colisionan(tmp_path):
    a = save_image(png_bytes(), output_root=tmp_path / "g", project=None, subdir=None, prompt="p", index=0, meta={}, write_sidecar=False, thumb_size=512)
    b = save_image(png_bytes(), output_root=tmp_path / "g", project=None, subdir=None, prompt="p", index=1, meta={}, write_sidecar=False, thumb_size=512)
    assert a.path != b.path

def test_save_image_jpeg_extension(tmp_path):
    buf = io.BytesIO(); Image.new("RGB", (10, 10)).save(buf, "JPEG")
    img = save_image(buf.getvalue(), output_root=tmp_path / "g", project=None, subdir=None, prompt="p", index=0, meta={}, write_sidecar=False, thumb_size=512)
    assert img.path.suffix == ".jpg" and img.format == "jpeg"
```

- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement `storage.py`**

```python
import io, json, re, time
from dataclasses import dataclass
from pathlib import Path
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from .errors import GIAError
from .refs import sniff_image_format

def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "img")[:max_len].strip("-")

def safe_output_dir(output_root: Path, project: str | None, subdir: str | None) -> Path:
    root = output_root.expanduser().resolve()
    parts = [p for p in (project, subdir) if p]
    for p in parts:
        if Path(p).is_absolute() or ".." in Path(p).parts:
            raise GIAError("invalid_ref", f"project/subdir invalido: {p!r}")
    target = root.joinpath(*parts) if parts else root
    target = target.resolve()
    if not (target == root or root in target.parents):
        raise GIAError("invalid_ref", f"salida fuera de output_root: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target

@dataclass(frozen=True)
class SavedImage:
    path: Path; width: int; height: int; nbytes: int; format: str; full_size: bool; thumbnail: bytes | None

def make_thumbnail_png(data: bytes, size: int) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

def save_image(data: bytes, *, output_root: Path, project: str | None, subdir: str | None, prompt: str,
               index: int, meta: dict, write_sidecar: bool, thumb_size: int) -> SavedImage:
    fmt = sniff_image_format(data)
    if fmt is None:
        raise GIAError("invalid_ref", "los bytes recibidos no son imagen")
    ext = "jpg" if fmt == "jpeg" else fmt
    out_dir = safe_output_dir(output_root, project, subdir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{stamp}_{slugify(prompt)}_{index}.{ext}"
    n = 0
    while path.exists():
        n += 1
        path = out_dir / f"{stamp}_{slugify(prompt)}_{index}_{n}.{ext}"
    path.write_bytes(data)
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
    if fmt == "png":  # incrusta metadata tEXt (re-escribe el PNG)
        with Image.open(io.BytesIO(data)) as im:
            info = PngInfo()
            for k, v in meta.items():
                info.add_text(k, str(v))
            im.save(path, "PNG", pnginfo=info)
    if write_sidecar:
        path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return SavedImage(path=path, width=w, height=h, nbytes=len(data), format=fmt, full_size=True,
                      thumbnail=make_thumbnail_png(data, thumb_size))
```

- [ ] **Step 4:** `uv run pytest tests/test_storage.py -v` → 6 passed
- [ ] **Step 5:** Commit `feat: storage con nombre unico, extension por magic bytes, miniatura y tEXt`

---

### Task 7: `state.py` — `generations.jsonl` único registro

**Files:**
- Create: `src/gemini_image_api/state.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: `Settings` (state_dir, generations_file, lock_file).
- Produces: `GenerationRecord`, `StateStore` con `append/load_all/conversation/count_images_today/new_id`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_state.py
import json
from datetime import datetime, timezone
from gemini_image_api.state import StateStore, GenerationRecord

def rec(cid="c_1", **kw):
    base = dict(id="g_1", ts=datetime.now(timezone.utc).isoformat(), type="generate", conversation_id=cid,
                chat_metadata=["cid", "rid", "rcid"], prompt="p", style=None, style_text=None, aspect_hint=None,
                model="gemini-flash", project=None, subdir=None, refs=[], images=[{"path": "/tmp/a.png"}],
                status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None)
    base.update(kw)
    return GenerationRecord(**base)

def test_append_and_load(settings):
    st = StateStore(settings.state_dir)
    st.append(rec())
    st.append(rec(id="g_2", conversation_id="c_1", chat_metadata=["cid2", "rid2", "rcid2"]))
    allr = st.load_all()
    assert len(allr) == 2 and allr[1].chat_metadata[0] == "cid2"

def test_conversation_devuelve_ultima_metadata(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(chat_metadata=["old"]))
    st.append(rec(id="g_2", chat_metadata=["new"]))
    assert st.conversation("c_1").chat_metadata == ["new"]
    assert st.conversation("nope") is None

def test_count_images_today(settings):
    st = StateStore(settings.state_dir)
    st.append(rec())
    st.append(rec(id="g_2", ts="2000-01-01T00:00:00+00:00"))
    assert st.count_images_today() == 1

def test_new_id(settings):
    a, b = StateStore.new_id("g"), StateStore.new_id("c")
    assert a.startswith("g_") and b.startswith("c_") and a != b and len(a) == 14

def test_line_roundtrip():
    r = rec()
    assert GenerationRecord.from_line(r.to_line()) == r
```

- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement `state.py`**

```python
import json, uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

@dataclass
class GenerationRecord:
    id: str; ts: str; type: str; conversation_id: str; chat_metadata: list | None; prompt: str
    style: str | None; style_text: str | None; aspect_hint: str | None; model: str | None
    project: str | None; subdir: str | None; refs: list[dict]; images: list[dict]
    status: str; code: str | None; gemini_text: str; elapsed_s: float; parent_id: str | None

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_line(cls, line: str) -> "GenerationRecord":
        return cls(**json.loads(line))

class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.state_dir / "generations.jsonl"
        self.lock_path = self.state_dir / "state.lock"

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def append(self, record: GenerationRecord) -> None:
        with FileLock(str(self.lock_path)):
            with self.file.open("a", encoding="utf-8") as f:
                f.write(record.to_line() + "\n")
                f.flush()

    def load_all(self) -> list[GenerationRecord]:
        if not self.file.exists():
            return []
        out = []
        with FileLock(str(self.lock_path)):
            for line in self.file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(GenerationRecord.from_line(line))
        return out

    def conversation(self, cid: str) -> GenerationRecord | None:
        last = None
        for r in self.load_all():
            if r.conversation_id == cid and r.type != "delete" and r.chat_metadata:
                last = r
        return last

    def count_images_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(len(r.images) for r in self.load_all()
                   if r.status == "ok" and r.ts.startswith(today))
```

- [ ] **Step 4:** `uv run pytest tests/test_state.py -v` → 5 passed
- [ ] **Step 5:** Commit `feat: StateStore sobre generations.jsonl (append con filelock, indice de conversaciones)`

---

### Task 8: `auth.py` — sesión híbrida

**Files:**
- Create: `src/gemini_image_api/auth.py`, `tests/test_auth.py`

**Interfaces:**
- Consumes: `Settings`, `errors`.
- Produces: `read_cookie_jar(path) -> dict`, `save_cookie_jar_atomic(jar, path)`, `async ensure_client(settings, client_cls) -> client` (con init + guest check → `GIAError(AUTH_EXPIRED)`), `login_guided(settings) -> None` (Playwright Firefox, import lazy dentro de la función).
- NOTA: `auth.py` NO importa gemini_webapi en top-level; recibe `client_cls` por parámetro (client.py se lo pasa) para poder mockear en tests.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_auth.py
import json, pytest
from pathlib import Path
from gemini_image_api.auth import read_cookie_jar, save_cookie_jar_atomic, ensure_client
from gemini_image_api.errors import GIAError

JAR = [{"name": "__Secure-1PSID", "value": "abc", "domain": ".google.com"},
       {"name": "SAPISID", "value": "x", "domain": ".google.com"},
       {"name": "OTRO", "value": "y", "domain": ".example.com"}]

def test_save_and_read_jar_atomic_0600(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    assert (settings.cookie_file.stat().st_mode & 0o777) == 0o600
    jar = read_cookie_jar(settings.cookie_file)
    assert {c["name"] for c in jar} == {"__Secure-1PSID", "SAPISID", "OTRO"}

def test_read_jar_vacio(settings):
    assert read_cookie_jar(settings.cookie_file) == []

class FakeClient:
    def __init__(self, **kw): self.kw = kw; self.inited = False
    async def init(self, **kw): self.inited = True; self.kw.update(kw)
    async def list_models(self): return [{"model_name": "gemini-flash", "is_available": True}]

class FakeClientGuest(FakeClient):
    async def list_models(self): return [{"model_name": "gemini-flash-lite", "is_available": True}]

async def test_ensure_client_ok(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    c = await ensure_client(settings, client_cls=FakeClient)
    assert c.inited

async def test_ensure_client_guest_mode_falla(settings):
    save_cookie_jar_atomic(JAR, settings.cookie_file)
    with pytest.raises(GIAError) as e:
        await ensure_client(settings, client_cls=FakeClientGuest)
    assert e.value.code == "auth_expired"

async def test_ensure_client_sin_cookies_falla(settings):
    with pytest.raises(GIAError) as e:
        await ensure_client(settings, client_cls=FakeClient)
    assert e.value.code == "auth_expired"
```

- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement `auth.py`**

```python
import json, os
from pathlib import Path
from .config import Settings
from .errors import GIAError, AUTH_EXPIRED

def read_cookie_jar(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))

def save_cookie_jar_atomic(jar: list[dict], path: Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(jar, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)

def _cookies_dict(jar: list[dict]) -> dict:
    return {c["name"]: c["value"] for c in jar if c.get("domain") == ".google.com"}

async def ensure_client(settings: Settings, client_cls):
    jar = read_cookie_jar(settings.cookie_file)
    cookies = _cookies_dict(jar)
    if not cookies.get("__Secure-1PSID"):
        raise GIAError(AUTH_EXPIRED, "no hay cookies guardadas", hint="run: gia auth")
    client = client_cls(**cookies)
    await client.init(timeout=30, auto_close=False, close_delay=300, auto_refresh=True)
    try:
        models = await client.list_models()
        names = {getattr(m, "model_name", m.get("model_name") if isinstance(m, dict) else str(m)) for m in models}
    except Exception:
        names = set()
    if names and "gemini-flash-lite" in names and len(names) == 1:
        raise GIAError(AUTH_EXPIRED, "sesion en guest mode (solo flash-lite disponible)", hint="run: gia auth")
    return client

def login_guided(settings: Settings) -> list[dict]:
    """Login guiado con Playwright + Firefox (perfil temporal). Devuelve el jar y lo guarda."""
    from playwright.sync_api import sync_playwright  # import lazy: extra opcional [auth]
    settings.ensure_dirs()
    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://gemini.google.com/app")
        page.wait_for_url("**/app**", timeout=300_000)  # el usuario inicia sesion manualmente
        input("Pulsa ENTER cuando hayas iniciado sesion en Gemini... ")
        cookies = ctx.cookies("https://gemini.google.com")
        browser.close()
    jar = [c for c in cookies if c.get("domain") == ".google.com"]
    save_cookie_jar_atomic(jar, settings.cookie_file)
    return jar
```

- [ ] **Step 4:** `uv run pytest tests/test_auth.py -v` → 5 passed (si `list_models` devuelve dicts, el set comprehension de arriba ya lo contempla).
- [ ] **Step 5:** Commit `feat: auth hibrida (jar 0600 atomico, ensure_client con deteccion de guest mode, login guiado Firefox)`

---

### Task 9: `client.py` — único import de gemini_webapi + locks + errores

**Files:**
- Create: `src/gemini_image_api/client.py`, `tests/test_client.py`

**Interfaces:**
- Consumes: `Settings`, `StateStore`, `ResolvedRef`, `SavedImage`/`save_image`, `GIAError` + codes, `auth.ensure_client`.
- Produces: `Backend` (Protocol), `GeminiWebBackend`, `GenerateResult`, `build_backend(settings=None, state=None, client_cls=None)`.
- Regla dura: `grep -rn "gemini_webapi" src/ --include="*.py" | grep -v "client.py"` → vacío.

- [ ] **Step 1: Write failing tests** (mockeando SIEMPRE el cliente; jamas red real)

```python
# tests/test_client.py
import asyncio, pytest
from pathlib import Path
from gemini_image_api.client import GeminiWebBackend, GenerateResult, build_backend
from gemini_image_api.state import StateStore
from gemini_image_api.errors import GIAError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

class FakeImage:
    def __init__(self, data=PNG): self._data = data
    async def save(self, path=None, filename=None, full_size=True, verbose=False):
        p = Path(path or "/tmp") / (filename or "img.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self._data)
        return p

class FakeOutput:
    def __init__(self, images, text="ok"): self.images = images; self.text = text

class FakeChat:
    def __init__(self): self.metadata = ["cid1", "rid1", "rcid1"]; self.sent = []
    async def send_message(self, prompt, files=None, **kw):
        self.sent.append(prompt)
        return FakeOutput([FakeImage()])

class FakeUpstreamError(Exception): pass

class FakeClient:
    def __init__(self, **kw): pass
    async def init(self, **kw): pass
    async def list_models(self): return [{"model_name": "gemini-flash"}]
    def start_chat(self, **kw): return FakeChat()
    async def generate_content(self, prompt, files=None, model=None, chat=None, **kw):
        return FakeOutput([FakeImage()], text="imagen creada")

async def make_backend(settings, monkeypatch, client=FakeClient):
    from gemini_image_api import client as client_mod
    monkeypatch.setattr(client_mod, "_default_client_cls", client)
    st = StateStore(settings.state_dir)
    return GeminiWebBackend(settings, st), st

async def test_generate_ok(settings, monkeypatch, tmp_path):
    settings.output_root = tmp_path / "generated"
    b, st = await make_backend(settings, monkeypatch)
    res = await b.generate("un gato astronauta", [], model="gemini-flash")
    assert res.status == "ok" and res.code is None
    assert res.conversation_id and res.chat_metadata == ["cid1", "rid1", "rcid1"]
    assert len(res.images) == 1 and res.images[0].path.exists()
    rec = st.conversation(res.conversation_id)
    assert rec is not None and rec.status == "ok" and rec.images[0]["path"] == str(res.images[0].path)

async def test_generate_no_image(settings, monkeypatch, tmp_path):
    class NoImg(FakeClient):
        async def generate_content(self, *a, **k): return FakeOutput([], text="no puedo generar eso")
    settings.output_root = tmp_path / "generated"
    b, _ = await make_backend(settings, monkeypatch, client=NoImg)
    res = await b.generate("prompt bloqueado", [], model="gemini-flash")
    assert res.status == "error" and res.code == "no_image" and res.gemini_text == "no puedo generar eso"

async def test_edit_reusa_chat_metadata(settings, monkeypatch, tmp_path):
    settings.output_root = tmp_path / "generated"
    b, st = await make_backend(settings, monkeypatch)
    r1 = await b.generate("base", [], model="gemini-flash")
    r2 = await b.edit(r1.conversation_id, "ponle gafas", [], model="gemini-flash")
    assert r2.status == "ok" and r2.conversation_id == r1.conversation_id

async def test_edit_conversation_desconocida(settings, monkeypatch, tmp_path):
    settings.output_root = tmp_path / "generated"
    b, _ = await make_backend(settings, monkeypatch)
    with pytest.raises(GIAError) as e:
        await b.edit("c_nope", "x", [], model="gemini-flash")
    assert e.value.code == "unknown_conversation"

async def test_timeout_mapeado(settings, monkeypatch, tmp_path):
    class Slow(FakeClient):
        async def generate_content(self, *a, **k):
            await asyncio.sleep(10); return FakeOutput([FakeImage()])
    settings.output_root = tmp_path / "generated"
    settings.gen_timeout_s = 0.2
    b, _ = await make_backend(settings, monkeypatch, client=Slow)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "error" and res.code == "timeout"

async def test_delete_conversation(settings, monkeypatch, tmp_path):
    class Del(FakeClient):
        def __init__(self, **kw): super().__init__(**kw); self.deleted = []
        async def delete_chat(self, cid): self.deleted.append(cid)
    settings.output_root = tmp_path / "generated"
    b, st = await make_backend(settings, monkeypatch, client=Del)
    r = await b.generate("x", [], model="gemini-flash")
    await b.delete_conversation(r.conversation_id)
    assert st.conversation(r.conversation_id) is None

async def test_build_backend(settings, monkeypatch):
    from gemini_image_api import client as client_mod
    monkeypatch.setattr(client_mod, "_default_client_cls", FakeClient)
    b = build_backend(settings)
    assert isinstance(b, GeminiWebBackend)
```

- [ ] **Step 2:** Run → FAIL ModuleNotFoundError
- [ ] **Step 3: Implement `client.py`** (esqueletico; detalles abajo)

```python
import asyncio, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

from .auth import ensure_client
from .config import Settings
from .errors import (GIAError, AUTH_EXPIRED, USAGE_LIMIT, TEMPORARILY_BLOCKED, NO_IMAGE,
                     TIMEOUT, GENERATED_NOT_SAVED, UNKNOWN_CONVERSATION)
from .refs import ResolvedRef
from .state import StateStore, GenerationRecord
from .storage import SavedImage, save_image

# UNICO import de la libreria upstream (verificar firma real con inspect al instalar):
from gemini_webapi import GeminiClient as _UpstreamClient  # noqa: F401  (sustituido por client_cls en tests)
from gemini_webapi.exceptions import (AuthError, UsageLimitExceededError, TemporarilyBlockedError)
_default_client_cls = _UpstreamClient

@dataclass
class GenerateResult:
    status: str
    code: str | None
    conversation_id: str | None
    images: list[SavedImage]
    gemini_text: str
    elapsed_s: float
    chat_metadata: list | None

@dataclass
class _PendingImage:   # imagen generada upstream, aun no descargada
    raw: object           # GeneratedImage upstream
    index: int

class Backend:  # Protocol por duck typing; documentado para mcp/cli
    async def status(self) -> dict: raise NotImplementedError
    async def list_models(self) -> list[dict]: raise NotImplementedError
    async def generate(self, prompt, refs, *, model, progress=None) -> GenerateResult: raise NotImplementedError
    async def edit(self, conversation_id, prompt, refs, *, model, progress=None) -> GenerateResult: raise NotImplementedError
    async def retry_download(self, conversation_id, image_index) -> SavedImage: raise NotImplementedError
    async def delete_conversation(self, conversation_id) -> None: raise NotImplementedError

def _map_exc(e: Exception) -> GIAError:
    if isinstance(e, GIAError):
        return e
    if isinstance(e, AuthError):
        return GIAError(AUTH_EXPIRED, f"auth upstream: {e}", hint="run: gia auth")
    if isinstance(e, UsageLimitExceededError):
        return GIAError(USAGE_LIMIT, f"cuota agotada: {e}")
    if isinstance(e, TemporarilyBlockedError):
        return GIAError(TEMPORARILY_BLOCKED, f"bloqueo temporal (429): {e}")
    if isinstance(e, asyncio.TimeoutError):
        return GIAError(TIMEOUT, "timeout en la llamada upstream")
    return GIAError("upstream_error", f"{type(e).__name__}: {e}")

class GeminiWebBackend:
    def __init__(self, settings: Settings, state: StateStore, client_cls=None):
        self.settings = settings
        self.state = state
        self.client_cls = client_cls or _default_client_cls
        self._client = None
        self._alock = asyncio.Lock()
        self._flock = FileLock(str(settings.lock_file))
        self._last_call = 0.0

    async def _ensure_client(self):
        if self._client is None:
            self._client = await ensure_client(self.settings, self.client_cls)
        return self._client

    async def status(self) -> dict:
        from pathlib import Path as _P
        jar_ok = self.settings.cookie_file.exists()
        try:
            c = await self._ensure_client()
            models = [getattr(m, "model_name", None) or (m.get("model_name") if isinstance(m, dict) else str(m))
                      for m in await c.list_models()]
            return {"cookie_present": jar_ok, "models": models, "images_today": self.state.count_images_today()}
        except GIAError as e:
            return {"cookie_present": jar_ok, "valid": False, "code": e.code, "images_today": self.state.count_images_today()}

    async def list_models(self) -> list[dict]:
        c = await self._ensure_client()
        return [{"model_name": getattr(m, "model_name", None) or (m.get("model_name") if isinstance(m, dict) else str(m)),
                 "available": getattr(m, "is_available", True)} for m in await c.list_models()]

    async def _generate_locked(self, prompt, ref_paths, *, model, chat=None):
        """Llamada upstream con timeout; devuelve (output, chat). Solo se ejecuta dentro del lock."""
        c = await self._ensure_client()
        await asyncio.sleep(max(0.0, self.settings.min_delay_s - (time.monotonic() - self._last_call)))
        self._last_call = time.monotonic()
        coro = c.generate_content(prompt, files=list(ref_paths) or None, model=model, chat=chat)
        out = await asyncio.wait_for(coro, timeout=self.settings.gen_timeout_s)
        return out, chat

    async def _save_outputs(self, out, *, prompt, project, subdir, style, style_text, aspect_hint, model,
                            cid, conv_short, parent_id, elapsed_s, refs_meta) -> GenerateResult:
        from gemini_webapi.types.image import GeneratedImage, WebImage  # filtrado de tipos upstream
        gen = [im for im in (out.images or []) if isinstance(im, GeneratedImage)]
        text = getattr(out, "text", "") or ""
        if not gen:
            rec = GenerationRecord(id=StateStore.new_id("g"), ts=datetime.now(timezone.utc).isoformat(),
                type="generate", conversation_id=conv_short, chat_metadata=None, prompt=prompt, style=style,
                style_text=style_text, aspect_hint=aspect_hint, model=model, project=project, subdir=subdir,
                refs=refs_meta, images=[], status="error", code=NO_IMAGE, gemini_text=text,
                elapsed_s=elapsed_s, parent_id=parent_id)
            self.state.append(rec)
            return GenerateResult("error", NO_IMAGE, conv_short, [], text, elapsed_s, None)
        images: list[SavedImage] = []
        pending = []
        for i, im in enumerate(gen):
            try:
                tmp = self.settings.state_dir / "tmp_downloads"; tmp.mkdir(parents=True, exist_ok=True)
                path = await asyncio.wait_for(
                    im.save(path=str(tmp), full_size=True, verbose=False),
                    timeout=self.settings.download_timeout_s)
                data = Path(path).read_bytes()
                meta = {"prompt": prompt, "model": model, "conversation_id": conv_short,
                        "ref_sha256": [r["sha256"] for r in refs_meta]}
                saved = save_image(data, output_root=self.settings.output_root, project=project, subdir=subdir,
                                   prompt=prompt, index=i, meta=meta, write_sidecar=self.settings.write_sidecar,
                                   thumb_size=self.settings.thumb_size)
                images.append(saved)
            except Exception:
                pending.append(_PendingImage(raw=im, index=i))
        code = None if not pending else GENERATED_NOT_SAVED
        status = "ok" if not pending else "generated_not_saved"
        rec = GenerationRecord(id=StateStore.new_id("g"), ts=datetime.now(timezone.utc).isoformat(),
            type="generate" if parent_id is None else "edit", conversation_id=conv_short,
            chat_metadata=list(getattr(_chat_holder, "metadata", []) or []), prompt=prompt, style=style,
            style_text=style_text, aspect_hint=aspect_hint, model=model, project=project, subdir=subdir,
            refs=refs_meta, images=[{"path": str(s.path), "width": s.width, "height": s.height,
                                     "bytes": s.nbytes, "format": s.format, "full_size": s.full_size,
                                     "index": i} for i, s in enumerate(images)],
            status=status, code=code, gemini_text=text, elapsed_s=elapsed_s, parent_id=parent_id)
        self.state.append(rec)
        chat_meta = list(getattr(_chat_holder, "metadata", []) or [])
        return GenerateResult(status, code, conv_short, images, text, elapsed_s, chat_meta or None)

    async def generate(self, prompt, refs, *, model, progress=None, project=None, subdir=None,
                       style=None, style_text=None, aspect_hint=None) -> GenerateResult:
        t0 = time.monotonic()
        conv_short = StateStore.new_id("c")
        refs_meta = [{"source": r.source, "path": str(r.path), "sha256": r.sha256} for r in refs]
        async with self._alock, self._flock:
            try:
                if progress: await progress("generando", 0.5)
                client = await self._ensure_client()
                chat = client.start_chat(model=model)
                global _chat_holder; _chat_holder = chat
                out, _ = await self._generate_locked(prompt, [r.path for r in refs], model=model, chat=chat)
                return await self._save_outputs(out, prompt=prompt, project=project, subdir=subdir, style=style,
                    style_text=style_text, aspect_hint=aspect_hint, model=model, cid=None, conv_short=conv_short,
                    parent_id=None, elapsed_s=time.monotonic() - t0, refs_meta=refs_meta)
            except Exception as e:
                err = _map_exc(e)
                return GenerateResult("error", err.code, conv_short, [], str(err), time.monotonic() - t0, None)

    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, project=None, subdir=None,
                   style=None, style_text=None, aspect_hint=None) -> GenerateResult:
        t0 = time.monotonic()
        rec = self.state.conversation(conversation_id)
        if rec is None or not rec.chat_metadata:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} no encontrada", hint="usa generate_image primero")
        refs_meta = [{"source": r.source, "path": str(r.path), "sha256": r.sha256} for r in refs]
        async with self._alock, self._flock:
            try:
                client = await self._ensure_client()
                chat = client.start_chat(model=model, metadata=rec.chat_metadata)
                global _chat_holder; _chat_holder = chat
                out, _ = await self._generate_locked(prompt, [r.path for r in refs], model=model, chat=chat)
                return await self._save_outputs(out, prompt=prompt, project=project, subdir=subdir, style=style,
                    style_text=style_text, aspect_hint=aspect_hint, model=model, cid=None, conv_short=conversation_id,
                    parent_id=rec.id, elapsed_s=time.monotonic() - t0, refs_meta=refs_meta)
            except Exception as e:
                err = _map_exc(e)
                return GenerateResult("error", err.code, conversation_id, [], str(err), time.monotonic() - t0, rec.chat_metadata)

    async def retry_download(self, conversation_id, image_index) -> SavedImage:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} no encontrada")
        async with self._alock, self._flock:
            client = await self._ensure_client()
            chat = client.start_chat(metadata=rec.chat_metadata)
            out = await client.read_chat(rec.chat_metadata[0]) if hasattr(client, "read_chat") else None
            # re-descarga via read_chat; si no hay read_chat, usa el ultimo output cacheado (ver implementacion real)
            raise GIAError("upstream_error", "retry_download requiere read_chat(cid) upstream; implementar con output cacheado")

    async def delete_conversation(self, conversation_id) -> None:
        rec = self.state.conversation(conversation_id)
        if rec is None:
            raise GIAError(UNKNOWN_CONVERSATION, f"conversation {conversation_id} no encontrada")
        async with self._alock, self._flock:
            client = await self._ensure_client()
            await client.delete_chat(rec.chat_metadata[0])
        self.state.append(GenerationRecord(id=StateStore.new_id("g"), ts=datetime.now(timezone.utc).isoformat(),
            type="delete", conversation_id=conversation_id, chat_metadata=None, prompt="", style=None,
            style_text=None, aspect_hint=None, model=None, project=None, subdir=None, refs=[], images=[],
            status="ok", code=None, gemini_text="", elapsed_s=0.0, parent_id=rec.id))

_chat_holder = None

def build_backend(settings=None, state=None, client_cls=None) -> Backend:
    from .config import load_settings
    settings = settings or load_settings()
    state = state or StateStore(settings.state_dir)
    return GeminiWebBackend(settings, state, client_cls=client_cls)
```

> NOTA para el implementer: el esqueletico de arriba usa un `_chat_holder` global fragil — mejóralo pasando `chat` explícitamente a `_save_outputs(..., chat=chat)`. `retry_download` debe implementarse de verdad: guardar el `ModelOutput` (o los `GeneratedImage` crudos serializables) en el registro state para poder re-descargar sin regenerar; si no es serializable, guardar las URLs de descarga. Ajusta según la API real de 2.1.1 (`uv run python -c "import inspect; from gemini_webapi.types.image import GeneratedImage; print(inspect.getsource(GeneratedImage.save))"`).

- [ ] **Step 4:** `uv run pytest tests/test_client.py -v` → all passed (mockeando `gemini_webapi` con `sys.modules` fake si hace falta: en `tests/conftest.py` añade `sys.modules.setdefault("gemini_webapi", fake_module)` ANTES de importar client, o usa `monkeypatch.setitem(sys.modules, ...)`).
- [ ] **Step 5:** Verificar la regla dura: `grep -rn "gemini_webapi" src/ --include="*.py" | grep -v "client.py"` → vacío.
- [ ] **Step 6:** Commit `feat: GeminiWebBackend (unico import de gemini-webapi), locks, timeouts, mapeo de errores, state`

---

### Task 10: `mcp_server.py` — FastMCP con las 9 tools

**Files:**
- Create: `src/gemini_image_api/mcp_server.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `Backend` (de client.py), `Settings`, `StyleCatalog`, `resolve_refs`, `build_prompt`.
- Produces: `create_server(backend, settings) -> FastMCP`, `main()` (stdio). Entry point `gia-mcp`.
- Verifica primero la API real: `uv run python -c "import fastmcp, inspect; print(fastmcp.__version__); print(inspect.signature(fastmcp.FastMCP.tool))"` y cómo se devuelven imágenes (`fastmcp.Image` / content blocks) y progreso (`ctx.report_progress`).

- [ ] **Step 1: Write failing tests** (cliente MCP in-memory con FakeBackend)

```python
# tests/test_mcp_server.py
import pytest
from gemini_image_api.mcp_server import create_server
from gemini_image_api.client import GenerateResult, Backend
from gemini_image_api.storage import SavedImage
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

class FakeBackend(Backend):
    def __init__(self): self.calls = []
    async def status(self): return {"valid": True, "models": ["gemini-flash"], "images_today": 0}
    async def list_models(self): return [{"model_name": "gemini-flash", "available": True}]
    async def generate(self, prompt, refs, *, model, progress=None, **kw):
        self.calls.append(("generate", prompt, model))
        img = SavedImage(path=Path("/tmp/x.png"), width=64, height=64, nbytes=len(PNG), format="png", full_size=True, thumbnail=PNG)
        return GenerateResult("ok", None, "c_abc", [img], "hecho", 1.0, ["cid", "rid", "rcid"])
    async def edit(self, conversation_id, prompt, refs, *, model, progress=None, **kw):
        self.calls.append(("edit", conversation_id, prompt))
        return GenerateResult("ok", None, conversation_id, [], "editado", 0.5, ["cid", "rid", "rcid"])
    async def retry_download(self, conversation_id, image_index):
        return SavedImage(path=Path("/tmp/y.png"), width=64, height=64, nbytes=len(PNG), format="png", full_size=False, thumbnail=None)
    async def delete_conversation(self, conversation_id): self.calls.append(("delete", conversation_id))

@pytest.fixture
def server(settings, tmp_path):
    import yaml
    (tmp_path / "styles.yaml").write_text(yaml.safe_dump({"styles": {"anime": {"description": "d", "append": "anime style"}}, "templates": {}}))
    settings.styles_file = tmp_path / "styles.yaml"
    settings.output_root = tmp_path / "generated"
    return create_server(FakeBackend(), settings)

async def test_tools_list(server):
    from fastmcp import Client
    async with Client(server) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
        assert {"auth_status", "auth_login", "list_styles", "list_templates", "list_models",
                "generate_image", "edit_image", "retry_download", "delete_conversation"} <= names

async def test_generate_image_estructura(server):
    from fastmcp import Client
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "un gato", "style": "anime", "model": "gemini-flash"})
        data = res.data if hasattr(res, "data") else res
        assert data["status"] == "ok" and data["conversation_id"] == "c_abc"
        assert data["images"][0]["path"] == "/tmp/x.png"

async def test_generate_image_style_desconocido(server):
    from fastmcp import Client
    async with Client(server) as c:
        res = await c.call_tool("generate_image", {"prompt": "x", "style": "nope"})
        data = res.data if hasattr(res, "data") else res
        assert data["status"] == "error" and data["code"] == "unknown_style"

async def test_edit_image(server):
    from fastmcp import Client
    async with Client(server) as c:
        res = await c.call_tool("edit_image", {"conversation_id": "c_abc", "prompt": "gafas"})
        data = res.data if hasattr(res, "data") else res
        assert data["status"] == "ok"
```

- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement `mcp_server.py`**

Esquema (ajusta a la API real de fastmcp instalada):

```python
import json
from pathlib import Path
import tempfile
from fastmcp import FastMCP
from .client import Backend, build_backend
from .config import Settings, load_settings
from .errors import GIAError
from .styles import StyleCatalog, build_prompt
from .refs import resolve_refs

def _err(e: GIAError) -> dict:
    return {"status": "error", "code": e.code, "message": str(e), "details": e.details,
            "conversation_id": None, "images": [], "gemini_text": "", "elapsed_s": 0.0}

def create_server(backend: Backend, settings: Settings) -> FastMCP:
    mcp = FastMCP("gemini-image-api")
    styles_path = settings.styles_file or (Path(__file__).parent.parent.parent / "styles.yaml")
    catalog = StyleCatalog.load(styles_path)

    def _catalog() -> StyleCatalog:  # recarga en caliente si el usuario edita styles.yaml
        try: return StyleCatalog.load(styles_path)
        except Exception: return catalog

    @mcp.tool()
    async def auth_status() -> dict:
        """Estado de la sesion de Gemini web (cookie, modelos, guest mode, uso hoy)."""
        return await backend.status()

    @mcp.tool()
    async def auth_login() -> dict:
        """Login guiado con Firefox (Playwright). Requiere extra [auth]."""
        from .auth import login_guided
        jar = login_guided(settings)
        return {"status": "ok", "cookies": len(jar)}

    @mcp.tool()
    async def list_styles() -> dict:
        c = _catalog()
        return {n: s.description for n, s in c.styles.items()}

    @mcp.tool()
    async def list_templates() -> dict:
        c = _catalog()
        return {n: {"description": t.description, "vars": t.vars} for n, t in c.templates.items()}

    @mcp.tool()
    async def list_models() -> list[dict]:
        """Modelos de chat disponibles en tu cuenta (el de chat determina la variante de imagen)."""
        return await backend.list_models()

    @mcp.tool()
    async def generate_image(prompt: str, refs: list[str] | None = None, style: str | None = None,
                             style_text: str | None = None, template: str | None = None,
                             template_vars: dict | None = None, aspect_hint: str | None = None,
                             model: str | None = None, project: str | None = None,
                             subdir: str | None = None, return_image: bool = True) -> dict:
        """Genera una imagen nueva en un chat nuevo de Gemini web y la descarga a output_root."""
        try:
            final_prompt = build_prompt(_catalog(), prompt=prompt, style=style, style_text=style_text,
                                        template=template, template_vars=template_vars, aspect_hint=aspect_hint)
            with tempfile.TemporaryDirectory() as td:
                resolved = await resolve_refs(refs or [], settings, Path(td))
                res = await backend.generate(final_prompt, resolved, model=model or settings.default_model,
                                             project=project, subdir=subdir, style=style, style_text=style_text,
                                             aspect_hint=aspect_hint)
            return _result_dict(res, return_image)
        except GIAError as e:
            return _err(e)

    @mcp.tool()
    async def edit_image(conversation_id: str, prompt: str, refs: list[str] | None = None,
                         style: str | None = None, style_text: str | None = None,
                         aspect_hint: str | None = None, model: str | None = None,
                         project: str | None = None, subdir: str | None = None,
                         return_image: bool = True) -> dict:
        """Edita la imagen de una conversacion existente (chat multi-turn)."""
        try:
            final_prompt = build_prompt(_catalog(), prompt=prompt, style=style, style_text=style_text,
                                        aspect_hint=aspect_hint)
            with tempfile.TemporaryDirectory() as td:
                resolved = await resolve_refs(refs or [], settings, Path(td))
                res = await backend.edit(conversation_id, final_prompt, resolved, model=model or settings.default_model,
                                         project=project, subdir=subdir, style=style, style_text=style_text,
                                         aspect_hint=aspect_hint)
            return _result_dict(res, return_image)
        except GIAError as e:
            return _err(e)

    @mcp.tool()
    async def retry_download(conversation_id: str, image_index: int) -> dict:
        """Reintenta SOLO la descarga de una imagen ya generada (no regenera, no gasta cuota)."""
        try:
            img = await backend.retry_download(conversation_id, image_index)
            return {"status": "ok", "code": None, "image": {"path": str(img.path), "width": img.width,
                    "height": img.height, "bytes": img.nbytes, "format": img.format, "full_size": img.full_size}}
        except GIAError as e:
            return _err(e)

    @mcp.tool()
    async def delete_conversation(conversation_id: str) -> dict:
        """Borra la conversacion en Gemini (delete_chat) y la marca en el historial local."""
        try:
            await backend.delete_conversation(conversation_id)
            return {"status": "ok", "code": None, "conversation_id": conversation_id}
        except GIAError as e:
            return _err(e)

    return mcp

def _result_dict(res, return_image: bool) -> dict:
    return {"status": res.status, "code": res.code, "conversation_id": res.conversation_id,
            "images": [{"path": str(s.path), "width": s.width, "height": s.height, "bytes": s.nbytes,
                        "format": s.format, "full_size": s.full_size, "index": i,
                        **({"thumbnail_png_b64": __import__("base64").b64encode(s.thumbnail).decode()} if (return_image and s.thumbnail) else {})}
                       for i, s in enumerate(res.images)],
            "gemini_text": res.gemini_text, "elapsed_s": res.elapsed_s}

def main() -> None:
    settings = load_settings()
    server = create_server(build_backend(settings), settings)
    server.run()  # stdio por defecto

if __name__ == "__main__":
    main()
```

> La miniatura como image block real de MCP (`fastmcp.Image`) se añade en lugar de `thumbnail_png_b64` si la API lo soporta; en cualquier caso el contrato estructurado lleva `images[].path`.

- [ ] **Step 4:** `uv run pytest tests/test_mcp_server.py -v` → 4 passed
- [ ] **Step 5:** Commit `feat: MCP server FastMCP con 9 tools y contrato estructurado`

---

### Task 11: `cli.py` — typer

**Files:**
- Create: `src/gemini_image_api/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Backend` (inyectable vía `cli.set_backend` para tests), `Settings`, `StyleCatalog`.
- Produces: `app` (typer.Typer), `main()`, entry point `gia`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py
import json
from typer.testing import CliRunner
from gemini_image_api import cli
from gemini_image_api.client import GenerateResult, Backend
from gemini_image_api.storage import SavedImage
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
runner = CliRunner()

class FakeBackend(Backend):
    async def status(self): return {"valid": True, "images_today": 3}
    async def list_models(self): return [{"model_name": "gemini-flash", "available": True}]
    async def generate(self, prompt, refs, *, model, **kw):
        return GenerateResult("ok", None, "c_cli1", [SavedImage(Path("/tmp/z.png"), 64, 64, len(PNG), "png", True, PNG)], "ok", 2.0, ["c", "r", "rc"])
    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        return GenerateResult("ok", None, conversation_id, [], "editado", 1.0, ["c", "r", "rc"])
    async def retry_download(self, conversation_id, image_index):
        return SavedImage(Path("/tmp/z.png"), 64, 64, len(PNG), "png", True, None)
    async def delete_conversation(self, conversation_id): pass

def test_gen(settings, monkeypatch, tmp_path):
    import yaml
    (tmp_path / "styles.yaml").write_text(yaml.safe_dump({"styles": {"anime": {"description": "d", "append": "anime"}}, "templates": {}}))
    settings.styles_file = tmp_path / "styles.yaml"; settings.output_root = tmp_path / "generated"
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["gen", "un gato", "--style", "anime"])
    assert r.exit_code == 0, r.output
    assert "c_cli1" in r.output and "/tmp/z.png" in r.output

def test_styles(settings, monkeypatch, tmp_path):
    import yaml
    (tmp_path / "styles.yaml").write_text(yaml.safe_dump({"styles": {"anime": {"description": "estilo anime", "append": "anime"}}, "templates": {}}))
    settings.styles_file = tmp_path / "styles.yaml"
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
    assert "mcpServers" in cfg and "gemini-image-api" in cfg["mcpServers"]

def test_edit(settings):
    cli.set_backend(FakeBackend(), settings)
    r = runner.invoke(cli.app, ["edit", "c_cli1", "ponle gafas"])
    assert r.exit_code == 0 and "c_cli1" in r.output
```

- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement `cli.py`**

```python
import asyncio, json, sys
from pathlib import Path
import typer
from .client import Backend, build_backend
from .config import Settings, load_settings
from .errors import GIAError
from .styles import StyleCatalog, build_prompt
from .refs import resolve_refs
import tempfile

app = typer.Typer(help="gia — generar/editar imágenes con tu sesión web de Gemini (sin API key)")
_backend: Backend | None = None
_settings: Settings | None = None

def set_backend(backend: Backend, settings: Settings) -> None:
    global _backend, _settings
    _backend, _settings = backend, settings

def _bs() -> tuple[Backend, Settings]:
    global _backend, _settings
    if _backend is None:
        _settings = load_settings()
        _backend = build_backend(_settings)
    return _backend, _settings

def _styles_path(s: Settings) -> Path:
    return s.styles_file or (Path(__file__).parent.parent.parent / "styles.yaml")

def _run(coro):
    return asyncio.run(coro)

def _print_result(res) -> None:
    typer.echo(json.dumps({"status": res.status, "code": res.code, "conversation_id": res.conversation_id,
        "images": [{"path": str(i.path), "width": i.width, "height": i.height} for i in res.images],
        "gemini_text": res.gemini_text, "elapsed_s": res.elapsed_s}, ensure_ascii=False, indent=2))

@app.command()
def gen(prompt: str, refs: list[Path] = typer.Option([], "--ref", "-r"),
        style: str | None = None, style_text: str | None = None, template: str | None = None,
        var: list[str] = typer.Option([], "--var", "-v", help="k=v"),
        aspect: str | None = typer.Option(None, "--aspect"),
        project: str | None = None, subdir: str | None = None,
        model: str | None = None):
    """Genera una imagen nueva (chat nuevo)."""
    b, s = _bs()
    catalog = StyleCatalog.load(_styles_path(s))
    tv = dict(kv.split("=", 1) for kv in var)
    final = build_prompt(catalog, prompt=prompt, style=style, style_text=style_text, template=template,
                         template_vars=tv or None, aspect_hint=aspect)
    with tempfile.TemporaryDirectory() as td:
        resolved = _run(resolve_refs([str(r) for r in refs], s, Path(td)))
        res = _run(b.generate(final, resolved, model=model or s.default_model, project=project, subdir=subdir,
                              style=style, style_text=style_text, aspect_hint=aspect))
    _print_result(res)
    if res.status == "error":
        raise typer.Exit(1)

@app.command()
def edit(conversation_id: str, prompt: str, refs: list[Path] = typer.Option([], "--ref", "-r"),
         style: str | None = None, style_text: str | None = None, aspect: str | None = None,
         project: str | None = None, subdir: str | None = None, model: str | None = None):
    """Edita la imagen de una conversación existente."""
    b, s = _bs()
    catalog = StyleCatalog.load(_styles_path(s))
    final = build_prompt(catalog, prompt=prompt, style=style, style_text=style_text, aspect_hint=aspect)
    with tempfile.TemporaryDirectory() as td:
        resolved = _run(resolve_refs([str(r) for r in refs], s, Path(td)))
        res = _run(b.edit(conversation_id, final, resolved, model=model or s.default_model, project=project,
                          subdir=subdir, style=style, style_text=style_text, aspect_hint=aspect))
    _print_result(res)
    if res.status == "error":
        raise typer.Exit(1)

@app.command()
def auth(status: bool = typer.Option(False, "--status")):
    """Login guiado (Firefox) o chequeo de sesión."""
    b, s = _bs()
    if status:
        typer.echo(json.dumps(_run(b.status()), ensure_ascii=False, indent=2))
        return
    from .auth import login_guided
    jar = login_guided(s)
    typer.echo(f"OK: {len(jar)} cookies guardadas en {s.cookie_file}")

@app.command()
def styles():
    """Lista estilos y plantillas."""
    _, s = _bs()
    c = StyleCatalog.load(_styles_path(s))
    typer.echo(json.dumps({"styles": {n: x.description for n, x in c.styles.items()},
                           "templates": {n: {"vars": t.vars, "description": t.description} for n, t in c.templates.items()}},
                          ensure_ascii=False, indent=2))

@app.command()
def models():
    """Modelos de chat disponibles en tu cuenta."""
    b, _ = _bs()
    typer.echo(json.dumps(_run(b.list_models()), ensure_ascii=False, indent=2))

@app.command(name="retry-download")
def retry_download(conversation_id: str, image_index: int):
    """Reintenta solo la descarga de una imagen ya generada."""
    b, _ = _bs()
    img = _run(b.retry_download(conversation_id, image_index))
    typer.echo(json.dumps({"path": str(img.path), "full_size": img.full_size}, ensure_ascii=False))

@app.command()
def delete(conversation_id: str):
    """Borra la conversación en Gemini y en el historial local."""
    b, _ = _bs()
    _run(b.delete_conversation(conversation_id))
    typer.echo(f"borrada {conversation_id}")

@app.command(name="mcp-config")
def mcp_config():
    """Imprime el JSON para registrar este MCP en tu agente (omp, Claude Code, Claude Desktop, Cursor)."""
    import gemini_image_api
    root = Path(gemini_image_api.__file__).parent.parent.parent
    typer.echo(json.dumps({"mcpServers": {"gemini-image-api": {
        "command": "uv", "args": ["run", "--directory", str(root), "gia-mcp"]} pia}}, indent=2))

@app.command()
def serve():
    """Corre el servidor MCP por stdio."""
    from .mcp_server import main
    main()

def main() -> None:
    app()

if __name__ == "__main__":
    main()
```

- [ ] **Step 4:** `uv run pytest tests/test_cli.py -v` → 5 passed
- [ ] **Step 5:** Commit `feat: CLI typer (gen/edit/auth/styles/models/retry-download/delete/mcp-config/serve)`

---

### Task 12: README + verificación final + commit

**Files:**
- Create: `README.md`
- Modify: `pyproject.toml` (añadir `license-files` si aplica)

- [ ] **Step 1: Write README.md** con: qué es, advertencia ToS + **cuenta secundaria dedicada recomendada**, install (`uv sync --extra auth --extra test`, `uv run playwright install firefox`), primer uso (`uv run gia auth`, `uv run gia gen "un gato astronauta" --style fotorealista`), registrar MCP en agente (ejemplo JSON para omp/Claude Code/Claude Desktop/Cursor con `uv run --directory <repo> gia-mcp`), tabla de tools MCP y comandos CLI, config (`~/.config/gemini-image-api/config.yaml`, env `GIA_*`), estructura de salida (`generated/<project>/…`), estado (`~/.gemini-image-api/generations.jsonl`, cookies 0600), seguridad (allowlist refs, anti-SSRF), E2E manual mínimo tras updates de gemini-webapi, tests (`uv run pytest`), roadmap v2, licencia AGPL-3.0.
- [ ] **Step 2: Run full suite** — `uv run pytest -q` → all passed
- [ ] **Step 3: Verify hard rule** — `grep -rn "gemini_webapi" src/ --include="*.py" | grep -v "client.py"` → vacío; `uv run gia --help` funciona; `uv run gia mcp-config` imprime JSON válido.
- [ ] **Step 4: Commit** — `git add README.md && git commit -m "docs: README con setup, disclaimer ToS, config MCP y E2E manual"`

---

## Self-Review (ejecutado al escribir este plan)

1. **Cobertura del spec:** §4 arquitectura → Tasks 1–11; §5 auth → Task 8; §6 persistencia única → Task 7; §7 contrato MCP → Task 10; §8 CLI → Task 11; §9 flujo → Tasks 5, 6, 9, 10; §10 locks/timeouts/errores → Task 9; §11 seguridad → Tasks 5, 6, 8; §12 riesgos → Task 12 (README); §13 tests → todas. Sin gaps.
2. **Placeholders:** ningún TBD; los únicos ajustes permitidos están acotados a "verificar firma real de fastmcp/gemini-webapi instalada" con comandos `inspect` explícitos (las versiones reales pueden diferir en detalle de sintaxis, no en contrato).
3. **Consistencia de tipos:** `GIAError.code`, `ResolvedRef`, `SavedImage`, `GenerationRecord`, `StateStore`, `GenerateResult`, `Backend` tienen las mismas firmas en las interfaces compartidas y en cada task. `delete_conversation` está en v1 en contrato MCP, CLI y Task 9/10/11. `aspect_hint` (no `aspect_ratio`) en todo el plan.
4. **Ambigüedad resuelta:** `_chat_holder` global se marca como a-mejorar (pasar `chat` explícito); `retry_download` requiere cachear el output upstream en state — anotado como nota del implementer con comando `inspect` para ver `GeneratedImage.save`.

## Execution Handoff

Plan guardado en `docs/superpowers/plans/2026-11-09-gemini-image-api.md`. Opciones:

1. **Subagent-Driven (recomendado)** — un subagente fresco por task vía subagent-driven-development, con review entre tasks.
2. **Inline Execution** — ejecuto las tasks en esta sesión con executing-plans, con checkpoints.
