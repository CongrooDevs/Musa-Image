"""Upstream error hierarchy."""

class GeminiError(Exception):
    """Base class for all upstream Gemini errors."""

class AuthError(GeminiError):
    """Authentication, session expiration or guest mode error."""

class ImageGenerationError(GeminiError):
    """Content blocked or image generation refused by Gemini."""

class ModelInvalidError(GeminiError):
    """Requested model is invalid or unavailable."""

class TemporarilyBlockedError(GeminiError):
    """Rate limit reached or temporary 429 block."""

class UsageLimitExceededError(GeminiError):
    """Account quota limit exceeded."""

class TimeoutError(GeminiError):
    """Call to upstream endpoint timed out."""

class APIError(GeminiError):
    """Unclassified or RPC-level failure."""
