# Musa · Animación de sprites (spec 1 de 3)

Fecha: 2026-11-13 · Estado: v2 tras evaluación · Base: `main` @ 390acb1
Specs hermanos: `2026-11-13-musa-composition-design.md` (2) y `2026-11-13-musa-animated-composition-design.md` (3). Este spec también define la **infraestructura común** (§P) que usan los tres.

## 0. Objetivo y decisiones del usuario
Esta cuenta de Gemini no genera vídeo (spec `2026-11-10-musa-video-design.md`). Por eso Musa pide a Gemini un **sprite sheet**, es decir, una rejilla de frames dentro de una sola imagen. Después detecta las celdas, quita el fondo y reproduce el resultado como animación.
- **Caso principal:** una mascota o personaje animado para una página web, con fondo transparente.
- **Caso secundario:** escenas completas, opacas.

Lo que decidió el usuario:
- Una animación por generación. Para otra animación del mismo personaje, el usuario pasa a mano su imagen como referencia.
- La exportación es **hoja PNG normalizada + JSON**.
- En la galería, la animación se reproduce **solo al pasar el ratón o al enfocar**. En pantallas táctiles hay un botón ▶ (§7).
- Se incluye **«Convertir en animación»** para cualquier imagen existente.
- Superficies: **Musa + MCP**. La CLI no cambia.

## 1. Evidencia (pruebas reales, 2026-11-13, 3 imágenes)
| Prueba | Modelo | Imagen | Franjas blancas | Centros de hueco detectados |
|---|---|---|---|---|
| Caminar 4×2, 16:9 | flash | 1376×768 | sí, sin pedirlas (~12 px) | x≈350/688/1026, y≈384 |
| Caminar 4×2, 16:9 | gemini-pro | 1376×768 | no | x≈345/686/1031, y≈399 |
| Salto 3×3, 1:1 | flash | 1024×1024 | sí (~16 px) | x≈338/686, y≈338/686 |

Lo que fija el diseño:
- Detectar las celdas por huecos funcionó con franjas y sin ellas. Si no hay huecos, se corta en partes iguales.
- Las franjas blancas cuentan como fondo.
- No se alinea por la base, porque el salto tiene que subir y bajar. Sí se centra en horizontal.
- El recorte suave conserva el cristal semitransparente.
- Las 3 hojas se usan como fixtures de test.

## P. Infraestructura común (la usan los specs 1, 2 y 3)
**P1. Prompt mostrado frente a prompt enviado.**
- `ImageService.generate(...)` y `ImageService.edit(...)` ganan el parámetro `display_prompt: str | None = None`. Este valor se pasa al backend como `slug_source` (`display_prompt or prompt`) y es el que se guarda en `GenerationRecord.user_prompt`, que es lo que muestra la galería (`state.display_prompt`).
- `Job` gana `display_prompt: str | None = None`. `_process` lo pasa al servicio. `Job.to_json()["prompt"]` devuelve `display_prompt or prompt`.

**P2. Cola** (`jobs.py`).
- `Job` gana dos campos:
  - `animation: dict | None = None`, que se incluye en `to_json` solo si existe;
  - `meta: dict | None = None`, que se incluye en `to_json` siempre que exista.
- `submit(...)` gana `animation=None, meta=None, display_prompt=None`. `meta` se copia tal cual a cada trabajo.
- `submit_many(specs: list[dict]) -> list[Job]`:
  - cada `spec` lleva los mismos argumentos con nombre que `submit` (n=1 implícito);
  - valida todos los specs antes de crear nada;
  - comprueba la capacidad una sola vez (`pending + len(specs) > MAX_PENDING` → `queue_full`);
  - los trabajos comparten `batch`.
- Gancho de fin de trabajo:
  - `JobQueue(..., on_finish: Callable[[Job], Awaitable[None]] | None = None)` y el atributo público `on_finish`.
  - Se invoca **después** de fijar el estado final en los cuatro sitios donde un trabajo termina: éxito en `_process`, `_fail`, `cancel` y `_drain_queue` (una vez por trabajo).
  - Siempre se programa con `asyncio.get_running_loop().create_task(self._run_hook(job))`. `_run_hook` captura y registra cualquier excepción (`log.exception`), así que nunca mata al trabajador.
  - Las tareas se guardan en un `set` y `aclose()` las espera con `asyncio.gather(..., return_exceptions=True)`.
  - `on_finish` es un solo callable. El spec 1 no lo usa (la ficha la crea el servicio dentro del propio trabajo); el spec 2 lo instala en `create_app` como `ctx.compositions.on_job_finished`, solo para trabajos con `meta.composition`.

**P3. Regla única de edición: «editar si es la cabeza; si no, referencia».**
- Gemini web siempre continúa una conversación desde su **último** turno (`client.edit` usa el último registro de la conversación, `state.conversation(cid)`).
- Helper `ImageService.conversation_head(key) -> bool`. Es True si la imagen `key` pertenece al **registro cabeza** de su conversación. El registro cabeza es el último registro (por `ts`) de tipo `generate`/`edit` con estado `ok` o `generated_not_saved`; los `retry_download` cuentan como su `parent_id`. Si la clave no está en el índice, devuelve False.
- `ImageService.change_spec(target_key, change_prompt, ref_prompt, **common) -> dict` devuelve los argumentos de `JobQueue.submit`, y también los entiende el ejecutor directo del spec 2:
  - si `conversation_head(target_key)` es True, devuelve una **edición** (`edit_of="gen:<key>"`, `prompt=change_prompt`);
  - si no, devuelve una **generación nueva** con `refs=["gen:<key>"]` y `prompt=ref_prompt`;
  - `common` (`model`, `ratio`, `collection`, `display_prompt`, `meta`, `animation`) se copia tal cual.
- Los specs 2 y 3 usan esta función para todas sus correcciones y estados. El spec 1 no la usa: hoy «Editar imagen» (`edit_of`) no comprueba que la imagen sea la cabeza; si no lo es, Gemini continúa desde el último turno. Por eso `edit()` (§6) toma la ficha de la **imagen cabeza** de la conversación. Una carrera aceptada: si entre encolar y ejecutar otra edición avanza la misma conversación, la edición continúa desde el turno más nuevo. Con un solo trabajo por capa (spec 2) esto solo puede pasar si el usuario edita a mano la misma conversación a la vez.

**P4. `rev` monotónico.**
- Toda `rev` que forme parte de una URL cacheada (`?rev=`) vale `time.time_ns() // 1_000_000` en el momento del cambio. Si esa marca no supera la `rev` anterior, se usa la anterior + 1.
- Así sobrevive a borrar y volver a crear sin que el navegador use una versión vieja.

**P5. Borrado de imágenes.**
- Helper `ImageService._purge_image(e: ImageEntry)`. Borra:
  - el archivo;
  - la miniatura `thumbs/<gen>_<i>.jpg`;
  - la ficha de animación y su caché (`self.animations.remove(key)` + `anim_cache/<gen>_<i>_*.png`);
  - la marca en la biblioteca (`library.mark_deleted`).
- Lo usan los tres caminos: `ImageService.delete_image`, la acción `delete` de `ImageService.batch_images` y `webapp.delete_image`. Este último pasa a llamar a `ctx.service.delete_image(f"gen:{gen}#{i}")` en un hilo en vez de duplicar el código.
- Las fichas de imágenes ocultas por `delete_conversation` (huérfanas) nunca se sirven, porque solo se sirven claves presentes en el índice de la galería, y se borran con `AnimationStore.prune(valid_keys)` al arrancar Musa.

**P6. SSE.** `events._mtimes()` añade `ctx.service.animations.mtime()` y `ctx.service.compositions.mtime()` (spec 2). El evento `changed` añade las claves `"animations"` y `"compositions"`. El cliente (`onChanged`) trata cualquier cambio con `refresh()`, como hoy.

**P7. Números finales tras los 3 specs.**
- Códigos de error: 25 → 29 (+2 aquí, +2 en el spec 2).
- Tools MCP: 29 → 37 (+3 aquí, +4 en el spec 2, +1 en el spec 3).
- Se actualizan `tests/test_errors.py` (cuenta), `tests/test_mcp_server.py` (`len(tools)`), `INSTRUCTIONS` del MCP y el README cuando entra cada spec, con la cuenta acumulada.

## 2. Conceptos
- **Hoja**: la imagen que devuelve Gemini. Queda en la galería con la clave `g_<12hex>#<i>`; como referencia se escribe `gen:g_<12hex>#<i>`.
- **Ficha de animación**: los datos de la animación, guardados aparte de la imagen. Cambiarla no gasta cuota.
- **Hoja normalizada**: un PNG de una sola fila con frames del mismo tamaño. Es lo que Musa reproduce y lo que se exporta.
- **Modo**:
  - `sprite`: el color clave pasa a transparente y los frames se centran en horizontal.
  - `scene`: la imagen queda opaca y no se quita el fondo. «Opaco», para personajes que no se pueden recortar, es este mismo modo.
- **Color clave** (solo en `sprite`): `green` (por defecto) o `magenta`, para personajes verdes. Los personajes rosas o morados deben usar `green`; esto se indica en la ayuda de la interfaz y en el README.

## 3. Ficha (`<state_dir>/animations.json`)
`AnimationStore` vive en `src/gemini_image_api/animations.py` y copia el patrón de `StyleGemStore`:
- bloqueo `FileLock(<state_dir>/animations.lock)`;
- escritura atómica: tmp con 0o600 y después `os.replace`;
- si el archivo está corrupto, se renombra a `animations.json.corrupt-<int(time)>` y se empieza vacío.

Formato: `{"version": 1, "animations": {"<key>": ficha}}`.

| Campo | Tipo / rango | Defecto |
|---|---|---|
| `key` | `g_<12hex>#<i>` | |
| `cols`, `rows` | int 1–8; `2 ≤ cols·rows ≤ 36` | |
| `mode` | `sprite` \| `scene` | `sprite` |
| `key_color` | `green` \| `magenta` en `sprite`; `null` en `scene` (forzado) | `green` |
| `tolerance` | int 5–80 | 25 |
| `fps` | int 2–30 | 10 |
| `loop` | `loop` \| `pingpong` | `loop` |
| `cells` | `[[x0,y0,x1,y1], …]`, fila a fila, `len = cols·rows` | detectado |
| `detected_by` | `lanes` \| `uniform` | |
| `status` | `ok` \| `review` | |
| `frames` | `cols·rows` | |
| `frame_w`, `frame_h`, `pad` | int, en píxeles de la hoja normalizada | |
| `frame_boxes` | `[[x0,y0,x1,y1], …]`: caja de contenido de cada frame dentro de su frame normalizado (`sprite`); en `scene`, el frame completo | |
| `rev` | P4; cambia cuando cambia algo de lo que produce la hoja | |
| `created`, `updated` | ISO 8601 UTC | |

Métodos:
- `get(key)`, `all()` (una sola lectura) y `mtime()`.
- `put(key, ficha)`:
  - conserva `created` y fija `updated`;
  - asigna una `rev` nueva si cambió alguno de `cols, rows, mode, key_color, tolerance, cells`, o si la ficha es nueva;
  - si solo cambian `fps` o `loop`, mantiene la `rev`.
- `remove(key) -> bool` y `prune(valid_keys) -> int`.

## 4. Imagen (`src/gemini_image_api/sprites.py`, solo Pillow)
Reglas generales:
- Nada de numpy.
- Nada de `Image.getdata` (está obsoleto); para leer valores se usa `tobytes()`, `ImageStat` o `histogram()`.
- Toda operación por píxel se hace con `ImageMath.lambda_eval` (disponible desde Pillow 10.3, que es el mínimo del proyecto) sobre imágenes `F`, o con `ImageChops`/`point`. Dentro de `lambda_eval`, `min`/`max` son las funciones que llegan en `args` (operan píxel a píxel), no las de Python.
- Las funciones públicas (las reutilizan los specs 2 y 3) son las de §4.1 a §4.6.

**4.1 `estimate_key(img, key_color) -> (bg_rgb: tuple, strength: float)`.**
- Usa los píxeles del marco exterior de 8 px de la imagen cuyo exceso del color clave es ≥ 40.
- `bg_rgb` es la mediana por canal de esos píxeles. `strength` es la mediana de su exceso.
- Si el marco tiene menos de 50 píxeles así (pasa cuando la hoja trae un margen blanco exterior, como la hoja real de caminar con franjas), se usan todos los píxeles de la hoja con exceso ≥ 40, siempre que sean al menos el 2 % de la imagen. Si tampoco hay suficientes, devuelve el color puro (`(0,255,0)` o `(255,0,255)`) y 255.
- Exceso `S` del color clave:
  - verde: `S = G − max(R,B)`;
  - magenta: `S = min(R,B) − G`;
  - en ambos casos, recortado a 0–255.

**4.2 `soft_key(img, key_color, tolerance, bg=None) -> Image RGBA`.**
1. Con `(bg, K) = bg or estimate_key(img, key_color)`, se definen `tol = min(tolerance, max(0, K − 20))` y `Kf = max(tol + 10, K − 15)`. Así, aunque el fondo sea débil, el fondo puro (`S ≈ K ≥ Kf`) siempre queda transparente.
2. **Alfa** (con umbral): `a = clamp((Kf − S) / (Kf − tol), 0, 1)`. Con `a ≥ 0.98` pasa a 1 y con `a ≤ 0.02` pasa a 0. El canal alfa es `round(255·a)`.
3. **Quitar la mezcla** (sin umbral): `a_u = clamp(1 − S/K, 0.02, 1)` y `c' = (c − (1 − a_u)·bg_c) / a_u`, recortado a 0–255, para cada canal `c`. En un primer plano neutro `S ≈ 0`, así que `a_u = 1` y el color no cambia.
4. Quitar el tinte:
   - verde: `G' = min(G', max(R',B'))`;
   - magenta: si `min(R',B') > G'`, se resta `min(R',B') − G'` a R' y a B'.
5. Los píxeles con alfa 0 quedan como `(0,0,0,0)`.

Test obligatorio: una rampa de gris 200 mezclada sobre `(0,250,10)` con coberturas de 0,1 a 1,0 debe devolver un color a ±12 del gris en todos los píxeles con alfa ≥ 64.

**4.3 `alpha_bbox(rgba, threshold=128) -> box | None`.** Toma la máscara de alfa ≥ `threshold`, le aplica una apertura (`MinFilter(3)` y luego `MaxFilter(3)`, para eliminar motas) y devuelve su `getbbox()`.

**4.4 `alpha_centroid_x(rgba, box) -> float`.**
- Es la x del centro de masa de la máscara (alfa ≥ 128, tras la apertura) dentro de `box`.
- Se calcula con `resize((w,1), BOX)` y una suma ponderada sobre las `w` columnas. Devuelve la x en **coordenadas de la imagen recibida** (la celda): `box.x0 + centroide relativo`.

**4.5 `detect_cells(img, cols, rows, mode, key_color, tolerance) -> dict` (`cells`, `detected_by`, `status`).**
1. Fracciones por columna y por fila, calculadas en C con `resize((W,1), BOX)` / `resize((1,H), BOX)` sobre máscaras `L`:
   - `transp` = fracción de alfa < 128 tras `soft_key`; vale 0 en `scene`;
   - `white` = fracción de píxeles con canal mínimo > 200.
2. Una línea es hueco si cumple `transp ≥ 0.996` (solo en `sprite`) o `white ≥ 0.90`. Por eso un personaje blanco sobre verde no se confunde con un hueco: en sus columnas `white` queda bajo y `transp` < 0.996.
3. Se agrupan las líneas de hueco en rachas. Para cada corte ideal `k·W/cols` se elige la racha **interior** (que no toca el borde) cuyo centro esté más cerca, como mucho a `0.25·W/cols`. Su centro redondeado es el corte. Si a un eje le falta algún corte, ese eje se corta en partes iguales (`round(k·W/cols)`).
4. Las celdas son los rectángulos entre cortes, recorridos fila a fila.
5. `detected_by` es `lanes` solo si ningún eje con más de una celda se cortó en partes iguales.
6. `status`:
   - en `sprite`, es `review` si `detected_by = uniform` o si alguna celda no tiene contenido (`alpha_bbox` None);
   - en `scene`, siempre es `ok`, porque se espera que la escena llene cada celda.

**4.6 `render_sheet(img, ficha) -> (Image, meta)`** (`meta`: `frame_w`, `frame_h`, `pad`, `frame_boxes`).

*Recorte de franja de cada celda:* desde cada borde hacia dentro, como mucho el 15 % de la dimensión, mientras la línea tenga `white ≥ 0.90`. En `sprite` esas líneas pasan a alfa 0.

*Modo `sprite`:*
- Para la celda `r,c`: `rgba = soft_key(celda)`, con `bg` y `K` estimados **una sola vez** sobre la hoja completa; `box = alpha_bbox(rgba)`; `cx = alpha_centroid_x(rgba, box)`.
- La posición vertical se mide desde el **origen de la fila**, que depende de si la celda tiene franja:
  - si se recortó franja blanca arriba de la celda (`trim_top > 0`, ver «Recorte de franja»), el origen es el borde superior de la zona útil: `y_rel = box.y0 − trim_top` (`box` en coordenadas de la celda);
  - si no hay franja (`trim_top = 0`), el origen es el **ideal** de la fila: `y_rel = (y0_celda + box.y0) − round(r·H/rows)`.

  Medido con las hojas reales: con franjas, el origen ideal dejaba la segunda fila ~20 px más baja (margen exterior de 27 px); sin franjas, el corte cae en el centro del hueco (398 frente a 384) y el origen ideal deja los pies de ambas filas a 3 px.
- `half = max sobre los frames de max(cx − box.x0, box.x1 − cx)`, redondeado hacia arriba.
- `top = min(y_rel)` y `bottom = max(y_rel + alto del box)`.
- `pad = max(2, round(0.04·2·half))`, `frame_w = 2·half + 2·pad`, `frame_h = bottom − top + 2·pad`.
- Cada frame: un lienzo transparente con el contenido de la celda dentro de `box` **ampliada 2 px** por cada lado (sin salir de la celda, para no cortar antenas finas ni el borde suavizado), pegado de modo que `box` quede en `x = round(pad + half − (cx − box.x0))` e `y = y_rel − top + pad`. `frame_boxes[k]` es `box` (sin ampliar) en su posición final.
- Un frame vacío deja el lienzo vacío, con `frame_boxes[k] = null`.

*Modo `scene`:*
- Se usa en todas las celdas el recorte de franja máximo por lado.
- El tamaño común es el mínimo ancho y alto interior. Cada frame se recorta desde la esquina superior izquierda interior.
- RGB, `pad = 0`, y `frame_boxes[k]` es el frame completo.

*Hoja:*
- Una fila de `frames·frame_w` de ancho.
- Si supera 8192 px de ancho, todo se reduce por igual con LANCZOS y se guardan los valores ya escalados.
- Se guarda en `<state_dir>/anim_cache/<gen>_<i>_<rev>.png`, que se regenera si falta. Al guardar una `rev` nueva se borran las anteriores de esa clave.

Objetivo de rendimiento: detectar y renderizar una hoja de 1376×768 en menos de 2 s en este Mac. Se mide en un test con margen ×3, marcado `@pytest.mark.slow` si supera 1 s en CI.

## 5. Prompts (`sprites.py`)
`sheet_prompt(prompt, cols, rows, mode, key_color) -> str` no lleva el prefijo `GENERATE_PREFIX`: lo añaden `build_prompt`, el estilo y la pista de proporción. En lo que sigue, `{n} = cols·rows` y `{bg}` es `chroma green #00FF00` o `magenta #FF00FF`.

Texto en modo `sprite`:
```
a sprite sheet for a looping animation. Subject and action: {prompt}, as one seamless loop.
Layout: exactly {n} frames in a grid of {cols} columns x {rows} rows, read left to right, top to bottom. All cells are exactly the same size and the grid fills the whole image evenly.
Consistency: the same character in every frame, with identical design, proportions, colors, outfit, size and camera angle. Fixed camera: no zoom, no pan, no perspective change.
Placement: in every cell the character is centered horizontally and fully inside the cell with generous empty padding on all sides. The ground level is the same in every cell; the character rises above it only while airborne. Nothing touches or crosses the cell edges.
Motion: each frame is the next evenly spaced step of the motion. Changes between consecutive frames are small but clearly visible, and the last frame flows naturally back into the first.
Background: solid flat {bg} in every cell. No gradient, no floor, no cast shadow, no scenery. The character itself does not use that color.
Do not draw grid lines, borders, frame numbers, labels, text or watermarks.
```

Cambios en modo `scene`:
- Línea 1: `a sprite sheet for a looping animated scene. Scene and action: {prompt}, as one seamless loop.`
- Consistency: `the same scene in every frame, with identical composition, framing, colors and lighting; only the described motion changes. Fixed camera: no zoom, no pan, no perspective change.`
- Placement: `every frame fills its whole cell edge to edge.`
- Sin línea Background.

`edit_suffix(ficha)` se añade al texto del usuario cuando se edita una animación:
- en `sprite`: ` Keep the exact same sprite sheet layout: a {cols}x{rows} grid of equal cells, the same character size and position in each cell, and the same solid flat {bg} background.`
- en `scene`: ` Keep the exact same sprite sheet layout: a {cols}x{rows} grid of equal cells, each filling its cell edge to edge.`

`GRIDS = {"4x2": (4, 2, "16:9"), "3x3": (3, 3, "1:1"), "2x2": (2, 2, "1:1")}`. Solo estas rejillas sirven para **generar**; para convertir o ajustar vale cualquier rejilla de §3.

## 6. Servicio, API y MCP
**`ImageService`** (`animations` es un parámetro opcional del constructor; por defecto, `AnimationStore(settings.state_dir)`):
- `generate(..., animation: dict | None = None)`. El argumento es `{"grid", "mode", "key_color"}` y se valida con `validate_animation_request`:
  - `grid` debe estar en `GRIDS`;
  - `mode` puede ser `sprite` o `scene`;
  - en `sprite`, `key_color` es `green` o `magenta` (si falta, `green`); en `scene` pasa a `null`;
  - cualquier otro valor da `invalid_animation`;
  - también da `invalid_animation` si viene junto con `template`.

  Con animación:
  - `aspect_hint` es el de la rejilla y se ignora lo que mande el cliente;
  - el texto que se envía es `sheet_prompt(prompt, …)`;
  - `display_prompt` es el prompt del usuario.

  Tras guardar, por cada imagen (clave calculada como en `_assign_collection`):
  - se ejecutan `detect_cells` y `render_sheet` en un hilo (`asyncio.to_thread`);
  - se guarda la ficha con valores por defecto.

  Si el procesado lanza una excepción, la imagen queda sin ficha y `ServiceResult.warning` pasa a `{code: "invalid_animation", message, next_action}`, pero solo si `res.warning` es `None`. Además, en `jobs._process`: `if sr.warning is not None and (job.warning is None or sr.warning["code"] != "invalid_animation"): job.warning = sr.warning`. Así, el aviso de guardado parcial y `gem_missing` tienen prioridad.
- `edit(conversation_id, ...)`:
  - busca la ficha de la imagen 0 del **registro cabeza** de la conversación (definido en §P3);
  - si existe, añade `edit_suffix` al texto enviado (`display_prompt` = texto del usuario);
  - la imagen nueva recibe una ficha que copia `cols, rows, mode, key_color, tolerance, fps, loop` y **vuelve a detectar** las celdas, sin copiar nunca `cells`.
- Operaciones sobre la ficha:
  - `get_animation(ref)`;
  - `set_animation(ref, *, cols=None, rows=None, mode=None, key_color=None, tolerance=None, fps=None, loop=None, redetect=False)`. Crea la ficha o la actualiza. Al crear, `cols` y `rows` son obligatorios y el resto toma su valor por defecto. Detecta de nuevo si la ficha es nueva, si `redetect` es True o si cambian la rejilla, el modo, el color o la tolerancia. Se ejecuta en un hilo.
  - `delete_animation(ref)`, `animation_sheet(ref) -> Path` y `animation_export(ref) -> (png_path, json_dict, base)`.
- Errores en `errors.py`:
  - `INVALID_ANIMATION = "invalid_animation"`. Next action: «Check the grid (cols×rows, 2–36 frames), mode (sprite/scene), key color (green/magenta), tolerance (5–80), fps (2–30) and loop (loop/pingpong).»
  - `UNKNOWN_ANIMATION = "unknown_animation"`. Next action: «This image has no animation; create one with set_animation (MCP) or “Convertir en animación” (Musa).»
  - Ninguno de los dos es reintentable.
  - Una imagen que Pillow no puede abrir da `invalid_image` (422, como hoy en la web).

**Web.** Todas las rutas van tras la clave y las mutaciones exigen `Origin`. `{gen}/{i}` se valida con `ctx.entry`; si falla, 404 `unknown_image`.

| Método y ruta | Cuerpo / respuesta |
|---|---|
| `GET /api/animations/{gen}/{i}` | ficha + `sheet_url`; 404 `unknown_animation` |
| `PUT /api/animations/{gen}/{i}` | `{cols, rows, mode, key_color, tolerance, fps, loop, redetect}`, todos opcionales al actualizar → ficha; 400 `invalid_animation`; 422 `invalid_image` |
| `DELETE /api/animations/{gen}/{i}` | `{"key", "deleted": true}`; la imagen se queda |
| `GET /api/animations/{gen}/{i}/sheet.png` | hoja normalizada, `Cache-Control: private, max-age=3600` |
| `GET /api/animations/{gen}/{i}/export.zip` | `<base>.png` + `<base>.json`, con `ZIP_STORED` en `tmp_downloads` y `_TempFileResponse`, `Content-Disposition: attachment; filename="<base>-animacion.zip"`, `Cache-Control: no-store` |

Notas de la exportación:
- `<base>` = `slugify(display_prompt, 30)`, que devuelve `img` si queda vacío.
- JSON exportado: `{"image": "<base>.png", "frameWidth", "frameHeight", "frames", "columns": frames, "rows": 1, "fps", "loop", "transparent": mode == "sprite"}`.
- `sheet_url` = `/api/animations/{gen}/{i}/sheet.png?rev=<rev>`.

`POST /api/jobs` acepta `"animation": {"grid", "mode", "key_color"}`:
- Cualquier otra clave de `animation` da `invalid_animation`. El cuerpo nunca acepta `meta` ni `display_prompt`: solo los usan los trabajos internos.
- La validación la hace `JobQueue._validate` con la misma `validate_animation_request`. Si viene con `template` o con `edit_of`, da 400 `invalid_animation`.
- El trabajo guarda la animación normalizada y `ratio` = la proporción de la rejilla.
- `_process` pasa `animation=job.animation` a `service.generate`.

`GET /api/generations`:
- Cada elemento gana `animation`: `null` o `{frames, fps, loop, mode, status, rev, frame_w, frame_h, sheet_url}`.
- Las fichas se leen **una vez** por petición (`animations.all()`) y se pasan a `_item_json`.

**MCP** (+3 tools; INSTRUCTIONS gana un párrafo «Animations»):
- `generate_animation(prompt, grid="4x2", mode="sprite", key_color="green", refs=None, style=None, style_text=None, model=None, project=None, subdir=None, copy_to=None, collection=None, return_image=True)`. Devuelve el contrato de `generate_image` más `"animations": [{"image": "gen:…", "frames", "status", "frame_w", "frame_h"}]`.
- `set_animation(image, cols=None, rows=None, mode=None, key_color=None, tolerance=None, fps=None, loop=None, redetect=False)`. Convierte o ajusta y devuelve `{"status": "ok", "animation": ficha}`.
- `export_animation(image, copy_to)`:
  - `copy_to` es un directorio que pasa el mismo filtro que `copy_to` de `generate_image` (`check_copy_dest`);
  - escribe `<base>.png` y `<base>.json` con `copy_file`, que nunca sobrescribe;
  - devuelve `{"status": "ok", "png", "json", "animation"}`.
- `edit_image` hereda la animación automáticamente.

## 7. Interfaz de Musa
**Compositor:**
- Píldora `#anim-btn` (icono nuevo `i-film`) antes de la de estilo. Muestra «Imagen» o «Anim. 4×2».
- Abre `#anim-pop`, registrado en `POPS` y montado con el patrón `.pop`/`role=listbox` y delegación `data-pick-anim-*`. Tiene tres grupos:
  - **Tipo**: Imagen / Animación;
  - **Rejilla**: 4×2 · 8 frames · 16:9; 3×3 · 9 frames · 1:1; 2×2 · 4 frames · 1:1;
  - **Fondo**: Mascota · verde; Mascota · magenta (personajes verdes); Escena · sin transparencia.
- Por defecto, la animación es 4×2 con Mascota · verde.
- `state.cmp.anim` vale `null` o `{grid, mode, key_color}`.
- En modo Animación:
  - la píldora de proporción se desactiva y muestra la de la rejilla;
  - la de plantilla se desactiva;
  - el placeholder es «Describe el personaje y la acción (p. ej. «gato astronauta caminando hacia la derecha»)»;
  - `sendJob` envía `animation` y no envía `ratio` ni `template`.
- En modo edición la píldora se desactiva y no se envía `animation`. Al salir se restaura el valor anterior.

**Galería (`tileEl`):**
- Las tarjetas con `item.animation` llevan `span.atag` con `svgUse("i-film")` y el texto oculto «Animación» junto a `.mtag`.
- Las tarjetas animadas muestran, en lugar de la hoja de Gemini, el **primer frame** de la hoja normalizada: un reproductor en pausa montado cuando la tarjeta entra en pantalla (`IntersectionObserver`) y destruido cuando sale o se retira del DOM.
- Con `pointerenter` o `focusin`, fuera del modo seleccionar y sin `prefers-reduced-motion: reduce` (`matchMedia`), el reproductor arranca; con `pointerleave` o `focusout` se pausa y vuelve al frame 0.
- Un botón `button.aplay` (▶/❚❚, 44 px) alterna la reproducción sin abrir el detalle. Se ve siempre con `(hover: none)` y, en el resto, cuando la tarjeta tiene el foco (`:focus-within`), para poder pausar con teclado.

**Reproductor (`createSpritePlayer(el, anim)`, en app.js):**
- Un `div` con la hoja de fondo: `background-size: N·100% 100%`, posición `x = idx/(N−1)·100 %` y `aspect-ratio: frame_w/frame_h`.
- Usa `requestAnimationFrame`. Con `loop` recorre 0…N−1; con `pingpong`, 0…N−1…1.
- Métodos: `play`, `pause`, `step(±1)`, `setFps`, `setLoop`, `destroy`, más la propiedad `playing`.

**Panel de detalle** (si hay ficha):
- `#dlg-art` tiene dos vistas, con pestañas `aria-pressed`:
  - **Animación**: el reproductor;
  - **Hoja original**: la imagen con las celdas dibujadas encima en %.
- Controles:
  - reproducir/pausa (icono nuevo `i-pause`);
  - ◀ / ▶ de frame, con el texto «Frame k/N»;
  - FPS de 2 a 30 (`input type=range`);
  - Repetición: Bucle / Ida y vuelta;
  - Fondo: Claro / Oscuro / Cuadros (la clase nueva `.checker`, hecha con un `conic-gradient` de `--surface` y `--raised`).
- Los cambios de FPS y Repetición se guardan con `PUT` a los 400 ms sin cambios.
- Si `status = review`, aparece el aviso «No encontré la rejilla esperada; revisa las celdas.» con el botón «Ajustar».
- Con `prefers-reduced-motion` la animación arranca en pausa.
- Acciones nuevas en `.dlg-actions`:
  - `#dlg-anim`: «Convertir en animación» si no hay ficha, «Ajustar animación» si la hay;
  - `#dlg-anim-export`: «Exportar animación», solo si hay ficha.

**Diálogo `#anim-dialog`** (440 px, patrón del diálogo de prompts):
- Campos:
  - Columnas y Filas (1–8);
  - Modo (Mascota · fondo transparente / Escena · sin transparencia);
  - Color de fondo (Verde / Magenta), desactivado en Escena, con la ayuda «Usa verde si tu personaje es rosa o morado; magenta si es verde.»;
  - Tolerancia (5–80), FPS (2–30) y Repetición;
  - «Volver a detectar las celdas», solo al ajustar.
- Pie:
  - a la izquierda, «Quitar animación» (peligro, con confirmación, solo al ajustar);
  - a la derecha, Cancelar / Guardar.
- Valores iniciales al convertir:
  - 4×2 si ancho/alto ≥ 1,3; si no, 3×3;
  - Mascota, Verde, 25, 10, Bucle.
- Nota: «No gasta cuota.»
- Las flechas del detalle se desactivan mientras está abierto, igual que con `#prompt-dialog`.

`ERR_ES`:
- `invalid_animation` → «Los ajustes de animación no son válidos.»
- `unknown_animation` → «Esta imagen ya no tiene animación.»

## 8. Documentación
README:
- sección «Animaciones (sprite sheets)», con cómo pedirlas, los modos, los colores, la exportación y un ejemplo CSS con `steps()`;
- tools 29 → 32;
- errores 25 → 27, con las filas nuevas;
- las rutas nuevas, en «API de Musa».

## 9. Pruebas
- **Imágenes de prueba sintéticas** (generadas en memoria por `tests/sprite_synth.py`; no se añaden binarios a `tests/`, porque `tests/upstream/test_fixture_sanitization.py` escanea `tests/fixtures/` como texto y las imágenes dan falsos positivos): hoja 4×2 con franjas blancas, 4×2 sin franjas, salto 3×3 con franjas y personaje blanco. En todas, `detect_cells` da `ok` + `lanes` y cortes a ±4 px de la rejilla conocida.
- **Salto** (3×3 sintético): entre los frames 2→3 y 5→6 (cambio de fila), la diferencia de `y_rel` debe ser menor de 40 px. No hay salto artificial de franja.
- **Imágenes reales** (fuera de la suite, en la verificación de la fase): las 3 hojas de §1 en `~/Pictures/gemini-image-api/spritesheet-probe/` dan `ok` + `lanes` y cortes a ±4 px de §1.
- **Sintéticos:**
  - rejilla 4×2 verde con discos a distintas alturas: 8 frames, centroide a ±1 px de `frame_w/2`, alturas relativas conservadas;
  - imagen sin huecos: `uniform` + `review` en `sprite`, `ok` en `scene`;
  - rampa de gris (§4.2);
  - personaje blanco sin contorno sobre verde: `lanes`, con las celdas encontradas.
- **Store:** `put`/`get`, la `rev` no cambia con `fps`, la `rev` es monotónica tras `remove` + `put`, `prune`, archivo corrupto.
- **Cola:** `submit_many` atómico (`queue_full` sin crear nada); `on_finish` llamado una vez en cada uno de los cuatro finales; un gancho que lanza no detiene al trabajador; `display_prompt` llega a `user_prompt`.
- **Web (FakeBackend devolviendo una fixture):**
  - convertir, ajustar y borrar la ficha;
  - la hoja mide `frames·frame_w × frame_h`;
  - el zip trae png y json coherentes;
  - borrar la imagen borra la ficha (por `DELETE` y por lote);
  - casos 400, 404 y 422;
  - un trabajo con `animation` envía `aspect_hint` 16:9 y un prompt con «4 columns x 2 rows», el prompt mostrado es el del usuario y la imagen sale con `animation`;
  - `template` o `edit_of` junto con animación → 400;
  - editar una animación añade el sufijo al prompt y crea una ficha nueva con los mismos ajustes.
- **MCP:** `generate_animation`, `set_animation` y `export_animation`; la cuenta de tools sube a 32.
- **Navegador:** script headless fuera del repo, como se ha hecho hasta ahora, contra un servidor con FakeBackend:
  - el compositor envía el cuerpo correcto;
  - la tarjeta reproduce al pasar el ratón;
  - el detalle reproduce, cambia de FPS y abre «Ajustar».

  Además, `node --check src/gemini_image_api/web/app.js`.
- **Real** (opcional, con aviso): 1 generación 4×2 desde Musa.
