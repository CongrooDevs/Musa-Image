"""High-level async Gemini client for images and Gems."""
import asyncio
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from .session import UpstreamSession
from .upload import upload_reference_image
from .batchexecute import get_rpc_result, build_stream_generate_req, parse_stream_generate_response
from .types import AccountStatus, UpstreamModel, UpstreamGem, UpstreamChatSession, UpstreamImage, UpstreamTurn
from .exceptions import (
    AuthError, APIError, ImageGenerationError, TemporarilyBlockedError,
    UsageLimitExceededError, ModelInvalidError, TimeoutError as UpstreamTimeout
)

RPC_LIST_MODELS = "otAQ7b"
RPC_LIST_GEMS = "CNgdBe"
RPC_CREATE_GEM = "oMH3Zd"
RPC_UPDATE_GEM = "kHv0Vd"
RPC_DELETE_GEM = "UXcSJb"
RPC_DELETE_CHAT = "GzXR5e"

class GeminiClient:
    """Async client replacing gemini-webapi for image generation and Gem management."""

    def __init__(
        self,
        secure_1psid: str,
        secure_1psidts: str | None = None,
        proxy: str | None = None,
        cache_dir: Any = None,
        **kwargs,
    ):
        cookies = {"__Secure-1PSID": secure_1psid}
        if secure_1psidts:
            cookies["__Secure-1PSIDTS"] = secure_1psidts
        self.session = UpstreamSession(cookies=cookies, proxy=proxy)
        self.account_status = AccountStatus.AVAILABLE
        self._models = [
            UpstreamModel("gemini-flash", "Gemini 2.5 Flash", True),
            UpstreamModel("gemini-pro", "Gemini 2.5 Pro", True),
            UpstreamModel("gemini-flash-lite", "Gemini 2.5 Flash Lite", True),
            UpstreamModel("gemini-4-hydrogen", "Gemini 4 Hydrogen", True),
            UpstreamModel("gemini-carbon", "Gemini Carbon", True),
        ]
        self._usage_info: dict | None = None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._rotation_task: asyncio.Task | None = None

    async def init(
        self,
        timeout: float = 30,
        auto_close: bool = False,
        auto_refresh: bool = True,
        refresh_interval: float = 600,
        **kwargs,
    ) -> None:
        """Initializes the session and verifies account access."""
        await self.session.init()
        await self.fetch_models()
        await self.fetch_usage_info()
        if auto_refresh and self.cache_dir and not self._rotation_task:
            self._rotation_task = asyncio.create_task(self._auto_rotate_loop(refresh_interval))

    async def fetch_models(self) -> list[UpstreamModel]:
        """Fetches dynamic models available for this account via RPC otAQ7b."""
        try:
            raw = await self.session.post_batchexecute([(RPC_LIST_MODELS, "[]", "generic")])
            data = get_rpc_result(raw, RPC_LIST_MODELS)
            models = []
            if isinstance(data, list) and len(data) > 15 and isinstance(data[15], list):
                for item in data[15]:
                    if isinstance(item, list) and len(item) >= 3 and isinstance(item[0], str) and len(item[0]) == 16:
                        raw_name = item[1].lower().replace(" ", "-")
                        m_name = f"gemini-{raw_name}" if not raw_name.startswith("gemini-") else raw_name
                        models.append(UpstreamModel(model_name=m_name, display_name=item[2], is_available=True))
            if models:
                alias_models = list(models)
                if any("flash" in m.model_name for m in models) and not any(m.model_name == "gemini-flash" for m in models):
                    alias_models.insert(0, UpstreamModel("gemini-flash", "Gemini Flash (Default)", True))
                if any("pro" in m.model_name for m in models) and not any(m.model_name == "gemini-pro" for m in models):
                    alias_models.append(UpstreamModel("gemini-pro", "Gemini Pro", True))
                self._models = alias_models
            return self._models
        except AuthError:
            raise
        except Exception as e:
            from loguru import logger
            logger.debug(f"Failed to fetch dynamic models: {e}")
            return self._models

    async def _auto_rotate_loop(self, interval_s: float = 600) -> None:
        from loguru import logger
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.session.rotate_cookies(cache_dir=self.cache_dir)
            except asyncio.CancelledError:
                break
            except AuthError as e:
                logger.warning(f"Background cookie rotation failed: session expired ({e})")
                break
            except Exception as e:
                logger.debug(f"Background cookie rotation transient error: {e}")
    @property
    def usage_info(self) -> dict | None:
        return self._usage_info

    async def fetch_usage_info(self) -> dict | None:
        """Fetches real account quota via RPC jSf9Qc."""
        from datetime import datetime, timezone
        try:
            raw = await self.session.post_batchexecute([("jSf9Qc", "[]", "generic")])
            data = get_rpc_result(raw, "jSf9Qc")
            if not isinstance(data, list) or len(data) < 2:
                return None
            tier_labels = {1: "FREE", 2: "PRO", 3: "ULTRA", 4: "PLUS", 6: "ULTRA"}
            tier_id = data[0] if len(data) > 0 and isinstance(data[0], int) else 2
            usage_info = {
                "tier": {"id": tier_id, "label": tier_labels.get(tier_id, "PRO")},
                "current_5h": None,
                "weekly": None,
            }
            if isinstance(data[1], list):
                for item in data[1]:
                    if not isinstance(item, list) or len(item) < 3:
                        continue
                    remaining = item[0]
                    usage_level = item[1]
                    metric_type = item[2]
                    reset_ts = item[3][0][0] if len(item) > 3 and item[3] and item[3][0] else None
                    reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc).astimezone().isoformat() if reset_ts else None
                    metric_info = {
                        "type": metric_type,
                        "remaining_credits": remaining,
                        "usage_level": usage_level,
                        "usage_percentage": round(usage_level * 100) if isinstance(usage_level, (int, float)) else 0,
                        "reset_at": reset_at,
                    }
                    if metric_type == 1:
                        usage_info["current_5h"] = metric_info
                    elif metric_type == 2:
                        usage_info["weekly"] = metric_info
            self._usage_info = usage_info
            return usage_info
        except Exception:
            return None
    async def close(self) -> None:
        """Closes the underlying HTTP session and stops background tasks."""
        import contextlib
        if self._rotation_task and not self._rotation_task.done():
            self._rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rotation_task
            self._rotation_task = None
        await self.session.close()

    def list_models(self) -> list[dict]:
        """Lists available chat/image models."""
        return [
            {
                "model_name": m.model_name,
                "display_name": m.display_name,
                "is_available": m.is_available,
            }
            for m in self._models
        ]

    def resolve_model(self, name: str) -> UpstreamModel:
        """Resolves an alias or requested model to an available model."""
        for m in self._models:
            if m.model_name == name:
                return m
        return UpstreamModel(name, name, True)

    def start_chat(self, gem: str | None = None, metadata: list | None = None, **kwargs) -> UpstreamChatSession:
        """Creates or resumes a chat session tracker from metadata."""
        cid = metadata[0] if metadata and len(metadata) > 0 and metadata[0] else ""
        rid = metadata[1] if metadata and len(metadata) > 1 and metadata[1] else ""
        rcid = metadata[2] if metadata and len(metadata) > 2 and metadata[2] else ""
        return UpstreamChatSession(cid=cid, rid=rid, rcid=rcid, gem=gem, metadata=list(metadata or []))

    async def generate_content(
        self,
        prompt: str,
        files: list[str | Path | bytes] | None = None,
        model: str | UpstreamModel | None = None,
        gem: str | None = None,
        chat: UpstreamChatSession | None = None,
        temporary: bool = False,
        **kwargs,
    ) -> SimpleNamespace:
        """Generates an image via Gemini."""
        chat_sess = chat or self.start_chat(gem=gem)
        uploaded_files = []
        if files:
            for f in files:
                fname = Path(f).name if isinstance(f, (str, Path)) else "ref.png"
                data = Path(f).read_bytes() if isinstance(f, (str, Path)) else f
                tok = await upload_reference_image(self.session, data, fname)
                uploaded_files.append((tok, fname))

        f_req = build_stream_generate_req(
            prompt,
            cid=chat_sess.cid,
            rid=chat_sess.rid,
            rcid=chat_sess.rcid,
            gem_id=gem or chat_sess.gem,
            file_data=uploaded_files or None,
            temporary=temporary,
        )
        data = {"at": self.session.snlm0e, "f.req": f_req}
        headers = self.session.auth_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"

        http = await self.session._get_session()
        url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
        async with http.stream("POST", url, data=data, headers=headers) as resp:
            if resp.status_code == 429:
                raise TemporarilyBlockedError("Rate limited: HTTP 429")
            if resp.status_code in (401, 403):
                raise AuthError(f"StreamGenerate auth failed: HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise APIError(f"StreamGenerate failed: HTTP {resp.status_code}")
            chunks = []
            async for chunk in resp.aiter_content():
                chunks.append(chunk)
            raw_text = b"".join(chunks).decode("utf-8", errors="replace")

        turn = parse_stream_generate_response(raw_text)
        chat_sess.cid = turn.chat.cid or chat_sess.cid
        chat_sess.rid = turn.chat.rid or chat_sess.rid
        chat_sess.rcid = turn.chat.rcid or chat_sess.rcid
        chat_sess.metadata = [chat_sess.cid, chat_sess.rid, chat_sess.rcid]

        for img in turn.images:
            img.client_ref = self
        # Refresh usage info after generation
        try:
            await self.fetch_usage_info()
        except Exception:
            pass

        return SimpleNamespace(text=turn.text, images=turn.images)

    async def download_image(self, url: str, full_size: bool = True) -> bytes:
        """Downloads the image bytes using session cookies."""
        http = await self.session._get_session()
        target_url = f"{url}=s0" if full_size else url
        resp = await http.get(target_url, headers=self.session.auth_headers())
        if resp.status_code != 200:
            raise APIError(f"Failed to download image (HTTP {resp.status_code})")
        if not resp.content:
            raise APIError("Empty response body when downloading image")
        return resp.content

    async def fetch_gems(self) -> list[UpstreamGem]:
        """Fetches custom Gems belonging to this account."""
        raw = await self.session.post_batchexecute([
            (RPC_LIST_GEMS, "[2,['es-419'],0]", "custom")
        ])
        data = get_rpc_result(raw, RPC_LIST_GEMS, label="custom")
        gems = []
        if isinstance(data, list) and len(data) > 2 and isinstance(data[2], list):
            for item in data[2]:
                if isinstance(item, list) and len(item) > 1:
                    g_id = item[0]
                    meta = item[1] if isinstance(item[1], list) else []
                    g_name = meta[0] if len(meta) > 0 else ""
                    g_desc = meta[1] if len(meta) > 1 else ""
                    g_prompt = meta[9] if len(meta) > 9 else ""
                    gems.append(UpstreamGem(id=g_id, name=g_name, prompt=g_prompt, description=g_desc))
        return gems

    async def create_gem(self, name: str, prompt: str, description: str = "") -> UpstreamGem:
        """Creates a custom Gem on Gemini, returning the real Gem ID."""
        payload = [[name, description, prompt, None, None, None, None, None, 0, None, 1, None, None, None, []]]
        raw = await self.session.post_batchexecute([
            (RPC_CREATE_GEM, payload, "generic")
        ])
        data = get_rpc_result(raw, RPC_CREATE_GEM)
        if not isinstance(data, list) or not data or not data[0]:
            raise APIError(f"Failed to extract created Gem ID from response: {data}")
        gem_id = str(data[0])
        return UpstreamGem(id=gem_id, name=name, prompt=prompt, description=description)

    async def update_gem(self, gem_id: str, name: str, prompt: str, description: str = "") -> None:
        """Updates a custom Gem."""
        payload = [gem_id, [name, description, prompt, None, None, None, None, None, 0, None, 1, None, None, None, [], 0]]
        await self.session.post_batchexecute([
            (RPC_UPDATE_GEM, payload, "generic")
        ])

    async def delete_gem(self, gem_id: str) -> None:
        """Deletes a custom Gem."""
        await self.session.post_batchexecute([
            (RPC_DELETE_GEM, [gem_id], "generic")
        ])

    async def delete_chat(self, cid: str) -> None:
        """Deletes a conversation on Gemini."""
        await self.session.post_batchexecute([
            (RPC_DELETE_CHAT, [cid], "generic")
        ])
    async def read_chat(self, cid: str) -> SimpleNamespace:
        """Reads turns from an upstream chat."""
        return SimpleNamespace(turns=[])
