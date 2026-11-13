# Rediseño de Musa con el estilo «Fabric»

Fecha: 2026-11-12 · Estado: aprobado por el usuario (formulario). Solo frontend (`web/*` y sus assets). Referencia: `local://paste-1.md` (guía «Fabric — editorial manuscript on a drafting table»).

## Objetivo
Sustituir el tema crema por un lienzo blanco casi monocromo, con serif editorial en títulos, botones limpios y un único juego de iconos de línea fina. Quitar la fila «Editar / Usar / Borrar» bajo las tarjetas.

## Tokens
- Color: lienzo `#ffffff`; superficie suave `#f3f3f3`; texto `#0a0a0a`; secundario `#666666`; enlaces e indicios activos `#19154e`; bordes `#e5e5e5` (decisión del usuario: suaves, no negros); borde fuerte `#b3b3b3`. Violeta `#1100ff` solo como halo (`box-shadow: 0 0 0 4px rgba(17,0,255,.12)` / `0 0 40px rgba(17,0,255,.15)`) en foco y hover de tarjetas; nunca relleno ni texto. Estados semánticos (error/éxito/aviso) se conservan, desaturados. Paleta 98 % acromática.
- Tipografía: Inter 400/500/600 en la interfaz (cuerpo 15–16 px); serif ligera **Cormorant Garamond** (300/400) para títulos de pantalla y de diálogo, con tracking -0.02em; Manrope no se usa. Fuentes empaquetadas en `web/assets/fonts/` (woff2 latin, licencia OFL con `OFL.txt` de cada familia; sin CDN). Sustituye Instrument Sans.
- Formas: tarjetas 16 px, tarjetas grandes y diálogos 24 px, botones y navegación 8 px, etiquetas en píldora 40 px, imágenes 4–8 px. Sombras mínimas: `0 1px 1px rgba(0,0,0,.1), 0 2px 4px rgba(0,0,0,.05)`.
- Espaciado: escala 4/6/8/10/12/16/20/24/40; separación entre elementos 10 px; max-width de contenido 1200 px.

## Botones
- **Primario:** fondo `#0a0a0a`, texto blanco, radio 8, Inter 600, sombra mínima; hover `#000` con leve elevación; foco con halo violeta.
- **Secundario:** blanco con borde `#e5e5e5` (hover `#b3b3b3` + fondo `#f3f3f3`).
- **Fantasma:** sin borde ni relleno; hover `#f3f3f3`.
- **Peligro:** borde y texto `#b42318`, hover fondo `#fef3f2`.
- **Icon button:** 36 px escritorio / 44 px táctil, círculo o 8 px, solo icono con `aria-label` y tooltip (`title`).
- Altura mínima 40 px (44 táctil/≤860 px), icono 16–18 px a la izquierda del texto cuando hay etiqueta.

## Iconos
Un único juego de línea fina (trazo 1.5, `round` caps/joins, 24×24, estilo Lucide, licencia ISC) como `<symbol>` en `index.html`, reemplazando los actuales: casa, destellos/estilos, cuadrícula, carpeta, medidor, play, lupa, más, equis, lápiz, papelera, descargar, copiar, marcador/favorita, ⋮, chevron arriba/abajo/izquierda/derecha, enviar (flecha arriba), subir, imagen, check, reutilizar/refresh, ojo, ajustes, Finder/carpeta-abierta, relámpago o estrella para Gem. Se acredita Lucide (ISC) en el README.

## Tarjetas (estilos e imágenes)
- Desaparece la fila de botones bajo la tarjeta de estilo. Toda la tarjeta es un botón (en Estilos = Usar, con `aria-pressed` si está seleccionado) con título serif, descripción y familia en píldora.
- Sobre la imagen (esquina superior derecha): icon buttons redondos, visibles al hover/foco y siempre en táctil: **lápiz** (editar, solo estilos propios) y **⋮** (menú: Usar, Editar, Borrar; los integrados solo Usar/Ver). En las tarjetas de imagen se mantiene el ⋮ ya existente con su menú corto, con los iconos nuevos.
- Sin recuadros negros gruesos: estado seleccionado = borde `#0a0a0a` 1 px + halo violeta suave.

## Pantallas
Aplicar tokens y componentes en: carga, sidebar y cabecera, Inicio (banner, Recientes, Pendientes), Estilos, Galería (filtros como píldoras), Colecciones (detalle y actividad), Límites, detalle de imagen (acciones con la nueva jerarquía: «Editar imagen» primario, resto secundarios o iconos), diálogos (Nuevo estilo, colección, confirmación, Gem), compositor (más ligero y blanco, selectores como píldoras con icono, enviar = botón primario negro redondeado), toasts y menús.

## No cambia
Estructura de pantallas y rutas, lógica, atajos de teclado, accesibilidad (foco visible, Esc, aria-*, contraste AA, `prefers-reduced-motion`, targets ≥44 px táctil), textos en español, backend y API, ni cero `innerHTML` con datos. La página sigue a pantalla completa, sin marco.

## Pruebas
Chromium headless contra servidor falso (sin cuota) a 1440 y 390 px, con capturas de cada pantalla y diálogo; contraste AA calculado sobre elementos reales; clics reales de: seleccionar estilo, editar desde el lápiz, ⋮ de estilo (borrar con confirmación), menú ⋮ de imágenes, compositor, diálogos. Consola vacía. `node --check` y `uv run pytest -q -W error`.
