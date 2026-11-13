import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from musa import chatgpt_web as cw
from musa.errors import GIAError

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "HeadlessChrome/155.0.8059.86 Safari/537.36")


class FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getVersion":
            return {"product": "HeadlessChrome/155.0.8059.86", "userAgent": UA}
        return {}


class FakeContext:
    def __init__(self):
        self.pages = [SimpleNamespace(name="page0")]
        self.cdp = FakeCDP()
        self.closed = False

    async def new_page(self):
        return SimpleNamespace(name="new")

    async def new_cdp_session(self, page):
        return self.cdp

    async def close(self):
        self.closed = True


class FakePW:
    def __init__(self, fail=None):
        self.launches, self.contexts, self.stopped, self.fail = [], [], False, fail
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    async def _launch(self, **kw):
        if self.fail:
            raise self.fail
        self.launches.append(kw)
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def stop(self):
        self.stopped = True


def _browser(settings, pw):
    async def start():
        return pw
    return cw.ChatGPTBrowser(settings, start_playwright=start)


def _lock_free(settings) -> bool:
    lock = FileLock(str(settings.chatgpt_lock_file))
    try:
        lock.acquire(timeout=0)
    except Exception:
        return False
    lock.release()
    return True


async def test_launch_kwargs_drop_automation_and_mock_keychain(settings):
    pw = FakePW()
    b = _browser(settings, pw)
    async with b.page():
        pass
    (kw,) = pw.launches
    assert kw["user_data_dir"] == str(settings.chatgpt_profile_dir)
    assert kw["headless"] is True and kw["channel"] == "chrome"
    assert set(kw["ignore_default_args"]) == {"--enable-automation", "--use-mock-keychain"}
    assert kw["args"] == ["--disable-blink-features=AutomationControlled"]
    assert "executable_path" not in kw
    await b.aclose()


async def test_chrome_path_replaces_channel(settings):
    settings.chrome_path = "/opt/chrome"
    kw = cw.ChatGPTBrowser(settings).launch_kwargs()
    assert kw["executable_path"] == "/opt/chrome" and "channel" not in kw


async def test_user_agent_comes_from_the_real_binary(settings):
    pw = FakePW()
    b = _browser(settings, pw)
    async with b.page():
        pass
    method, params = pw.contexts[0].cdp.sent[-1]
    assert method == "Network.setUserAgentOverride"
    assert "Headless" not in params["userAgent"] and "Chrome/155.0.8059.86" in params["userAgent"]
    meta = params["userAgentMetadata"]
    assert {"brand": "Google Chrome", "version": "155"} in meta["brands"]
    assert all("Headless" not in b["brand"] for b in meta["brands"])
    assert meta["fullVersion"] == "155.0.8059.86" and meta["mobile"] is False
    await b.aclose()


async def test_reuses_one_browser_and_closes_when_idle(settings):
    settings.chatgpt_idle_close_s = 0.05
    pw = FakePW()
    b = _browser(settings, pw)
    async with b.page() as p1:
        pass
    async with b.page() as p2:
        assert p1 is p2
    assert len(pw.launches) == 1 and not _lock_free(settings)
    await asyncio.sleep(0.2)
    assert pw.contexts[0].closed and pw.stopped and _lock_free(settings)


async def test_profile_busy_is_temporarily_blocked(settings):
    settings.gen_timeout_s = 0.1
    settings.ensure_dirs()
    b = _browser(settings, FakePW())
    with FileLock(str(settings.chatgpt_lock_file)):
        with pytest.raises(GIAError) as e:
            async with b.page():
                pass
    assert e.value.code == "temporarily_blocked"


async def test_launch_failure_is_auth_failed_and_releases_lock(settings):
    b = _browser(settings, FakePW(fail=RuntimeError("no chrome")))
    with pytest.raises(GIAError) as e:
        async with b.page():
            pass
    assert e.value.code == "auth_failed" and "GIA_CHROME_PATH" in e.value.details["hint"]
    assert _lock_free(settings)


async def test_unexpected_error_tears_down_but_giaerror_keeps_browser(settings):
    pw = FakePW()
    b = _browser(settings, pw)
    with pytest.raises(GIAError):
        async with b.page():
            raise GIAError("timeout", "slow")
    assert not pw.contexts[0].closed
    with pytest.raises(RuntimeError):
        async with b.page():
            raise RuntimeError("target closed")
    assert pw.contexts[0].closed and _lock_free(settings)
    await b.aclose()


async def test_cancelled_context_close_still_stops_playwright_and_frees_the_lock(settings):
    pw = FakePW()
    b = _browser(settings, pw)

    async def cancelled_close():
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        async with b.page():
            pw.contexts[0].close = cancelled_close
            raise RuntimeError("target closed")
    assert pw.stopped and _lock_free(settings)


# --- ChatGPTPage over a fake Playwright page ---------------------------------
class FakeResponse:
    def __init__(self, url, status=200, method="POST"):
        self.url, self.status = url, status
        self.request = SimpleNamespace(method=method)

    async def finished(self):
        await asyncio.Event().wait()  # the real stream never ends: attach must not depend on it


class FakeInput:
    """Entries are an accept string/None or a dict with accept/capture/multiple, like the real attributes."""

    def __init__(self, page, spec):
        self.page = page
        self.spec = spec if isinstance(spec, dict) else {"accept": spec}
        self.accept = self.spec.get("accept")

    async def get_attribute(self, name):
        if name == "accept":
            return self.accept
        if name == "capture":
            return self.spec.get("capture")
        if name == "multiple":
            return "" if self.spec.get("multiple") else None
        return None

    async def set_input_files(self, files):
        if len(files) > 1 and not self.spec.get("multiple"):
            raise RuntimeError("Locator.set_input_files: Error: Non-multiple file input can only accept single file")
        self.page.uploaded.append((self.accept, files))
        for r in self.page.on_upload(files):
            self.page.emit(r)


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    async def wait_for(self, **kw):
        return None

    async def click(self):
        self.page.events.append(("click", self.selector))

    async def count(self):
        return len(self.page.file_inputs)

    def nth(self, i):
        return FakeInput(self.page, self.page.file_inputs[i])


class FakePage:
    def __init__(self):
        self.url = "https://chatgpt.com/"
        self.events, self.uploaded, self.handlers, self.goto_errors = [], [], [], []
        self.file_inputs = ["image/*", "image/*,video/*", None]
        self.on_upload = lambda files: []
        self.api_calls = []
        self.api_handler = lambda path, method, body: (200, {})
        self.keyboard = SimpleNamespace(type=self._type, press=self._press)

    async def _type(self, text, delay=0):
        self.events.append(("type", text))

    async def _press(self, key):
        self.events.append(("press", key))

    async def goto(self, url, wait_until=None):
        self.events.append(("goto", url))
        if self.goto_errors:
            raise self.goto_errors.pop(0)
        self.url = url

    def locator(self, selector):
        return FakeLocator(self, selector)

    def on(self, event, fn):
        self.handlers.append(fn)

    def remove_listener(self, event, fn):
        self.handlers.remove(fn)

    def emit(self, resp):
        for fn in list(self.handlers):
            fn(resp)

    async def evaluate(self, script, arg=None):
        if script is cw.JS_API:
            path, method, body = arg
            self.api_calls.append((path, method, body))
            status, data = self.api_handler(path, method, body)
            return {"status": status, "json": data}
        if script is cw.JS_BYTES:
            return {"status": 200, "b64": base64.b64encode(b"IMG:" + arg.encode()).decode()}
        if script is cw.JS_TZ:
            return ["America/Bogota", 300]
        if script is cw.JS_SESSION:
            return {"logged_in": True, "plan": "free"}
        raise AssertionError(f"unexpected script {script[:40]!r}")


def _cpage(fake, **kw):
    async def no_sleep(s):
        await asyncio.sleep(0)
    return cw.ChatGPTPage(fake, sleep=no_sleep, **kw)


async def test_open_retries_err_aborted_once():
    fake = FakePage()
    fake.goto_errors = [RuntimeError("Page.goto: net::ERR_ABORTED at https://chatgpt.com/")]
    await _cpage(fake).open("https://chatgpt.com/")
    assert [e for e in fake.events if e[0] == "goto"] == [("goto", "https://chatgpt.com/")] * 2
    fake.goto_errors = [RuntimeError("net::ERR_ABORTED"), RuntimeError("net::ERR_ABORTED")]
    with pytest.raises(RuntimeError):
        await _cpage(fake).open("https://chatgpt.com/")


async def test_send_keeps_line_breaks_inside_the_message():
    fake = FakePage()
    await _cpage(fake).send("linea uno\n\nlinea dos")
    keys = [e for e in fake.events if e[0] in ("type", "press")]
    assert keys == [("type", "linea uno"), ("press", "Shift+Enter"), ("press", "Shift+Enter"),
                    ("type", "linea dos"), ("press", "Enter")]


async def test_attach_waits_for_one_finished_upload_per_file(tmp_path):
    fake = FakePage()
    fake.file_inputs = [{"accept": "image/*", "multiple": True}]
    up = "https://chatgpt.com" + cw.UPLOAD_PATH
    fake.on_upload = lambda files: [FakeResponse(up, method="GET"), FakeResponse(up, status=500),
                                    *[FakeResponse(up) for _ in files]]
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    await _cpage(fake).attach(paths)
    assert fake.uploaded == [("image/*", [str(p) for p in paths])]
    assert fake.handlers == []


async def test_attach_times_out_without_upload_signal(tmp_path):
    fake = FakePage()
    with pytest.raises(GIAError) as e:
        await _cpage(fake, upload_timeout_s=0.05, clock=iter_clock()).attach([tmp_path / "a.png"])
    assert e.value.code == "upstream_error" and e.value.details == {"expected": 1, "finished": 0}
    assert fake.handlers == []


def iter_clock():
    ticks = iter(x * 0.02 for x in range(10_000))
    return lambda: next(ticks)


async def test_attach_without_refs_does_nothing():
    fake = FakePage()
    await _cpage(fake).attach([])
    assert fake.uploaded == []


async def test_download_follows_the_signed_url():
    fake = FakePage()
    fake.api_handler = lambda path, method, body: (200, {"download_url": "https://chatgpt.com/backend-api/estuary/content?id=f"})
    data = await _cpage(fake).download("conv", "file_1")
    assert data == b"IMG:https://chatgpt.com/backend-api/estuary/content?id=f"
    assert fake.api_calls == [("/backend-api/files/download/file_1?conversation_id=conv&inline=false", "GET", None)]


async def test_attach_skips_the_camera_input_and_prefers_a_multiple_one(tmp_path):
    fake = FakePage()
    fake.file_inputs = [{"accept": "image/*", "capture": "environment"},
                        {"accept": "image/*,video/*"},
                        {"accept": "image/*", "multiple": True}]
    up = "https://chatgpt.com" + cw.UPLOAD_PATH
    fake.on_upload = lambda files: [FakeResponse(up) for _ in files]
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    await _cpage(fake).attach(paths)
    assert fake.uploaded == [("image/*", [str(p) for p in paths])]


async def test_attach_sets_files_one_per_call_without_a_multiple_input(tmp_path):
    fake = FakePage()
    fake.file_inputs = [{"accept": "image/*", "capture": "environment"}, {"accept": "image/*,video/*"}]
    up = "https://chatgpt.com" + cw.UPLOAD_PATH
    fake.on_upload = lambda files: [FakeResponse(up) for _ in files]
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    await _cpage(fake).attach(paths)
    assert fake.uploaded == [("image/*,video/*", [str(paths[0])]), ("image/*,video/*", [str(paths[1])])]


async def test_attach_without_an_upload_input_is_upstream_error(tmp_path):
    fake = FakePage()
    fake.file_inputs = [{"accept": "image/*", "capture": "environment"}, {"accept": "audio/*"}]
    with pytest.raises(GIAError) as e:
        await _cpage(fake).attach([tmp_path / "a.png"])
    assert e.value.code == "upstream_error" and fake.handlers == []


async def test_download_without_url_is_upstream_error():
    fake = FakePage()
    fake.api_handler = lambda path, method, body: (404, None)
    with pytest.raises(GIAError) as e:
        await _cpage(fake).download("conv", "file_1")
    assert e.value.code == "upstream_error"


async def test_image_quota_sends_the_page_timezone():
    fake = FakePage()
    fake.api_handler = lambda path, method, body: (200, {"limits_progress": [
        {"feature_name": "image_gen", "remaining": 2, "reset_after": "R"}]})
    assert await _cpage(fake).image_quota() == {"remaining": 2, "reset_after": "R"}
    path, method, body = fake.api_calls[0]
    assert (path, method) == ("/backend-api/conversation/init", "POST")
    assert body["timezone"] == "America/Bogota" and body["timezone_offset_min"] == 300


async def test_watch_blocking_collects_only_blocking_posts():
    fake = FakePage()
    page = _cpage(fake)
    with page.watch_blocking() as blocked:
        fake.emit(FakeResponse("https://chatgpt.com/backend-api/f/conversation", 403))
        fake.emit(FakeResponse("https://chatgpt.com/backend-api/conversation/x", 404, method="GET"))
    assert blocked == [("https://chatgpt.com/backend-api/f/conversation", 403)]
    assert fake.handlers == []


async def test_send_normalizes_carriage_returns():
    fake = FakePage()
    await _cpage(fake).send("uno\r\ndos\rtres")
    keys = [e for e in fake.events if e[0] in ("type", "press")]
    assert keys == [("type", "uno"), ("press", "Shift+Enter"), ("type", "dos"), ("press", "Shift+Enter"),
                    ("type", "tres"), ("press", "Enter")]
    assert not any("\r" in e[1] for e in keys)
