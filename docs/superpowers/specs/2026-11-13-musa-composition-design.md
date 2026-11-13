# Musa · Composición por capas (spec 2 de 3)

Fecha: 2026-11-13 · Estado: v2 tras evaluación · Base: `main` @ 390acb1

Depende del spec 1 (`2026-11-13-musa-sprites-design.md`). De allí usa:
- la infraestructura común §P: `display_prompt`, `submit_many`, `meta`, `on_finish`, la regla «editar si es la cabeza; si no, referencia», la `rev` monotónica, el borrado y los números finales;
- las funciones de imagen `soft_key`, `estimate_key` y `alpha_bbox`.

El spec 3 (`2026-11-13-musa-animated-composition-design.md`) amplía este.

## 0. Objetivo y decisiones del usuario
El objetivo es separar una escena en **piezas, cada una en su propia imagen**: un fondo más un personaje y varios objetos. Al apilar las imágenes se obtiene la composición pedida.

Ejemplo del usuario: «Hermes (robot) en un espacio lleno de tiendas que llenan un tarro de monedas». Las capas serían: fondo con tiendas, Hermes, tarro y monedas.

Lo que decidió el usuario:
- **El usuario escribe la lista de piezas**, una por línea.
- Superficies: Musa + MCP.
- Exportación estática: capas PNG + `scene.json` + `composite.png`. La animación y el WebP llegan en el spec 3.

## 1. Evidencia (pruebas reales, 2026-11-13, 3 imágenes, gemini-flash, 1200×896)
Se hicieron 3 pasos:
1. La imagen completa, generada con el prompt del usuario.
2. El fondo, como **edición en la misma conversación**: «quita Hermes, tarro, monedas y brazos; deja todo igual».
3. La pieza, como **generación nueva con la imagen completa de referencia**: «solo Hermes, misma posición y tamaño, fondo verde».

| Capa | Desplazamiento | Diferencia media | Problema |
|---|---|---|---|
| Fondo | 0, 0 px (búsqueda ±12) | 9,7 | redibujo leve |
| Hermes (píxeles opacos) | 0, 0 px; a 1 px la diferencia sube de 6,7 a 15 | 6,8 (mediana 3) | **incluyó el tarro y las monedas** |

Al montar fondo + Hermes, solo el 1,2 % de los píxeles difiere en más de 60 respecto a la imagen completa.

Conclusiones, con 2 muestras, 1 escena y 1 modelo:
- Las capas salen **probablemente** alineadas. Por eso se apilan a lienzo completo, con una comprobación automática de desplazamiento. No se corrige la escala.
- Gemini no siempre aísla solo lo que se pide, así que cada capa se puede regenerar o corregir.
- Las piezas entrelazadas se pierden. Por eso el prompt de la imagen completa pide las piezas separadas.

## 2. Conceptos
- **Composición**: un registro en `<state_dir>/compositions.json`.
- **Imagen completa** (*master*): fija las posiciones y los tamaños.
- **Fondo**: capa inferior obligatoria, que no se mueve ni se oculta.
- **Pieza**: imagen recortada con `soft_key` y apilada a lienzo completo.
- Todas las imágenes generadas son imágenes normales de la galería. Se guardan en la colección **«Composición · {nombre}»**; si ese nombre ya existe, se usa «Composición · {nombre} (2)», «(3)», y así sucesivamente.
- El prompt que muestra la galería es `{nombre} · {capa}` (§P1). Para la imagen completa es `{nombre} · imagen completa`.

## 3. Datos (`src/gemini_image_api/compositions.py`)
`CompositionStore` sigue el mismo patrón que `AnimationStore`: bloqueo `compositions.lock`, escritura atómica y archivo corrupto renombrado.

El formato es `{"version": 1, "compositions": [composición]}`, con un máximo de 100 composiciones. Pasar de 100 da `invalid_composition`.

Los artefactos de cada composición viven en `<state_dir>/compositions/<cmp_id>/` (0700).

**Composición:**

| Campo | Notas |
|---|---|
| `id` | `cmp_<8hex>` |
| `name` | 1–80 caracteres, sin espacios al inicio ni al final; si no se da, los 40 primeros caracteres del prompt |
| `prompt` | 1–4000 caracteres |
| `pieces` | `[{"id": "pc_<6hex>", "name"}]`. De 1 a 6 piezas. Nombre de 1 a 80 caracteres. Sin duplicados, sin distinguir mayúsculas. |
| `ratio` | una de `ASPECT_HINTS`; por defecto `4:3` |
| `model` | id o `null` (auto) |
| `style` | id del catálogo o `null` |
| `refs` | Hasta 3 referencias, solo para la imagen completa. En Musa, con las formas de `POST /api/jobs` (`{"gen"}`/`{"upload"}`). En el MCP, rutas permitidas, URLs https o `gen:`; nunca uploads. La referencia del estilo cuenta aparte, dentro del límite `settings.max_refs` (10), que no se puede superar. |
| `collection` | `col_<8hex>` o `null` (si el usuario la borró) |
| `origin` | `musa` \| `mcp` |
| `owner_pid` | pid del proceso que lanzó los trabajos en curso |
| `master` | `{"image", "conversation_id", "status", "job", "error", "width", "height"}`. `status` es `queued` \| `running` \| `done` \| `error`. `image` es la clave de galería sin `gen:`. |
| `layers` | lista ordenada de abajo arriba; el índice 0 es el fondo |
| `rev` | §P4; sube con cada cambio visible (orden, visibilidad, imagen o recorte de una capa, desplazamiento). No sube al cambiar el nombre. |
| `created`, `updated` | ISO |

**Capa:**

| Campo | Notas |
|---|---|
| `id` | `ly_<6hex>` |
| `role` | `background` \| `piece` |
| `piece` | id de la pieza, o `null` para el fondo |
| `name` | nombre de la pieza, o «Fondo» |
| `image`, `conversation_id` | la imagen actual de la capa |
| `status` | `pending` \| `queued` \| `running` \| `done` \| `error`. `error` solo cuando la capa **nunca** tuvo imagen. Si una regeneración, corrección, `frames` o `states` (spec 3) falla y la capa ya tenía `file`, vuelve a `done` y el fallo queda en `error` como aviso. Mientras `queued`/`running`, una capa con `file` sigue mostrando su imagen. |
| `job` | id del trabajo en curso, o `null` |
| `error` | `{code, message}` o `null` |
| `visible` | bool; el fondo siempre `true` |
| `key_color` | `green` \| `magenta` para las piezas; `null` para el fondo. Es el color con el que se **generó** la imagen de la pieza: solo cambia al regenerar (§5.4). `{bg}` en los prompts = `chroma green #00FF00` o `magenta #FF00FF` (spec 1 §5). |
| `tolerance` | 5–80; por defecto 25 |
| `bbox` | `[x0,y0,x1,y1]` del contenido **antes** de aplicar `offset`, en píxeles de la imagen completa; en el fondo, el lienzo entero |
| `offset` | `[dx, dy]`; la posición final es `bbox + offset` |
| `rev` | §P4; cambia cuando cambian la imagen, el recorte o el desplazamiento |
| `file` | nombre del PNG procesado dentro del directorio de la composición, o `null` |

**Códigos de `layer.error`.** No son errores de API y no cuentan en `errors.py`:
- cualquier código del trabajo (`usage_limit`, `content_blocked`, …);
- `queue_full`, si la cola estaba llena al encolar los trabajos de seguimiento;
- `cancelled`, si el trabajo se canceló;
- `interrupted`, si Musa o el MCP se reiniciaron;
- `invalid_image`, si la proporción no coincide o la imagen salió vacía;
- `invalid_ref`, si la imagen completa se borró.

La interfaz traduce estos códigos con `ERR_ES`; si un código no está allí, muestra el `message`.

**Recuperación.**
- Al crear el gestor (al arrancar Musa o el servidor MCP), toda capa o imagen completa que esté en `queued` o `running` y cuyo `owner_pid` no esté vivo se cierra con `interrupted` siguiendo la regla de fallo de §5.3 (vuelve a `done` con aviso si tenía `file`; si no, `error`). «No está vivo» significa que `os.kill(pid, 0)` lanza `ProcessLookupError`.
- Además, Musa marca como `interrupted` las entradas con `origin = musa`, su propio `owner_pid` y un `job` que no existe en su cola.

## 4. Prompts (`compositions.py`)
**Imagen completa:**
```
{prompt}

Compose the scene so that each of these elements is clearly separate, fully visible, and does not overlap or touch the others: {p1}; {p2}; ….
```
Se envía con `style`, `refs`, `ratio` y `model` de la composición.

**Fondo:**
- Si la imagen completa es la cabeza de su conversación, se **edita** esa conversación (esto pasa siempre la primera vez):
  ```
  Remove {p1}, {p2}, … completely. Keep everything else exactly identical: same framing, same positions and sizes of the remaining elements, same style and line work. Fill the removed areas with the background that would be behind them. Keep the same image size.
  ```
- Si no es la cabeza (por ejemplo, al regenerar), se hace una **generación** con `refs=[gen:master]` y este prompt:
  ```
  From the reference image, remove {p1}, {p2}, … completely. Keep everything else exactly identical: same framing, same positions and sizes of the remaining elements, same style and line work. Fill the removed areas with the background that would be behind them. Same image size and framing as the reference.
  ```

**Pieza** (siempre una generación nueva, con `refs=[gen:master]`):
```
Isolate only {pieza} from the reference image. Keep it exactly the same: same design, same size, same position in the frame, same style and line work. Remove everything else, including {otras piezas}, and replace it with solid flat {bg}. No shadows, no floor, no other objects. Keep the same framing and image size as the reference.
```
Con una sola pieza se omite «, including …».

**Corrección de una capa:** se aplica la regla §P3 sobre la imagen de la capa.
- *Edición:* `{texto}` + sufijo.
  - Sufijo para piezas: ` Keep the solid flat {bg} background and the same position, size and framing.`
  - Sufijo para el fondo: ` Keep the same framing and the same positions of everything else.`
- *Generación con referencia:* `Same image as the reference, with this change: {texto}.` + el mismo sufijo.

**Parámetros de los trabajos.** Estos trabajos (fondo, piezas, correcciones y regeneraciones) usan:
- `model` = el de la composición;
- `ratio` = el de la composición;
- `style` = `null`;
- `collection` = la de la composición, o `null` si se borró;
- `display_prompt` (§P1);
- `meta = {"composition": id, "target": "master" | layer_id, "action": "create" | "regenerate" | "correct"}`.

## 5. Flujo (`CompositionManager` en `compositions.py`)
`CompositionManager(service, store, runner)` es el dueño de la lógica. `runner` es una de dos implementaciones con la misma interfaz (`submit(spec) -> job_id` y `submit_many(specs) -> [job_id]`; los `spec` son los argumentos de `JobQueue.submit`, incluidos `display_prompt`, `meta` y `animation`):
- `QueueRunner(jobs)` (Musa): delega en `jobs.submit` y `jobs.submit_many`. Webapp crea el gestor en `create_app` y engancha `jobs.on_finish = manager.on_job_finished` (§P2).
- `DirectRunner(service)` (MCP): ejecuta cada spec en el acto con `await service.generate(...)` o `await service.edit(...)` (`edit_of` → conversación, como `JobQueue._validate`). Construye un `Job` con estado final `done`/`error` igual que `_process` y llama a `await manager.on_job_finished(job)` antes de devolver. Los specs de `submit_many` se ejecutan uno tras otro.

1. **Crear** (`create(...)`):
   - valida los datos;
   - solo con `QueueRunner`: comprueba que caben `2 + n_piezas` trabajos en la cola (`pending + 2 + n > MAX_PENDING` → `queue_full`) sin crear nada (`DirectRunner` no tiene cola y omite esta comprobación);
   - crea la colección;
   - guarda la composición con todas las capas en `pending`;
   - lanza la imagen completa con `runner.submit(spec)` (spec con `meta=…`).
2. **Imagen completa terminada** (gancho):
   - guarda `master.image`, la conversación y `width`/`height`;
   - encola con `submit_many` el fondo y una pieza por capa, y las capas pasan a `queued`;
   - si `submit_many` da `queue_full`, todas esas capas pasan a `error` con `queue_full` (se pueden reintentar una a una con «Regenerar»);
   - si la imagen completa falló, las capas siguen en `pending` y la composición muestra el error con «Reintentar».
3. **Capa terminada:**
   - procesa la imagen en un hilo (§6), y la capa pasa a `done` con `file`, `bbox`, `offset` y `rev`;
   - si el trabajo falló o se canceló: si la capa tenía `file`, vuelve a `done` con `error` = aviso; si no, pasa a `error`.
4. **Regenerar una capa:**
   - pieza: la misma generación con referencia de §4;
   - fondo: §P3 sobre la imagen completa.

   Requiere que la imagen completa exista en el índice; si no, da `invalid_composition` («la imagen completa se borró; crea la composición de nuevo»). Gasta 1 imagen.
5. **Corregir una capa:** §P3 sobre la imagen de la capa, con el texto del usuario. Requiere que la capa tenga `file`, que no esté ocupada y que su imagen exista en el índice. Gasta 1 imagen.
6. **Reintentar la imagen completa:** solo si está en `error`. Encola la imagen completa otra vez.
   - Las referencias de tipo *upload* caducan a las 24 h; en ese caso el reintento falla con `unknown_upload` y el mensaje lo indica.
   - Las capas siguen en `pending`.
7. Una capa solo admite un trabajo a la vez. Pedir regenerar, corregir, `frames` o `states` mientras está en `queued` o `running` da `invalid_composition`. Regenerar admite `{"key_color"}` opcional: la pieza se genera de nuevo con ese color de fondo, y el diálogo de confirmación lo dice («Gasta 1 imagen»).

**Imágenes borradas.**
- Las capas `done` siguen mostrando su PNG procesado (`file`), que es un artefacto propio y no una caché.
- Las acciones que necesitan la imagen fuente dan `invalid_composition` con un mensaje claro:
  - cambiar la tolerancia (vuelve a recortar);
  - regenerar o corregir.

**MCP** (§7): el mismo flujo con `DirectRunner`, `origin = "mcp"` y `owner_pid = os.getpid()`.
- Si un trabajo termina con un código de `QUEUE_STOP_CODES`, el gestor no lanza más trabajos de esa llamada. Lo que falta pasa a `error` con ese código y el resultado es `partial`. Con cualquier otro error en una capa se sigue con las demás, y el resultado también es `partial`.
- Las llamadas a Gemini de Musa y del MCP ya se serializan entre procesos (el `_flock` del cliente).

## 6. Procesado (`src/gemini_image_api/layers.py`)
Funciones públicas (el spec 3 las reutiliza):
- `normalize_size(img, size) -> Image`:
  - si `img.size == size`, devuelve la imagen tal cual;
  - si la proporción difiere < 2 %, escala con LANCZOS;
  - si no, lanza `GIAError("invalid_image", "la capa no tiene la proporción de la imagen completa")`; es el mismo código de texto que ya usa la web, mapeado a 422.
- `prepare_piece(img, key_color, tolerance) -> (rgba_full, bbox)`:
  - aplica `soft_key` sobre la imagen ya normalizada y calcula `alpha_bbox`;
  - si `bbox` es `None`, lanza «la pieza salió vacía» (`invalid_image`).
- `search_offset(master, rgba_full, bbox, radius=12) -> (dx, dy)`:
  - trabaja en gris, solo con los píxeles de alfa > 200 dentro de `bbox`;
  - mide con `ImageChops.difference` + máscara + `ImageStat`;
  - primero busca a mitad de resolución en ±radius y después refina ±1 a resolución completa;
  - se queda con (0,0) salvo que otra posición mejore la diferencia media en más de 1,0.

  No corrige la escala. El fondo no pasa por `search_offset`: su `offset` es siempre `(0,0)`.
- `layer_png(rgba_full, bbox, offset) -> Image`: recorta a `bbox`. La posición final la da `bbox + offset` en los datos, no el PNG.
- `composite(size, layers) -> Image`: fondo RGB a lienzo completo + piezas visibles en orden, pegadas en `bbox + offset`.

**Artefactos** en `<state_dir>/compositions/<cmp>/`:
- `<layer_id>_<rev>.png` (fondo RGB a lienzo completo; piezas RGBA recortadas);
- `preview_<rev>.png` (la composición plana, regenerada si falta).

Al escribir una `rev` nueva se borra la anterior. Borrar la composición borra el directorio.

## 7. API
Errores nuevos (27 → 29). Ninguno es reintentable:
- `INVALID_COMPOSITION = "invalid_composition"`. Next action: «Check the composition data (name ≤80, prompt ≤4000, 1–6 unique pieces, ≤3 refs, layer order with the background first) and that the layer is not busy and its images still exist.»
- `UNKNOWN_COMPOSITION = "unknown_composition"`. Next action: «List compositions with list_compositions (MCP) or GET /api/compositions.»

**Web.** Todas las rutas van tras la clave y las mutaciones exigen `Origin`. Toda URL de PNG lleva `?rev=`, y la respuesta lleva `Cache-Control: private, max-age=3600`.

| Método y ruta | Cuerpo / respuesta |
|---|---|
| `GET /api/compositions` | `{"items": [{id, name, rev, progress: {done, total, errors}, cover_url, created, updated}]}`, del más reciente al más antiguo. `cover_url` es `preview.png` si hay al menos una capa `done`; si no, la miniatura de la imagen completa; si no, `null`. |
| `POST /api/compositions` | `{name?, prompt, pieces: [str], ratio?, model?, style?, refs?}` → 201, composición. Errores: 400 `invalid_composition`, 429 `queue_full`, 404 `unknown_model`/`unknown_style`. |
| `GET /api/compositions/{id}` | composición + `width`, `height` + por capa `image_url` (PNG procesado con `?rev=`) y `source_url` (`/api/image/…` si existe) + `master_url` |
| `PATCH /api/compositions/{id}` | `{name?, order?: [layer ids]}`; `order` debe tener las mismas capas, con el fondo primero |
| `DELETE /api/compositions/{id}` | borra el registro y su directorio; las imágenes y la colección se quedan |
| `POST /api/compositions/{id}/retry` | reintenta la imagen completa (§5.6) |
| `PATCH /api/compositions/{id}/layers/{lid}` | `{visible?, tolerance?, offset?: [dx,dy]}`; `offset` admite enteros entre −200 y 200; cambiar `tolerance` vuelve a recortar desde la imagen fuente |
| `POST /api/compositions/{id}/layers/{lid}/regenerate` | `{key_color?}` → 202 `{job}` |
| `POST /api/compositions/{id}/layers/{lid}/correct` | `{prompt}` (1–2000 caracteres) → 202 `{job}` |
| `GET /api/compositions/{id}/layers/{lid}.png` | PNG procesado |
| `GET /api/compositions/{id}/preview.png` | vista plana |
| `GET /api/compositions/{id}/export.zip` | `background.png`, `<slug>.png` por cada pieza, `composite.png` y `scene.json` |

Notas sobre el zip:
- `slug` = `slugify(nombre, 30)`; si dos coinciden, se añade `-2`, `-3`.
- Solo incluye las capas `done`.
- Se construye en `tmp_downloads` y se envía con `_TempFileResponse`, como `zip_images`.

`scene.json` v1:
```json
{"version": 1, "name": "…", "width": 1200, "height": 896,
 "layers": [{"id": "ly_…", "name": "Fondo", "role": "background", "visible": true, "file": "background.png", "x": 0, "y": 0, "w": 1200, "h": 896},
            {"id": "ly_…", "name": "Hermes", "role": "piece", "visible": true, "file": "hermes.png", "x": 503, "y": 190, "w": 215, "h": 539}]}
```
En cada capa, `x, y, w, h` = `bbox + offset` (tamaño del PNG).

**MCP** (+4 tools; `INSTRUCTIONS` gana el párrafo «Compositions»):
- `generate_composition(prompt, pieces, name=None, refs=None, style=None, aspect_hint=None, model=None)` (refs: rutas permitidas, URLs https o `gen:`):
  - sigue el flujo secuencial de §5;
  - devuelve `{"status": "ok"|"partial", "composition": composición, "quota"}`.
- `list_compositions()` devuelve `{"status": "ok", "compositions": [resumen + layers: [{id, name, role, status}]]}`.
- `get_composition(composition)` devuelve `{"status": "ok", "composition"}`.
- `export_composition(composition, copy_to)`:
  - escribe el zip en el directorio `copy_to` (pasa por `check_copy_dest`) con `copy_file`;
  - devuelve `{"status": "ok", "zip"}`.

## 8. Interfaz de Musa
**Navegación.**
- Pantalla nueva `#screen-composiciones`, con las rutas `#composiciones` y `#composiciones/<id>`.
- Se actualizan:
  - `parseRoute` (`app.js:172`) y `render` (`app.js:184`);
  - la lista de pantallas guardadas en `musa-screen`;
  - un botón en la barra lateral, `data-nav="composiciones"`, con el icono nuevo `i-compose`;
  - la paleta ⌘K: pantalla «Composiciones» y acción «Nueva composición».

**Lista.** Tarjetas con:
- la portada (`cover_url`);
- el nombre;
- el estado «k/n capas listas», o «con errores»;
- el botón «Nueva composición».

**Diálogo `#comp-dialog`** (600 px, patrón del diálogo de estilo):
- Campos:
  - Nombre;
  - Escena (textarea);
  - Piezas (textarea, una por línea), con la ayuda «Cada pieza sale en su propia capa. El fondo se crea solo. Elige piezas que no se toquen entre sí.»;
  - Proporción, Modelo y Estilo (los mismos selectores de datos que el compositor);
  - la casilla «Usar las referencias del compositor (N)».
- La nota «Gastará {2+n} imágenes de tu cuota.» se actualiza en vivo.
- Al enviar, se pide confirmación con `confirmDlg` mostrando ese número.

**Editor.**
- **Escenario** con `aspect-ratio: W/H`:
  - las capas visibles se apilan como `<img>` posicionadas en % (`x/W`, `y/H`, `w/W`, `h/H`), con `image_url`;
  - un conmutador «Composición / Original» muestra la imagen completa.
- **Panel de capas**, de arriba abajo, con el fondo al final. En cada capa:
  - el ojo (`aria-pressed`; el fondo no lo tiene);
  - miniatura, nombre y estado: En espera / En cola / Generando / Lista / Error + mensaje;
  - ↑/↓ (el fondo no se mueve);
  - «Regenerar», con confirmación de que gasta 1 imagen;
  - «Corregir…», que abre `#layer-edit-dialog` (textarea + nota de que gasta 1 imagen);
  - «Ajustes», un `<details>` con tolerancia, `dx`/`dy`, «Restablecer a 0» y «Regenerar con fondo magenta/verde» (gasta 1 imagen, con confirmación).
- **Barra superior:**
  - nombre editable, que se guarda al salir del campo;
  - «Reintentar», solo si la imagen completa está en `error`;
  - «Exportar (.zip)»;
  - «Borrar composición», con confirmación que aclara que las imágenes se quedan en la galería.
- Se refresca por SSE, con `compositions` en `changed`.

**`ERR_ES`:**
- `invalid_composition` → «Los datos de la composición no son válidos.»
- `unknown_composition` → «La composición ya no existe.»
- `interrupted` → «Musa se reinició antes de terminar.»
- `cancelled` → «Cancelada.»

## 9. Pruebas
- **Imágenes de prueba sintéticas** (`tests/sprite_synth.composition_set()`, en memoria; sin binarios en `tests/`): imagen completa, placa y pieza sobre verde con su caja exacta.
  - `prepare_piece`: la `bbox` queda a ±3 px de la caja conocida.
  - `search_offset`: da `(0,0)`; con la pieza desplazada 5 px da `(5,0)` ±1.
- **Imágenes reales** (fuera de la suite, en la verificación de la fase): con `~/Pictures/gemini-image-api/composition-probe/`, la `bbox` de Hermes queda a ±6 px de `(503,190,718,729)` y `search_offset` da `(0,0)`.
- **Store y gestor:**
  - validaciones: 0 o 7 piezas, piezas duplicadas, nombres demasiado largos, más de 3 refs;
  - nombre de la colección con sufijo cuando ya existe;
  - prompts con todas las piezas;
  - recuperación con un `owner_pid` muerto, que da `interrupted`.
- **Web con FakeBackend:**
  - crear: la cola recibe la imagen completa y, al terminar, el fondo como edición (`edit_of`) y n piezas con `refs` de la imagen completa;
  - las capas terminan en `done` con su `file`;
  - `queue_full` al crear no crea nada; `queue_full` en el seguimiento deja las capas en `error`;
  - regenerar el fondo cuando ya no es la cabeza da una generación con `refs`;
  - corregir: edición si la capa es la cabeza, generación con `refs` si no;
  - una capa ocupada da 400;
  - PATCH de orden, visibilidad y desplazamiento;
  - el PNG de capa y el zip, con un `scene.json` coherente;
  - DELETE deja las imágenes;
  - si la imagen completa se borró, regenerar da 400.
- **MCP:** `generate_composition` en secuencia con una parada a mitad (`partial`), `list_compositions`, `get_composition` y `export_composition`; la cuenta de tools pasa a 36.
- **Navegador** (script headless): crear desde el diálogo, ver las capas apiladas, ocultar una capa y exportar.
