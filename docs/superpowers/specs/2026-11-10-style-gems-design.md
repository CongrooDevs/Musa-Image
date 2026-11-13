# Gem de estilo (subproyecto 3, parte A)

Fecha: 2026-11-10 · Estado: aprobado por el usuario (formulario, sin cambios)
Base: `main` tras Musa parte 2. La parte B (vídeo) tendrá su propio spec.

## 1. Objetivo

Cualquier estilo de Musa, de los incluidos o de los propios, puede tener asociado un **Gem** en la cuenta de Gemini del usuario. Así, las generaciones de ese estilo usan instrucciones de sistema persistentes y salen más consistentes. El Gem se activa bajo demanda, estilo por estilo.

## 2. Decisiones del usuario

| Tema | Decisión |
|---|---|
| Qué es un «Gem de estilo» | Un estilo de Musa convertido en un Gem de Gemini, creado y sincronizado desde la app |
| Activación | Bajo demanda, con el interruptor «Usar Gem» de cada estilo. Al activarlo se crea el Gem; al desactivarlo se borra |
| Orden | Primero el Gem; el vídeo va después, en un spec aparte |

## 3. Datos

- **`<state_dir>/gems.json`**: `{version:1, gems:{<style_id>: {gem_id, name, instructions, custom: bool, created, updated}}}`.
  - Escritura atómica bajo `gems.lock`.
  - Si el archivo está corrupto, se trata como vacío y se renombra a `.corrupt-<ts>`, igual que `library.json`.
- **Nombre del Gem:** `"Musa · <style.name>"`, con 60 caracteres como máximo.
- **Instrucciones por defecto:** la plantilla fija en inglés que se describe en §3.1, rellenada con los datos del estilo. Si el usuario las edita, se guardan con `custom: true` y dejan de regenerarse a partir del estilo.

### 3.1 Plantilla de instrucciones

Esta es la plantilla que se usa (los `{…}` se rellenan con los datos del estilo):

```
You are an image-generation assistant that always works in the "{name}" style.
Style description: {description}
Visual rules: {append}
{if ref: The first attached image is the style reference ({credit}); match its palette, brushwork and composition principles, never its subject.}
Always answer by generating an image. Do not search the web. Keep any extra text to one short sentence.
If the user attaches other images, keep their subject and layout and apply this style.
```

## 4. Backend (`client.py` es el único módulo que importa `gemini_webapi`)

- **API nueva del backend**, que el protocolo `Backend` declara con implementaciones por defecto que lanzan `NotImplementedError`:
  - `async create_gem(name, instructions, description) -> str` devuelve el `gem_id`;
  - `async update_gem(gem_id, name, instructions, description) -> None`;
  - `async delete_gem(gem_id) -> None`;
  - `async gem_exists(gem_id) -> bool`, mediante `fetch_gems()` con una caché de 60 s.
- **Uso en las llamadas:**
  - `generate` y `edit` aceptan `gem_id: str | None = None`.
  - `_call_upstream` lo pasa en `start_chat(gem=…)` y en `generate_content(gem=…)`.
  - Antes de una llamada con `gem_id`, el backend comprueba `gem_exists(gem_id)`. Si el Gem no existe, la llamada se hace **sin Gem** y `GenerateResult` lleva `warning = {"code": "gem_missing", "message", "next_action"}`, un campo nuevo que va al final y por defecto es `None`. No se intenta deducir el caso a partir de los mensajes de error de Gemini.
  - Si `fetch_gems` falla (red), se intenta igualmente la llamada **con** Gem: no se desactiva nada por un fallo de red.
  - Estas operaciones respetan los mismos locks (`_alock` y `_flock`) y el mismo throttle que el resto de llamadas a Gemini.
- **Registro:** `GenerationRecord` gana un último campo, `gem_id: str | None = None`.
  - `edit` reutiliza el `gem_id` de la conversación.
  - `retry_download` lo copia.
- **Errores:** fallos de red y de autenticación al gestionar Gems → los códigos que ya existen (`auth_*`, `upstream_error`, `timeout`). No hay códigos nuevos para el agente.
  - `gem_missing` es solo un aviso (`warning`), nunca un error.

## 5. Servicio (`style_gems.py` nuevo + `ImageService`)

- **`StyleGemStore(state_dir)`**: lee y escribe `gems.json`.
- **`ImageService.set_style_gem(style_id, enabled, instructions=None) -> dict`**:
  - Si `enabled` y el estilo no tiene Gem: `backend.create_gem(...)` y se guarda.
  - Si `enabled`, ya existe y llegan `instructions`: `update_gem(...)` y `custom=true`.
  - Si no `enabled`: `delete_gem` y se quita la entrada. Si el Gem ya no existe en Gemini, se da por borrado sin error.
  - Un estilo desconocido → `UNKNOWN_STYLE`.
- **`ImageService.style_gem_instructions(style_id) -> str`**: devuelve la plantilla rellena, o las instrucciones personalizadas si las hay.
- **Generación:** en `_run`, si `style` tiene Gem, se pasa `gem_id` al backend. El texto del estilo (`append`) y su imagen de referencia se siguen enviando.
  - Si el backend devuelve `warning gem_missing`, se borra la entrada local, de modo que el interruptor queda apagado.
  - El `warning` viaja en `to_payload` / `ServiceResult`.
- **Sincronización:** cuando se crea o edita un estilo propio con Gem y `custom=false`, se regeneran las instrucciones y se llama a `update_gem`. Si el estilo propio se borra, también se borra su Gem.
  - Los estilos incluidos no se editan, así que no hay nada que sincronizar.

## 6. Superficies

- **MCP:**
  - `list_styles` sigue devolviendo `{id: description}`.
  - Tool nueva `list_style_gems()` (solo lectura) → `[{style, gem_id, name, custom}]`.
  - Tool nueva `style_gem(style, enabled, instructions?)` → resultado de `set_style_gem`.
  - `INSTRUCTIONS` menciona los Gems.
- **CLI:** `gia styles gem <style> on|off [--instructions TEXT]` y `gia styles gems`.
- **Web:**
  - `GET /api/styles` añade `gem: {gem_id, name, custom} | null` a cada estilo.
  - `PUT /api/styles/{id}/gem` con `{enabled, instructions?}` → 200 con el estilo.
  - `GET /api/styles/{id}/gem/instructions` → `{instructions, custom}`.
  - Errores con el payload de 6 campos.
- **Frontend:**
  - Cada tarjeta de estilo tiene el interruptor «Usar Gem» (un `<button role="switch" aria-checked>`), sin abrir la tarjeta.
  - Si el estilo tiene Gem, la tarjeta muestra la insignia «Gem» y el enlace «Instrucciones», que abre un diálogo con un textarea y los botones «Guardar» y «Restaurar plantilla».
  - En el compositor, el chip del estilo muestra «· Gem» cuando el estilo lo tiene.
  - Si el resultado de un trabajo llega con `warning gem_missing`, se muestra el toast «El Gem de <estilo> ya no existía; se generó sin él».

## 7. Pruebas

- **Backend falso** que guarda los Gems en memoria.
- **Unitarias:**
  - `StyleGemStore`: archivo corrupto, escritura atómica.
  - `set_style_gem`: activar, desactivar, editar instrucciones; desactivar cuando el Gem ya no existe.
  - `_run` pasa `gem_id` y sigue enviando el `append` y la imagen de referencia.
  - `gem_missing` desactiva el Gem y deja el aviso.
  - `edit` reutiliza el `gem_id` de la conversación.
  - Registro y `retry_download`.
- **Interfaces:** MCP, CLI y web, con forma de la respuesta y códigos de error.
- **`client.py`:** con un `gemini_webapi` simulado:
  - `create`, `update` y `delete` llaman a la API de la librería con los argumentos correctos;
  - `gem=` llega a `start_chat` y a `generate_content`;
  - reintento sin Gem cuando el Gem no existe.
- **Verificación real (el controlador, sobre una copia del estado en `/tmp`):**
  1. **Antes de construir nada:** crear un Gem y generar **1 imagen** con él, para confirmar que Gemini genera imágenes dentro de un Gem. Si no, se detiene el proyecto y se informa.
  2. **Al final:** activar el Gem de «Impresionismo» desde la app, generar 1 imagen, comprobar `gem_id` en el registro, desactivarlo y confirmar con `fetch_gems` que el Gem ya no está en la cuenta.

## 8. Fuera de alcance

- Usar Gems que ya existían en la cuenta y no fueron creados por Musa.
- Un Gem «director de arte» global.
- Vídeo (parte B).
