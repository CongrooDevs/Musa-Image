"""ChatGPT (web) provider: drives chatgpt.com in the user's Google Chrome (dedicated profile, headless).

The only module that imports playwright, lazily. Spec: docs/superpowers/specs/2026-11-13-chatgpt-web-provider-design.md.
"""
from __future__ import annotations

import asyncio
import base64
import os
import platform
import re
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from filelock import AsyncFileLock, Timeout as LockTimeout

from .errors import AUTH_FAILED, TEMPORARILY_BLOCKED, UPSTREAM_ERROR, GIAError

BASE_URL = "https://chatgpt.com"
UPLOAD_PATH = "/backend-api/files/process_upload_stream"
_CONV_URL_RE = re.compile(r"/c/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


@dataclass(frozen=True)
class OutputImage:
    message_id: str
    file_id: str
    width: int | None
    height: int | None


def _messages(conversation: dict) -> list[dict]:
    """Every message of a conversation's mapping tree, oldest first."""
    msgs = [n["message"] for n in (conversation.get("mapping") or {}).values() if n.get("message")]
    return sorted(msgs, key=lambda m: m.get("create_time") or 0)


def _role(message: dict) -> str | None:
    return (message.get("author") or {}).get("role")


def message_ids(conversation: dict) -> set[str]:
    return {m["id"] for m in _messages(conversation) if m.get("id")}


def new_messages(conversation: dict, known: set[str]) -> list[dict]:
    return [m for m in _messages(conversation) if m.get("id") not in known]


def output_images(messages: list[dict]) -> list[OutputImage]:
    """Generated images only: image_asset_pointer parts of finished tool messages (user uploads are excluded)."""
    out = []
    for m in messages:
        if _role(m) != "tool" or m.get("status") != "finished_successfully":
            continue
        for part in (m.get("content") or {}).get("parts") or []:
            if (isinstance(part, dict) and part.get("content_type") == "image_asset_pointer"
                    and "://" in str(part.get("asset_pointer", ""))):
                pointer = part["asset_pointer"]
                out.append(OutputImage(m["id"], pointer.split("://", 1)[1], part.get("width"), part.get("height")))
    return out


def has_tool_message(messages: list[dict]) -> bool:
    return any(_role(m) == "tool" for m in messages)


def assistant_text(messages: list[dict]) -> str | None:
    """Text of the last finished assistant message, or None when there is none."""
    for m in reversed(messages):
        if _role(m) == "assistant" and m.get("status") == "finished_successfully":
            parts = (m.get("content") or {}).get("parts") or []
            return "".join(p for p in parts if isinstance(p, str)).strip()
    return None


def image_gen_quota(init_json: dict | None) -> dict | None:
    for item in (init_json or {}).get("limits_progress") or []:
        if item.get("feature_name") == "image_gen":
            return {"remaining": item.get("remaining"), "reset_after": item.get("reset_after")}
    return None


def conversation_id_from_url(url: str) -> str | None:
    m = _CONV_URL_RE.search(url or "")
    return m.group(1) if m else None


def is_blocking_response(method: str, url: str, status: int) -> bool:
    """403/404 on a POST of the send path: Cloudflare/sentinel refused the turn ('Unusual activity' or a
    disguised 'Model not found'). Musa's own polling is GET and never counts."""
    path = (url or "").split("?", 1)[0]
    return (method == "POST" and status in (403, 404) and "/backend-api/" in path
            and ("conversation" in path or "sentinel" in path))


# -- browser -------------------------------------------------------------------
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# --use-mock-keychain makes Chrome unable to decrypt the cookies saved with the macOS keychain (it then drops
# them: the session is lost); --enable-automation sets navigator.webdriver and the automation infobar.
DROPPED_DEFAULT_ARGS = ["--enable-automation", "--use-mock-keychain"]
UPLOAD_TIMEOUT_S = 90.0
TYPE_DELAY_MS = 10
COMPOSER = "#prompt-textarea, div[contenteditable='true']"


async def _start_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise GIAError(AUTH_FAILED, "playwright is not installed", hint="run: uv sync") from e
    return await async_playwright().start()


def _client_hint_platform() -> tuple[str, str]:
    plat = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
    arch = "arm" if platform.machine().lower() in ("arm64", "aarch64") else "x86"
    return plat, arch


def ua_override(version_info: dict) -> dict:
    """Network.setUserAgentOverride params from Browser.getVersion: the installed binary's real UA and client
    hints, without the 'HeadlessChrome' marker."""
    full = version_info["product"].split("/", 1)[1]
    major = full.split(".", 1)[0]
    plat, arch = _client_hint_platform()
    return {"userAgent": version_info["userAgent"].replace("HeadlessChrome", "Chrome"),
            "userAgentMetadata": {
                "brands": [{"brand": "Google Chrome", "version": major}, {"brand": "Chromium", "version": major},
                           {"brand": "Not_A Brand", "version": "24"}],
                "fullVersion": full, "platform": plat, "platformVersion": "", "architecture": arch,
                "model": "", "mobile": False}}


class ChatGPTBrowser:
    """One headless Chrome on the dedicated profile, launched on demand over Playwright's pipe (no TCP port).

    The profile file lock is held while Chrome runs (Chrome refuses a profile open twice, and `musa auth
    --chatgpt` must not race it); the browser closes after `chatgpt_idle_close_s` without use."""

    def __init__(self, settings, start_playwright=None):
        self.settings = settings
        self._start = start_playwright or _start_playwright
        self._lock = asyncio.Lock()
        self._flock = AsyncFileLock(str(settings.chatgpt_lock_file))
        self._pw = self._ctx = self._page = None
        self._idle = None

    def launch_kwargs(self) -> dict:
        kw = {"user_data_dir": str(self.settings.chatgpt_profile_dir), "headless": True,
              "ignore_default_args": list(DROPPED_DEFAULT_ARGS), "args": list(LAUNCH_ARGS)}
        if self.settings.chrome_path:
            kw["executable_path"] = self.settings.chrome_path
        else:
            kw["channel"] = "chrome"
        return kw

    @asynccontextmanager
    async def page(self):
        """Exclusive use of the logged-in page; an unexpected error tears the browser down (relaunch next time)."""
        async with self._lock:
            self._cancel_idle()
            try:
                yield await self._ensure()
            except GIAError:
                raise
            except BaseException:
                await self._teardown()
                raise
            finally:
                if self._ctx is not None:
                    self._arm_idle()

    async def aclose(self) -> None:
        self._cancel_idle()
        async with self._lock:
            await self._teardown()

    async def _ensure(self):
        if self._page is not None:
            return self._page
        profile = self.settings.chatgpt_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        os.chmod(profile, 0o700)
        try:
            await self._flock.acquire(timeout=self.settings.gen_timeout_s)
        except LockTimeout:
            raise GIAError(TEMPORARILY_BLOCKED, "the ChatGPT Chrome profile is in use by another Musa process",
                           hint="retry when the other ChatGPT job or `musa auth --chatgpt` ends") from None
        try:
            self._pw = await self._start()
            self._ctx = await self._pw.chromium.launch_persistent_context(**self.launch_kwargs())
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            cdp = await self._ctx.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride", ua_override(await cdp.send("Browser.getVersion")))
            self._page = page
            return page
        except GIAError:
            await self._teardown()
            raise
        except Exception as e:
            await self._teardown()
            raise GIAError(AUTH_FAILED, f"could not start Google Chrome: {e}",
                           hint="install Google Chrome or set GIA_CHROME_PATH") from e

    async def _teardown(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._pw = self._ctx = self._page = None
        for close in (ctx.close if ctx else None, pw.stop if pw else None):
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        if self._flock.is_locked:
            await self._flock.release(force=True)

    def _arm_idle(self) -> None:
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(self.settings.chatgpt_idle_close_s,
                                     lambda: loop.create_task(self._close_if_idle()))

    def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    async def _close_if_idle(self) -> None:
        async with self._lock:
            self._idle = None
            await self._teardown()


# -- page adapter ----------------------------------------------------------------
JS_SESSION = """async () => {
  const r = await fetch('/api/auth/session', {credentials: 'include'});
  if (!r.ok) return {logged_in: false, plan: null, status: r.status};
  const s = await r.json();
  return {logged_in: !!s.accessToken, plan: (s.account || {}).planType || null};
}"""
# The bearer token is read and used inside the page; it never crosses into Python.
JS_API = """async ([path, method, body]) => {
  const s = await (await fetch('/api/auth/session', {credentials: 'include'})).json();
  const r = await fetch(path, {method, credentials: 'include',
    headers: {'Authorization': 'Bearer ' + (s.accessToken || ''), 'Content-Type': 'application/json'},
    body: body === null || body === undefined ? undefined : JSON.stringify(body)});
  const t = await r.text();
  let json = null; try { json = JSON.parse(t); } catch (e) {}
  return {status: r.status, json};
}"""
JS_BYTES = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, b64: btoa(s)};
}"""
JS_TZ = "() => [Intl.DateTimeFormat().resolvedOptions().timeZone, new Date().getTimezoneOffset()]"


class ChatGPTPage:
    """Thin adapter over a Playwright page: the backend reaches chatgpt.com only through these methods."""

    def __init__(self, page, upload_timeout_s: float = UPLOAD_TIMEOUT_S, clock=time.monotonic, sleep=asyncio.sleep):
        self._page = page
        self._upload_timeout_s = upload_timeout_s
        self._clock, self._sleep = clock, sleep

    @property
    def url(self) -> str:
        return self._page.url

    async def open(self, url: str) -> None:
        for attempt in (0, 1):
            try:
                await self._page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as e:
                if attempt or "ERR_ABORTED" not in str(e):
                    raise

    async def session(self) -> dict:
        return await self._page.evaluate(JS_SESSION)

    async def api(self, path: str, method: str = "GET", body=None) -> tuple[int, dict | None]:
        r = await self._page.evaluate(JS_API, [path, method, body])
        return r["status"], r.get("json")

    async def image_quota(self) -> dict | None:
        tz, offset = await self._page.evaluate(JS_TZ)
        status, data = await self.api("/backend-api/conversation/init", "POST", {
            "conversation_id": None, "conversation_origin": None, "gizmo_id": None,
            "requested_default_model": None, "system_hints": None,
            "timezone": tz, "timezone_offset_min": offset})
        return image_gen_quota(data) if status == 200 else None

    async def download(self, conversation_id: str, file_id: str) -> bytes:
        status, meta = await self.api(
            f"/backend-api/files/download/{file_id}?conversation_id={conversation_id}&inline=false")
        url = (meta or {}).get("download_url")
        if status != 200 or not url:
            raise GIAError(UPSTREAM_ERROR, f"ChatGPT gave no download link for {file_id} (HTTP {status})")
        got = await self._page.evaluate(JS_BYTES, url)
        if got["status"] != 200:
            raise GIAError(UPSTREAM_ERROR, f"ChatGPT image download failed (HTTP {got['status']})")
        return base64.b64decode(got["b64"])

    async def attach(self, paths: list[Path]) -> None:
        """Attach every reference in one go, then wait for one finished 200 POST to process_upload_stream per
        file. The send button is not used: it is only reachable through its localized aria-label."""
        if not paths:
            return
        finished: list[asyncio.Future] = []

        def on_response(resp) -> None:
            if (resp.request.method == "POST" and resp.status == 200
                    and resp.url.split("?", 1)[0].endswith(UPLOAD_PATH)):
                finished.append(asyncio.ensure_future(resp.finished()))

        self._page.on("response", on_response)
        try:
            inputs = self._page.locator("input[type='file']")
            target = None
            for i in range(await inputs.count()):
                if "image" in (await inputs.nth(i).get_attribute("accept") or ""):
                    target = inputs.nth(i)
                    break
            if target is None:
                raise GIAError(UPSTREAM_ERROR, "the ChatGPT page has no image upload input")
            await target.set_input_files([str(p) for p in paths])
            deadline = self._clock() + self._upload_timeout_s
            while True:
                done = [f for f in finished if f.done() and f.exception() is None and f.result() is None]
                if len(done) >= len(paths):
                    return
                if self._clock() >= deadline:
                    raise GIAError(UPSTREAM_ERROR, "the reference upload did not finish",
                                   expected=len(paths), finished=len(done))
                await self._sleep(0.25)
        finally:
            self._page.remove_listener("response", on_response)

    async def send(self, prompt: str) -> None:
        """Type the prompt (line breaks as Shift+Enter, which keeps them inside the message) and send with Enter."""
        box = self._page.locator(COMPOSER).first
        await box.wait_for(state="visible", timeout=30_000)
        await box.click()
        lines = prompt.split("\n")
        for i, line in enumerate(lines):
            if line:
                await self._page.keyboard.type(line, delay=TYPE_DELAY_MS)
            if i < len(lines) - 1:
                await self._page.keyboard.press("Shift+Enter")
        await self._page.keyboard.press("Enter")

    @contextmanager
    def watch_blocking(self):
        """Collect (url, status) of responses that mean ChatGPT refused the turn while the block runs."""
        blocked: list[tuple[str, int]] = []

        def on_response(resp) -> None:
            if is_blocking_response(resp.request.method, resp.url, resp.status):
                blocked.append((resp.url.split("?", 1)[0], resp.status))

        self._page.on("response", on_response)
        try:
            yield blocked
        finally:
            self._page.remove_listener("response", on_response)
