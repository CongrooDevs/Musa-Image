# Editor de estilos, compositor y herramientas MCP

Fecha: 2026-11-11 · Estado: borrador para aprobación
Base: `main` @ df7df43

## 1. Objetivo

Cerrar tres huecos detectados en la revisión de Musa:

1. Los estilos propios no se pueden editar, ni ver qué prompt añaden, ni probar.
2. «Nuevo estilo» no se entiende: tres pestañas con el mismo formulario.
3. El compositor da el mismo peso a todos los controles y esconde referencias y plantillas.

Y cerrar la asimetría web/MCP: hoy solo la web crea estilos y solo ella puede deshacer acciones sobre colecciones.

## 2. Estilos: editar, ver, probar

### 2.1 Backend
- `PUT /api/styles/{sid}`: JSON o multipart con `name`, `description`, `append`, `family` y `image` opcionales. Solo estilos `source == "user"`; un estilo integrado devuelve 403 `invalid_style`. `remove_image=true` quita la referencia.
- `UserStyleStore.update(sid, **campos)`: mantiene el `id` (no se renombra el slug), reemplaza la imagen de forma atómica y borra la anterior solo si estaba dentro de `<state_dir>/styles`.
- Si el estilo tiene Gem activo, al cambiar `name`/`description`/`append` se actualiza el Gem con `StyleGemStore` (instrucciones personalizadas no se tocan).
- `GET /api/styles/{sid}/preview?prompt=...`: devuelve `{final_prompt, refs:[...], aspect_hint}` sin llamar a Gemini. Reutiliza `build_prompt` y el orden real de refs (estilo primero).

### 2.2 «Probar» (gasta 1 imagen)
- Botón «Probar estilo» en el editor: diálogo de confirmación «Gasta 1 imagen de tu cuota» y un campo de prompt de prueba (por defecto «un retrato» sin texto adicional).
- Se envía por la cola normal (`POST /api/jobs`, N=1, estilo=sid). El resultado se abre en el detalle como cualquier imagen; no se guarda aparte.
- Desactivado si la cuota no está disponible (`/api/quota`).

### 2.3 Tarjeta
- Muestra el texto del estilo (2 líneas, completo al abrir), la familia y la insignia «Gem» si procede.
- Acciones visibles (no solo en hover): Editar, Usar, Borrar (solo propios).

## 3. «Nuevo estilo» rediseñado

Un solo diálogo, sin pestañas, mismo componente para crear y editar:

1. **Imagen de referencia (opcional)**: zona de arrastrar/pegar/subir con vista previa, más un botón «Elegir de la galería» que abre un selector de miniaturas. Texto de ayuda: «Gemini copia el movimiento, la paleta y la técnica de esta imagen».
2. **Nombre**.
3. **Instrucciones** (antes «Texto del estilo»): textarea con placeholder de ejemplo («acuarela suave, bordes difusos, colores pastel») y una línea «Se añade al prompt en cada generación». Contador de caracteres.
4. **Más opciones** (cerrado por defecto): descripción y familia, con «Familia: para agrupar en Estilos».
5. **Vista previa del prompt**: bloque de solo lectura con el prompt final de un ejemplo, actualizado al escribir (llama a `/preview` con debounce).
6. Acciones: Cancelar · Probar (si hay cuota) · Guardar.

Las pestañas «Desde la galería» y «Subir imagen» desaparecen; el backend sigue aceptando `from_key` y subida multipart.

## 4. Compositor

- **Jerarquía**: fila 1 el textarea; fila 2 a la izquierda referencias (miniaturas visibles, máx. 3, con quitar), a la derecha estilo, proporción, variaciones y enviar. El modelo pasa a un selector con nombre («Auto / Flash / Pro»).
- **Proporción**: selector con nombre y forma («1:1 Cuadrado», «16:9 Horizontal»), no un checkbox.
- **Referencias**: arrastrar, pegar desde el portapapeles y botón `+`; contador «2/3».
- **Plantillas**: menú «Plantilla» (antes de las opciones) que lista `list_templates`; al elegir una, el textarea pasa a campos por variable y se envía `template`/`template_vars` por la cola. Nuevo endpoint `GET /api/templates`.
- **«Guardar en» y cola**: «Guardar en» sube a la fila de opciones; la cola queda solo como indicador a la derecha.
- Historial: flecha arriba en el textarea vacío recupera el último prompt (localStorage, 20 entradas).
- «Recientes» deja de quedar tapado: el contenido recibe `padding-bottom` igual a la altura del compositor.

## 5. Editar imagen desde el detalle

- Botón «Editar» en el panel de detalle: abre el compositor en modo edición («Editando: <prompt>») con la imagen como contexto; al enviar se usa `edit_image` sobre su `conversation_id` (misma conversación, gasta 1 imagen).
- `POST /api/jobs` acepta `edit_of: "gen:<id>#<i>"`; la cola llama a `ImageService.edit`.
- Salir del modo edición con Esc o con «Cancelar edición».

## 6. Herramientas MCP

| Tool | Notas |
|---|---|
| `create_style(name, instructions, description?, family?, image?)` | `image` = ruta local permitida, URL https o `gen:<id>[#i]`; devuelve el estilo. |
| `update_style(style, ...)` | Solo propios; mismos campos opcionales. |
| `delete_style(style)` | Solo propios; `destructiveHint`. |
| `describe_style(style, prompt?)` | Devuelve el prompt final y las refs sin gastar cuota. |
| `rename_collection(collection, name, description?)` | Idempotente. |
| `delete_collection(collection)` | Quita la etiqueta, no borra imágenes; `destructiveHint`. |
| `remove_from_collection(image, collection)` | Idempotente. |
| `favorite_image(image, favorite=true)` | Idempotente. |
| `get_image(image)` | Metadatos, ruta, colecciones y favorita. |
| `delete_image(image)` | Borra el archivo local y lo marca borrado; `destructiveHint`; no toca Gemini. |
| `generate_batch(prompts[], ...)` | Hasta 8 generaciones secuenciales con el mismo estilo/colección; devuelve una lista de resultados y se detiene ante `quota_guard`/`usage_limit`. |

CLI equivalente: `gia styles add|edit|rm|show`, `gia collections rename|rm|remove`, `gia fav`, `gia rm`.
Los estilos creados por MCP/CLI aparecen en la web por SSE (`styles` mtime ya existe).

## 6. Errores y seguridad

- Sin códigos nuevos: `invalid_style`, `unknown_style`, `invalid_collection`, `unknown_collection`, `invalid_ref`.
- Toda imagen de `create_style`/`update_style` pasa por `validate_local_ref`/`validate_trusted_image` y `sniff_image_format`; SVG se convierte a PNG como en subidas.
- `delete_image`/`delete_style` no aceptan rutas, solo ids.
- Las rutas nuevas quedan tras el middleware de clave/Host/Origin.

## 7. Pruebas

- Unitarias: `UserStyleStore.update` (campos, imagen, ref fuera de directorio, estilo integrado rechazado, Gem sincronizado), `preview`, nuevas tools MCP (con `FakeBackend`), `edit_of` en la cola, plantillas por la cola.
- Web: clics reales en el navegador headless contra un backend falso (editar y guardar, probar con confirmación, compositor con referencia pegada, plantilla, editar desde detalle). Capturas a 1440 y 390 px.
- Verificación real: 1 imagen (botón «Probar») y 1 edición, sobre una copia del estado, con aviso previo de cuota.

## 8. Fuera de alcance

- Combinar varios estilos en una generación.
- Generación de vídeo (detenida por falta de Veo en la cuenta).
- Cambiar la identidad visual (tokens, tipografía, logo).
