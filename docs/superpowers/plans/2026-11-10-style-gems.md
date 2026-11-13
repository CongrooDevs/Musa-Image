# Gem de estilo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** implementar `docs/superpowers/specs/2026-11-10-style-gems-design.md`. Cualquier estilo de Musa puede tener un Gem de Gemini, que se activa estilo por estilo y se usa automáticamente al generar.

**Verified (2026-11-10, controller spike on a state copy):**
- `client.create_gem(name, prompt, description)` returns a `Gem` with `.id`.
- `client.start_chat(model=..., gem=id)` + `client.generate_content(prompt, model=..., gem=id, chat=chat)` returns `out.images` (1 image, saved full size).
- `client.fetch_gems()` returns a `GemJar`; iterating it yields `Gem` objects with `.id`.
- `client.delete_gem(id)` removes the Gem (checked through `fetch_gems`).
- `update_gem(gem, name, prompt, description)` exists in `gemini_webapi` 2.1.1 (`components/gem_mixin.py`).

## Global Constraints
- Solo `src/gemini_image_api/client.py` importa `gemini_webapi`. Los tests nunca usan la red.
- `uv run pytest -q -W error` debe quedar verde en cada task.
- Commit: uno por task, mensaje convencional y solo los archivos propios (`git add <rutas>`). Usar `git -c user.name="diegoandresmc" -c user.email="diegoandresmc@localhost" commit`.
- Idioma: código e identificadores en inglés; interfaz y README en español.
- Códigos de error: los 22 del agente no cambian. `gem_missing` es solo un `warning`.
- Formato de warning: `{"code","message","next_action"}`.
- Nada de pruebas contra la cuenta real. La verificación real la hace el controlador.

## Interfaces compartidas

```python
# client.py
@dataclass GenerateResult: ... , warning: dict | None = None          # LAST field (after record_id)
GenerationRecord.gem_id: str | None = None                               # LAST field (state.py)
class Backend:
    async def create_gem(self, name: str, instructions: str, description: str = "") -> str: raise NotImplementedError
    async def update_gem(self, gem_id: str, name: str, instructions: str, description: str = "") -> None: raise NotImplementedError
    async def delete_gem(self, gem_id: str) -> None: raise NotImplementedError
    async def gem_exists(self, gem_id: str) -> bool: raise NotImplementedError
# generate(..., gem_id: str | None = None) / edit(..., gem_id: str | None = None)
# edit: gem_id param ignored if the conversation record has gem_id (conversation keeps its Gem); else uses param.
GEM_MISSING = "gem_missing"   # warning code constant in client.py (not in errors.py codes)

# style_gems.py
class StyleGemStore:
    def __init__(self, state_dir: Path)
    def get(self, style_id: str) -> dict | None      # {gem_id, name, instructions, custom, created, updated}
    def put(self, style_id: str, entry: dict) -> None
    def remove(self, style_id: str) -> None
    def all(self) -> dict[str, dict]
    def mtime(self) -> float
def default_instructions(style: Style) -> str       # template of spec §3.1
def gem_name(style: Style) -> str                   # "Musa · <name>"[:60]

# service.py
ImageService(backend, settings, state, library=None, styles=None, gems: StyleGemStore | None = None)
ImageService.set_style_gem(style_id: str, enabled: bool, instructions: str | None = None) -> dict   # async
    # returns {"style": id, "gem": {gem_id, name, custom} | None}
ImageService.style_gem_instructions(style_id: str) -> dict   # {"instructions": str, "custom": bool}
ImageService.list_style_gems() -> list[dict]                 # [{style, gem_id, name, custom}]
ServiceResult.warning: dict | None = None                    # LAST field; from GenerateResult.warning
# to_payload adds "warning" when not None
```

---

### Task 1: backend de Gems en `client.py` + `gem_id` en el registro

**Files:** `client.py`, `state.py`, `tests/test_client.py` y `tests/test_state.py` si hace falta.

- [ ] **Tests primero**, con el `gemini_webapi` falso que ya usa `test_client.py` (léelo):
  - `create_gem` llama a `client.create_gem(name=…, prompt=instructions, description=…)` y devuelve el id.
  - `update_gem` y `delete_gem` llaman a la librería.
  - `gem_exists` usa `fetch_gems` con caché de 60 s: dos llamadas seguidas hacen una sola petición. Recibe un reloj inyectable.
  - `generate(gem_id="x")` con un Gem que existe: `start_chat` y `generate_content` reciben `gem="x"`, y el registro guarda `gem_id="x"`.
  - Gem que no existe: se llama sin `gem`, `warning.code == "gem_missing"` y el registro queda con `gem_id=None`.
  - `fetch_gems` lanza error: se intenta igualmente con el Gem.
  - `edit` sobre una conversación con `gem_id` reutiliza ese Gem.
  - `retry_download` copia `gem_id`.
  - Las líneas antiguas del historial sin `gem_id` se siguen cargando.
- [ ] **Implementar:**
  - Todas las operaciones de Gem van bajo `_alock` + `_flock`, con `_throttle`, y los errores pasan por `_map_exc`.
  - `_call_upstream(prompt, ref_paths, model, metadata=None, gem=None)` pasa `gem` a `start_chat` y a `generate_content` solo si no es `None`.
- [ ] Commit: `feat: Gems en el backend (crear, editar, borrar, usar) y gem_id en el historial`.

### Task 2: `StyleGemStore` + servicio

**Files:**
- Create: `style_gems.py`, `tests/test_style_gems.py`.
- Modify: `service.py`, `tests/test_service.py`.

- [ ] **`StyleGemStore`** sigue el patrón de `LibraryStore`: lock, escritura atómica y archivo corrupto → vacío + renombrado.
- [ ] **`default_instructions`** usa exactamente la plantilla del spec §3.1. La línea de la ref solo aparece si `style.ref`; el `credit` vacío se omite.
- [ ] **`set_style_gem`:**
  - Activar crea el Gem y guarda la entrada con `custom=False`.
  - Activar con `instructions` (exista o no): crea el Gem si no existe, o llama a `update_gem` si ya existe, y guarda `custom=True`.
  - `instructions=""` restaura la plantilla (`custom=False`) y llama a `update_gem`.
  - Desactivar llama a `delete_gem` y quita la entrada. Si `delete_gem` falla porque el Gem ya no existe, se quita igualmente. Un error de red se propaga y la entrada se conserva.
  - Un estilo desconocido da `UNKNOWN_STYLE`.
- [ ] **En `_run`:** si `style` tiene entrada, pasa `gem_id=` al backend; en `edit` también. Si `res.warning` trae el código `gem_missing`, se ejecuta `gems.remove(style)`.
  - `ServiceResult.warning` y `to_payload` incluyen el warning.
  - `_run` recibe un callback, o pasa `gem_id` por `call`: adapta el lambda de `generate` y `edit`.
- [ ] **Borrado de estilos propios:** se expone `ImageService.delete_user_style(style_id)`, que borra el Gem (si existe) y luego el estilo. Los handlers de la web y la CLI lo usan en lugar de llamar directamente a `UserStyleStore.delete`; revisa dónde se borra hoy.
- [ ] **Tests** de todo lo anterior, con el `FakeBackend` de `test_service.py` ampliado con Gems en memoria.
- [ ] Commit: `feat: Gem por estilo en el servicio (StyleGemStore, activar/desactivar, uso al generar)`.

### Task 3: MCP + CLI + cola

**Files:** `mcp_server.py`, `cli.py`, `jobs.py` y sus tests.

- [ ] **MCP:**
  - `list_style_gems()` → `{status:"ok", gems:[…]}`, solo lectura.
  - `style_gem(style, enabled, instructions?)`: los errores se devuelven con `error_payload`.
  - Añadir los Gems a `INSTRUCTIONS`.
  - `generate_image` y `edit_image` devuelven `warning` cuando lo hay.
- [ ] **CLI:** `gia styles gem STYLE on|off [--instructions TEXT]` y `gia styles gems`.
- [ ] **`JobQueue`:** si `sr.warning` viene informado y el trabajo no tiene otro aviso, `job.warning = sr.warning`.
- [ ] **Tests**, incluido el esquema MCP.
- [ ] Commit: `feat: Gems de estilo en MCP, CLI y cola`.

### Task 4: web — endpoints y frontend

**Files:** `webapp.py`, `web/app.js`, `web/app.css`, `web/index.html`, `tests/test_webapp.py`.

- [ ] **API:**
  - `GET /api/styles` añade `gem`.
  - `PUT /api/styles/{id}/gem` con `{enabled: bool, instructions?: str}`. Errores: `invalid_json`, `unknown_style` (404) y los del backend con el payload de 6 campos.
  - `GET /api/styles/{id}/gem/instructions`.
  - `DELETE /api/styles/{id}` usa `service.delete_user_style`.
  - El SSE `changed` incluye el mtime de `gems.json`.
- [ ] **Frontend (spec §6):**
  - Interruptor «Usar Gem» (`button role=switch aria-checked`) en cada tarjeta de estilo, como hermano de la tarjeta y no anidado, igual que `.del`. Mientras se procesa queda deshabilitado con un spinner.
  - Insignia «Gem».
  - Enlace «Instrucciones» que abre un diálogo con textarea, «Guardar», «Restaurar plantilla» y «Cancelar».
  - Chip del compositor: «· Gem».
  - Toast de `gem_missing`.
  - Sin `innerHTML`.
- [ ] **Verificación:**
  - Navegador con un servidor de backend falso: `/tmp/musa2_server.py` ampliado en una copia fuera del repo, con Gems en memoria.
  - Clics reales: activar, editar instrucciones, restaurar y desactivar; generar con un estilo que tenga Gem (el backend falso recibe `gem_id`).
  - Capturas y consola vacía.
- [ ] Commit: `feat: interruptor Usar Gem e instrucciones en Musa`.

### Task 5: README

- [ ] Nueva sección «Gem de estilo» en español, más las tablas de MCP y CLI. Cada nombre se verifica contra `src/`.
- [ ] Commit: `docs: README Gem de estilo`.

### Task 6: verificación real (controlador)

- [ ] Sobre una copia del estado en `/tmp`:
  1. `gia app` en otro puerto.
  2. Activar Gem en «Impresionismo» y generar 1 imagen desde el compositor; comprobar que el registro tiene `gem_id`.
  3. Desactivar y comprobar con `fetch_gems` que el Gem ya no está en la cuenta.

  Gasta 1 imagen.
- [ ] Después, borrar la copia y comprobar que el estado real no cambió.
