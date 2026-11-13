# Rediseño Fabric - Plan

Spec: `docs/superpowers/specs/2026-11-12-fabric-redesign-design.md` (aprobado). Rama `feat/fabric-redesign`. Único dueño de `web/**` y de la sección Créditos del README: `agent: task` (FabricUI). Revisión: `agent: reviewer`.

Cada paso termina con `node --check src/gemini_image_api/web/app.js` y `uv run pytest -q -W error` verdes y un commit propio.

1. **Fundación.** Tokens CSS (color, tipografía, radios, sombras, espaciado). Fuentes locales Inter y Cormorant Garamond (woff2 latin + `OFL.txt`); retirar Instrument Sans. Juego de iconos Lucide como `<symbol>`, migrar todos los `#i-*` y borrar los sin usar.
2. **Componentes.** Botones (primario, secundario, fantasma, peligro, icon button), píldoras, inputs, diálogos, toasts, menús, sidebar, cabecera, foco con halo violeta.
3. **Tarjetas.** Quitar la fila Editar/Usar/Borrar; tarjeta de estilo clicable; lápiz y ⋮ flotantes sobre la imagen (siempre visibles en táctil) reutilizando el menú ⋮ existente; tarjetas de imagen con iconos nuevos.
4. **Pantallas.** Carga, Inicio, Estilos, Galería, Colecciones, Límites, detalle, diálogos, compositor.
5. **Créditos y verificación.** README (Inter, Cormorant Garamond, Lucide). Capturas a 1440 y 390 px contra servidor falso, contraste AA calculado, clics reales y consola vacía.

Revisión final por `reviewer`; después merge a `main` y recarga de Musa (los estáticos se sirven desde disco).
