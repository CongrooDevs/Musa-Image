"""Native async Gemini client components."""
from .client import GeminiClient
from .types import AccountStatus, GeneratedImage, UpstreamImage, UpstreamGem, UpstreamModel, UpstreamTurn, UpstreamChatSession
from .exceptions import (
    GeminiError, AuthError, ImageGenerationError, ModelInvalidError,
    TemporarilyBlockedError, UsageLimitExceededError, TimeoutError, APIError
)

def set_log_level(level: str) -> None:
    """No-op logger level hook for upstream compatibility."""
    pass

__all__ = [
    "GeminiClient",
    "AccountStatus",
    "GeneratedImage",
    "UpstreamImage",
    "UpstreamGem",
    "UpstreamModel",
    "UpstreamTurn",
    "UpstreamChatSession",
    "GeminiError",
    "AuthError",
    "ImageGenerationError",
    "ModelInvalidError",
    "TemporarilyBlockedError",
    "UsageLimitExceededError",
    "TimeoutError",
    "APIError",
    "set_log_level",
]
