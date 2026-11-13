# Proveedor ChatGPT (web) en Musa

Fecha: 2026-11-13 · Estado: aprobado por el usuario en el chat
Base: `main` en `0ca2322`.

## 1. Objetivo

Musa genera y edita imágenes también con **ChatGPT**, usando la sesión web del usuario (cuenta Free o de pago). El proveedor se elige como un **modelo más** (`chatgpt-web`). Gemini no cambia.

**Riesgo aceptado por el usuario:** los Términos de uso de OpenAI prohíben extraer contenido generado de forma automática o programática. Este canal automatiza la interfaz web y descarga las imágenes con un script. No hay datos públicos sobre qué hace OpenAI con las cuentas que lo hacen.

## 2. Decisiones del usuario

| Tema | Decisión |
|---|---|
| Canal | Solo la web con navegador. Codex OAuth queda descartado: en Free el servidor quita la herramienta `image_generation` (probado, ver §9) |
| Cómo se elige | Como modelo en el selector: `chatgpt-web` («ChatGPT (web)»). Las ediciones siguen al proveedor de su conversación |
| Referencias | Desde el principio: refs, uploads, imagen de estilo y `gen:<id>` |
| Ventana | Headless |
| Historial | Las conversaciones quedan visibles en la barra lateral de ChatGPT. `delete_conversation` las oculta |

## 3. Arquitectura

- **`src/musa/chatgpt_web.py`** (nuevo): `ChatGPTBrowser`, que gestiona Chrome, y `ChatGPTWebBackend(Backend)`. Es el único módulo que importa `playwright`.
- **`client.py`:** `RoutedBackend(Backend)` contiene `gemini: GeminiWebBackend` y `chatgpt: ChatGPTWebBackend`. `build_backend` devuelve un `RoutedBackend`. Ningún consumidor (CLI, MCP, webapp, `ImageService`) cambia de tipo.
  - `generate(model=…)`: con `model == "chatgpt-web"` va a ChatGPT; con cualquier otro valor, incluido `None`/`auto`, va a Gemini.
  - `edit`, `retry_download` y `delete_conversation`: buscan `rec = state.conversation(id)` y deciden por `rec.model == "chatgpt-web"`. En `edit`, si piden otro modelo para una conversación de ChatGPT, se ignora y se usa ChatGPT, igual que el Gem en Gemini.
  - `list_models`: los modelos de Gemini más `{"model_name": "chatgpt-web", "display_name": "ChatGPT (web)", "available": <hay sesión>}`. Si Gemini falla, se propaga el error, como hoy (el webapp ya tiene una lista de reserva).
  - `resolve_model_name("chatgpt-web")` devuelve `"chatgpt-web"`; los demás nombres van a Gemini.
  - Gems (`create/update/delete_gem`, `gem_exists`): siempre Gemini.
  - `status()`: el de Gemini más `"chatgpt": {"session": bool, "image_gen": {"remaining", "reset_after", "checked_at"} | None}`. **Nunca lanza Chrome**, porque la webapp consulta `status()` cada 60 s: `session` sale de la base de cookies (§6, leída de una copia) e `image_gen` es la última cuota guardada en `state_dir/chatgpt_quota.json`, que se escribe en cada llamada a ChatGPT.
  - `quota()`: la de Gemini, sin cambios (la usa el quota guard del servicio). ChatGPT controla su propia cuota en §5.
  - `aclose()`: cierra los dos.
- **Gem con ChatGPT:** si `generate`/`edit` de ChatGPT recibe `gem_id`, no lo usa y devuelve `warning = {"code": "gem_ignored", …}` con el mensaje «ChatGPT no usa Gems; el estilo se aplicó solo como texto». El servicio ya envía el `append` y la imagen del estilo. **No se usa `gem_missing`:** `service.py:263-266` borra la entrada del Gem del estilo cuando recibe `GEM_MISSING` con su `gem_id`. `gem_ignored` es un código de aviso, no de error (los 29 códigos de error no cambian).
- **`jobs.py`:** `MODEL_LABELS["chatgpt-web"] = "ChatGPT (web)"`.
- **`config.py`:**
  - `chrome_path: str | None = None` (`GIA_CHROME_PATH`); por defecto `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
  - `chatgpt_idle_close_s: float = 120.0`. **Es una propuesta, no un valor medido.**
  - Propiedad `chatgpt_profile_dir = state_dir / "chatgpt-chrome-profile"`, que `ensure_dirs` crea con permisos 0700.
- **`pyproject.toml`:** se agrega `playwright>=1.50`. Solo el paquete: se usa la Chrome instalada (`channel="chrome"`) y no hace falta `playwright install`.

## 4. Navegador (`ChatGPTBrowser`)

- **Lanzamiento:**
  - `pw.chromium.launch_persistent_context(chatgpt_profile_dir, channel="chrome", headless=True, ignore_default_args=["--enable-automation", "--use-mock-keychain"], args=["--disable-blink-features=AutomationControlled"])`.
  - Si existe `chrome_path`, se pasa como `executable_path` en lugar de `channel`.
  - La comunicación es por **pipe**: no se abre ningún puerto TCP de depuración.
  - **`--use-mock-keychain` se excluye siempre.** Con él, Chrome no puede descifrar las cookies guardadas con el llavero de macOS y las borra; eso le cerró la sesión al usuario durante las pruebas.
- **User-Agent:** nunca con una versión fija.
  - Al abrir la primera página, se lee `navigator.userAgent` y se reemplaza `HeadlessChrome` por `Chrome`.
  - Se aplica con CDP `Network.setUserAgentOverride` en cada página. Si las client hints (`navigator.userAgentData.brands`) traen la marca `HeadlessChrome`, se pasa también `userAgentMetadata` con las marcas corregidas y la versión real (`browser.version`).
  - **Sin verificar:** si en headless las client hints incluyen esa marca. La primera tarea lo comprueba (§9).
- **Ciclo de vida:**
  - Se lanza en el primer uso.
  - Lleva un temporizador de inactividad: tras `chatgpt_idle_close_s` sin trabajo, se cierra el contexto.
  - `aclose()` lo cierra de inmediato.
- **Exclusión:** un `AsyncFileLock(state_dir / "chatgpt-profile.lock")` cubre todo el tiempo que el contexto está abierto, además de un `asyncio.Lock` dentro del proceso.
  - Chrome no permite abrir el mismo perfil desde dos procesos. Si musa-mcp y la app Musa coinciden, el segundo espera (con el límite `gen_timeout_s`) y, si no obtiene el lock, falla con `temporarily_blocked`.
  - El inicio de sesión (§6) toma el mismo lock.
- **Navegación:** `page.goto(url, wait_until="domcontentloaded")` con **un** reintento si aparece `net::ERR_ABORTED`, que se vio una vez en las pruebas.
- **Llamadas a la API:** siempre dentro de la página, con `page.evaluate(fetch(…))`.
  - Primero se lee `accessToken` de `/api/auth/session` y se envía como `Authorization: Bearer`, todo dentro del mismo `evaluate`.
  - El token nunca sale de la página ni se registra.

## 5. Flujo de `ChatGPTWebBackend`

### 5.1 Generar y editar

1. Abrir `https://chatgpt.com/`, o `https://chatgpt.com/c/<id>` si es una edición.
2. **Sesión:** `GET /api/auth/session`. Si no hay `accessToken`, se lanza `auth_expired`.
3. **Cuota:** `POST /backend-api/conversation/init` con el cuerpo de §5.3. Si `image_gen.remaining == 0`, se lanza `usage_limit` con `resets_at = reset_after`.
4. **Referencias:** si las hay, `set_input_files([...])` sobre el primer `input[type=file]` cuyo `accept` contiene `image`, todas en una sola llamada.
   - **Señal de fin de subida:** se cuentan las respuestas `POST /backend-api/files/process_upload_stream` con estado 200 y `await response.finished()`. Cuando el contador llega al número de referencias, se continúa.
   - Límite: 90 s; si se supera, `upstream_error` («la subida de referencias no terminó»).
   - No se usa el estado del botón de enviar: solo se localizó por `aria-label="Enviar"`, que depende del idioma.
   - **Verificado una sola vez, con 1 referencia** (1 respuesta). Que haya una respuesta por archivo con varias referencias se comprueba en la tarea de verificación (§9).
5. **Enviar:** clic en el editor (`#prompt-textarea`, o `div[contenteditable='true']` como alternativa), `keyboard.type(prompt, delay=20)` y luego `Enter`.
   - Mientras se envía, se registran las respuestas de `/backend-api/` que contengan `conversation` o `sentinel`. Un 403 o 404 en ellas se traduce a `temporarily_blocked`.
6. **Id de la conversación:** en una generación, esperar a que la URL coincida con `/c/<uuid>` (60 s); si no ocurre, `upstream_error`. En una edición se usa el id guardado en `rec.chat_metadata[0]`.
7. **Esperar la imagen:** `GET /backend-api/conversation/<id>` cada 4 s, hasta `gen_timeout_s`.
   - Una **imagen de salida** es una parte con `content_type == "image_asset_pointer"` dentro de un mensaje con `author.role == "tool"` y `status == "finished_successfully"`.
   - Las imágenes que subió el usuario (en mensajes `user`) no cuentan.
   - Se ordenan por `create_time`, y se toman las que no estaban antes del envío (se cuentan antes).
   - **`no_image`:** solo si ya existe un mensaje del asistente terminado posterior al envío, no hay ningún mensaje `tool` posterior al envío y la conversación no cambia durante 30 s. En ese caso, el texto del asistente va en `gemini_text`. La generación es asíncrona y el asistente puede terminar su turno antes de que llegue la imagen, así que no basta con `end_turn`.
   - Si se agota `gen_timeout_s`, `timeout`.
8. **Descarga:** `file_id` = lo que va después de `://` en `asset_pointer`.
   - `GET /backend-api/files/download/<file_id>?conversation_id=<id>&inline=false` → `download_url`.
   - `fetch(download_url)` dentro de la página → bytes (en base64 dentro de `evaluate`).
   - Se guarda con `save_image` (mismo `_store` que Gemini: `full_size=True`).
   - Si la descarga falla, la entrada queda con `path=None` y estado `generated_not_saved`.
9. **Registro:** `GenerationRecord` con `model="chatgpt-web"`, `conversation_id` = id propio de Musa (`c_…`) y `chat_metadata=[<id de ChatGPT>]`.
   - Cada entrada de `images` guarda `{"index", "path", "file_id", "url": ""}`, con los campos de `_UPSTREAM_FIELDS` vacíos menos `image_id = file_id`.

### 5.2 `retry_download` y `delete_conversation`

- **`retry_download(conv, i)`:** busca la entrada `i` y, con su `image_id` (el `file_id`), repite el paso 8. No hay alternativa por índice: entre ediciones, el número `i` de una conversación no identifica una imagen de forma unívoca. Si el `file_id` ya no sirve, el error es `upstream_error`.
- **`delete_conversation(conv)`:** `PATCH /backend-api/conversation/<id>` con `{"is_visible": false}` y luego agrega el registro `type="delete"`, como en Gemini. **Sin probar:** lo comprueba la tarea de verificación.

### 5.3 Cuota

Cuerpo de `conversation/init`:

```json
{"conversation_id": null, "conversation_origin": null, "gizmo_id": null, "requested_default_model": null, "system_hints": null, "timezone": "<IANA local>", "timezone_offset_min": <minutos>}
```

De `limits_progress` se usa la entrada con `feature_name == "image_gen"`, de la que solo se exponen `remaining` y `reset_after`. **No se asume ninguna regla diaria:** en las pruebas, `remaining` pasó de 1 a 5 después de volver a iniciar sesión.

## 6. Inicio de sesión

- **`auth.py`:** `login_chatgpt(settings, launcher=None, closer=None)`.
  1. Toma el lock del perfil (§4) **sin esperar**. Si está ocupado, `temporarily_blocked` («ChatGPT está en uso; reintenta»).
  2. Lanza Chrome como **proceso normal**: `[chrome, --user-data-dir=<perfil>, --no-first-run, --no-default-browser-check, https://chatgpt.com/auth/login]`, sin automatización ni puerto, con `start_new_session=True`.
  3. Cada 2 s copia `Default/Cookies` y `Default/Cookies-journal` a un directorio temporal y lee la **copia**. Nunca se lee la base que Chrome tiene abierta.
     - Se busca `host_key LIKE '%chatgpt.com'` con `name LIKE '__Secure-next-auth.session-token%'`. Solo se mira el nombre: los valores están cifrados.
     - Chrome guarda las cookies en disco con retraso, así que la detección puede tardar unos segundos.
  4. Al detectarla, envía SIGTERM a los procesos con `--user-data-dir=<perfil>`, **espera a que terminen** (10 s; después, SIGKILL) y lee una copia otra vez para confirmar.
  5. Suelta el lock. Límite total: `LOGIN_TIMEOUT_S` (300 s) → `auth_expired`.
- **CLI:** `musa auth --chatgpt` (y `--status` con ese flag muestra `status()["chatgpt"]`).
- **MCP:**
  - `auth_login(provider: "gemini" | "chatgpt" = "gemini")`.
  - `auth_status` ya devuelve `status()`, que incluye `chatgpt`.
  - `INSTRUCTIONS` y las descripciones de las tools mencionan el modelo `chatgpt-web` y el riesgo de §1.
- **`errors.py`:** sin códigos nuevos. Se generalizan los textos de `NEXT_ACTION` que dicen «Gemini/Google» para que valgan para los dos proveedores, y `AUTH_FAILED` menciona Chrome y `GIA_CHROME_PATH`.

## 7. Errores

| Situación | Código |
|---|---|
| Sin `accessToken` en `/api/auth/session` | `auth_expired` |
| `image_gen.remaining == 0` | `usage_limit` (detalles: `resets_at`) |
| 403/404 en `conversation`/`sentinel`, o una página de verificación de Cloudflare | `temporarily_blocked` |
| Perfil ocupado por otro proceso más allá del límite | `temporarily_blocked` |
| Terminó la respuesta sin imagen | `no_image` (texto en `gemini_text`) |
| Sin imagen tras `gen_timeout_s` | `timeout` |
| La subida de referencias no terminó en 90 s, o no apareció `/c/<id>` | `upstream_error` |
| Falló la descarga | `generated_not_saved` (se puede llamar a `retry_download`) |
| Chrome no está instalado o no arranca | `auth_failed` (pista: `GIA_CHROME_PATH`) |
| `playwright` no está instalado | `auth_failed` (pista: `uv sync`) |

## 8. Superficies

- **Webapp:** el selector de modelo ya se arma con `list_models()`, así que «ChatGPT (web)» aparece solo.
  - La cuota de la cabecera sigue siendo la de Gemini.
  - Los textos de `ERR_ES` que nombraban a Gemini o a Firefox (`auth_expired`, `auth_failed`, `usage_limit`, `temporarily_blocked`, `no_image`, `timeout`) pasan a ser neutrales respecto al proveedor, y se añade `gem_ignored`.
  - No hay UI nueva de inicio de sesión: se usa `musa auth --chatgpt` o la tool `auth_login`.
- **MCP / CLI:** `generate_image(model="chatgpt-web")` y `musa gen --model chatgpt-web`. Las ediciones no necesitan `model`.
- **README:** una sección «ChatGPT (web)» con: el riesgo frente a los Términos de uso, `musa auth --chatgpt`, que hace falta Google Chrome, que las conversaciones quedan en el historial de ChatGPT, y que la cuota es la que muestre `auth_status`.

## 9. Pruebas

### Evidencia previa (pruebas con la cuenta del usuario, 2026-11-13)

| Prueba | Resultado | Ejecuciones |
|---|---|---|
| Codex OAuth con plan Free | `image_gen` eliminado de `tools` → canal descartado | 3 peticiones |
| Generar por CDP sobre una Chrome ya abierta | Imagen en 15 s | 1 |
| Editar en la misma conversación | Mismo encuadre, solo cambió el color; 15 s | 1 |
| Chrome lanzado por Playwright con ventana + 1 referencia | `webdriver=false`; 1 `process_upload_stream` 200; acuarela del gato de referencia; 15 s | 1 (más 1 comprobación sin imagen) |
| Headless con User-Agent corregido | Imagen en 10 s, ningún ≥400 | 1 |

Son 5 ejecuciones, no una prueba de fiabilidad.

### Unitarias (sin red ni navegador)

- `ChatGPTWebBackend` recibe una «página falsa» inyectable (objeto con `goto`, `evaluate`, `keyboard`, `locator`, `on` y `url`).
- Se prueba:
  - El extractor de imágenes de salida: ignora las imágenes subidas por el usuario, los estados no terminados y el orden por `create_time`; toma solo las imágenes nuevas.
  - La normalización de la cuota y `usage_limit` cuando `remaining == 0`.
  - El mapeo de errores de §7.
  - El contador de subidas: no envía hasta tener N `process_upload_stream`; con 90 s → `upstream_error`.
  - `retry_download` con el `file_id` guardado tras una descarga fallida.
  - El registro: `model`, `chat_metadata`, `image_id`.
- `RoutedBackend`:
  - El reparto por `model` y por `rec.model`.
  - `list_models` agrega `chatgpt-web`.
  - Los Gems van siempre a Gemini.
  - `gem_id` con ChatGPT → aviso `gem_ignored`.
- `login_chatgpt` con `launcher`/`closer` falsos y un `Cookies` SQLite de prueba:
  - Lee siempre una copia.
  - Espera a que el proceso termine antes de confirmar.
  - Con el lock ocupado → `temporarily_blocked`.
- `ChatGPTBrowser`: los argumentos de lanzamiento excluyen `--use-mock-keychain` y `--enable-automation`, y el UA se deriva del real (con un lanzador falso).

### Verificación real (al final, con la cuenta del usuario)

**Presupuesto:** los pasos 2, 3 y 4 gastan 1 imagen cada uno (3 en total); los pasos 1, 5 y 6 no gastan. La última lectura fue `remaining: 3`, sin margen. Antes de empezar se lee la cuota con `musa auth --chatgpt --status`, y se ejecutan solo los pasos que quepan, en este orden: 1, 2, 3, 5, 6 y por último 4. Lo que no quepa (normalmente el paso 4, la prueba con 2 referencias) **se deja para después de `reset_after`** y el trabajo se entrega indicando qué queda pendiente. No se supone ningún límite fijo de cuota: se usa siempre el `remaining` que devuelva la API.

1. **Client hints en headless:** con el override aplicado, `navigator.userAgentData.brands` no contiene `HeadlessChrome`. No gasta cuota.
2. `musa gen --model chatgpt-web "<prompt>"` → imagen guardada y registro correcto.
3. `musa edit <conv> "<cambio>"` → imagen nueva en la misma conversación de ChatGPT.
4. `musa gen --model chatgpt-web -r <img1> -r <img2> "<prompt>"` → **2 referencias**: confirmar 2 respuestas `process_upload_stream`.
5. `musa delete <conv>` (sobre la conversación de los pasos 2 y 3) → la conversación ya no aparece en `GET /backend-api/conversations`. No gasta cuota.
6. Al terminar: ningún proceso Chrome usando el perfil y ningún puerto escuchando.

Si falla el paso 1 o el 4, se corrige el diseño antes de dar el trabajo por terminado. Si el paso 4 quedó pendiente, el paso 4 de §5.1 (la subida de varias referencias) sigue marcado como «sin verificar».

## 10. Fuera de alcance

- Canal Codex OAuth (requiere Plus; se puede retomar).
- Ocultar automáticamente cada conversación tras generarla. Antes habría que probar que una conversación oculta se puede editar.
- UI de inicio de sesión de ChatGPT dentro de la webapp.
- Usar la cuota de ChatGPT en el quota guard del servicio, que sigue siendo solo de Gemini.
- Firefox para ChatGPT: Playwright no puede controlar el Firefox real.
