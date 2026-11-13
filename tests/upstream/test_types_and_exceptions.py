import pytest
from gemini_image_api.upstream.types import (
    AccountStatus, UpstreamModel, UpstreamGem, UpstreamChatSession, UpstreamImage, UpstreamTurn
)
from gemini_image_api.upstream.exceptions import (
    GeminiError, AuthError, ImageGenerationError, ModelInvalidError,
    TemporarilyBlockedError, UsageLimitExceededError, TimeoutError, APIError
)

def test_exceptions_hierarchy():
    assert issubclass(AuthError, GeminiError)
    assert issubclass(ImageGenerationError, GeminiError)
    assert issubclass(ModelInvalidError, GeminiError)
    assert issubclass(TemporarilyBlockedError, GeminiError)
    assert issubclass(UsageLimitExceededError, GeminiError)
    assert issubclass(TimeoutError, GeminiError)
    assert issubclass(APIError, GeminiError)

def test_account_status_values():
    assert AccountStatus.AVAILABLE == 1000
    assert AccountStatus.SUSPENDED == 1001

def test_upstream_model():
    m = UpstreamModel("gemini-flash", "Gemini Flash", True)
    assert m.model_name == "gemini-flash"
    assert m.display_name == "Gemini Flash"
    assert m.is_available is True

def test_upstream_gem():
    gem = UpstreamGem(id="gem_1", name="Musa", prompt="Style instructions", description="A gem")
    assert gem.id == "gem_1"
    assert gem.name == "Musa"

def test_upstream_turn_and_image():
    chat = UpstreamChatSession(cid="c1", rid="r1", rcid="rc1", gem="gem_1")
    img = UpstreamImage(url="https://example.com/img.png", cid="c1", rid="r1", rcid="rc1", image_id="img_1")
    turn = UpstreamTurn(text="A generated image", images=[img], chat=chat)

    assert turn.text == "A generated image"
    assert len(turn.images) == 1
    assert turn.images[0].url == "https://example.com/img.png"
    assert turn.chat.cid == "c1"
