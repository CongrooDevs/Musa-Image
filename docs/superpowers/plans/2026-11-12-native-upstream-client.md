# Native Upstream Client (`src/gemini_image_api/upstream/`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar la dependencia externa `gemini-webapi` por un cliente asíncrono interno, nativo y enfocado (`src/gemini_image_api/upstream/`) que cubra exactamente lo necesario para `gia` y `Musa` (autenticación de sesión con rotación de cookies, protocolo `batchexecute`, subida de imágenes de referencia a Scotty, generación/edición de imágenes con Imagen 3 y CRUD de Gems) sin replicar malas abstracciones ni acoplarse a clases intermedias obsoletas.

**Architecture:**
1. **Fixtures reales y sanitización:** Captura empírica de respuestas reales de Google con redacción estricta de credenciales (`__Secure-1PSID`, `SAPISID`, tokens, etc.) para pruebas reproducibles sin red.
2. **Contrato nativo limpio:** Eliminación de `GeneratedImage.save()` a archivos temporales; el cliente expone tipos de datos directos (`UpstreamTurn`, `UpstreamImage`, `UpstreamGem`, `UpstreamModel`) y descarga asíncrona directa `download_image(url) -> bytes`.
3. **Transporte e impersonación:** `curl_cffi` para impersonar TLS/HTTP2 de navegador contra `gemini.google.com`.
4. **Desacoplamiento total:** `src/gemini_image_api/client.py` implementa el duck-typed `Backend` consumiendo directamente el cliente nativo interno.

**Tech Stack:** Python 3.11+, `curl_cffi` (impersonación TLS y HTTP/2), `orjson`, `pytest`, `pytest-asyncio`.

## Global Constraints

- **Seguridad absoluta en fixtures:** NUNCA almacenar cookies reales (`__Secure-1PSID`, `__Secure-1PSIDTS`, `SAPISID`), tokens de sesión (`SNlM0e`), URLs firmadas sensibles ni identificadores personales en `tests/fixtures/`. Todas las respuestas deben pasar por un pipeline de redacción estricta y contar con un test que bloquee cualquier commit con credenciales reales.
- **Cero cambios de comportamiento externo:** CLI `gia`, servidor MCP (`gia-mcp`) y webapp Musa deben funcionar exactamente igual.
- **Suite de tests íntegra:** Los 541 tests existentes deben pasar sin regresiones.
- **Sin llamadas reales a red en tests unitarios:** Toda la suite automatizada corre offline contra mocks y fixtures sanitizadas.
- **Aislamiento:** Todo el código que interactúa con las URLs y RPCs de Google vive exclusivamente dentro de `src/gemini_image_api/upstream/`.
- **Dependencias:** `gemini-webapi` se elimina completamente de `pyproject.toml` y `curl-cffi` pasa a ser dependencia directa del proyecto.

---

### Fase 0: Fixtures reales sanitizadas

#### Tarea 0.1: Script de captura y redacción estricta de fixtures
- Interceptar y registrar las respuestas reales de:
  - Estado inicial / cuota / modelos (`batchexecute`).
  - Lista de Gems (`CNgdBe`).
  - Generación de imagen (`StreamGenerate`).
- Sanitizar inmediatamente:
  - Reemplazar valores de `__Secure-1PSID`, `__Secure-1PSIDTS`, `SAPISID` por `"REDACTED_COOKIE"`.
  - Reemplazar `SNlM0e` por `"REDACTED_SNLM0E_TOKEN"`.
  - Reemplazar tokens de cuenta por valores genéricos.
- Guardar en `tests/fixtures/upstream/`.

#### Tarea 0.2: Test de seguridad contra fuga de credenciales
- Crear `tests/upstream/test_fixture_sanitization.py`.
- Leer todas las fixtures en `tests/fixtures/upstream/` y verificar:
  - No contiene ninguna cookie real del entorno local (`~/.gemini-image-api/cookies/`).
  - No contiene prefijos típicos de cookies secretas sin redactar.

---

### Fase 1: Contrato nativo limpio

#### Tarea 1.1: Tipos de datos (`types.py`) y Excepciones (`exceptions.py`)
- Crear `src/gemini_image_api/upstream/types.py`:
  - `UpstreamTurn`: `text: str`, `images: list[UpstreamImage]`, `cid: str`, `rid: str`, `rcid: str`.
  - `UpstreamImage`: `url: str`, `cid: str`, `rid: str`, `rcid: str`, `image_id: str`.
  - `UpstreamGem`: `id: str`, `name: str`, `prompt: str`, `description: str`.
  - `UpstreamModel`: `name: str`, `display_name: str`, `available: bool`.
  - `AccountStatus`: `AVAILABLE`, `SUSPENDED`.
- Crear `src/gemini_image_api/upstream/exceptions.py`:
  - Jerarquía limpia: `GeminiError`, `AuthError`, `ImageGenerationError`, `ModelInvalidError`, `TemporarilyBlockedError`, `UsageLimitExceededError`, `TimeoutError`, `APIError`.

---

### Fase 2: Cliente nativo upstream

#### Tarea 2.1: Parser de `batchexecute` (`batchexecute.py`)
- Implementar decodificador de arrays JSON anidados y extracción de RPCs contra las fixtures sanitizadas.

#### Tarea 2.2: Manejador de sesión y rotación (`session.py`)
- `curl_cffi.requests.AsyncSession` con impersonación `chrome120`.
- Generación de cabecera `Authorization: SAPISIDHASH <timestamp>_<sha1>`.
- Extracción de token `SNlM0e`.
- Rotación de `__Secure-1PSIDTS` ante `RotateCookies`.

#### Tarea 2.3: Uploader multimedia Scotty (`upload.py`)
- Subida resumable de imágenes de entrada para obtener tokens multimedia utilizables en el prompt.

#### Tarea 2.4: Cliente unificado (`client.py`)
- Fachada asíncrona `GeminiClient` con:
  - `init()`, `close()`
  - `list_models()`, `resolve_model()`
  - `generate_content(...)` -> `UpstreamTurn`
  - `download_image(url) -> bytes` (directo a memoria)
  - `fetch_gems()`, `create_gem()`, `update_gem()`, `delete_gem()`
  - `read_chat()`, `delete_chat()`

---

### Fase 3: Cutover y verificación

#### Tarea 3.1: Migración en `client.py` y `auth.py`
- Adaptar `src/gemini_image_api/client.py` para usar el contrato limpio de `upstream`.
- Eliminar la lógica redundante de descarga en carpetas temporales (`img.save(tmp)`).

#### Tarea 3.2: Actualización de dependencias
- Eliminar `gemini-webapi` de `pyproject.toml`.
- Añadir `curl-cffi` a `pyproject.toml`.
- Ejecutar `uv sync --extra test`.

#### Tarea 3.3: Verificación integral
- Ejecutar los 541 tests existentes: `uv run pytest -q`.
- Ejecutar prueba E2E contra la sesión real: generación y edición de imagen.
- Actualizar `README.md`.
