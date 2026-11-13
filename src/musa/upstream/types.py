"""Data models and contract types for the native upstream client."""
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

class AccountStatus(IntEnum):
    AVAILABLE = 1000
    SUSPENDED = 1001

@dataclass
class UpstreamModel:
    model_name: str
    display_name: str = ""
    is_available: bool = True

@dataclass
class UpstreamGem:
    id: str
    name: str
    prompt: str = ""
    description: str = ""

@dataclass
class UpstreamChatSession:
    cid: str = ""
    rid: str = ""
    rcid: str = ""
    gem: str | None = None
    metadata: list = field(default_factory=list)
@dataclass
class UpstreamImage:
    url: str
    cid: str = ""
    rid: str = ""
    rcid: str = ""
    image_id: str = ""
class GeneratedImage(UpstreamImage):
    def __init__(
        self,
        url: str = "",
        cid: str = "",
        rid: str = "",
        rcid: str = "",
        image_id: str = "",
        client_ref: Any = None,
        **kwargs,
    ):
        super().__init__(url=url, cid=cid, rid=rid, rcid=rcid, image_id=image_id)
        self.client_ref = client_ref

    async def save(self, path: str = "temp", full_size: bool = True, filename: str | None = None, **kwargs) -> str:
        from pathlib import Path
        dest = Path(path) / (filename or f"{self.image_id or 'gemini_image'}.png")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (self.client_ref and hasattr(self.client_ref, "download_image")):
            from .exceptions import APIError
            raise APIError("No client available to download image")
        data = await self.client_ref.download_image(self.url, full_size=full_size)
        dest.write_bytes(data)
        return str(dest.resolve())

@dataclass
class UpstreamTurn:
    text: str
    images: list[UpstreamImage] = field(default_factory=list)
    chat: UpstreamChatSession = field(default_factory=UpstreamChatSession)
