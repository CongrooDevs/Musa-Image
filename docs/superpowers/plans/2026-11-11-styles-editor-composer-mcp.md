# Editor de estilos, compositor y herramientas MCP - Plan

Spec: `docs/superpowers/specs/2026-11-11-styles-editor-composer-mcp-design.md` (aprobado). Rama `feat/styles-editor-composer`.

## Global Constraints
- Python >=3.11, uv. `uv run pytest -q -W error` verde al terminar cada task. Sin red real en tests.
- Solo `client.py` importa `gemini_webapi`.
- Código, identificadores, comentarios y mensajes de error en inglés; textos de UI y README en español.
- Un commit convencional por task, solo con tus archivos (`git add <rutas>`), con `git -c user.name="diegoandresmc" -c user.email="diegoandresmc@localhost" commit`. Si hay `index.lock`, reintenta. Si fallan tests ajenos, anótalo.
- Sin `innerHTML` con datos de la API en el frontend (usa `h()` / `textContent`).
- Sin códigos de error nuevos (22 existentes). Estilos integrados no existen hoy (`styles: {}`); `source` es `"builtin"|"user"`.

## Archivos por task (dueño único)
| Task | Archivos |
|---|---|
| T1 | `style_store.py`, `styles.py`, `tests/test_style_store.py`, `tests/test_styles.py` |
| T2 | `service.py`, `library.py`, `state.py`, `gallery.py`, sus tests |
| T3 | `mcp_server.py`, `cli.py`, `tests/test_mcp_server.py`, `tests/test_cli.py` |
| T4 | `webapp.py`, `jobs.py`, `tests/test_webapp.py`, `tests/test_jobs.py` |
| T5 | `web/index.html`, `web/app.css`, `web/app.js` |
| T6 | `README.md` |

## Interfaces compartidas
T1 expone en `UserStyleStore`:
- `update(sid, *, name=None, description=None, append=None, family=None, image: tuple[bytes, str] | None = None, remove_image=False) -> Style`. Solo `source=="user"`; si no, `GIAError(INVALID_STYLE)`; id inmutable; imagen reemplazada de forma atómica (SVG -> PNG como en `create`); borra la imagen anterior solo si está dentro de `self.dir`; estilo inexistente -> `UNKNOWN_STYLE`.
- `styles.preview_prompt(catalog, *, prompt, style=None, style_text=None, template=None, template_vars=None, aspect_hint=None) -> dict` = `{final_prompt, style_ref: str|None, aspect_hint}` sin red, reutiliza `build_prompt`.

T2 expone en `ImageService` (async salvo que se indique), todas aceptan `image="gen:<id>[#i]"`:
- `rename_collection(collection, name, description=None) -> dict`, `delete_collection(collection) -> dict`, `remove_from_collection(image, collection) -> dict`
- `set_favorite(image, favorite=True) -> dict`, `get_image(image) -> dict` (`{ref, path, prompt, style, model, ts, width, height, favorite, collections, conversation_id}`), `delete_image(image) -> dict` (unlink archivo y miniatura, `mark_deleted`; no toca Gemini)
- `generate_batch(prompts: list[str], *, style=None, collection=None, aspect_hint=None, model=None, **kw) -> dict`: máx. 8, secuencial, devuelve `{status, results:[to_payload...], stopped_at}`; se detiene ante `quota_guard`/`usage_limit`/`auth_*`.
- `create_user_style(...)`/`update_user_style(...)`/`delete_user_style(...)` ya existen o delegan en `UserStyleStore` (+ Gem); T2 añade `update_user_style(sid, **campos)` que llama a `UserStyleStore.update` y actualiza el Gem si está activo (instrucciones personalizadas no se tocan) y `describe_style(style, prompt=None) -> dict` llamando a `styles.preview_prompt`. T2 usa la firma de T1 tal como está arriba (T1 corre en paralelo: implementa contra la interfaz y, al integrar, ejecuta los tests de T1 ya commiteados).

## Tasks
### T1 - UserStyleStore.update + preview_prompt
Requisitos: interfaz de arriba + tests (campos, imagen nueva, `remove_image`, imagen vieja fuera de `dir` no se borra, builtin rechazado, id inexistente, SVG convertido, preview con estilo/plantilla/aspect). Commit `feat: UserStyleStore.update y preview_prompt`.

### T2 - Servicio: colecciones, favoritas, imágenes, lote, describe y update de estilo
Requisitos: interfaz de arriba. Reutiliza `LibraryStore` (añade `rename_collection`, `delete_collection` si faltan), `gallery.image_index` para resolver `gen:` y respetar `output_root`. Tests con `FakeBackend`. Commit `feat: servicio de colecciones, favoritas, imagenes y lote`.

### T3 - MCP + CLI (depende de T1, T2)
Tools nuevas con las mismas anotaciones que las existentes (RO / idempotent / destructive): `create_style(name, instructions, description?, family?, image?)` (image = ruta permitida, https o `gen:`; se resuelve con `resolve_refs`), `update_style`, `delete_style`, `describe_style`, `rename_collection`, `delete_collection`, `remove_from_collection`, `favorite_image`, `get_image`, `delete_image`, `generate_batch`. Errores vía `error_payload`. `INSTRUCTIONS` y la lista de tools actualizadas (total 25). CLI: `gia styles add|edit|rm|show`, `gia collections rename|rm|remove`, `gia fav <gen> [--off]`, `gia rm <gen>`. Commit `feat: herramientas MCP y CLI de estilos, colecciones e imagenes`.

### T4 - Web backend (depende de T1, T2)
- `PUT /api/styles/{sid}` (JSON o multipart; 403 `invalid_style` en builtin; reutiliza `_reject_oversized_body`, 20 MB, `remove_image`).
- `GET /api/styles/{sid}/preview?prompt=&aspect=` -> `styles.preview_prompt`; `GET /api/templates` -> `{name: {description, vars}}`.
- `POST /api/jobs` acepta `template`, `template_vars` y `edit_of:"gen:<id>#<i>"`; `jobs.py` llama a `service.edit(conversation_id, ...)` cuando hay `edit_of` (N fijo a 1, sin lote) y a `generate(template=...)` si hay plantilla. Validación síncrona en `submit`.
- SSE `styles` mtime ya existe: debe cambiar al editar.
Tests con `FakeBackend` y TestClient (forma de errores de 6 campos, `edit_of` borrado -> `invalid_ref`, plantilla sin variable -> `template_missing_var`). Commit `feat: web backend para editar estilos, plantillas y editar imagen`.

### T5 - Frontend (depende de T4)
Implementa spec §2.3, §3, §4, §5 en `web/*`: diálogo único de estilo (crear/editar, vista previa del prompt con debounce, «Probar» con confirmación y cuota, «Más opciones»), tarjeta con acciones visibles, compositor reordenado (referencias con miniaturas + arrastrar/pegar, proporción como selector con nombre, selector de modelo, menú de plantillas con campos por variable, historial con flecha arriba, «Guardar en» en la fila de opciones, padding para «Recientes»), botón «Editar» en el detalle (modo edición con `edit_of`). Verificar con Chromium headless contra un servidor falso (no gastar cuota) a 1440 y 390 px; consola vacía; capturas en `/tmp/musa-v2/`. Commit `feat: editor de estilos, compositor y editar imagen en Musa`.

### T6 - README (depende de T3-T5)
Actualiza tools (25), CLI, Musa (estilos editables, compositor, editar imagen), tabla de errores sin cambios. Commit `docs: README editor de estilos y herramientas MCP`.

## Verificación final (controlador)
Copia del estado en `/tmp`, 1 «Probar» real + 1 edición real, aviso de cuota previo, limpieza de la copia.
