# Musa parte 2 — Diseño completo: Inicio, compositor y cola, Límites de uso

Fecha: 2026-11-10 · Estado: borrador para aprobación
Base: Musa parte 1 en `main` (`fbbb193`). Fuente visual: `docs/design/musa-v6/galeria-musa-v6.html`.

## 1. Objetivo

Musa debe ser el diseño v6 **tal cual**: las mismas pantallas, bloques, textos y animaciones.

Hay una sola regla de fondo: **nada ficticio**. Cada elemento del diseño que en el mock era inventado (créditos, plan, modelos de OpenAI, progreso simulado) se conecta a un dato real o se adapta. En ningún caso queda un botón que no hace nada.

Decisiones del usuario (2026-11-10):

| Tema | Decisión |
|---|---|
| Alcance | Diseño completo y funcional ahora. Esto adelanta las antiguas partes 2 y 3. |
| Créditos / plan / Límites | El mismo diseño, con la cuota real de Gemini. |
| Modelos | Solo los reales: Auto, Gemini Flash y Gemini Pro. |
| Variaciones (− N +) | N generaciones en cola; cada una gasta cuota. |
| Pantalla de carga | Siempre al abrir. Se puede volver a ver desde el logo y desde «Pantalla de carga». |
| Nombre en la esquina | Ninguno: se quita el chip de usuario «Mi estudio». |

## 2. Qué cambia respecto a la parte 1

| Elemento del diseño | Parte 1 | Parte 2 |
|---|---|---|
| Pantalla de carga | 1 vez por sesión | Siempre al abrir. El logo y «Pantalla de carga» la repiten. |
| Barra lateral | Galería, Estilos, Colecciones | Orden del diseño: Inicio, Estilos, Galería, Colecciones │ Límites de uso, Pantalla de carga, tarjeta de plan |
| Cabecera | Migas + búsqueda | + píldora de cuota (lleva a Límites). Sin chip de usuario. |
| Inicio | — | Banner de Seurat con crédito; «Empieza con un estilo» (4 estilos); «Recientes» (pendientes + últimas); panel «Pendientes» |
| Compositor fijo | — | En todas las pantallas: referencias, estilo, proporción, variaciones, modelo, enviar, bandeja de cola, «Guardar en» |
| Estilos | Clic en un estilo → filtra la galería | Como el diseño: clic → selecciona el estilo para el compositor (✓ y toast). Se mantienen «Mis estilos» y «Nuevo estilo». |
| Galería | Masonry | + tarjetas de pendientes al principio (filtro «Todas», sin búsqueda) + chip «Colección: X» cuando hay filtro de colección |
| Colecciones, detalle | Cabecera + rejilla | Diseño completo: banner con la portada, pestañas Imágenes/Actividad, panel lateral con «Creada …», nº de imágenes, nº de estilos, última actualización, «Añadir imágenes», «Volver» |
| Detalle de imagen | Acciones de la parte 1 | + «Reutilizar prompt» (con la casilla «también estilo, proporción y modelo») + «Usar como referencia» + «Listo» |
| Límites de uso | — | Pantalla completa con datos reales (§5) |

Se mantiene todo lo que añadió la parte 1 y no está en el diseño, porque es funcionalidad real: favoritas, borrar, Finder, copiar referencia, guardar como estilo y «Mis estilos».

## 3. Mapeo de lo ficticio a lo real

| Diseño (mock) | Real |
|---|---|
| «388 créditos» (píldora, panel lateral) | % libre de la ventana de 5 h: `100 − window_5h_pct`. Texto: «92% libre». |
| Tarjeta de plan «Plan Pro · 612 de 1.000 créditos · Renovación 1 dic» | «Plan PRO» (`tier`) · «8% usado en 5 h» · barra · «Se renueva a las 03:28» (`resets_at_5h`, hora local) |
| «4 cr» junto a Enviar y en los estilos | «N img» junto a Enviar (N = variaciones). En los estilos, la familia. |
| Modelos Gemini 3 Pro, Gemini 2.5 Flash, GPT Image 1, GPT Image 1 mini, con multiplicadores | Auto (= `GIA_DEFAULT_MODEL`), Gemini Flash, Gemini Pro. Sin multiplicadores. |
| Restricción de proporciones de OpenAI | No aplica: Gemini acepta todas como indicación de texto |
| 10 proporciones | Las 10, como pista de texto. Se amplía `ASPECT_HINTS` con 4:5, 2:3, 5:4, 3:2 y 21:9 (§6.4). |
| «Se procesan dos a la vez» / «2 / 4 simultáneas» | **Una a la vez**: el backend serializa las llamadas a Gemini (`upstream.lock`). Texto: «Se procesan una a una». Panel: «1 a la vez · N en cola». |
| Progreso 0–100% simulado | Estado real (En cola #k / Generando · 12 s). Barra **estimada** con la mediana de `elapsed_s` de las últimas 20 generaciones correctas (por defecto 30 s). Tope del 95% hasta que termina. El texto dice «≈». |
| Cancelar devolviendo créditos | Solo se cancelan los trabajos **en cola**. Uno en curso no se cancela, porque Gemini ya está gastando la cuota. |
| «Almacenamiento 3,1 / 5 GB · papelera 30 días» | Tamaño de `output_root` frente al espacio libre del disco (`shutil.disk_usage`). Sin texto de papelera. |
| «Imágenes por día» (datos fijos) | Últimos 7 días desde `generations.jsonl` (imágenes guardadas por día, hora local) |
| Tabla de actividad (Trabajo, Modelo, Estilo, Imágenes, Créditos, Estado) | Últimas 20 generaciones: Trabajo (prompt), Modelo, Estilo, Imágenes, **Tiempo** (`elapsed_s`), Estado (Completado / Revisar si no se guardó / Error) |
| «Servicio operativo» | `account_status` de Gemini: AVAILABLE → «Servicio operativo»; si no, el estado tal cual con estilo `warn`. Sin cookie → «Sesión caducada · ejecuta gia login». |
| «Pro · $24 / mes · Cambiar plan» | «Plan PRO» + uso semanal (`weekly_pct`, se renueva `resets_at_weekly`). Sin botón: no hay nada real que cambiar desde aquí. |
| Colección: «Pública», «Creada abr 2026», actividad inventada | Sin «Pública». «Creada <fecha real>». Actividad real (§6.3). |
| Toasts «−4 créditos» / «créditos devueltos» | «Enviado a Gemini Flash · N en cola» / «Cancelada» |

## 4. Generar desde la app

### 4.1 Cola (`src/gemini_image_api/jobs.py`, nuevo)

- `JobQueue(service, library)` vive en el proceso de la app: `gia app`, o `gia-mcp` con `GIA_APP_WITH_MCP=true`. Un solo worker `asyncio`. Como mucho **20 trabajos** pendientes, contando en cola y en curso; por encima → 429 `queue_full`.
- `Job = {id:"j_"+8hex, prompt, style, model, ratio, refs:[…], collection, status: queued|running|done|error, created, started, finished, record_id, images:[keys], error:{code,message,next_action}}`.
- **Un trabajo = una llamada** a `ImageService.generate(...)`. Variaciones N → N trabajos con el mismo `batch` id, que el frontend agrupa en una sola fila («4 img»).
- El worker usa el `ImageService` que ya existe. Por tanto aplican `quota_guard`, el reintento de auth y el registro en `generations.jsonl`.
- Si un trabajo termina en error, los siguientes del mismo lote siguen (cada uno es una generación independiente). La excepción son los errores que afectan a toda la cola: `usage_limit`, `quota_guard`, `temporarily_blocked`, `auth_expired` y `auth_failed`. Con cualquiera de ellos, todos los que quedan en cola se marcan como error con el mismo código, para no seguir intentando.
- Los trabajos terminados se conservan en memoria 10 minutos, para que el frontend muestre su resultado o error, y después se descartan. La cola **no persiste** si se reinicia la app: lo que estaba en cola se pierde y lo que estaba en curso queda registrado por el propio backend.
- Las generaciones lanzadas por el MCP o la CLI no pasan por la cola: aparecen en la galería al terminar, por SSE, como ya ocurre.

### 4.2 Referencias del compositor

- Como mucho **3** por generación, como en el diseño. Además, la referencia del estilo cuenta para `max_refs` (10), como ya hace el servicio.
- **Desde la galería:** `gen:<id>#<i>`, resuelta por la ruta normal.
- **Subidas** (botón +, arrastrar, pegar): `POST /api/uploads` con multipart, ≤20 MB, comprobación de magic bytes y lectura acotada.
  - Se guardan en `<state_dir>/uploads/<u_id>.<ext>` (0600) y se borran pasadas 24 h; la limpieza se hace al arrancar la app y cada hora.
  - Al generar **no** pasan por `validate_local_ref`, que rechaza a propósito cualquier ruta dentro de `state_dir`, donde están las cookies. Se tratan igual que la imagen de un estilo: `refs.validate_trusted_image(path, root)` comprueba que no sea symlink, que sea un archivo, que tras `resolve()` siga dentro de `<state_dir>/uploads`, el tamaño (≤ `max_ref_bytes`) y los magic bytes. Después el servicio la añade como `ResolvedRef(source="upload:<u_id>")`.
  - El cliente solo envía el `u_id`; nunca una ruta.

### 4.3 Endpoints nuevos

Todos con clave, Host y Origin, como en la parte 1.

| Método y ruta | Cuerpo / respuesta |
|---|---|
| `GET /api/models` | `[{id:"auto", name:"Auto"}, {id:"gemini-flash", name:"Gemini Flash"}, {id:"gemini-pro", name:"Gemini Pro"}]` |
| `POST /api/uploads` | multipart `file` → 201 `{id, thumb_url}`; 422 `invalid_image` |
| `GET /api/uploads/{id}/thumb` | Miniatura de la subida |
| `POST /api/jobs` | `{prompt, style?, model?, ratio?, n:1–8, refs:[{gen:"gen:…"}│{upload:"u_…"}] ≤3, collection?}` → 202 `{batch, jobs:[Job]}`. 400 si los datos no son válidos (prompt vacío, ratio, modelo, n); 404 `unknown_style`/`unknown_collection`; 429 `queue_full`. |
| `GET /api/jobs` | `[Job]` (en cola, en curso y terminados de los últimos 10 min) + `eta_s` (mediana) |
| `DELETE /api/jobs/{id}` | Cancela uno en cola → 200; uno en curso → 409 `job_running`; uno inexistente → 404 |
| `GET /api/quota` | `{tier, window_5h_pct, weekly_pct, resets_at_5h, resets_at_weekly, account_status, cookie_present}`. Caché de 60 s; `?refresh=1` la fuerza. Se refresca sola tras cada trabajo, con la `quota` que devuelve la generación. |
| `GET /api/usage` | `{per_day:[{date, images}×7], activity:[…20], storage:{output_bytes, disk_free, disk_total}}` |
| SSE `/api/events` | Además de `changed`, evento `jobs` con la lista cuando cambia el estado de la cola |

Los códigos nuevos son **solo web** y van en `WEB_NEXT_ACTION`: `queue_full`, `job_running`, `unknown_job`, `unknown_upload`. Los 22 códigos del agente no cambian.

## 5. Límites de uso (pantalla)

La estructura del diseño se mantiene, con datos reales:

1. **Cabecera:** «Consumo de tu cuenta de Gemini» + estado del servicio.
2. **Anillo:** % libre de la ventana de 5 h. Abajo: «Ventana de 5 h · se renueva a las HH:MM».
3. **Panel «Generaciones»:** «1 a la vez» + «N en cola».
4. **Panel «Almacenamiento»:** tamaño de la galería frente al disco libre, con barra.
5. **Gráfica «Imágenes por día»:** últimos 7 días, con la animación del diseño.
6. **Panel de plan:** «Plan PRO» + uso semanal con barra y su renovación.
7. **Tabla de actividad:** la definida en §3.

Si `quota` no está disponible (sin sesión o error de red), los paneles de cuota muestran «Sin datos de cuota · <next_action>» y el resto de la pantalla funciona igual.

## 6. Cambios de backend

### 6.1 `ImageService`

Sin cambios de comportamiento. La cola llama a `generate(prompt, refs, style, model, aspect_hint=ratio, collection)`.

`generate`/`edit` aceptan un parámetro nuevo, `uploads: list[Path] = ()`. Son refs de confianza: se validan con `validate_trusted_image(p, <state_dir>/uploads)` y se envían después de la referencia del estilo y antes de las refs del usuario, contando para `max_refs`. Solo la cola lo usa. El MCP y la CLI no lo exponen. La imagen del estilo pasa a usar también `validate_trusted_image` con su raíz (paquete o `<state_dir>/styles`), lo que unifica las comprobaciones.

### 6.2 Modelo «Auto»

`model=None` → `settings.default_model`. Es lo que ya hace el servicio.

### 6.3 Actividad de colecciones

`library.json` gana `activity: [{ts, collection, kind: created|added|removed|renamed, key?}]`, limitado a las 500 entradas más recientes. Lo escriben las operaciones que ya existen. Las bibliotecas sin `activity` se cargan con la lista vacía; no hace falta migración.

### 6.4 Proporciones

`ASPECT_HINTS` pasa de 5 a 10 entradas: se añaden `4:5`, `2:3`, `5:4`, `3:2` y `21:9` con su texto en inglés. Afecta a la vez al MCP (`aspect_hint`), la CLI y la app. Gemini las trata como **pista** y no las garantiza; ya era así y está documentado en el README.

### 6.5 Uso y almacenamiento

- `StateStore` gana `images_per_day(days=7)`. Usa la hora local y cuenta solo imágenes guardadas.
- `webapp` calcula el almacenamiento sumando los tamaños de las imágenes del índice (no recorre el disco entero) y llama a `shutil.disk_usage(output_root)`.

## 7. Frontend

- **Base:** se porta el resto del diseño a `web/index.html`, `app.css` y `app.js` sin perder nada de la parte 1. Se mantienen tokens, animaciones, textos y estructura `data-*` del diseño. Los datos de ejemplo (`ART`, `STYLES`, `COLLS`, `IMAGES`, `JOBS`, `USAGE`) se sustituyen por la API. `ART` solo se queda para la pantalla de carga y el banner, que son obras de dominio público incluidas en `web/assets/arte/`.
- **Rutas:** `#inicio` (pantalla por defecto), `#estilos`, `#galeria`, `#colecciones[/<id>]`, `#limites`, `#imagen/<gen>/<i>`. Se recuerda la última pantalla en `localStorage`, como en el diseño.
- **Compositor:**
  - Enter envía; Mayús+Enter hace salto de línea.
  - El prompt empieza vacío, no con el texto de ejemplo del diseño.
  - Al enviar se limpian el prompt y las referencias, y se muestra un toast.
  - «Guardar en» lista las colecciones reales.
  - El estilo seleccionado se recuerda en `localStorage`; por defecto es «Postimpresionismo», como en el diseño.
- **Pendientes:** bloque neutro, con el color de superficie del diseño y un brillo animado (sin animación con `prefers-reduced-motion`), que muestra la etiqueta del modelo, «≈NN%» o «En cola» y la barra estimada. No llevan imagen de placeholder. Al terminar, la tarjeta se sustituye por la imagen real (SSE `changed`) y aparece el toast «Listo: N imágenes de «…»». Si hay error, toast con mensaje y `next_action`, y la fila queda marcada en rojo hasta que se cierra.
- **Textos de la pantalla de carga:**
  - «Elige un estilo: postimpresionismo, ukiyo-e, barroco o neoplasticismo.»
  - «Escribe la escena y elige el modelo: Gemini Flash o Gemini Pro.» (sustituye a la mención de ChatGPT)
  - «Guarda lo que funciona en colecciones por cliente o campaña.»
- **Barra lateral:** se quitan los contadores «créditos» del diseño y se añade el de Galería y Colecciones, que ya existía.
- **Accesibilidad:**
  - Los popovers del compositor son listbox navegables y se cierran con Esc.
  - La cola se anuncia con `role=status`.
  - Se respeta `prefers-reduced-motion`.

## 8. Seguridad

Todo lo de la parte 1 sigue igual. Además:

- `POST /api/jobs` gasta cuota real. Tiene las mismas protecciones de clave, Host y Origin que el resto de mutaciones, más el límite de 20 trabajos pendientes y n ≤ 8.
- Las subidas se identifican solo por id: el cliente nunca envía una ruta. Se guardan con 0600 y se borran a las 24 h.
- `GET /api/quota` no expone cookies; solo los campos de §4.3.

## 9. Pruebas

- **Unitarias:**
  - `JobQueue`: orden, una a la vez, cancelar en cola y no en curso, vaciado de la cola ante `usage_limit`/`quota_guard`/`auth_*`, límite de 20, retención de 10 min.
  - Subidas: tamaño, magic bytes, caducidad y que solo se puedan usar por id.
  - `images_per_day` y `ASPECT_HINTS`.
  - Actividad de la biblioteca.
- **Webapp:** todos los endpoints nuevos (forma y códigos), incluido el evento SSE `jobs`. Backend falso; nunca la red.
- **Frontend:** pruebas de clic reales en el navegador headless. Un script fuera del repo monta `create_app` con un `ImageService` sobre un backend falso que escribe imágenes de verdad en un estado temporal; el producto no tiene ningún modo falso. Casos:
  1. Enviar con N=2 → 2 tarjetas pendientes → imágenes.
  2. Cancelar una en cola.
  3. Referencia desde la galería y desde una subida.
  4. Reutilizar prompt.
  5. Pantalla de carga al abrir y al pulsar «Pantalla de carga».
  6. Límites con datos.
- **Verificación real (controlador):** 1 generación desde el compositor con un estilo, N=1, sobre una **copia** del estado en `/tmp` (estado y `output_root`). Se gasta 1 imagen de cuota.

## 10. Fuera de alcance

- Generar con ChatGPT/OpenAI.
- Cancelar un trabajo en curso.
- Cola persistente entre reinicios.
- Varias generaciones simultáneas contra Gemini.
- Subproyecto 3 (vídeo y Gem de estilo).
