import io
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from PIL import Image

from musa import chatgpt_web as cw
from musa.client import CHATGPT_MODEL
from musa.errors import GIAError
from musa.refs import ResolvedRef
from musa.state import StateStore

CID = "6af76eed-1aec-83e9-b323-d348dba09bfa"


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 12), (200, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def _msg(mid, role, t, status="finished_successfully", parts=None):
    return {"id": mid, "author": {"role": role}, "create_time": t, "status": status,
            "content": {"content_type": "multimodal_text", "parts": parts or []}}


def _img(fid):
    return {"content_type": "image_asset_pointer", "asset_pointer": f"sediment://{fid}", "width": 16, "height": 12}


def _conv(*msgs):
    return {"mapping": {m["id"]: {"message": m} for m in msgs}}


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeBrowser:
    def __init__(self):
        self.uses, self.closed = 0, False

    @asynccontextmanager
    async def page(self):
        self.uses += 1
        yield "raw-page"

    async def aclose(self):
        self.closed = True


class FakeChatGPT:
    """Stands in for ChatGPTPage: scripted conversation polls, quota, uploads and downloads."""

    def __init__(self, *, logged_in=True, quota=(3, "2026-11-14T20:21:58Z"), polls=None, before=None,
                 blocked=None, download_fail=False):
        self.url = "https://chatgpt.com/"
        self.logged_in, self.quota = logged_in, quota
        self.polls = list(polls or [])
        self.before = before
        self.blocked = blocked or []
        self.download_fail = download_fail
        self.calls = []

    async def open(self, url):
        self.calls.append(("open", url))

    async def session(self):
        if self.logged_in == "cloudflare":
            return {"logged_in": False, "plan": None, "status": 403}
        return {"logged_in": self.logged_in, "plan": "free"}

    async def image_quota(self):
        if self.quota is None:
            return None
        return {"remaining": self.quota[0], "reset_after": self.quota[1]}

    async def api(self, path, method="GET", body=None):
        self.calls.append(("api", method, path, body))
        if method == "PATCH":
            return 200, {"success": True}
        if path == f"/backend-api/conversation/{CID}":
            if self.before is not None and not any(c[0] == "send" for c in self.calls):
                return 200, self.before
            conv = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return 200, conv
        raise AssertionError(path)

    async def attach(self, paths):
        self.calls.append(("attach", [Path(p).name for p in paths]))

    async def send(self, prompt):
        self.calls.append(("send", prompt))
        self.url = f"https://chatgpt.com/c/{CID}"

    async def download(self, conversation_id, file_id):
        self.calls.append(("download", conversation_id, file_id))
        if self.download_fail:
            raise GIAError("upstream_error", "link expired")
        return png()

    @contextmanager
    def watch_blocking(self):
        yield self.blocked


@pytest.fixture
def make(settings, tmp_path):
    settings.output_root = tmp_path / "out"

    def build(fake):
        ft = FakeTime()
        state = StateStore(settings.state_dir)
        b = cw.ChatGPTWebBackend(settings, state, browser=FakeBrowser(), page_factory=lambda raw: fake,
                                 clock=ft.clock, sleep=ft.sleep)
        return b, state, ft
    return build


def _done(*parts_ids):
    return _conv(_msg("u1", "user", 1, parts=["x"]),
                 *[_msg(f"t{i}", "tool", 2 + i, parts=[_img(fid)]) for i, fid in enumerate(parts_ids)])


async def test_generate_saves_image_and_records_chatgpt_conversation(make, settings, tmp_path):
    fake = FakeChatGPT(polls=[_conv(_msg("u1", "user", 1)), _done("file_out")])
    b, state, _ = make(fake)
    ref = ResolvedRef(path=tmp_path / "ref.png", source="local", sha256="abc")
    res = await b.generate("Generate an image (do not search the web): un gato", [ref], model=CHATGPT_MODEL,
                           slug_source="un gato")
    assert res.status == "ok" and res.code is None and len(res.images) == 1
    assert res.images[0].path.exists() and res.chat_metadata == [CID]
    rec = state.conversation(res.conversation_id)
    assert rec.model == CHATGPT_MODEL and rec.chat_metadata == [CID] and rec.type == "generate"
    assert rec.images[0]["image_id"] == "file_out" and rec.refs[0]["sha256"] == "abc"
    assert ("attach", ["ref.png"]) in fake.calls and ("open", "https://chatgpt.com/") in fake.calls
    st = await b.status()
    assert st["image_gen"]["remaining"] == 3 and st["image_gen"]["reset_after"] == "2026-11-14T20:21:58Z"


async def test_post_turn_quota_failure_does_not_discard_the_generation(make):
    class Flaky(FakeChatGPT):
        n = 0

        async def image_quota(self):
            self.n += 1
            if self.n > 1:
                raise RuntimeError("cloudflare html")
            return await super().image_quota()
    fake = Flaky(polls=[_conv(_msg("u1", "user", 1)), _done("file_out")])
    b, state, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "ok" and len(res.images) == 1 and res.images[0].path.exists()
    assert state.conversation(res.conversation_id).images[0]["image_id"] == "file_out"


async def test_edit_reopens_the_conversation_and_takes_only_new_images(make):
    old_user, old_tool = _msg("u1", "user", 1, parts=["x"]), _msg("t0", "tool", 2, parts=[_img("file_old")])
    before = _conv(old_user, old_tool)
    after = _conv(old_user, old_tool, _msg("u2", "user", 10), _msg("t9", "tool", 11, parts=[_img("file_new")]))
    b, state, _ = make(FakeChatGPT(polls=[before]))
    first = await b.generate("p", [], model=CHATGPT_MODEL)
    fake = FakeChatGPT(polls=[after], before=before)
    b.page_factory = lambda raw: fake
    res = await b.edit(first.conversation_id, "hazla verde", [], model="gemini-flash")
    assert res.status == "ok"
    rec = state.conversation(first.conversation_id)
    assert rec.type == "edit" and rec.parent_id == first.record_id and rec.images[0]["image_id"] == "file_new"
    assert ("open", f"https://chatgpt.com/c/{CID}") in fake.calls


async def test_edit_unknown_conversation(make):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    with pytest.raises(GIAError) as e:
        await b.edit("c_missing", "x", [], model=CHATGPT_MODEL)
    assert e.value.code == "unknown_conversation"


async def test_not_logged_in_is_auth_expired_without_record(make, settings):
    b, state, _ = make(FakeChatGPT(logged_in=False, polls=[_done("f")]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "error" and res.code == "auth_expired"
    assert state.load_all() == []


async def test_cloudflare_on_the_session_check_is_temporarily_blocked(make, settings):
    b, state, _ = make(FakeChatGPT(logged_in="cloudflare", polls=[_done("f")]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "temporarily_blocked" and state.load_all() == []


async def test_exhausted_quota_is_usage_limit(make):
    fake = FakeChatGPT(quota=(0, "R"), polls=[_done("f")])
    b, _, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "usage_limit" and not any(c[0] == "send" for c in fake.calls)


async def test_blocked_send_is_temporarily_blocked(make):
    fake = FakeChatGPT(blocked=[("https://chatgpt.com/backend-api/f/conversation", 403)], polls=[_done("f")])
    b, _, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "temporarily_blocked"


async def test_text_reply_without_tool_becomes_no_image_after_30s(make):
    refusal = _conv(_msg("u1", "user", 1), _msg("a1", "assistant", 2, parts=["No puedo generar eso."]))
    b, state, ft = make(FakeChatGPT(polls=[refusal]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "error" and res.code == "no_image" and res.gemini_text == "No puedo generar eso."
    assert ft.t >= cw.NO_IMAGE_STABLE_S
    assert state.load_all()[-1].code == "no_image"


async def test_pending_tool_message_is_not_no_image(make):
    pending = _conv(_msg("u1", "user", 1), _msg("a1", "assistant", 2, parts=["Creando imagen"]),
                    _msg("t1", "tool", 3, status="in_progress"))
    polls = [pending] * 12 + [_done("file_late")]
    b, _, _ = make(FakeChatGPT(polls=polls))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "ok"


async def test_empty_reasoning_message_is_not_no_image(make):
    thoughts = _conv(_msg("u1", "user", 1), _msg("a1", "assistant", 2, parts=[]))
    polls = [thoughts] * 12 + [_done("file_late")]
    b, _, ft = make(FakeChatGPT(polls=polls))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "ok" and ft.t > cw.NO_IMAGE_STABLE_S


async def test_no_image_until_timeout_is_timeout(make, settings):
    settings.gen_timeout_s = 20
    b, _, _ = make(FakeChatGPT(polls=[_conv(_msg("u1", "user", 1))]))
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.code == "timeout"


async def test_download_failure_then_retry_download(make):
    fake = FakeChatGPT(polls=[_done("file_out")], download_fail=True)
    b, state, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    assert res.status == "generated_not_saved" and res.code == "generated_not_saved"
    assert state.conversation(res.conversation_id).images[0]["path"] is None
    fake.download_fail = False
    saved = await b.retry_download(res.conversation_id, 0)
    assert saved.path.exists()
    rec = state.conversation(res.conversation_id)
    assert rec.type == "retry_download" and rec.status == "ok" and rec.images[0]["path"] == str(saved.path)
    assert ("download", CID, "file_out") in fake.calls


async def test_gem_is_ignored_with_its_own_warning_code(make):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    res = await b.generate("p", [], model=CHATGPT_MODEL, gem_id="gem_1")
    assert res.status == "ok" and res.warning["code"] == "gem_ignored" and res.warning["gem_id"] == "gem_1"


async def test_delete_conversation_hides_it(make):
    fake = FakeChatGPT(polls=[_done("f")])
    b, state, _ = make(fake)
    res = await b.generate("p", [], model=CHATGPT_MODEL)
    await b.delete_conversation(res.conversation_id)
    assert ("api", "PATCH", f"/backend-api/conversation/{CID}", {"is_visible": False}) in fake.calls
    assert state.conversation(res.conversation_id) is None


async def test_list_models_reflects_the_session_cookie(make, monkeypatch):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    monkeypatch.setattr("musa.auth.chatgpt_session_present", lambda p: True)
    assert await b.list_models() == [{"model_name": "chatgpt-web", "display_name": "ChatGPT (web)", "available": True}]
    monkeypatch.setattr("musa.auth.chatgpt_session_present", lambda p: False)
    assert (await b.list_models())[0]["available"] is False
    assert (await b.status())["session"] is False
    assert await b.resolve_model_name("chatgpt-web") == "chatgpt-web"


async def test_aclose_closes_the_browser(make):
    b, _, _ = make(FakeChatGPT(polls=[_done("f")]))
    await b.aclose()
    assert b.browser.closed
