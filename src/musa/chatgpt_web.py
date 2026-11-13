"""ChatGPT (web) provider: drives chatgpt.com in the user's Google Chrome (dedicated profile, headless).

The only module that imports playwright, lazily. Spec: docs/superpowers/specs/2026-11-13-chatgpt-web-provider-design.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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
