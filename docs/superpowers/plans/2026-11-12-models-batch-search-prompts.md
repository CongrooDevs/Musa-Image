# Modelos, lote, búsqueda, prompts y deuda v1.2 - Plan

Spec aprobado: `docs/superpowers/specs/2026-11-12-models-batch-search-prompts-design.md`. Rama `feat/models-batch-search-prompts`. Implementa `agent: task`, revisa `agent: reviewer`. Fusión a `main` solo con OK del usuario.

## Global Constraints
- `uv run pytest -q -W error` verde al terminar cada task; sin red real en tests; solo `client.py` importa `gemini_webapi`.
- Código y errores en inglés; UI y README en español. Cero `innerHTML` con datos; ningún `replaceChildren(`/`.append(` nativo con `null` posible.
- Un commit convencional por task, solo con tus archivos (`git add <rutas>`) y `git -c user.name="diegoandresmc" -c user.email="diegoandresmc@localhost" commit`. Si hay `index.lock`, reintenta.
- Nunca tocar el puerto 8765 (app real del usuario) ni imprimir su clave; pruebas contra servidor falso `/tmp/musa2_server.py` (puerto 8791, clave `k3y`).
- Sin gastar cuota real salvo orden expresa del controlador.

## Orden y dueños (un dueño por archivo y por ola)
**Ola 1 (en paralelo, archivos disjuntos)**
- D1 deuda v1.2: `state.py`, `client.py`, `config.py`, `tests/test_state.py`, `tests/test_client.py`, `tests/test_config.py`, `tests/conftest.py`.
- P1 PromptStore: `prompts.py` (nuevo), `errors.py`, `tests/test_prompts.py`, `tests/test_errors.py`.

**Ola 2 (en paralelo, archivos disjuntos)**
- M1 modelos (backend): `jobs.py`, `webapp.py` (solo `list_models` y validación de modelo), `mcp_server.py` (solo `list_models`), `tests/test_jobs.py`, `tests/test_webapp.py`, `tests/test_mcp_server.py`; añade a `client.py` solo `resolve_model_name` (D1 ya cerró).
- P2 prompts (servicio, MCP, CLI): `service.py`, `mcp_server.py` (solo tools de prompts), `cli.py`, tests. M1 y P2 editan `mcp_server.py` en bloques distintos: commits separados y `git pull`-style cuidado (releer antes de editar).

**Ola 3**
- P3 prompts (web backend): `webapp.py`, `tests/test_webapp.py`.
- S1 búsqueda (backend): `gallery.py`, `state.py` (solo helpers), `tests/test_gallery.py` (ola 2 cerrada). `webapp.py` y `mcp_server.py` los tocará S2 después.

**Ola 4**
- S2 búsqueda (web API + MCP `list_generations(model)`): `webapp.py`, `mcp_server.py`, tests.
- B1 lote (backend): `service.py` (batch/zip), `webapp.py` tras S2, tests. (Secuencial con S2.)

**Ola 5 (frontend, un solo dueño de `web/*`, tres pasos con commit)**
- F1 modelos + prompts (popover «Prompts», selector de modelos reales con «Auto · usa …»).
- F2 filtros, orden y paleta ⌘K.
- F3 selección múltiple, barra de acciones y descarga zip.

**Ola 6**
- R README (tools 28, modelos, prompts, lote, búsqueda, «Para el equipo»).
- Revisión final de la rama por `reviewer`.
- Controlador: capturas y verificación; 0 imágenes reales salvo decisión del usuario.

## Interfaces compartidas
- `backend.resolve_model_name(name) -> str` (client.py): resuelve alias a `model_name` de la cuenta; si no resuelve devuelve `name`.
- `PromptStore(state_dir)`: `list()`, `create(name, text) -> dict`, `update(pid, name=None, text=None) -> dict`, `delete(pid)`, `use(pid)`, `mtime()`. Errores `GIAError("invalid_prompt")`, `GIAError("unknown_prompt")`.
- Códigos nuevos: `invalid_prompt`, `unknown_prompt` (22 -> 24) con `NEXT_ACTION` y test de unicidad actualizado.
- `query_images(..., model=None, sort="newest", since=None, until=None)`; `sort in {"newest","oldest","favorites"}`.
- Batch: `POST /api/images/batch`, `POST /api/images/zip` según spec §2.

## Tasks
Cada task lee el spec y el código real antes de escribir; el informe de cada uno va a `.superpowers/sdd/2026-11-12-models-batch/task-<id>-report.md` (no versionado) y se devuelve: status, hash, tests, concerns.
