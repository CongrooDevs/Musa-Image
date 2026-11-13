import asyncio
import enum
import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


# --- fake `gemini_webapi` (installed only while importing client; never touches the network) ---
def _install_fake_upstream():
    names = ["gemini_webapi", "gemini_webapi.constants", "gemini_webapi.exceptions",
             "gemini_webapi.types", "gemini_webapi.types.image"]
    saved = {n: sys.modules.get(n) for n in names}
    saved["musa.client"] = sys.modules.pop("musa.client", None)

    pkg = types.ModuleType("gemini_webapi")
    const = types.ModuleType("gemini_webapi.constants")
    exc = types.ModuleType("gemini_webapi.exceptions")
    typ = types.ModuleType("gemini_webapi.types")
    img = types.ModuleType("gemini_webapi.types.image")

    from musa.upstream import (
        AccountStatus,
        GeneratedImage as _RealGeneratedImage,
        AuthError, ImageGenerationError, ModelInvalidError,
        TemporarilyBlockedError, TimeoutError, UsageLimitExceededError,
        APIError, GeminiError,
    )
    const.AccountStatus = AccountStatus
    exc.GeminiError = GeminiError
    exc.AuthError = AuthError
    exc.ImageGenerationError = ImageGenerationError
    exc.ModelInvalidError = ModelInvalidError
    exc.TemporarilyBlockedError = TemporarilyBlockedError
    exc.TimeoutError = TimeoutError
    exc.UsageLimitExceededError = UsageLimitExceededError
    exc.APIError = APIError

    class WebImage:
        def __init__(self, url="", **kw): self.url = url

    class GeneratedImage(_RealGeneratedImage, WebImage):
        async def save(self, path="temp", filename=None, verbose=False, **kw):
            p = Path(path) / (filename or "img.png")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(png_bytes())
            return str(p.resolve())

    img.WebImage, img.GeneratedImage = WebImage, GeneratedImage
    pkg.GeminiClient = type("GeminiClient", (), {})
    pkg.set_log_level = lambda level: None
    typ.image = img
    pkg.exceptions, pkg.types, pkg.constants = exc, typ, const
    sys.modules.update({"gemini_webapi": pkg, "gemini_webapi.constants": const,
                        "gemini_webapi.exceptions": exc,
                        "gemini_webapi.types": typ, "gemini_webapi.types.image": img})
    import musa
    prev_attr = getattr(musa, "client", None)
    try:
        import musa.client as mod
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
        if prev_attr is not None:  # re-bind the package to the real module, not the fakes
            musa.client = prev_attr
        elif hasattr(musa, "client"):
            delattr(musa, "client")
    return mod, exc, img


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (0, 128, 255)).save(buf, "PNG")
    return buf.getvalue()


client_mod, exc, upstream_img = _install_fake_upstream()
GeneratedImage, WebImage = upstream_img.GeneratedImage, upstream_img.WebImage

from musa.errors import GIAError, UPSTREAM_ERROR  # noqa: E402
from musa.state import StateStore  # noqa: E402

GeminiWebBackend, build_backend = client_mod.GeminiWebBackend, client_mod.build_backend


class FakeOutput:
    def __init__(self, images, text="ok"):
        self.images, self.text = images, text


class FakeChat:
    def __init__(self, metadata=None):
        self.metadata = metadata or ["cid1", "rid1", "rcid1"]


class FakeGem:
    def __init__(self, id, name="", prompt="", description="", predefined=False):
        self.id, self.name, self.prompt, self.description = id, name, prompt, description
        self.predefined = predefined


class FakeClient:
    instances = []

    def __init__(self, secure_1psid=None, secure_1psidts=None, **kw):
        self.psid = secure_1psid
        self.chats = []
        self.gems = {}
        self.fetch_gems_calls = 0
        self.gen_gem_kwargs = []
        self.gen_temporary = []
        FakeClient.instances.append(self)

    async def init(self, **kw): pass

    def list_models(self):  # sync in gemini-webapi 2.1.1
        return [{"model_name": "gemini-flash", "is_available": True},
                {"model_name": "gemini-pro", "is_available": True}]

    def start_chat(self, **kw):
        c = FakeChat(kw.get("metadata"))
        c.kw = kw
        self.chats.append(c)
        return c

    async def generate_content(self, prompt, files=None, model=None, chat=None, **kw):
        self.gen_gem_kwargs.append(kw.get("gem"))
        self.gen_temporary.append(kw.get("temporary"))
        return FakeOutput([GeneratedImage(url="u", cid="cid1", rid="rid1", rcid="rcid1", image_id="im1")],
                          text="image created")

    async def fetch_gems(self, **kw):
        self.fetch_gems_calls += 1
        return list(self.gems.values())

    async def create_gem(self, name, prompt, description=""):
        g = FakeGem(f"gem_{len(self.gems) + 1:03d}", name=name, prompt=prompt, description=description)
        self.gems[g.id] = g
        return g

    async def update_gem(self, gem, name, prompt, description=""):
        gid = gem if isinstance(gem, str) else gem.id
        g = self.gems[gid]
        g.name, g.prompt, g.description = name, prompt, description
        return g

    async def delete_gem(self, gem, **kw):
        gid = gem if isinstance(gem, str) else gem.id
        self.gems.pop(gid, None)

    async def download_image(self, url, **kw):
        return png_bytes()

    async def read_chat(self, cid, **kw):
        return SimpleNamespace(turns=[
            SimpleNamespace(
                model_output=FakeOutput(
                    [GeneratedImage(url="u1", cid="cid1", rid="rid1", rcid="rcid1", image_id="im1")]
                )
            )
        ])


@pytest.fixture
def backend_factory(settings, monkeypatch):
    settings.min_delay_s = 0
    settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)
    settings.cookie_file.write_text(json.dumps([
        {"name": "__Secure-1PSID", "value": "a", "domain": ".google.com"},
        {"name": "__Secure-1PSIDTS", "value": "b", "domain": ".google.com"}]))
    FakeClient.instances.clear()

    def make(client=FakeClient):
        monkeypatch.setattr(client_mod, "_default_client_cls", client)
        st = StateStore(settings.state_dir)
        return GeminiWebBackend(settings, st), st
    return make


async def test_generate_ok(backend_factory):
    b, st = backend_factory()
    res = await b.generate("a cat astronaut", [], model="gemini-flash")
    assert res.status == "ok" and res.code is None, res
    assert res.conversation_id and res.chat_metadata == ["cid1", "rid1", "rcid1"]
    assert len(res.images) == 1 and res.images[0].path.exists()
    rec = st.conversation(res.conversation_id)
    assert rec is not None and rec.status == "ok" and rec.images[0]["path"] == str(res.images[0].path)
    assert rec.images[0]["image_id"] == "im1"
    assert res.record_id == rec.id
    res2 = await b.generate("PREFIXED", [], model="gemini-flash", slug_source="un gato")
    assert st.conversation(res2.conversation_id).user_prompt == "un gato"


async def test_temp_chats_default_on(backend_factory):
    b, _ = backend_factory()
    await b.generate("x", [], model="gemini-flash")
    assert FakeClient.instances[-1].gen_temporary == [True]  # por defecto, chats temporales


def test_temp_chats_env_override(monkeypatch):
    monkeypatch.setenv("GIA_TEMP_CHATS", "false")
    from musa.config import load_settings
    assert load_settings().temp_chats is False


async def test_generate_filters_web_images(backend_factory):
    class Mixed(FakeClient):
        async def generate_content(self, *a, **k):
            return FakeOutput([WebImage(url="w"), GeneratedImage(url="u")])
    b, _ = backend_factory(Mixed)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "ok" and len(res.images) == 1


async def test_generate_no_image(backend_factory):
    class NoImg(FakeClient):
        async def generate_content(self, *a, **k): return FakeOutput([], text="cannot do that")
    b, _ = backend_factory(NoImg)
    res = await b.generate("blocked prompt", [], model="gemini-flash")
    assert res.status == "error" and res.code == "no_image" and res.gemini_text == "cannot do that"


async def test_generate_strips_image_placeholder_residue(backend_factory):
    # Real Gemini reply (2026-11-09): upstream left `_910` where the image placeholder link was.
    class Real(FakeClient):
        async def generate_content(self, *a, **k):
            return FakeOutput([GeneratedImage(url="u", cid="cid1", rid="rid1", rcid="rcid1", image_id="im1")],
                              text="Here is a photorealistic image of an astronaut cat:\n\n_910\n\n")
    b, st = backend_factory(Real)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.gemini_text == "Here is a photorealistic image of an astronaut cat:"
    assert st.conversation(res.conversation_id).gemini_text == res.gemini_text


async def test_edit_reuses_chat_metadata(backend_factory):
    b, st = backend_factory()
    r1 = await b.generate("base", [], model="gemini-flash")
    r2 = await b.edit(r1.conversation_id, "add glasses", [], model="gemini-flash")
    assert r2.status == "ok" and r2.conversation_id == r1.conversation_id
    assert FakeClient.instances[-1].chats[-1].kw["metadata"] == ["cid1", "rid1", "rcid1"]
    assert st.conversation(r1.conversation_id).type == "edit"


async def test_edit_unknown_conversation(backend_factory):
    b, _ = backend_factory()
    with pytest.raises(GIAError) as e:
        await b.edit("c_nope", "x", [], model="gemini-flash")
    assert e.value.code == "unknown_conversation"


async def test_timeout_mapped(backend_factory, settings):
    class Slow(FakeClient):
        async def generate_content(self, *a, **k):
            await asyncio.sleep(10)
    settings.gen_timeout_s = 0.2
    b, _ = backend_factory(Slow)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "error" and res.code == "timeout"


@pytest.mark.parametrize("name,code", [
    ("UsageLimitExceededError", "usage_limit"), ("TemporarilyBlockedError", "temporarily_blocked"),
    ("ImageGenerationError", "content_blocked"), ("ModelInvalidError", "unknown_model"),
    ("AuthError", "auth_expired"), ("APIError", "upstream_error")])
async def test_error_mapping(backend_factory, name, code):
    class Boom(FakeClient):
        async def generate_content(self, *a, **k): raise getattr(exc, name)("boom")
    b, _ = backend_factory(Boom)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "error" and res.code == code


async def test_auth_error_rebuilds_client_once(backend_factory):
    class Flaky(FakeClient):
        async def generate_content(self, *a, **k):
            if len(FakeClient.instances) < 2:
                raise exc.AuthError("expired")
            return await super().generate_content(*a, **k)
    b, _ = backend_factory(Flaky)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "ok" and len(FakeClient.instances) == 2


async def test_usage_limit_not_retried(backend_factory):
    calls = []

    class Limited(FakeClient):
        async def generate_content(self, *a, **k):
            calls.append(1)
            raise exc.UsageLimitExceededError("q")
    b, _ = backend_factory(Limited)
    await b.generate("x", [], model="gemini-flash")
    assert len(calls) == 1 and len(FakeClient.instances) == 1


async def test_delete_conversation(backend_factory):
    deleted = []

    class Del(FakeClient):
        async def delete_chat(self, cid): deleted.append(cid)
    b, st = backend_factory(Del)
    r = await b.generate("x", [], model="gemini-flash")
    await b.delete_conversation(r.conversation_id)
    assert deleted == ["cid1"] and st.conversation(r.conversation_id) is None


class _FailingSave(GeneratedImage):
    async def save(self, *a, **k): raise RuntimeError("net down")


async def test_retry_download_rebuilds_image(backend_factory):
    class Fail(FakeClient):
        async def generate_content(self, *a, **k):
            return FakeOutput([_FailingSave(url="u1", cid="cid1", rid="rid1", rcid="rcid1", image_id="im1")])
    b, st = backend_factory(Fail)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "generated_not_saved" and res.code == "generated_not_saved" and res.images == []
    rec = st.conversation(res.conversation_id)
    assert rec.images[0]["path"] is None and rec.images[0]["image_id"] == "im1" and rec.images[0]["url"] == "u1"
    saved = await b.retry_download(res.conversation_id, 0)
    assert saved.path.exists()
    rec2 = st.conversation(res.conversation_id)
    assert rec2.status == "ok" and rec2.images[0]["path"] == str(saved.path)


async def test_retry_download_falls_back_to_read_chat(backend_factory, monkeypatch):
    class Hist:
        turns = [types.SimpleNamespace(role="model", text="", model_output=types.SimpleNamespace(
            images=[GeneratedImage(url="fresh", image_id="im1")]))]

    class Fail(FakeClient):
        async def generate_content(self, *a, **k):
            return FakeOutput([_FailingSave(url="u1", image_id="im1")])

        async def read_chat(self, cid, limit=10):
            assert cid == "cid1"
            return Hist()
    b, st = backend_factory(Fail)
    res = await b.generate("x", [], model="gemini-flash")
    # rebuilding from stored fields fails (client_ref is only passed on rebuild); history image saves fine
    orig_init = GeneratedImage.__init__

    def init(self, *a, client_ref=None, **k):
        if client_ref is not None:
            raise ValueError("cannot rebuild")
        orig_init(self, *a, **k)
    monkeypatch.setattr(GeneratedImage, "__init__", init)
    saved = await b.retry_download(res.conversation_id, 0)
    assert saved.path.exists()


async def test_retry_download_unknown(backend_factory):
    b, _ = backend_factory()
    with pytest.raises(GIAError) as e:
        await b.retry_download("c_nope", 0)
    assert e.value.code == "unknown_conversation"


async def test_status_and_list_models(backend_factory):
    b, _ = backend_factory()
    s = await b.status()
    assert s["valid"] is True and "gemini-flash" in s["models"]
    assert {"model_name": "gemini-pro", "available": True} in await b.list_models()


async def test_resolve_model_name(backend_factory):
    class Resolving(FakeClient):
        def resolve_model(self, name):
            if name == "flash":
                return SimpleNamespace(model_name="gemini-flash")
            raise ValueError(f"Unknown model name: {name}")

    b, _ = backend_factory(Resolving)
    assert await b.resolve_model_name("flash") == "gemini-flash"
    assert await b.resolve_model_name("nope") == "nope"  # unresolved -> unchanged


async def test_resolve_model_name_without_upstream_resolver(backend_factory):
    b, _ = backend_factory()  # FakeClient has no resolve_model
    assert await b.resolve_model_name("gemini-flash") == "gemini-flash"


async def test_list_models_includes_display_name_when_present(backend_factory):
    class Rich(FakeClient):
        def list_models(self):
            return [SimpleNamespace(model_name="gemini-pro", is_available=True, display_name="Razonamiento"),
                    SimpleNamespace(model_name="gemini-flash", is_available=True, display_name="")]

    b, _ = backend_factory(Rich)
    assert await b.list_models() == [
        {"model_name": "gemini-pro", "available": True, "display_name": "Razonamiento"},
        {"model_name": "gemini-flash", "available": True}]


async def test_build_backend(settings, monkeypatch):
    monkeypatch.setattr(client_mod, "_default_client_cls", FakeClient)
    b = build_backend(settings)
    assert isinstance(b, client_mod.RoutedBackend) and isinstance(b.gemini, GeminiWebBackend)
    assert type(b.chatgpt).__name__ == "ChatGPTWebBackend"


async def test_status_not_blocked_during_slow_generation(backend_factory):
    started = asyncio.Event()

    class Slow(FakeClient):
        async def generate_content(self, *a, **k):
            started.set()
            await asyncio.sleep(0.5)
            return await super().generate_content(*a, **k)
    b, _ = backend_factory(Slow)
    gen = asyncio.create_task(b.generate("x", [], model="gemini-flash"))
    await started.wait()
    s = await asyncio.wait_for(b.status(), timeout=2)
    assert s["valid"] is True
    assert (await gen).status == "ok"


async def test_status_maps_raw_upstream_exceptions(backend_factory):
    class BadInit(FakeClient):
        async def init(self, **kw): raise exc.AuthError("cookies rejected")
    b, _ = backend_factory(BadInit)
    s = await b.status()
    assert s["valid"] is False and s["code"] == "auth_expired" and "cookies rejected" in s["message"]


async def test_auth_rebuild_closes_old_client(backend_factory):
    closed = []

    class Flaky(FakeClient):
        async def close(self): closed.append(self)

        async def generate_content(self, *a, **k):
            if len(FakeClient.instances) < 2:
                raise exc.AuthError("expired")
            return await super().generate_content(*a, **k)
    b, _ = backend_factory(Flaky)
    assert (await b.generate("x", [], model="gemini-flash")).status == "ok"
    assert closed == [FakeClient.instances[0]]


def test_cookie_cache_path_points_into_state_dir(backend_factory, settings, monkeypatch):
    import os
    monkeypatch.setenv("GEMINI_COOKIE_PATH", "x")  # registers cleanup so the backend's setdefault can't leak
    monkeypatch.delenv("GEMINI_COOKIE_PATH")
    backend_factory()
    assert os.environ["GEMINI_COOKIE_PATH"] == str(settings.cookie_dir / "upstream_cache")



def test_upstream_cache_dir_is_0700_and_existing_ones_are_fixed(backend_factory, settings):
    import os, stat
    cache = settings.cookie_dir / "upstream_cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.chmod(cache, 0o755)
    leaked = cache / ".cached_cookies_x.json"
    leaked.write_text("{}")
    os.chmod(leaked, 0o644)
    backend_factory()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(leaked.stat().st_mode) == 0o600


def test_upstream_cache_dir_created_0700(backend_factory, settings):
    import stat
    backend_factory()
    assert stat.S_IMODE((settings.cookie_dir / "upstream_cache").stat().st_mode) == 0o700


async def test_auth_rebuild_clears_client_under_client_lock(backend_factory):
    import asyncio
    seen = []

    class Flaky(FakeClient):
        async def generate_content(self, *a, **k):
            if len(FakeClient.instances) < 2:
                await b._client_lock.acquire()  # simulate a concurrent _ensure_client holding the lock

                def release():
                    seen.append(b._client is not None)  # still set: nobody cleared it without the lock
                    b._client_lock.release()
                asyncio.get_running_loop().call_later(0.05, release)
                raise exc.AuthError("expired")
            return await super().generate_content(*a, **k)
    b, _ = backend_factory(Flaky)
    assert (await b.generate("x", [], model="gemini-flash")).status == "ok"
    assert seen == [True]

async def test_slug_comes_from_user_prompt_not_prefix(backend_factory):
    from musa.styles import GENERATE_PREFIX
    b, _ = backend_factory()
    r1 = await b.generate(GENERATE_PREFIX + "un gato", [], model="gemini-flash")
    assert "un-gato" in r1.images[0].path.name and "generate-an-image" not in r1.images[0].path.name
    r2 = await b.generate(GENERATE_PREFIX + "un gato oscuro", [], model="gemini-flash",
                          slug_source="un perro")
    assert "un-perro" in r2.images[0].path.name


USAGE = {"tier": {"id": 2, "label": "PRO"},
         "current_5h": {"usage_percentage": 4, "reset_at": "2026-11-10T03:28:32-05:00"},
         "weekly": {"usage_percentage": 2, "reset_at": "2026-11-12T10:28:32-05:00"}}


def test_normalize_quota():
    from musa.client import normalize_quota
    assert normalize_quota(USAGE) == {"tier": "PRO", "window_5h_pct": 4, "weekly_pct": 2,
                                      "resets_at_5h": "2026-11-10T03:28:32-05:00",
                                      "resets_at_weekly": "2026-11-12T10:28:32-05:00"}
    assert normalize_quota({}) is None and normalize_quota(None) is None
    assert normalize_quota({"tier": {}}) is None


async def test_quota_and_status(backend_factory):
    class Q(FakeClient):
        usage_info = USAGE
    b, _ = backend_factory(Q)
    assert (await b.quota())["window_5h_pct"] == 4
    st = await b.status()
    assert st["valid"] and st["quota"]["tier"] == "PRO"
    assert "account_status" in st and "cookie_age_s" in st


async def test_save_error_reason_is_reported(backend_factory, monkeypatch):
    b, _ = backend_factory()
    def boom(*a, **k): raise OSError("[Errno 30] Read-only file system: '/generated'")
    monkeypatch.setattr(b, "_store", boom)
    res = await b.generate("x", [], model="gemini-flash")
    assert res.status == "generated_not_saved" and "Read-only file system" in res.save_error


async def test_rebuilds_client_after_upstream_auto_close(backend_factory):
    b, _ = backend_factory()
    first = await b._ensure_client()
    first._running = False                       # upstream auto_close fired
    assert (await b._ensure_client()) is not first


async def test_aclose_closes_and_forgets_client(backend_factory):
    closed = []
    class C(FakeClient):
        async def close(self, *a): closed.append(True)
    b, _ = backend_factory(C)
    await b._ensure_client()
    await b.aclose()
    assert closed == [True] and b._client is None


async def test_create_gem_calls_upstream_and_returns_id(backend_factory):
    b, _ = backend_factory()
    gid = await b.create_gem("Mi estilo", "instrucciones", "desc")
    c = FakeClient.instances[-1]
    assert gid in c.gems
    assert c.gems[gid].name == "Mi estilo" and c.gems[gid].prompt == "instrucciones"
    assert c.gems[gid].description == "desc"


async def test_update_and_delete_gem_call_upstream(backend_factory):
    b, _ = backend_factory()
    gid = await b.create_gem("N", "p", "d")
    await b.update_gem(gid, "N2", "p2", "d2")
    c = FakeClient.instances[-1]
    assert (c.gems[gid].name, c.gems[gid].prompt, c.gems[gid].description) == ("N2", "p2", "d2")
    await b.delete_gem(gid)
    assert gid not in c.gems


async def test_gem_exists_caches_fetch_for_60s(backend_factory):
    b, _ = backend_factory()
    c = await b._ensure_client()
    c.gems["gx"] = FakeGem("gx")
    t = [1000.0]
    b._clock = lambda: t[0]
    assert await b.gem_exists("gx") is True
    assert await b.gem_exists("nope") is False
    assert c.fetch_gems_calls == 1
    t[0] += 61
    assert await b.gem_exists("gx") is True
    assert c.fetch_gems_calls == 2


async def test_failed_gem_op_still_invalidates_cache(backend_factory):
    class FailDelete(FakeClient):
        async def delete_gem(self, gem, **kw): raise exc.APIError("already gone")
    b, _ = backend_factory(FailDelete)
    c = await b._ensure_client()
    c.gems["gx"] = FakeGem("gx")
    assert await b.gem_exists("gx") is True
    assert c.fetch_gems_calls == 1
    with pytest.raises(GIAError) as e:
        await b.delete_gem("gx")
    assert e.value.code == UPSTREAM_ERROR
    assert await b.gem_exists("gx") is True  # cache was invalidated -> refetched
    assert c.fetch_gems_calls == 2


async def test_generate_with_existing_gem_uses_it_and_records(backend_factory):
    b, st = backend_factory()
    c = await b._ensure_client()
    c.gems["gx"] = FakeGem("gx")
    res = await b.generate("x", [], model="gemini-flash", gem_id="gx")
    assert res.status == "ok" and res.warning is None
    assert c.chats[-1].kw.get("gem") == "gx" and c.gen_gem_kwargs[-1] == "gx"
    assert st.conversation(res.conversation_id).gem_id == "gx"


async def test_generate_with_missing_gem_warns_and_calls_without_it(backend_factory):
    b, st = backend_factory()
    res = await b.generate("x", [], model="gemini-flash", gem_id="gone")
    c = FakeClient.instances[-1]
    assert res.status == "ok" and res.warning and res.warning["code"] == "gem_missing"
    assert "gem" not in c.chats[-1].kw and c.gen_gem_kwargs[-1] is None
    assert st.conversation(res.conversation_id).gem_id is None


async def test_generate_with_gem_fetch_error_still_uses_gem(backend_factory):
    class NoFetch(FakeClient):
        async def fetch_gems(self, **kw): raise exc.APIError("net down")
    b, st = backend_factory(NoFetch)
    res = await b.generate("x", [], model="gemini-flash", gem_id="gx")
    c = FakeClient.instances[-1]
    assert res.status == "ok" and res.warning is None
    assert c.chats[-1].kw.get("gem") == "gx" and c.gen_gem_kwargs[-1] == "gx"
    assert st.conversation(res.conversation_id).gem_id == "gx"


async def test_edit_reuses_conversation_gem_over_param(backend_factory):
    b, st = backend_factory()
    c = await b._ensure_client()
    c.gems["gx"] = FakeGem("gx")
    r1 = await b.generate("base", [], model="gemini-flash", gem_id="gx")
    c.chats.clear()
    c.gen_gem_kwargs.clear()
    r2 = await b.edit(r1.conversation_id, "more", [], model="gemini-flash", gem_id="other")
    assert c.chats[-1].kw.get("gem") == "gx" and c.gen_gem_kwargs[-1] == "gx"
    assert r2.warning is None and st.conversation(r1.conversation_id).gem_id == "gx"


async def test_retry_download_copies_gem_id(backend_factory):
    class Fail(FakeClient):
        async def generate_content(self, *a, **k):
            return FakeOutput([_FailingSave(url="u1", cid="cid1", rid="rid1", rcid="rcid1", image_id="im1")])
    b, st = backend_factory(Fail)
    c = await b._ensure_client()
    c.gems["gx"] = FakeGem("gx")
    res = await b.generate("x", [], model="gemini-flash", gem_id="gx")
    assert res.status == "generated_not_saved"
    await b.retry_download(res.conversation_id, 0)
    assert st.conversation(res.conversation_id).gem_id == "gx"


async def test_missing_gem_warning_kept_on_error_result(backend_factory):
    class Boom(FakeClient):
        async def generate_content(self, *a, **k): raise exc.APIError("upstream down")
    b, _ = backend_factory(Boom)
    res = await b.generate("x", [], model="gemini-flash", gem_id="gone")
    assert res.status == "error" and res.code == "upstream_error"
    assert res.warning and res.warning["code"] == "gem_missing"
