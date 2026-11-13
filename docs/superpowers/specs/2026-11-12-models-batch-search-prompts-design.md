# Modelos reales, lote, búsqueda, prompts, deuda v1.2 y publicación

Fecha: 2026-11-12 · Estado: borrador para aprobación · Base: `main` @ 0781d65
Elegido por el usuario: modelos, selección múltiple, búsqueda y orden, biblioteca de prompts, deuda v1.2, publicar. Vídeo descartado (sin Veo en la cuenta).

## 0. Orden de ejecución
1. Deuda v1.2 (§5), pequeña y sin UI.
2. Modelos reales (§1).
3. Biblioteca de prompts (§4).
4. Búsqueda, orden y ⌘K (§3).
5. Selección múltiple y lote (§2).
6. Publicar (§6), solo con tu OK explícito y la visibilidad elegida.
Cada frente = una rama, revisión con `reviewer`, y fusión a `main` **solo cuando lo apruebes**.

## 1. Modelos reales de la cuenta
Problema: `jobs.MODELS` es una lista fija (`auto`, `gemini-flash`, `gemini-pro`) y `/api/models` la devuelve tal cual, mientras que la cuenta hoy registra `gemini-flash-lite`, `gemini-4-hydrogen`, `gemini-pro`, `gemini-carbon`. Además `gemini-flash` resuelve a `gemini-flash-lite`: la etiqueta «Gemini Flash» miente.
- `GET /api/models` -> `{"models": [{"id", "name", "description"}], "default": {"requested", "resolved"}}`. Los datos salen de `backend.list_models()` (cache 60 s, fallback a la lista fija si falla). `name` = nombre amable (mapa corto: `gemini-flash-lite` «Flash Lite», `gemini-pro` «Pro», `gemini-carbon` «Carbon», `gemini-4-hydrogen` «Hydrogen»; desconocidos: `model_name`), `description` = `display_name` de Google («Respuestas rápidas»).
- `backend.resolve_model_name(name)` (solo `client.py`, sin red de generación) para `default.resolved`. «Auto» se muestra como «Auto · usa <resolved>».
- La validación de `POST /api/jobs` usa la lista viva; un id no registrado -> `unknown_model` con `valid=[...]`.
- Selector de modelo del compositor y etiquetas de miniaturas muestran el nombre real; el destello de Gemini se mantiene. Sin cambiar `config.default_model` en este frente (cambiarlo exige probar 1 imagen real con el modelo nuevo: decisión tuya, §7).
- MCP: `list_models` ya es vivo; se añade `resolved_default` en su respuesta.

## 2. Selección múltiple y lote
- Modo «Seleccionar» en Galería, Colecciones y Recientes: casilla en cada miniatura, Mayús+clic para rango, «Seleccionar todo lo visible», Esc limpia. Barra fija inferior con contador y acciones: **Favorita**, **Quitar favorita**, **Añadir a colección…** (lista + crear), **Quitar de colección** (en detalle de colección), **Descargar (.zip)**, **Borrar** (confirmación con el número exacto).
- `POST /api/images/batch` `{action, keys[≤200], collection?}` con `action ∈ {favorite, unfavorite, add_to_collection, remove_from_collection, delete}`; respuesta `{results:[{key, ok, error?}]}`; un fallo no aborta el resto. Solo claves del índice (nunca rutas del cliente).
- `POST /api/images/zip` `{keys[≤100]}` -> `application/zip` en streaming, máx. 500 MB, solo archivos dentro de `output_root`, nombres `<fecha>_<slug>_<n>.<ext>` sin rutas; sin compresión de imágenes ya comprimidas (`ZIP_STORED`).
- Reutiliza `ImageService` (`set_favorite`, `add_to_collection`, `delete_image`); sin códigos de error nuevos.

## 3. Búsqueda, orden y ⌘K
- `query_images` y `GET /api/generations` aceptan `model`, `sort` (`newest` por defecto, `oldest`, `favorites`), `since` y `until` (ISO date, comparadas como fechas). `query` también busca en el nombre del estilo. MCP `list_generations` gana `model`.
- UI: botón «Filtros» (modelo, estilo, rango de fechas) y selector «Ordenar»; los filtros activos aparecen como píldoras quitables y viven en el hash (`#galeria?model=...`).
- **Paleta ⌘K / Ctrl+K**: diálogo con campo y lista (`role=dialog` + `combobox/listbox`): pantallas, estilos («Usar …»), colecciones, acciones («Nuevo estilo», «Nueva colección»), y hasta 8 imágenes por búsqueda de prompt. ↑/↓/Enter/Esc; foco devuelto al origen; no se abre con foco en un campo de texto salvo con el atajo.

## 4. Biblioteca de prompts
- `PromptStore` (`prompts.py`) -> `<state_dir>/prompts.json` `{version:1, prompts:[{id:"pr_"+8hex, name, text, created, uses}]}`; `FileLock` + escritura atómica; fichero corrupto -> renombrado `.corrupt-<ts>` y vacío. Límites: 200 prompts, nombre ≤80, texto ≤4000.
- API: `GET/POST /api/prompts`, `PUT/DELETE /api/prompts/{id}`, `POST /api/prompts/{id}/use` (incrementa `uses`). SSE: `prompts` mtime en `changed`.
- UI: botón «Prompts» (icono marcador) en el compositor -> popover con buscador y lista (más usados primero); clic inserta en el textarea; «Guardar este prompt…» (nombre) y editar/borrar por fila. «Guardar prompt» también en el panel de detalle (prompt de la imagen).
- MCP: `list_prompts`, `save_prompt(name, text)`, `update_prompt(prompt, name?, text?)`, `delete_prompt(prompt)` -> 29 tools. Errores: tres códigos nuevos `invalid_prompt`, `unknown_prompt` e `invalid_query` (22 -> 25, con `NEXT_ACTION` y test de unicidad actualizado).

## 5. Deuda técnica v1.2 (cada punto con test)
1. `StateStore.list(since=...)` compara `ts` como texto: comparar como `datetime` (zonas mixtas).
2. `limit <= 0` hoy devuelve 1: devolver lista vacía (y `invalid_query` en la web).
3. Sumidero de `loguru` duplicado si `build_backend` se llama dos veces: configuración idempotente.
4. `_backend` no se pone a `None` tras `aclose`: limpiar y recrear bajo demanda.
5. `_checkout_root` compara por subcadena: comparar por componentes de ruta.
6. `upstream_cache/` se crea 0755: crear 0700 y corregir los existentes al arrancar.
7. `test_output_root_default` no aísla el YAML del usuario: aislar `HOME`/config en el test.
8. `_call_upstream` pone `_client = None` sin `_client_lock`: hacerlo bajo el lock.

## 6. Publicar
- Con tu OK explícito y visibilidad elegida (público/privado): `git remote add origin https://github.com/CongrooDevs/Musa-Image.git` y `git push -u origin main` (solo `main`). Antes: nuevo escaneo de secretos (cookies, claves `?key=`, rutas personales) sobre el árbol y el historial. Hoy no hay remoto configurado.
- README: sección «Para el equipo» (instalar con `uv tool install git+https://github.com/CongrooDevs/Musa-Image.git`, `gia auth` por persona, las cookies nunca se comparten) y badge de licencia ya existente.
- No se hace push sin tu mensaje explícito.

## 7. Decisiones abiertas
- Modelo por defecto: dejar `gemini-flash` (hoy = Flash Lite) o pasar a `gemini-4-hydrogen` tras probarlo con 1 imagen real.
- Visibilidad del repositorio al publicar.
- `27bb1b5` (rediseño Fabric) sigue en `main` hasta que decidas mantenerlo o revertirlo.

## 8. Pruebas
Unitarias por frente (store, servicio, webapp con FakeBackend). Web: Chromium headless con servidor falso a 1440 y 390 px (clics reales: filtros, ⌘K, selección y lote, popover de prompts); capturas. Real: como mucho 1 imagen para validar el modelo por defecto, con aviso previo y sobre copia de estado.
