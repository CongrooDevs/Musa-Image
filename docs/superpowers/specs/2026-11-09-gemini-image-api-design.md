# Diseño: `gemini-image-api` — MCP + CLI de generación de imágenes vía sesión web de Gemini

- **Fecha:** 2026-11-09
- **Estado:** APROBADO por el usuario (diseño v3, tras validación con 4 subagentes)
- **Repo:** `~/Desktop/Web/GeminiImageApi`
- **Licencia del proyecto:** AGPL-3.0 (obligado por la dependencia `gemini-webapi`, también AGPL-3.0)

---

## 1. Objetivo

Servicio local en Python que usa la **sesión web de Gemini** (cookies del navegador, vía la librería `gemini-webapi`) para que agentes de IA generen y editen imágenes —con referencias, prompts, estilos y descarga automática— **sin API key de pago**, usando la suscripción Gemini del usuario. Se expone como **MCP server (stdio)** para agentes y como **CLI** para uso manual.

Fuera de alcance v1: video/audio, API oficial de pago, UI web propia, multi-usuario, upscaling, inpainting con máscara, export a cloud, quitar la marca de agua de Gemini.

## 2. Decisiones tomadas por el usuario

| Tema | Decisión |
|---|---|
| Acceso a Gemini | **B**: API interna no oficial de `gemini.google.com` con cookies de sesión (no navegador automatizado, no API de pago) |
| Exposición | **A**: MCP server (FastMCP, stdio) + CLI (typer) |
| Autenticación | **C híbrida**: sesión guardada en disco → si falla/expirada, login guiado automático (Playwright + Firefox) → fallback manual exportando cookies a `cookies.json` |
| Estilos | **C**: catálogo en `styles.yaml` (`list_styles`) + `style_text` libre; nombre desconocido → error con lista de válidos |
| Multi-turn | **C**: `generate_image` crea conversación y devuelve `conversation_id`; `edit_image(conversation_id, …)` itera sobre ella |
| Defaults asumidos y aprobados | refs = paths locales + URLs (con protección SSRF); salida en raíz global con `project`/`subdir`; 1 generación a la vez; timeout 300 s generación / 60 s descarga; `model` = modelo de chat; persistencia única en `generations.jsonl` |

## 3. Base técnica validada

- **Dependencia core:** `gemini-webapi==2.1.1` (pin exacto; 2026-08-27, última en PyPI). Repo: https://github.com/HanaokaYuzu/Gemini-API (3.8k★, activo). La serie 1.17.x está obsoleta: en 2.x los modelos son dinámicos (`client.list_models()`), el enum `Model` está deprecado.
- API real confirmada (v2.1.1):
  - `GeminiClient(...)` + `await client.init(timeout=30, auto_close=False, close_delay=300, auto_refresh=True)`
  - Generación: `await client.generate_content(prompt, files=[...], model=..., chat=...) -> ModelOutput`; la imagen se pide en el prompt ("generate…"). **No existe `generate_image()` dedicado.**
  - `ModelOutput.images`: lista de `GeneratedImage` | `WebImage` → **filtrar solo `GeneratedImage`** (si el prompt no dice "generate" puede devolver `WebImage`).
  - Edición: mismo `ChatSession` (`chat.send_message("…", files=[...])`) o `generate_content(..., chat=chat)`.
  - Refs: `files` acepta `str | Path | bytes | BytesIO` (**no URLs** → nosotros descargamos con httpx a temp con extensión real; pasar bytes hace que el MIME salga `text/plain` por el nombre aleatorio `input_NNN.txt`).
  - Descarga: `await image.save(path=..., filename=..., full_size=True)` (default; RPC `c8o8Fe`, fallback a `=s2048-rj`; `full_size=False` → `=s1024-rj`). Devuelve ruta absoluta.
  - Persistencia de chat: **en servidor**. `start_chat(metadata=chat.metadata)` retoma el chat en otro proceso. `chat.metadata` es una lista (`[cid, rid, rcid, …]`, ~10 elementos) → hay que persistirla **completa**, no solo el cid. También `read_chat(cid)`, `list_chats()`, `delete_chat(cid)`.
  - Modelos: `client.list_models()` → `AvailableModel(model_name, display_name, is_available)`. En web hoy: `gemini-pro`, `gemini-flash`, `gemini-flash-lite`. El modelo de **chat** determina la variante de imagen: flash/pro → Nano Banana 2, pro con "Redo with Pro" → Nano Banana Pro, flash-lite → Nano Banana 2 Lite (degradado).
  - Cookies: `auto_refresh=True` rota `__Secure-1PSIDTS` en background (mín. 60 s entre rotaciones, solo mientras vive el proceso). Caché por defecto en tmpdir → fijar `GEMINI_COOKIE_PATH` dentro de `~/.gemini-image-api/cookies/`.
  - Errores: `AuthError`, `APIError`, `TimeoutError`, `GeminiError`, `TemporarilyBlockedError` (429), `UsageLimitExceededError`.
- **No existe** en la librería: parámetro `aspect_ratio` (va como hint en el prompt y se valida el tamaño real tras guardar), `seed`, login guiado (lo hacemos nosotros con Playwright), concepto de estilos.
- Proyectos de referencia: AndyShaman/gemini-webapi-mcp (AGPL, lock + timeout 600 s + quitar marca), miuceo/gemini-image-mcp (MIT, corrige extensión por contenido, galería HTML).
- Timeout observado real: generaciones 22–345 s → nuestro timeout 300 s (configurable), descarga 60 s.

## 4. Arquitectura

```
Agente IA ──stdio──▶ mcp_server.py (FastMCP) ─┐
Tú ─────▶ cli.py (typer) ──────────────────────┼─▶ client.py ─▶ gemini-webapi==2.1.1 ─▶ gemini.google.com (tu sesión)
                                               │     ▲ único archivo que toca la librería
                        config.py ◀───────────┘     filelock (multi-proceso)
                        auth.py / styles.py / refs.py / storage.py / errors.py
```

### Estructura de archivos

```
pyproject.toml                 # uv, python>=3.11; deps: gemini-webapi==2.1.1 (pin), fastmcp, typer,
                               #   httpx, pydantic-settings, pyyaml, pillow, filelock, playwright (extra auth)
styles.yaml                    # {styles: {nombre: {description, append}}, templates: {nombre: {prompt, vars}}}
.gitignore                     # .venv, __pycache__, generated/, ~/.gemini-image-api estado, cookies
README.md                      # setup, disclaimer ToS, cuenta secundaria, config MCP para agentes (omp/Claude Code/Desktop/Cursor)
src/gemini_image_api/
  __init__.py
  config.py                    # ~/.config/gemini-image-api/config.yaml + env GIA_* (output_root, timeouts, allowed_ref_dirs, …)
  errors.py                    # códigos tipados: auth_expired, usage_limit, temporarily_blocked, no_image,
                               #   timeout, content_blocked, generated_not_saved, invalid_ref, ssrf_blocked, …
  auth.py                      # sesión híbrida: cargar cookies → login guiado (Playwright Firefox) → export manual;
                               #   guardar jar completo 0600, escrituras atómicas; detectar guest mode vía list_models
  refs.py                      # validación allowlist dirs (resolve+is_relative_to, no symlinks, nunca ~/.gemini-image-api),
                               #   magic bytes png/jpeg/webp/gif, ≤20 MB, ≤10 refs; URLs → httpx con anti-SSRF → temp con extensión real
  client.py                    # ÚNICO módulo que importa gemini_webapi. Interfaz propia (ports/adapters) para poder
                               #   cambiar de backend (fallback API oficial en v2). filelock + asyncio.Lock,
                               #   timeout 300/60 s, reintentos SOLO en errores transitorios (TimeoutError/red), nunca AuthError/429/límite
  styles.py                    # carga styles.yaml, resuelve style (error con válidos si no existe) + style_text + template con vars
  storage.py                   # guarda imágenes bajo output_root/project/subdir; nombre YYYYMMDD-HHMMSS_slug_<i>.<ext>;
                               #   extensión por magic bytes (suele ser JPEG); miniatura 512 px; metadata tEXt en PNG; sidecar .json opcional
  state.py                     # generations.jsonl append-only bajo filelock (ver §6)
  mcp_server.py                # FastMCP stdio; tools en §7; logs a stderr/archivo, nunca stdout; filtro de secretos en logs
  cli.py                       # typer: gia auth|styles|models|gen|edit|retry-download|delete|mcp-config|serve
tests/                         # unitarios con gemini-webapi mockeado (NUNCA se golpea Google en tests)
docs/superpowers/specs/2026-11-09-gemini-image-api-design.md   # este documento
```

### Estado en disco (`~/.gemini-image-api/`, dir 0700)

- `cookies/` — jar completo de cookies (0600), vía `GEMINI_COOKIE_PATH`; escrituras atómicas (temp + `os.replace`)
- `generations.jsonl` — **único registro de estado** (ver §6)
- `logs/` — log rotativo (sin secretos)

## 5. Autenticación (híbrida, decisión C)

1. Al iniciar, `client.py` carga cookies guardadas (`GEMINI_COOKIE_PATH`) e `init(auto_refresh=True)`.
2. `auth_status` verifica: cookie presente, init OK, `list_models()` devuelve modelos de cuenta (no guest: guest → solo `gemini-flash-lite`, fallo silencioso → detectar y reportar `auth_expired`).
3. Si falla/expirada: `auth_login` abre **Playwright con Firefox** (Chromium usa Device Bound Session Credentials: cookies que caducan en horas), perfil nuevo, el usuario inicia sesión en Google, se exportan las cookies con `domain == ".google.com"` exacto (no subcadena — bug conocido con `.google.com.bd`), se cierra el navegador y se guarda el jar.
4. Fallback manual: el usuario pega un export de cookies en `cookies.json`.
5. Tras cada sesión exitosa se persisten las cookies renovadas (no depender de `__Secure-1PSID` como clave de caché).

## 6. Persistencia consolidada (un solo registro)

**`generations.jsonl`** (append-only, bajo `filelock` multi-proceso, línea JSON por evento):

```json
{"id": "g_01J…", "ts": "2026-11-09T21:30:00Z", "type": "generate|edit",
 "conversation_id": "c_01J…", "chat_metadata": ["cid", "rid", "rcid", "…"],
 "prompt": "…", "style": "fotorealista", "style_text": null, "aspect_hint": "16:9", "model": "gemini-flash",
 "project": "clienteA", "subdir": null,
 "refs": [{"source": "path|url", "path": "…", "sha256": "…"}],
 "images": [{"index": 0, "path": "/abs/generated/…/20261109-213000_gato-astronauta_0.jpg", "width": 1408, "height": 768, "bytes": 123456, "full_size": true}],
 "status": "ok|generated_not_saved|error", "code": null, "gemini_text": "…", "elapsed_s": 42.1, "parent_id": null}
```

- El índice de conversaciones se deriva del jsonl (última entrada por `conversation_id` = metadata vigente). **No hay `conversations.json`.**
- **Sidecar `.json` por imagen: opcional y off por defecto** (`write_sidecar: false` en config); si se activa, copia la entrada del jsonl junto a la imagen.
- **Metadata dentro del PNG (tEXt)**: prompt, style, model, conversation_id, sha256 de refs → cada imagen es reproducible por sí sola.

## 7. Contrato MCP (v1)

Todas las tools devuelven respuesta estructurada:
`{status, code, conversation_id, images: [{path, width, height, bytes, full_size, index}], gemini_text, elapsed_s}`.
La respuesta incluye **miniatura 512 px** (image block MCP) + ruta absoluta; `return_image` (default true) para desactivarla.

| Tool | Params clave | Notas |
|---|---|---|
| `auth_status()` | — | cookie, modelos (detecta guest mode), antigüedad cookie, imágenes hoy (conteo local desde jsonl) |
| `auth_login()` | — | login guiado Firefox |
| `list_styles()` / `list_templates()` | — | catálogo desde `styles.yaml` |
| `list_models()` | — | modelos de chat disponibles en la cuenta |
| `generate_image()` | `prompt`*, `refs[]`, `style`, `style_text`, `template`+`template_vars`, `aspect_hint` (1:1 default, 16:9, 9:16, 4:3, 3:4 — **hint en el prompt**, se valida tamaño real), `model` (default `gemini-flash`), `project`, `subdir`, `return_image` | nueva conversación; prompt siempre prefijado con instrucción de "generate an image" para evitar `WebImage` |
| `edit_image()` | `conversation_id`*, `prompt`*, `refs[]`, `style`, `style_text`, `aspect_hint`, `model`, … | continúa la conversación vía `chat_metadata` persistida |
| `retry_download()` | `conversation_id`*, `image_index`* | reintenta SOLO la descarga (fallback preview `full_size=False`); nunca regenera |
| `delete_conversation()` | `conversation_id`* | `client.delete_chat(cid)` + marca entradas como borradas en el jsonl (**v1**) |

Progreso: notificaciones MCP (`report_progress`) en los pasos: esperando lock → subiendo refs → generando → descargando.

## 8. CLI (typer)

```
gia auth [--status]                 # login guiado / chequear sesión
gia styles                          # listar estilos y plantillas
gia models                          # modelos disponibles
gia gen "prompt" -r foto.png -r https://… --style fotorealista --template anuncio --vars producto=… --aspect 16:9 --project clienteA
gia edit <conversation_id> "ponle gafas de sol" [-r ref.png]
gia retry-download <conversation_id> <image_index>
gia delete <conversation_id>
gia mcp-config                      # imprime JSON de config MCP para omp / Claude Code / Claude Desktop / Cursor
gia serve                           # corre el MCP por stdio
```

## 9. Flujo de una generación

1. Agente → `generate_image` → valida style/template → adquiere `filelock` + `asyncio.Lock` → asegura sesión (`auth.py`, guest-mode check).
2. Refs: paths dentro de `allowed_ref_dirs` (default cwd + output_root) validados por magic bytes; URLs → descarga anti-SSRF a temp con extensión real.
3. Prompt final = instrucción "generate" + prompt + style/template + aspect hint → `client.generate_content(..., files=refs)`.
4. Filtrar `GeneratedImage` de `ModelOutput.images`; si `images == []` → `code: "no_image"` + `gemini_text`.
5. `image.save(full_size=True)` → storage: nombre único con sufijo `_<i>`, extensión por magic bytes, metadata tEXt, sidecar opcional, miniatura.
6. Append a `generations.jsonl` (metadata completa del chat) → respuesta estructurada + miniatura + `conversation_id`.
7. Descarga fallida → `status: "generated_not_saved"`, se puede `retry_download` después (nunca regenera, no gasta cuota).

## 10. Concurrencia, timeouts, reintentos

- `filelock` (entre procesos MCP/CLI) + `asyncio.Lock` (dentro del proceso) → **1 generación a la vez** (issue #356: concurrencia cierra el cliente compartido).
- Timeout: 300 s generación, 60 s descarga (configurables); **plazo total por llamada**, no por intento.
- Reintentos: solo `TimeoutError` y errores de red, con backoff, dentro del plazo total. `AuthError` → 1 rebuild del cliente dentro del lock, luego `auth_expired`. `UsageLimitExceededError` → `usage_limit` (falla rápido). `TemporarilyBlockedError` (429) → `temporarily_blocked` (falla rápido; reintentar empeora el bloqueo). Error cards transitorias de Google se tratan como fallo, no como éxito (issue #364).
- Ritmo bajo: retardo mínimo configurable entre peticiones + tope diario configurable (la cuota web es compute-based, opaca, refresh cada 5 h hasta límite semanal; degradación silenciosa a modelo menor → detectarla comparando modelo devuelto vs pedido y reportar `model_downgraded`).

## 11. Seguridad

- Refs: allowlist de directorios (`Path.resolve()` + `is_relative_to`, corta symlinks), nunca `~/.gemini-image-api`, magic bytes de imagen real, ≤20 MB/archivo, ≤10 refs/llamada.
- URLs en refs (anti-SSRF): resolver DNS y bloquear loopback/privadas/link-local (169.254.169.254 etc.), límite de tamaño durante la descarga, timeout, máx. redirects.
- Salida: solo bajo `output_root` (config, default `./generated`) vía `project`/`subdir` saneados (sin `..`, sin absolutos).
- Cookies: dir 0700, archivo 0600, creados con ese modo desde el principio; escrituras atómicas.
- Logs: stderr + archivo rotativo, nunca stdout (rompe stdio MCP); filtro que oculta `__Secure-*`, `SAPISID`, cabecera `Cookie`.

## 12. Riesgos aceptados y mitigaciones (README)

- **ToS**: automatizar `gemini.google.com` puede quedar fuera de los Términos de Servicio de Google; las consecuencias recaen sobre tu cuenta (riesgo bajo-medio documentado, sin bans de cuenta confirmados, pero el ToS permite suspenderla) → **usa una cuenta Google secundaria dedicada con la suscripción**, no tu cuenta principal (Gmail/Drive).
- **Rotura técnica frecuente** (cada 1–2 meses observado; bus factor de 1 mantenedor): pin exacto de `gemini-webapi`, `client.py` aislado con interfaz propia, tests mockeados, E2E manual tras cada update.
- **Cuota opaca**: compute-based, puede degradar en silencio → conteo local + detección de degradación; tratar `usage_limit` como estado normal.
- **Sesión**: cookies Chromium device-bound (usan Firefox para exportar), auto-refresh poco fiable → `auth_status` detecta guest mode; fallback manual `cookies.json`.
- **Ejecutar en tu máquina / IP residencial** (bloqueos reportados desde proxies/datacenter).
- No usar el contenido generado para entrenar modelos (prohibido expresamente por el ToS).
- v2: fallback al API oficial de pago detrás de la misma interfaz de `client.py`.

## 13. Tests y verificación

- Unitarios con `gemini-webapi` **mockeado** (nunca se golpea Google): resolución de estilos/templates, validación de refs (allowlist, magic bytes, límites), anti-SSRF, naming/extensión/sufijo único, storage + metadata PNG, estado jsonl (append, derivación de conversaciones), lock, clasificación de errores, contrato de respuestas.
- E2E manual (única vez que se toca Google): `gia auth` → `gia gen "un gato astronauta"` → verificar que 2.1.1 devuelve imágenes en el formato actual, refs máximas aceptadas, aspect hints reales, `edit_image` tras reinicio, `auto_refresh` con sesión exportada.

## 14. Roadmap

- **v1** (este spec): todo lo de §4–§13.
- **v2**: `list_generations(query, since)` (búsqueda en historial), recursos MCP `image://{id}`, `gia serve --http` (servidor único compartido entre agentes), `keyring` para cookies, fallback API oficial de pago, galería HTML estática.
- **Descartado**: upscaling local, inpainting con máscara, export a cloud, quitar marca de agua, modo async con `job_id` (el progreso MCP cubre la espera en v1), `format`/conversión a JPG, `new_chat`, `aspect_ratio` como contrato duro, `seed`.

## 15. Fuentes

- Librería: https://pypi.org/project/gemini-webapi/ · https://github.com/HanaokaYuzu/Gemini-API (README, `types/image.py`, `utils/upload_file.py`, `utils/rotate_1psidts.py`, `exceptions.py`; issues #313, #318, #319, #323, #344, #356, #364, #365)
- MCPs de referencia: https://github.com/AndyShaman/gemini-webapi-mcp · https://github.com/miuceo/gemini-image-mcp
- ToS: https://policies.google.com/terms · Límites Gemini Apps: https://support.google.com/gemini/answer/16275805
- Ayuda modelos de imagen en web: https://support.google.com/gemini/answer/14286560
