"""Session and network transport layer using curl_cffi."""
import hashlib
import re
import time
from typing import Any
from curl_cffi.requests import AsyncSession
from .batchexecute import build_batchexecute_req
from .exceptions import AuthError, TemporarilyBlockedError, APIError

_SNLM0E_RE = re.compile(r'\"SNlM0e\":\"([^\"]+)\"')

def compute_sapisid_hash(sapisid: str, origin: str = "https://gemini.google.com") -> str:
    """Computes Google SAPISIDHASH."""
    timestamp = str(int(time.time()))
    payload = f"{timestamp} {sapisid} {origin}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{timestamp}_{digest}"

class UpstreamSession:
    """Handles authenticated HTTP sessions against gemini.google.com using curl_cffi."""

    def __init__(
        self,
        cookies: dict[str, str],
        proxy: str | None = None,
        impersonate: str = "chrome120",
        cookie_jar: list[dict] | None = None,
    ):
        if not cookies.get("__Secure-1PSID"):
            raise AuthError("Missing __Secure-1PSID cookie")
        # Full jar [{name, value, domain}]: Gemini needs its host-only OSID cookies too. The
        # explicit `cookies` (PSID, possibly rotated PSIDTS) win over the jar's .google.com values.
        self.host_cookies = [
            (c["name"], c["value"], c.get("domain") or ".google.com") for c in (cookie_jar or [])
            if not (c.get("domain", ".google.com") == ".google.com" and c["name"] in cookies)
        ]
        self.cookies = {n: v for n, v, d in self.host_cookies if d == ".google.com"}
        self.cookies.update(cookies)
        self.proxy = proxy
        self.impersonate = impersonate
        self.snlm0e: str | None = None
        self._session: AsyncSession | None = None

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            from curl_cffi.requests import Cookies
            jar = Cookies()
            for k, v in self.cookies.items():
                jar.set(k, v, domain=".google.com", secure=k.startswith("__Secure-"))
            for k, v, d in self.host_cookies:
                if d != ".google.com":
                    jar.set(k, v, domain=d, secure=k.startswith("__Secure-"))
            self._session = AsyncSession(
                impersonate=self.impersonate,
                cookies=jar,
                proxy=self.proxy,
                timeout=60.0,
            )
        return self._session

    def auth_headers(self, origin: str = "https://gemini.google.com") -> dict[str, str]:
        sapisid = (
            self.cookies.get("SAPISID")
            or self.cookies.get("__Secure-1PAPISID")
            or self.cookies.get("__Secure-3PAPISID")
        )
        headers = {
            "Origin": origin,
            "Referer": f"{origin}/",
            "X-Same-Domain": "1",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if sapisid:
            headers["Authorization"] = f"SAPISIDHASH {compute_sapisid_hash(sapisid, origin)}"
        return headers

    async def init(self) -> None:
        """Fetches the main app page to extract SNlM0e anti-CSRF token."""
        session = await self._get_session()
        resp = await session.get("https://gemini.google.com/app", headers=self.auth_headers())
        if resp.status_code in (401, 403):
            raise AuthError(f"Authentication failed: HTTP {resp.status_code}")
        if resp.status_code == 429:
            raise TemporarilyBlockedError("Rate limited: HTTP 429")

        match = _SNLM0E_RE.search(resp.text)
        if not match:
            raise AuthError("Could not extract SNlM0e anti-CSRF token (session expired or invalid)")
        self.snlm0e = match.group(1)
    async def rotate_cookies(self, cache_dir: Any = None) -> str | None:
        """Refreshes __Secure-1PSIDTS via RotateCookies and optionally saves atomically."""
        http = await self._get_session()
        headers = {
            "Origin": "https://accounts.google.com",
            "Referer": "https://accounts.google.com/",
            "Content-Type": "application/json",
        }
        data = '[000,"-0000000000000000000"]'
        resp = await http.post("https://accounts.google.com/RotateCookies", headers=headers, data=data)
        if resp.status_code in (401, 403):
            raise AuthError("Failed to rotate cookies: unauthorized")
        if resp.status_code != 200:
            return None

        new_ts = None
        for c in http.cookies.jar:
            if getattr(c, "name", None) == "__Secure-1PSIDTS":
                new_ts = c.value
                self.cookies["__Secure-1PSIDTS"] = new_ts
                break

        if new_ts and cache_dir:
            import json
            import os
            from pathlib import Path
            cdir = Path(cache_dir)
            cdir.mkdir(parents=True, exist_ok=True)
            psid = self.cookies.get("__Secure-1PSID", "default")
            cache_file = cdir / f".cached_cookies_{psid}.json"
            tmp_file = cache_file.with_suffix(".tmp")
            cookie_list = [
                {"name": k, "value": v, "domain": ".google.com"}
                for k, v in self.cookies.items()
            ]
            fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(cookie_list, ensure_ascii=False))
            os.replace(tmp_file, cache_file)

        return new_ts


    async def post_batchexecute(self, calls: list[tuple[str, Any, str | None]]) -> str:
        """Sends batchexecute calls to Gemini's BardChatUi endpoint."""
        if not self.snlm0e:
            await self.init()

        session = await self._get_session()
        rpc_ids = ",".join(c[0] for c in calls)
        params = {"rpcids": rpc_ids, "_reqid": str(int(time.time() * 1000) % 1000000)}
        f_req = build_batchexecute_req(calls)
        data = {"f.req": f_req}
        if self.snlm0e:
            data["at"] = self.snlm0e

        headers = self.auth_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"

        resp = await session.post(
            "https://gemini.google.com/_/BardChatUi/data/batchexecute",
            params=params,
            data=data,
            headers=headers,
        )
        if resp.status_code in (401, 403):
            raise AuthError(f"batchexecute auth error: HTTP {resp.status_code}")
        if resp.status_code == 429:
            raise TemporarilyBlockedError("batchexecute rate limited: HTTP 429")
        if resp.status_code != 200:
            raise APIError(f"batchexecute HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.text

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
