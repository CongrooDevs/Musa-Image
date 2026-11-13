# Musa · Composición animada (spec 3 de 3)

Fecha: 2026-11-13 · Estado: v2 tras evaluación · Base: `main` @ 390acb1

Depende de:
- **spec 1** (sprites): ficha con `frame_boxes`, hoja normalizada, `soft_key`, §P;
- **spec 2** (composición): capas, `bbox`/`offset`, `search_offset`, `CompositionManager`, `scene.json` v1.

## 0. Objetivo y decisiones del usuario
Que en una composición **algunas capas se animen y otras no**. Ejemplo: Hermes con animación *idle*, las monedas cayendo y el tarro llenándose, mientras las tiendas y el fondo se quedan quietos.

Lo que decidió el usuario:
- Exportación: un **paquete web** (capas + hojas + `scene.json` + reproductor JS sin dependencias + `index.html`) y además un **WebP animado aplanado**.
- Superficies: Musa + MCP.

Todavía no se ha probado con imágenes reales:
- los estados (generaciones sucesivas de una capa);
- los frames colocados sobre una capa.

Por eso los dos guardan su propio desplazamiento y se pueden rehacer o quitar.

## 1. Tipos de animación por capa
Cada capa tiene **un solo** `animation`. Los tipos se excluyen entre sí, y el fondo solo admite `static`.

| Tipo | Ejemplo | Cuota | Datos guardados |
|---|---|---|---|
| `static` (por defecto) | fondo, tiendas | no | `{"type": "static"}` |
| `motion` | Hermes flotando, monedas cayendo | no | `{"type": "motion", "preset", "amplitude", "period", "phase", "copies"}` |
| `frames` | Hermes parpadeando | 1 imagen | `{"type": "frames", "image": clave de una hoja con ficha (spec 1)}` |
| `states` | tarro vacío → lleno | 1 por estado | `{"type": "states", "hold", "loop", "states": [estado]}` |

Cada `estado` es `{"image", "conversation_id", "bbox", "offset", "file", "rev"}`. El estado 0 **no se guarda**: es siempre la imagen de la capa.

**Presets de `motion`.** Las unidades son píxeles de la escena salvo que se indique otra cosa.

| `preset` | `amplitude` (defecto; rango) | `period` s (defecto) | Pivote |
|---|---|---|---|
| `bob` (flotar) | 6 px (1–60) | 2.0 | — |
| `sway` (balanceo) | 4° (1–30) | 2.4 | centro inferior de la caja de la capa |
| `pulse` (latido) | 0.04 (0.01–0.3) | 1.6 | centro de la caja |
| `fall` (caer en bucle) | `null` = auto, `round(1.2·alto de la caja)`; o 10–2000 | 1.2 | — |

Otros rangos: `period` de 0.3 a 10; `phase` de 0 a 1 (por defecto 0); `copies` solo en `fall`, de 1 a 5 (por defecto 3). El pivote se calcula siempre a partir de la caja y no se acepta en PATCH.

La **caja de la capa** es `L = bbox + offset` (spec 2).

## 2. Matemática común
Se implementa dos veces, en Python (`scene_math.py`) y en JS (`musa-scene.js`). Ambas pasan los mismos vectores de prueba (§9).

Definiciones: `t` = segundos y `p = frac(t/period + phase)`.

| Preset o tipo | Fórmula |
|---|---|
| `bob` | `dy = −amplitude·sin(2πp)` |
| `sway` | `θ = amplitude·sin(2πp)` grados, positivo en sentido horario. CSS: `rotate(θdeg)`. Pillow: `rotate(−θ, center=…)`. |
| `pulse` | `s = 1 + amplitude·sin(2πp)` |
| `fall` | Para cada copia `c`: `pc = frac(p + c/copies)`, `dy = −A·(1 − pc)`, opacidad `min(1, pc/0.15, (1−pc)/0.15)`. `A` es `amplitude`, o el valor automático. |
| `frames` | `n = floor(t·fps)`. En `loop`, el frame es `n mod N`. En `pingpong` (N>1), el índice sale de la secuencia 0…N−1…1, de longitud `2N−2`. |
| `states` | Hay `M` = 1 + número de estados guardados. `n = floor(t/hold)`. En `loop`: `n mod M`. En `pingpong`: como en `frames`. En `once`: `min(n, M−1)`. |

**Colocación de `frames`.** La capa usa la ficha de la hoja (`frame_w`, `frame_h`, `frame_boxes`). Se toma `F = frame_boxes[0]`, o el primer frame que no sea nulo.
- Escala: `s = (L.y1 − L.y0)/(F.y1 − F.y0)`.
- El elemento se coloca en `x = L.x0 − F.x0·s` e `y = L.y0 − F.y0·s`, con tamaño `frame_w·s × frame_h·s`.

Así el frame 0 cae encima de la pieza estática. Mientras haya `frames`, la imagen estática de la capa no se dibuja.

**Colocación de `states`.**
- El estado 0 se dibuja en `L`.
- El estado k ≥ 1 se dibuja en `bbox_k + offset_capa + offset_k`.
- `offset_k` se mide contra la imagen de la capa con `search_offset`. Si no mejora en más de 1,0, se queda en (0,0).

**`transform-origin` en JS.** El pivote está en píxeles de la escena. En el elemento se usa `((px − x)/w·100 %, (py − y)/h·100 %)`.

**Unidades en JS.** `translate` en CSS usa porcentajes de la propia caja del elemento, no de la escena. Por eso los desplazamientos se aplican en px de pantalla: `k = container.clientWidth / W` (actualizado con un `ResizeObserver` en el contenedor) y `translate(dx·k px, dy·k px)`. Posición y tamaño de cada elemento siguen en % de la escena.

## 3. Cómo se generan
Todos los trabajos usan `CompositionManager`, la cola, `meta` (`action`: `"frames"` o `"state"`) y `display_prompt` = `{nombre} · {capa} · frames` o `{nombre} · {capa} · estado k`.

**`frames`** («Animar con frames», solo en capas de pieza `done`):
- Entrada: acción (1–500 caracteres) y rejilla (`4x2`, `3x3` o `2x2`).
- Trabajo: generación del spec 1 con:
  - `animation = {grid, mode: "sprite", key_color: el de la capa}`;
  - `refs = ["gen:" + imagen de la capa]`;
  - prompt `{nombre de la pieza} from the reference image, {acción}. Keep the exact same character design, colors and line style as the reference image.` (lo envuelve `sheet_prompt`).
- Al terminar: `animation = {"type": "frames", "image": clave nueva}`.
- Si la generación falla o no deja ficha (el procesado falló), se aplica la regla de fallo del spec 2 §5.3: la capa vuelve a `done` con el aviso en `error` (`invalid_animation` u otro código) y la animación anterior no cambia.

**`states`** («Añadir estado…», solo en capas de pieza `done`):
- Entrada: un texto, por ejemplo «el tarro lleno de monedas hasta arriba».
- Se aplica la regla §P3 sobre la **imagen del último estado**, o sobre la imagen de la capa si aún no hay estados, con los prompts de corrección del spec 2.
- Al terminar:
  - se recorta la imagen nueva con `normalize_size` + `prepare_piece` (color y tolerancia de la capa);
  - se calcula `offset_k`;
  - se añade el estado.
- Se puede quitar **cualquier** estado k ≥ 1. Como §P3 se decide en el momento de generar, quitar un estado no rompe nada.
- Máximo 6 estados guardados.

**`motion`**: solo ajustes; no se genera nada.

**Reglas sobre la capa:**
- Si la capa se **regenera** o se **corrige** (spec 2), su `animation` vuelve a `static` cuando es `states` (los estados dependían de la imagen anterior). Si es `motion` o `frames`, se conserva.
- Si se cambia la `tolerance` de la capa, también se vuelven a recortar sus estados (cada uno desde su imagen fuente; si alguna ya no existe, ese estado conserva su PNG).
- Si la hoja de `frames` pierde su ficha o se borra, al leer la capa se trata como `static` con `"broken": true`, y la interfaz avisa: «La hoja de frames ya no existe.»
- Ajustar la ficha en la galería (celdas, tolerancia, fps) cambia lo que muestra la capa. Las URLs llevan la `rev` de la ficha. La vista previa y la portada usan la **rev efectiva** de la composición = `max(rev de la composición, rev de cada ficha usada por capas frames)`, y `preview_<rev efectiva>.png`.

## 4. Reproductor web (`src/gemini_image_api/web/musa-scene.js`)
Es un script clásico, no un módulo, para que funcione abriendo el archivo desde disco. No tiene dependencias, no usa `fetch` ni canvas, y expone:

`window.MusaScene = { mountScene(container, scene, {autoplay = true, baseUrl = ""}) → {play, pause, seek(t), destroy, playing} }`

- `scene` sigue el formato de §5. `baseUrl` se antepone a los nombres de archivo.
- El contenedor recibe `position: relative; overflow: hidden; aspect-ratio: W/H; width: 100%`. Cada capa es un elemento con `position: absolute`, colocado en % de la escena:
  - `static` y `motion`: un `<img>`;
  - `fall`: `copies` imágenes `<img>`;
  - `states`: un `<img>` por estado, de los que solo uno es visible;
  - `frames`: un `<div>` con la hoja de fondo, `background-size: N·100% 100%`.
- En cada `requestAnimationFrame` se aplica `transform` (`translate` en px de pantalla según «Unidades en JS» de §2, + `rotate` + `scale`) y `opacity`.
- Con `prefers-reduced-motion: reduce` no arranca solo: dibuja `t = 0` y espera a `play()`.
- Musa lo carga con `<script src="/static/musa-scene.js">` en `index.html`, antes de `app.js`. El mismo archivo va dentro del zip.

## 5. `scene.json` v1 con animación
Amplía el del spec 2 sin cambiar la versión: cada capa gana `animation`.
```json
{"type": "static"}
{"type": "motion", "preset": "bob", "amplitude": 6, "period": 2.0, "phase": 0, "pivot": [610, 729]}
{"type": "motion", "preset": "fall", "amplitude": 120, "period": 1.2, "phase": 0, "copies": 3, "pivot": [0, 0]}
{"type": "frames", "file": "hermes-frames.png", "frames": 8, "fps": 10, "loop": "loop", "x": 470, "y": 160, "w": 281, "h": 600}
{"type": "states", "hold": 0.8, "loop": "loop", "states": [{"file": "tarro-1.png", "x": 518, "y": 395, "w": 164, "h": 205}]}
```

| Tipo | Significado de los datos |
|---|---|
| Capa | `x, y, w, h` = caja `L` de la imagen estática (spec 2) |
| `frames` | `x, y, w, h` = caja colocada de **un frame**: `w = frame_w·s`, `h = frame_h·s`, `x, y` como en §2. El `<div>` muestra un frame cada vez (`background-size: N·100% 100%`). |
| `states` | `states` lista solo los estados **guardados** (k ≥ 1), cada uno con su caja final; el estado 0 es la imagen estática de la capa (`file`, `x, y, w, h` de la capa), así que la secuencia tiene `M = 1 + len(states)` pasos |
| `pivot` | en px de la escena; `[0,0]` cuando no aplica. En `fall`, `amplitude` ya va resuelto (nunca `null`). |

Nombres de archivo:
- hojas: `<slug>-frames.png`;
- estados: `<slug>-<k>.png`, con k ≥ 1;
- si dos coinciden, se añade `-2`, `-3`, como en el spec 2.

`GET /api/compositions/{id}/scene.json` devuelve lo mismo con URLs de Musa (`…/layers/{lid}.png?rev=`, `sheet.png?rev=` del spec 1 y `…/states/{k}.png?rev=`) en lugar de nombres. Así el reproductor lo usa con `baseUrl = ""`.

## 6. WebP animado (`src/gemini_image_api/scene_render.py`)
`render_webp(scene, open_file, out_path, *, T=72, tick_fps=12, max_width=800)`. `open_file: Callable[[str], Image]` recibe un nombre de archivo de `scene` (`layers[].file`, `animation.file`, `states[].file`) y devuelve la imagen. En Musa la resuelve el gestor contra el directorio de la composición y la caché de hojas; en los tests, un diccionario.
- **Duración y cuantización** (así el bucle cierra exacto): la duración total es de `T` ticks a 12 fps, es decir, 6 s, si hay alguna capa animada; si no, 1 frame. Cada capa animada ajusta su ciclo a un número entero de repeticiones dentro de `T`:
  - `ticks_i` = ciclo de la capa en ticks: `period·12` para `motion`; `N/fps·12` (o `(2N−2)/fps·12`) para `frames`; `M·hold·12` (o `(2M−2)·hold·12`) para `states`;
  - `cycles = max(1, floor(T/ticks_i + 0.5))` (redondeo con empates hacia arriba, no el `round` bancario de Python);
  - la fase en el tick `j` (entero, 0…T) se calcula con aritmética de enteros: `p = frac(j·cycles/T + phase)` para `motion`; índice de secuencia `floor(j·cycles·L/T) mod L` para `frames`/`states` (L = longitud de su secuencia), con `j·cycles·L` entero y división entera;
  - por eso las velocidades del WebP pueden diferir hasta ±25 % de las del navegador (p. ej. `sway` 2,4 s → 3,0 s). La interfaz lo dice junto al botón «Exportar WebP animado».
  - `states` en `once` no hace bucle: índice `min(floor(j / (hold·12)), M−1)` calculado en flotante (no cierra bucle, así que no necesita aritmética entera), es decir, se muestra el último estado hasta el final.
- **Escala:** `k = min(1, max_width/W)` sobre todo.
- **Cada tick** `j = 0…T−1`: `render_tick(j)` = fondo + capas visibles en orden, con la matemática de §2 evaluada con las fases de arriba. Operaciones de Pillow:
  - `alpha_composite`;
  - `rotate(−θ, center, resample=BICUBIC, expand=False)` sobre un lienzo del tamaño de la caja con margen;
  - `resize` con LANCZOS en `pulse` y `frames`;
  - alfa multiplicado con `point` para la opacidad de `fall`.
- **Salida:** `save(out, "WEBP", save_all=True, append_images=…, duration=[round((j+1)·1000/12) − round(j·1000/12) for j in range(T)], loop=0, quality=85, lossless=False)`. Así la suma es exactamente 6000 ms. libwebp puede fusionar frames consecutivos idénticos (suma sus duraciones); es correcto y no se altera ningún píxel para evitarlo. Tras guardar, se vuelve a abrir y se comprueba que es WebP y que la **suma de duraciones** es 6000 ms (1 frame si la escena es estática); si no (o el guardado lanza), se lanza `GIAError(INVALID_COMPOSITION, "este Pillow no puede crear WebP animado")`.
- **Tiempo objetivo:** menos de 10 s para 72 frames a 800 px. Se ejecuta en un hilo.

`composite.png` (zip y vista previa) es la escena en `t = 0`.

## 7. API
**Web** (además de las rutas del spec 2):

| Método y ruta | Cuerpo / respuesta |
|---|---|
| `PATCH /api/compositions/{id}/layers/{lid}` | Ahora acepta `animation`: `{"type": "static"}` (quita cualquier animación); `{"type": "motion", preset, amplitude?, period?, phase?, copies?}`; o, si ya es `states`, `{"type": "states", hold?, loop?}` para ajustarlo. |
| `POST /api/compositions/{id}/layers/{lid}/frames` | `{action, grid}` → 202 `{job}` |
| `POST /api/compositions/{id}/layers/{lid}/states` | `{prompt}` (1–2000) → 202 `{job}` |
| `DELETE /api/compositions/{id}/layers/{lid}/states/{k}` | `k ≥ 1`; si no queda ningún estado, la capa vuelve a `static` |
| `GET /api/compositions/{id}/layers/{lid}/states/{k}.png` | PNG del estado recortado (`k ≥ 1`) |
| `GET /api/compositions/{id}/scene.json` | escena con URLs de Musa |
| `GET /api/compositions/{id}/export.zip` | capas + hojas + estados + `composite.png` + `scene.json` + `musa-scene.js` + `index.html` |
| `GET /api/compositions/{id}/scene.webp` | WebP animado, con `Content-Disposition: attachment; filename="scene-<slug>.webp"` y `Cache-Control: no-store` |

**`index.html` del zip.**
- Contiene:
  - `<div id="scene" style="max-width:{W}px;margin:auto">`;
  - `<script type="application/json" id="scene-data">…</script>`;
  - `<script src="musa-scene.js"></script>`;
  - un script que hace `MusaScene.mountScene(document.getElementById("scene"), JSON.parse(document.getElementById("scene-data").textContent))`.
- El JSON incrustado se escapa: `<` → `\u003c`, `>` → `\u003e`, `&` → `\u0026`, U+2028 → `\u2028` y U+2029 → `\u2029`.
- El nombre de la composición va en `<title>`, escapado como HTML.
- Abierto desde disco, funciona sin servidor.

**Validación.**
- `invalid_composition` se usa cuando:
  - el preset o un rango no son válidos;
  - se pide una animación en el fondo;
  - se piden `frames` o `states` por PATCH con datos de generación;
  - `k` está fuera de rango;
  - se pide un estado o frames en una capa que no está `done` o que está ocupada;
  - se superan 6 estados.
- `unknown_composition` se usa para una capa o un estado que no existen.

**MCP** (+1 tool; total 37):
- `set_layer_animation(composition, layer, animation)`: acepta solo `static` o `motion`, que no gastan cuota. Los tipos que gastan (`frames` y `states`) quedan para Musa.
- `export_composition(composition, copy_to, webp=False)`: con `webp=True` escribe también `scene-<slug>.webp`.
- `get_composition` y `list_compositions` muestran `animation` en cada capa.

## 8. Interfaz de Musa
En cada capa de pieza hay un bloque **Animación** con un selector: Ninguna / Movimiento / Frames / Estados. Cambiar de tipo pide confirmación si se pierden frames o estados.

- **Movimiento:**
  - campos: preset (Flotar / Balanceo / Latido / Caer), amplitud (con «auto» en Caer), periodo, desfase y copias (solo en Caer);
  - se ve en vivo en el escenario;
  - se guarda con `PATCH` tras 400 ms sin cambios.
- **Frames:**
  - si no hay: campo «Acción», rejilla y «Generar frames (1 imagen)»;
  - si hay: miniatura animada, «Abrir en la galería» y «Quitar»;
  - si `broken`, aparece un aviso.
- **Estados:**
  - lista con el estado 0 (la imagen de la capa, que no se puede quitar) y los estados guardados, cada uno con su miniatura y «Quitar»;
  - «Añadir estado…» (textarea, 1 imagen; desactivado al llegar a 6);
  - `hold` (0.2–10 s) y Repetición (Bucle / Ida y vuelta / Una vez).

El escenario del editor:
- usa `MusaScene.mountScene` con `scene.json` de Musa;
- tiene un botón reproducir/pausa;
- se vuelve a montar cuando cambia `rev` o cambia la animación de alguna capa.

Exportar:
- «Exportar web (.zip)»;
- «Exportar WebP animado», que tarda unos segundos y muestra el botón ocupado mientras tanto.

## 9. Pruebas
- **Vectores de oro** en `tests/fixtures/scene_golden.json`: casos `{type, params, t}` → `{dy | θ | s | opacity[] | index}`.
  - `tests/test_scene_math.py` los comprueba en Python.
  - `tests/test_scene_js.py` ejecuta `node tests/js/scene_golden.cjs`, que carga `musa-scene.js` en un `vm` y expone las funciones puras como `MusaScene._math`. Se marca `skip` si no hay `node`.
- **Cuantización:**
  - para cada preset y para `frames`/`states` en bucle, `render_tick(0)` y `render_tick(T)` son iguales con tolerancia ±1 por canal (el WebP guarda los ticks 0…T−1; el tick T se evalúa aparte; el ±1 absorbe el residuo de coma flotante de `phase`);
  - el número de frames es 72 (1 sin animación);
  - el ancho es ≤ 800.
- **Colocación de `frames`:** con una ficha sintética, el frame 0 colocado coincide con `L` (±1 px).
- **Web con FakeBackend:**
  - PATCH de `motion` válido e inválido;
  - `frames` encola una generación con `animation` y `refs` de la capa y, al terminar, deja `type frames`;
  - `states` encola una edición si la capa es la cabeza y una generación con `refs` si no, y añade el estado con su `offset`;
  - quitar un estado;
  - regenerar la capa devuelve `states` a `static`;
  - `scene.json` con URLs;
  - un zip con `musa-scene.js`, `index.html` (con el JSON escapado: un nombre con `</script>` no rompe la página) y `scene.json` coherentes;
  - `scene.webp` es un WebP animado con duración total de 6000 ms (`1 < n_frames ≤ 72`; `== 72` cuando todos los ticks difieren);
- **Navegador:**
  - abrir el `index.html` del zip descomprimido (`file://`) monta la escena;
  - el editor reproduce y pausa;
  - con reducir movimiento la escena no arranca sola.
- **MCP:** `set_layer_animation` y `export_composition(webp=True)`; la cuenta de tools pasa a 37.
