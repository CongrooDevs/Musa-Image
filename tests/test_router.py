import pytest

from musa.client import CHATGPT_MODEL, Backend, GenerateResult, RoutedBackend, append_record
from musa.state import StateStore


class Side(Backend):
    def __init__(self, name):
        self.name, self.calls, self.closed = name, [], False

    async def status(self):
        return {"valid": True, "who": self.name}

    async def list_models(self):
        return [{"model_name": f"{self.name}-m", "available": True}]

    async def resolve_model_name(self, name):
        return f"{self.name}:{name}"

    async def generate(self, prompt, refs, *, model, **kw):
        self.calls.append(("generate", model))
        return GenerateResult("ok", None, "c_x", [], self.name, 0.0, None)

    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        self.calls.append(("edit", conversation_id))
        return GenerateResult("ok", None, conversation_id, [], self.name, 0.0, None)

    async def retry_download(self, conversation_id, image_index):
        self.calls.append(("retry", conversation_id))

    async def delete_conversation(self, conversation_id):
        self.calls.append(("delete", conversation_id))

    async def create_gem(self, name, instructions, description=""):
        self.calls.append(("create_gem", name))
        return "gem_1"

    async def gem_exists(self, gem_id):
        return True

    async def quota(self):
        return {"window_5h_pct": 1, "who": self.name}

    async def aclose(self):
        self.closed = True


@pytest.fixture
def routed(settings):
    state = StateStore(settings.state_dir)
    append_record(state, type="generate", conversation_id="c_gpt", chat_metadata=["x"], model=CHATGPT_MODEL)
    append_record(state, type="generate", conversation_id="c_gem", chat_metadata=["a", "b", "c"], model="gemini-flash")
    g, c = Side("gemini"), Side("chatgpt")
    return RoutedBackend(g, c, state), g, c


async def test_generate_routes_by_model(routed):
    r, g, c = routed
    assert (await r.generate("p", [], model=CHATGPT_MODEL)).gemini_text == "chatgpt"
    assert (await r.generate("p", [], model="gemini-flash")).gemini_text == "gemini"
    assert (await r.generate("p", [], model=None)).gemini_text == "gemini"


async def test_conversation_operations_follow_the_recorded_model(routed):
    r, g, c = routed
    await r.edit("c_gpt", "x", [], model="gemini-flash")
    await r.retry_download("c_gpt", 0)
    await r.delete_conversation("c_gpt")
    await r.edit("c_gem", "x", [], model="gemini-flash")
    await r.edit("c_unknown", "x", [], model="gemini-flash")
    assert c.calls == [("edit", "c_gpt"), ("retry", "c_gpt"), ("delete", "c_gpt")]
    assert g.calls == [("edit", "c_gem"), ("edit", "c_unknown")]


async def test_models_status_quota_and_gems(routed):
    r, g, c = routed
    assert [m["model_name"] for m in await r.list_models()] == ["gemini-m", "chatgpt-m"]
    assert await r.resolve_model_name(CHATGPT_MODEL) == CHATGPT_MODEL
    assert await r.resolve_model_name("gemini-flash") == "gemini:gemini-flash"
    st = await r.status()
    assert st["who"] == "gemini" and st["chatgpt"]["who"] == "chatgpt"
    assert (await r.quota())["who"] == "gemini"
    assert await r.create_gem("n", "i") == "gem_1" and g.calls == [("create_gem", "n")]
    assert await r.gem_exists("gem_1") is True


async def test_status_survives_a_chatgpt_failure(routed):
    r, g, c = routed

    async def boom():
        raise RuntimeError("cookie db locked")
    c.status = boom
    st = await r.status()
    assert st["chatgpt"]["session"] is False and "cookie db locked" in st["chatgpt"]["error"]


async def test_aclose_closes_both(routed):
    r, g, c = routed
    await r.aclose()
    assert g.closed and c.closed
