"""Traffic and fixture sanitizer to prevent secret leakage in tests."""
import re
from typing import Any

_SNLM0E_RE = re.compile(r'\"SNlM0e\":\"([^\"]+)\"')
_SAPISID_HASH_RE = re.compile(r'SAPISIDHASH \d+_[0-9a-f]{40}')
_EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
class TrafficSanitizer:
    """Sanitizes HTTP traffic, stripping cookies, tokens, and authorization headers."""

    def __init__(self, cookies: list[dict]):
        self.replacements: list[tuple[str, str]] = []
        for c in cookies:
            val = c.get("value", "")
            name = c.get("name", "COOKIE")
            # Only consider values with sufficient entropy/length
            if len(val) >= 6:
                self.replacements.append((val, f"REDACTED_{name}"))

        # Sort descending by length to replace longer secrets first
        self.replacements.sort(key=lambda x: len(x[0]), reverse=True)
        self.extra_tokens: set[str] = set()

    def add_token(self, token: str, label: str = "TOKEN") -> None:
        if token and len(token) >= 6 and token not in self.extra_tokens:
            self.extra_tokens.add(token)
            self.replacements.append((token, f"REDACTED_{label}"))
            self.replacements.sort(key=lambda x: len(x[0]), reverse=True)

    def sanitize_text(self, text: str) -> str:
        if not text:
            return ""

        # Auto-detect SNlM0e tokens in text
        for match in _SNLM0E_RE.finditer(text):
            tok = match.group(1)
            self.add_token(tok, "SNLM0E")

        out = text
        for secret, placeholder in self.replacements:
            out = out.replace(secret, placeholder)

        # Redact any SAPISIDHASH pattern
        out = _SAPISID_HASH_RE.sub("SAPISIDHASH REDACTED_HASH", out)
        # Redact any personal email
        out = _EMAIL_RE.sub("redacted_user@example.com", out)
        return out

    def sanitize_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in data.items():
            k_lower = k.lower()
            if k_lower in ("set-cookie", "cookie", "authorization"):
                out[k] = f"REDACTED_{k_lower.upper()}"
            elif isinstance(v, str):
                out[k] = self.sanitize_text(v)
            elif isinstance(v, dict):
                out[k] = self.sanitize_dict(v)
            elif isinstance(v, list):
                out[k] = [
                    self.sanitize_dict(item) if isinstance(item, dict)
                    else self.sanitize_text(item) if isinstance(item, str)
                    else item
                    for item in v
                ]
            else:
                out[k] = v
        return out
