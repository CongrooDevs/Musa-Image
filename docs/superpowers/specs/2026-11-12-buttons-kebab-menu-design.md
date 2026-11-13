# Botones con iconos y menú ⋮ por imagen

Fecha: 2026-11-12 · Estado: aprobado por el usuario (formulario). Solo frontend (`web/*`).

## Problema
Los botones son pequeños, sin iconos y quedan apretados. Faltan atajos rápidos por imagen.

## Diseño
- **Botones**: altura mínima 40 px (44 px en táctil o ≤860 px), más padding, separación ≥10 px, filas con wrap sin amontonarse. Tres niveles: primario (relleno), secundario (borde), peligro (rojo suave). Contraste AA y foco visible.
- **Iconos + texto** en los botones de acción de tarjetas de estilo, detalle de imagen y diálogos. Compositor: iconos con texto corto en los selectores y botón de enviar más grande.
- **Menú ⋮** en cada imagen de galería, Recientes y colecciones: botón flotante en la esquina de la miniatura; siempre visible en táctil, en escritorio al hover/foco (alcanzable con teclado). El clic en ⋮ no abre el detalle.
- **Acciones (menú corto, cada una con icono)**: Abrir · Editar imagen · Favorita · Añadir a colección · Copiar ID · Borrar (con la confirmación existente).
- **Accesibilidad**: `role="menu"`/`menuitem`, Esc y clic fuera cierran, ↑/↓ navegan, el foco vuelve al ⋮, reposicionado arriba/izquierda si no cabe, no se cierra por el refresco SSE.
- **No cambia**: lógica de las acciones (se reutilizan las del detalle), diseño general, backend.

## Pruebas
Chromium headless contra servidor falso a 1440 y 390 px: abrir menú, Esc, ↑/↓, favorita, añadir a colección, Editar imagen, Borrar con confirmación, copiar ID; consola vacía; `node --check` y `uv run pytest -q -W error`.
