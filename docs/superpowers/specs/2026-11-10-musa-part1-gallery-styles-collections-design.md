# Diseño: Musa · parte 1 — Galería, Estilos y Colecciones

- **Fecha:** 2026-11-10
- **Estado:** APROBADO por el usuario (9/9 secciones, sin cambios)
- **Subproyecto:** 2 («galería de estilos»), dividido en 3 partes. Este spec cubre solo la **parte 1**. La parte 2 (Inicio: generar desde la app + cola) y la parte 3 (Límites de uso) tendrán su propio spec.
- **Base:** `main` @ `c4f2299`+ (v1.1). Spec anterior: `docs/superpowers/specs/2026-11-09-v1.1-robustness-agent-ux-design.md`.
- **Diseño visual de referencia:** `docs/design/musa-v6/galeria-musa-v6.html` y `docs/design/musa-v6/assets/` (copia versionada del proyecto Open Design del usuario; es la fuente de verdad del aspecto).

## 1. Objetivo

Una app web local, **Musa**, para ver y organizar lo generado y para gestionar estilos, con el aspecto del diseño `galeria-musa-v6`: tema crema, Instrument Sans y obras de dominio público como referencia de estilo.

Comparte datos con el MCP y la CLI: lo que genera el agente aparece solo en la galería, y los estilos y colecciones creados en la app los usa el agente.

**Esta parte nunca llama a Gemini y no gasta cuota.**

## 2. Decisiones del usuario

| Tema | Decisión |
|---|---|
| Alcance del subproyecto | App Musa completa, en 3 partes. Parte 1 = Galería + Estilos + Colecciones |
| Uso de un estilo al generar | Texto del estilo **+ su imagen como referencia** (lo más fiel) |
| Crear estilos | Desde una imagen de la galería · con nombre + descripción + texto · subiendo una imagen |
| Colección | **Etiqueta**: una imagen puede estar en varias colecciones; el archivo no se mueve |
| Colecciones para el agente | Sí (MCP y CLI) |
| Arranque | `gia app`, y además el MCP puede levantarla |
| Actualización | En vivo: lo que genera el agente aparece sin recargar |
| Acciones en el detalle de imagen | Colecciones · guardar como estilo · copiar `gen:<id>` · descargar / abrir en Finder · borrar · favorita |
| Enfoque técnico | HTML del diseño como base (HTML/CSS/JS sin framework) + API JSON en Python (Starlette/uvicorn) + SSE |

Adaptaciones del diseño a Gemini web, aceptadas al elegir el alcance:
- no hay GPT Image;
- no hay "créditos" ni plan de pago: la cuota real llega en la parte 3;
- se genera de una en una, no "2/4 simultáneas";
- 1 imagen por petición.

En la parte 1 no aparece ninguno de estos elementos.

## 3. Alcance de la parte 1

**Incluye:**
- Pantalla de carga: figuras de la Encyclopédie + obras, con un botón para saltarla. Se muestra una vez por sesión del navegador.
- Barra lateral con **Galería**, **Estilos** y **Colecciones**, con contadores. Inicio y Límites no aparecen hasta sus partes.
- Galería en mosaico:
  - filtros: Todas · En colección · Sin colección · Favoritas;
  - filtro por estilo y búsqueda por texto en el prompt.
- Estilos:
  - filtro por familia: Todos · Pintura · Grabado · Diseño · Mis estilos;
  - tarjeta «Nuevo estilo».
- Colecciones: lista con portadas apiladas, tarjeta «Nueva colección» y vista de detalle, que es una galería filtrada.
- Panel de detalle de imagen: imagen grande, prompt, estilo, modelo, fecha y `gen:<id>`, con 6 acciones.
- Colecciones disponibles también para el agente (MCP/CLI) y estilos con imagen de referencia al generar.

**No incluye:** generar desde la app, cola de pendientes, Límites de uso, créditos, GPT, generación en paralelo, acceso desde otra máquina y multiusuario.

## 4. Arquitectura

```
navegador ──HTTP/SSE──▶ webapp.py (Starlette) ─┐
agente ─stdio─▶ mcp_server.py ─────────────────┼─▶ service.py (ImageService)
tú ─▶ cli.py ──────────────────────────────────┘     ├─ state.py       generations.jsonl (historial, append-only)
                                                      ├─ library.py     library.json (colecciones, favoritas, borradas)  [nuevo]
                                                      ├─ style_store.py estilos propios en <state_dir>/styles/  [nuevo]
                                                      ├─ styles.py      catálogo (incluidos + propios) y prompt
                                                      └─ client.py      Gemini (sin cambios de contrato; no se usa en la parte 1)
```

- **`library.py` (nuevo)** — `LibraryStore(state_dir)`:
  - datos de organización: colecciones, pertenencias, favoritas y borradas;
  - escritura atómica (temp + `os.replace`) bajo `FileLock(<state_dir>/library.lock)`.
- **`style_store.py` (nuevo)** — `UserStyleStore(state_dir)`: crear y borrar estilos propios (`<state_dir>/styles/styles.yaml` + imágenes en el mismo directorio).
- **`webapp.py` (nuevo)** — `create_app(settings, service, library, styles, key) -> Starlette`: API + archivos estáticos + SSE.
- **`web/` (nuevo, package data)** — `index.html`, `app.css`, `app.js`, `assets/{fonts,arte,figuras}/` y `assets/fonts/OFL.txt`.
- **`service.py`:**
  - inyecta la imagen de referencia del estilo;
  - parámetro `collection`;
  - oculta lo borrado.
- **`cli.py` / `mcp_server.py`:** comando `gia app`, opción para que el MCP levante la app, y tools/opciones de colecciones.

Dependencias directas nuevas en `pyproject.toml`: `starlette`, `uvicorn`, `sse-starlette`, `python-multipart`. Hoy ya llegan a través de fastmcp; se declaran para no depender de eso.

## 5. Arranque

- **`gia app [--port N] [--no-open]`:**
  - `--port` vale por defecto `settings.app_port = 8765` (`GIA_APP_PORT`);
  - arranca uvicorn en `127.0.0.1`;
  - genera una clave aleatoria (`secrets.token_urlsafe(24)`);
  - escribe `<state_dir>/app.json` (`{url, port, pid, started}`, permisos 0600);
  - imprime la URL en stderr y la abre en el navegador, salvo con `--no-open`.
- **Si el puerto ya está ocupado** y `app.json` apunta a un proceso vivo en ese puerto, `gia app` no arranca otro: abre la URL guardada.
- **Con el MCP** (`GIA_APP_WITH_MCP=true`, por defecto `false`):
  - `gia-mcp` arranca la app en el mismo proceso, como tarea concurrente junto a `run_async(transport="stdio")`, con la misma lógica de puerto ocupado. Si no puede enlazar el puerto, registra un warning y sigue solo con MCP.
  - **stdout es del protocolo MCP:** uvicorn va con `log_config=None` y `access_log=False`, y los logs salen por el logging propio (stderr/archivo).
  - `auth_status` añade `app_url` cuando la app está corriendo (leído de `app.json` con el pid vivo).

## 6. Seguridad

- Solo `127.0.0.1`. No hay opción de otra interfaz en la parte 1.
- **Clave por arranque:**
  - `GET /?key=<k>` responde con la cookie `gia_key` (HttpOnly, SameSite=Strict, Path=/) y redirige a `/`;
  - `index.html` y todo `/api/*` exigen la cookie o la cabecera `X-GIA-Key`; si no, `401`;
  - los estáticos de `web/assets/*` no la exigen, porque no contienen datos del usuario.
- **Anti-CSRF / anti-DNS-rebinding:**
  - todas las peticiones deben traer `Host` ∈ {`127.0.0.1:<port>`, `localhost:<port>`};
  - las que modifican (`POST/PUT/PATCH/DELETE`) exigen además `Origin` ∈ {`http://127.0.0.1:<port>`, `http://localhost:<port>`};
  - si no, `403`.
- **Archivos servidos:** `/api/image` y `/api/thumb` solo sirven rutas registradas en `generations.jsonl` que, tras `resolve()`, estén dentro de `output_root`, existan y no estén borradas. Si no, `404`. Nunca se aceptan rutas del cliente.
- **Borrar** solo actúa sobre esas mismas rutas validadas.
- **Subida de estilo:**
  - hasta `max_ref_bytes` (20 MB), validada por magic bytes (png/jpeg/webp/gif);
  - se guarda con un nombre generado por el servidor;
  - si no es una imagen o es demasiado grande → `422`.
- **Revelar en Finder:** `open -R <ruta validada>` sin shell, en macOS. En otro sistema, `501`.

## 7. Datos

### 7.1 `generations.jsonl`: prompt del usuario

- `GenerationRecord` añade un último campo `user_prompt: str | None = None`, con default para que las líneas antiguas sigan cargando.
- `client.py` lo rellena con `slug_source`, el prompt tal como lo escribió el usuario.
- **`display_prompt(rec)`** (en `state.py`) devuelve `rec.user_prompt` si existe; para registros antiguos devuelve `rec.prompt.removeprefix(GENERATE_PREFIX)`, sin intentar separar el texto añadido por estilo o aspecto (en esos registros puede verse al final; es aceptable).
- Lo usan la API y `list_generations`.

### 7.2 `library.json`

```json
{
  "version": 1,
  "collections": [
    {"id": "col_1a2b3c4d", "name": "Campaña otoño", "description": "Piezas para redes", "created": "2026-11-10T07:00:00+00:00"}
  ],
  "membership": {"g_0123456789ab#0": ["col_1a2b3c4d"]},
  "favorites": ["g_0123456789ab#0"],
  "deleted": ["g_ffffffffffff#0"]
}
```

- **Clave de imagen:** `"<record_id>#<index>"`, la misma forma que `gen:` sin el prefijo.
- **`LibraryStore`:**
  - `collections()`;
  - `get_collection(id_or_name)`, que busca por id o por nombre sin distinguir mayúsculas;
  - `create_collection(name, description="")`: nombre único sin distinguir mayúsculas; `id = "col_" + 8 hex`; si el nombre está vacío o duplicado → `GIAError(INVALID_COLLECTION)`;
  - `update_collection(id, name?, description?)`;
  - `delete_collection(id)`, que también quita sus pertenencias;
  - `add(key, coll_id)` y `remove(key, coll_id)`;
  - `collections_of(key)`;
  - `set_favorite(key, bool)` y `is_favorite(key)`;
  - `mark_deleted(key)` e `is_deleted(key)`;
  - `mtime()`.
- **`resolve_collection(name_or_id, create: bool)`** sirve al agente: si `create` es true, crea la colección si no existe.
- Un `library.json` corrupto o ilegible se trata como vacío: se registra un warning y se renombra a `library.json.corrupt-<ts>`. Nunca debe romper la galería.

### 7.3 Borrar una imagen

1. Valida la ruta (§6).
2. `unlink` del archivo y de su miniatura.
3. `library.mark_deleted(key)` y la quita de colecciones y favoritas.

`generations.jsonl` no se reescribe, porque es append-only. Efectos:
- la galería y `list_generations` omiten la imagen;
- un registro sin imágenes visibles no aparece;
- `gen:<id>#i` de una imagen borrada → `invalid_ref` con el mensaje `"image i of g_… was deleted"`.

### 7.4 Códigos de error nuevos

`INVALID_COLLECTION` (`invalid_collection`), `UNKNOWN_COLLECTION` (`unknown_collection`) y `INVALID_STYLE` (`invalid_style`). Pasan de 19 a 22 códigos, cada uno con su `NEXT_ACTION`. Ninguno es retryable.

## 8. Estilos

### 8.1 Esquema

```yaml
styles:
  post:
    name: "Postimpresionismo"
    family: pintura            # pintura | grabado | diseno | otros
    description: "Pincelada en espiral, color saturado"
    append: "post-impressionist painting, swirling expressive brushstrokes, saturated complementary colors, thick impasto"
    ref: "web/assets/arte/starry.jpg"   # relativo al directorio base del catálogo (ver 8.2)
    credit: "Vincent van Gogh · La noche estrellada · 1889"
```

`Style` pasa a tener los campos `id, name, family, description, append, ref: Path | None, credit, source ("builtin"|"user")`. Un estilo sin `ref` es solo texto. Los YAML antiguos (solo `description` + `append`) siguen siendo válidos: `name` vale el id y `family` vale `otros`.

### 8.2 Catálogo

**Incluidos** (`src/gemini_image_api/styles.yaml`), con `ref` relativo a la raíz del paquete. Son los 8 del diseño:

| id | name | family | ref (`web/assets/arte/`) | append |
|---|---|---|---|---|
| post | Postimpresionismo | pintura | starry.jpg | post-impressionist painting, swirling expressive brushstrokes, saturated complementary colors, thick impasto |
| ukiyo | Ukiyo-e | grabado | wave.jpg | ukiyo-e woodblock print, flat color areas, bold outlines, Prussian blue, washi paper texture |
| impre | Impresionismo | pintura | lilies.jpg | impressionist painting, plein air light, loose visible brushstrokes, soft broken color |
| barroco | Barroco | pintura | pearl.jpg | baroque oil painting, dramatic chiaroscuro, single window light, deep shadows, rich glazes |
| renac | Renacimiento | pintura | venus.jpg | renaissance painting, sfumato, balanced classical composition, tempera and oil on panel |
| roman | Romanticismo | pintura | wanderer.jpg | romantic landscape painting, sublime scale, mist and atmosphere, dramatic sky |
| nouveau | Art Nouveau | diseno | kiss.jpg | art nouveau, gold leaf, ornamental flowing patterns, decorative mosaic |
| neo | Neoplasticismo | diseno | mondrian.jpg | de stijl neoplasticism, strict grid, primary colors with black lines on white |

A ellos se suman `fotorealista`, `anime` e `isometrico`, solo con texto y `family: otros`. El `credit` de cada obra se copia del diseño (`docs/design/musa-v6/galeria-musa-v6.html`, objeto `ART`).

**Composición del catálogo:**
- base = el archivo de `resolve_styles_path` (`GIA_STYLES_FILE` → `~/.config/gemini-image-api/styles.yaml` → el del paquete), con sus `ref` relativos al directorio de ese archivo, salvo el paquete, cuyos `ref` son relativos a la raíz del paquete;
- **más** los estilos propios (`<state_dir>/styles/styles.yaml`), siempre;
- si hay choque de ids, gana el incluido: `UserStyleStore` rechaza crear un id que ya existe.

### 8.3 Estilos propios (`UserStyleStore`)

`create(name, description, append, family="otros", from_key=None, upload=(bytes, filename)|None) -> Style`:
- `id = slugify(name)`, con sufijo `-2`, `-3`… si ya existe;
- `from_key`: copia la imagen de esa generación (ruta validada como en §6) a `<state_dir>/styles/<id>.<ext>`;
- `upload`: la valida (§6) y la guarda igual;
- si no hay imagen, el estilo es solo texto;
- `append` vacío → se usa `description`;
- `name` vacío → `GIAError(INVALID_STYLE)`.

`delete(id)`: solo estilos `user`, y borra también su imagen. Un estilo incluido → `GIAError(INVALID_STYLE)`.

Escritura atómica bajo `FileLock(<state_dir>/styles.lock)`.

### 8.4 Uso al generar (`ImageService`)

- Si el estilo elegido tiene `ref`, se antepone a las refs del usuario un `ResolvedRef(path=style.ref, source=f"style:{id}", sha256=…)`.
- Es **de confianza**, porque no lo elige el agente: no pasa por la allowlist. Sí se comprueba que existe y que es una imagen.
- Cuenta para `max_refs`: refs del usuario + 1 ≤ `max_refs`, si no `invalid_ref`.
- El texto `append` se sigue añadiendo como hoy, con `build_prompt`.
- `list_styles` (MCP) mantiene su forma `{id: description}` y añade los estilos propios sin reiniciar, porque el catálogo se recarga en cada llamada.

## 9. Colecciones para el agente

- `generate_image` y `edit_image` ganan `collection: str | None` (nombre o id). Después de guardar, cada imagen guardada se añade con `resolve_collection(…, create=True)`. La respuesta añade `collection: {id, name}`.
- Tool nueva **`list_collections()`**, de solo lectura: `{status, collections: [{id, name, description, count}]}`.
- Tool nueva **`add_to_collection(image, collection)`**, idempotente:
  - `image` = `gen:<id>[#i]`;
  - `collection` = nombre o id; se crea si no existe;
  - imagen inexistente o borrada → `invalid_ref`.
- `list_generations` gana `collection` (filtra) y omite las imágenes borradas.
- `INSTRUCTIONS` del MCP: una línea sobre `collection=` y sobre que los estilos con imagen la envían como referencia.
- **CLI:**
  - `gia gen/edit --collection X`;
  - `gia collections` (lista);
  - `gia collections add gen:<id>[#i] <colección>`;
  - `gia history --collection X`;
  - `gia app`.

## 10. API HTTP

Todas bajo la clave y las reglas de §6. Los errores devuelven el mismo `error_payload` (`code`, `message`, `details`, `retryable`, `next_action`) con estado HTTP 400/401/403/404/409/422.

| Método y ruta | Cuerpo / parámetros | Respuesta |
|---|---|---|
| `GET /` | `?key=` la primera vez | `index.html` (cookie fijada) |
| `GET /api/state` | — | `{counts: {images, collections, favorites, styles}, output_root, app_version}` |
| `GET /api/generations` | `query, collection, filter=all\|in_collection\|no_collection\|favorites, style, limit=60 (≤200), offset` | `{items: [ImageItem], total}`, ordenado de más reciente a más antiguo |
| `GET /api/image/{gen}/{i}` | — | archivo original |
| `GET /api/thumb/{gen}/{i}` | — | JPEG de 640 px (caché en `<state_dir>/thumbs/<gen>_<i>.jpg`, se regenera si el original es más reciente) |
| `PUT` / `DELETE /api/favorites/{gen}/{i}` | — | `{key, favorite}` |
| `DELETE /api/images/{gen}/{i}` | — | `{key, deleted: true}` |
| `POST /api/images/{gen}/{i}/reveal` | — | `{ok: true}` / 501 |
| `GET /api/collections` | — | `[{id, name, description, count, covers: [thumb_url ≤3]}]` |
| `POST /api/collections` | `{name, description?}` | 201 colección / 409 duplicada |
| `PATCH /api/collections/{id}` | `{name?, description?}` | colección |
| `DELETE /api/collections/{id}` | — | `{deleted: id}` |
| `POST /api/collections/{id}/items` | `{key}` | `{key, collections}` |
| `DELETE /api/collections/{id}/items/{gen}/{i}` | — | `{key, collections}` |
| `GET /api/styles` | — | `[{id, name, family, description, credit, cover_url\|null, has_ref, source}]` |
| `POST /api/styles` | JSON `{name, description?, append?, family?, from_key?}` o multipart con `file` + campos | 201 estilo |
| `DELETE /api/styles/{id}` | — | `{deleted: id}` / 403 si es incluido |
| `GET /api/styles/{id}/cover` | — | imagen de referencia o portada |
| `GET /api/events` | — | SSE: evento `changed` con `{generations, library, styles}` (mtimes) cuando cambia alguno. Se comprueba cada 1 s, con un comentario de keep-alive cada 15 s |

**`ImageItem`** = `{key, gen_id, index, conversation_id, prompt (display_prompt), style, style_name, model, ts, width, height, favorite, collections: [ids], thumb_url, image_url, ref: "gen:<gen>#<i>"}`.

## 11. Frontend (`web/`)

- **Origen:** `docs/design/musa-v6/galeria-musa-v6.html`, separado en `index.html` + `app.css` + `app.js`, con los assets copiados a `web/assets/`.
- **Se conservan:** los tokens de color (tema crema, oklch), las tipografías, los espacios, las animaciones y la pantalla de carga.
- **Se elimina del diseño:** los datos de ejemplo (`ART`, `STYLES`, `COLLS`, las generaciones simuladas), los créditos, el plan, la cola, el compositor, GPT, Inicio y Límites.
- **Datos:** `app.js` los pide a la API y se suscribe a `/api/events`; al recibir `changed` vuelve a pedir la vista actual.
- **Insignias de modelo:** `gemini-flash` → «Gemini Flash», `gemini-pro` → «Gemini Pro», el resto tal cual.
- **Navegación:** se guarda en el hash (`#galeria`, `#estilos`, `#colecciones/<id>`, `#imagen/<gen>/<i>`), así que recargar mantiene la vista.
- **Panel de detalle:**
  - imagen grande, prompt, estilo, modelo, fecha y `gen:<id>#i`;
  - **Colecciones:** casillas y «nueva colección…»;
  - **Guardar como estilo:** abre un diálogo con nombre, descripción y texto, prerrellenado con el prompt;
  - **Copiar referencia:** copia `gen:<id>#i` al portapapeles;
  - **Descargar** y **Abrir en Finder**;
  - **Favorita:** conmutador;
  - **Borrar:** pide confirmación.
- **Nuevo estilo:** diálogo con 3 pestañas: «Desde texto», «Desde la galería» (selector de imagen) y «Subir imagen».
- **Estados vacíos:** galería sin imágenes («Genera tu primera imagen con tu agente o `gia gen`»), colección vacía, sin resultados de búsqueda.
- **Accesibilidad:**
  - el texto alternativo de cada imagen es su prompt;
  - foco visible y navegación con teclado: Esc cierra el panel, ←/→ pasa de imagen en el panel;
  - `prefers-reduced-motion` desactiva las animaciones;
  - contraste AA en el texto.
- **Textos de la interfaz en español.**
- **Licencias:**
  - Instrument Sans: SIL OFL 1.1 (`web/assets/fonts/OFL.txt` con el texto oficial);
  - las obras y figuras son de dominio público, con créditos visibles como en el diseño.

## 12. Configuración nueva

| Variable | Por defecto | Uso |
|---|---|---|
| `GIA_APP_PORT` | `8765` | puerto de la app |
| `GIA_APP_WITH_MCP` | `false` | si es true, `gia-mcp` levanta la app en el mismo proceso |

## 13. Pruebas

**Unitarias y de API** (sin red; `starlette.testclient.TestClient`):
- `LibraryStore`:
  - crear / renombrar / borrar colección;
  - nombre duplicado → `invalid_collection`;
  - `add`/`remove` idempotentes;
  - favoritas y borradas;
  - archivo corrupto → vacío + renombrado;
  - escritura atómica.
- `UserStyleStore`:
  - crear desde texto, desde `from_key` y por subida;
  - subida que no es imagen → `invalid_style`/422;
  - id único con sufijo;
  - borrar un estilo incluido → error;
  - el catálogo une incluidos + propios.
- `display_prompt`: con `user_prompt`, y con un registro antiguo con prefijo.
- `ImageService`:
  - la imagen del estilo va primera, como ref de confianza, y respeta `max_refs`;
  - `collection=` crea la colección y añade la imagen;
  - `list_generations` omite las borradas;
  - `gen:` de una imagen borrada → `invalid_ref`.
- Seguridad HTTP:
  - sin clave → 401;
  - `Host` ajeno → 403;
  - `POST` sin `Origin` válido → 403;
  - `/api/image` de un registro con ruta fuera de `output_root` → 404;
  - nunca se sirve una ruta del cliente.
- Endpoints de §10:
  - forma de `ImageItem`, filtros y paginación;
  - borrar quita el archivo y la miniatura;
  - favoritas, colecciones y estilos (los 3 modos).
- SSE: tras `append` en `generations.jsonl` llega un evento `changed` (con el intervalo de comprobación parcheado a 0,05 s).
- MCP: `list_collections`, `add_to_collection` y `generate_image(collection=)` con un backend falso.
- `gia app`: con el puerto ocupado y un `app.json` vivo no arranca otro.
- uvicorn con `GIA_APP_WITH_MCP` no escribe en stdout.

**Verificación real** (controller, con la cuenta del usuario; unas 2 imágenes):
1. `gia app` con las imágenes reales: captura de Galería, Estilos, Colecciones y panel de detalle; recorrido con teclado.
2. Crear una colección y una favorita desde la app; crear un estilo «desde la galería».
3. Desde el MCP: `generate_image(prompt, style=<estilo propio>, collection=<la creada>)`. La imagen aparece sola en la galería y en la colección, sin recargar, y la ref del estilo se envía (`refs[0].source == "style:<id>"` en el historial).
4. Borrar una imagen desde la app: desaparece de la galería y `gen:` de esa imagen da `invalid_ref`.

## 14. Fuera de alcance (partes 2 y 3, o nunca)

- **Parte 2:** Inicio con compositor (prompt, estilo, aspecto, variaciones, modelo, «guardar en»), cola visible de pendientes, editar y variaciones desde el detalle.
- **Parte 3:** Límites de uso (cuota real 5 h/semanal, imágenes por día, almacenamiento real, tabla de trabajos).
- **Nunca, por la plataforma:** GPT Image, créditos de pago, generación en paralelo y acceso remoto o multiusuario.
