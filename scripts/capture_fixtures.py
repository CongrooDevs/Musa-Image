"""Capture and strictly sanitize live Gemini HTTP traffic as test fixtures."""
import asyncio
import json
import os
import sys
from pathlib import Path
from curl_cffi.requests import AsyncSession
from gemini_image_api.config import Settings
from gemini_image_api.auth import read_cookie_jar
from gemini_image_api.client import GeminiWebBackend
from gemini_image_api.state import StateStore
from gemini_image_api.upstream.sanitizer import TrafficSanitizer

FIXTURES_DIR = Path("tests/fixtures/upstream")

async def main():
    settings = Settings()
    # Ensure cookie cache dir is configured as client.py does
    os.environ.setdefault("GEMINI_COOKIE_PATH", str(settings.upstream_cache_dir))

    jar = read_cookie_jar(settings.cookie_file)
    sanitizer = TrafficSanitizer(jar)
    state = StateStore(settings.state_dir)
    backend = GeminiWebBackend(settings, state)

    captured_entries = []

    orig_post = AsyncSession.post
    orig_get = AsyncSession.get
    orig_stream = AsyncSession.stream
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_stream(self, method, url, *args, **kwargs):
        async with orig_stream(self, method, url, *args, **kwargs) as resp:
            orig_aiter = resp.aiter_content
            collected_chunks = []

            async def patched_aiter():
                async for chunk in orig_aiter():
                    collected_chunks.append(chunk)
                    yield chunk
                full_raw = b"".join(collected_chunks).decode("utf-8", errors="replace")
                captured_entries.append({
                    "method": method,
                    "url": sanitizer.sanitize_text(str(url)),
                    "request_data": sanitizer.sanitize_text(str(kwargs.get("data") or "")),
                    "status_code": resp.status_code,
                    "response_text": sanitizer.sanitize_text(full_raw),
                })

            resp.aiter_content = patched_aiter
            yield resp
    async def patched_post(self, url, *args, **kwargs):
        resp = await orig_post(self, url, *args, **kwargs)
        captured_entries.append({
            "method": "POST",
            "url": sanitizer.sanitize_text(str(url)),
            "request_data": sanitizer.sanitize_text(str(kwargs.get("data") or "")),
            "status_code": resp.status_code,
            "response_text": sanitizer.sanitize_text(resp.text),
        })
        return resp

    async def patched_get(self, url, *args, **kwargs):
        resp = await orig_get(self, url, *args, **kwargs)
        captured_entries.append({
            "method": "GET",
            "url": sanitizer.sanitize_text(str(url)),
            "status_code": resp.status_code,
            "response_text": sanitizer.sanitize_text(resp.text),
        })
        return resp

    AsyncSession.post = patched_post
    AsyncSession.get = patched_get
    AsyncSession.stream = patched_stream

    try:
        print("1. Checking status and verifying auth...")
        status = await backend.status()
        if not status.get("valid") or status.get("account_status") != "AVAILABLE":
            print(f"ERROR: Session is not authenticated: {status}", file=sys.stderr)
            sys.exit(1)
        print("Auth verified: status AVAILABLE, valid True.")

        print("2. Capturing gems list call...")
        await backend.gem_exists("nonexistent_test_gem_id")
        print("Gems check completed.")

        print("3. Capturing image generation (StreamGenerate)...")
        gen_res = await backend.generate("un simple cuadrado verde sobre fondo blanco", [], model="gemini-flash")
        print(f"Generation status: {gen_res.status}, images: {len(gen_res.images)}")
        if gen_res.status != "ok" or not gen_res.images:
            print(f"ERROR: Image generation failed: status={gen_res.status}, code={gen_res.code}", file=sys.stderr)
            sys.exit(1)

    finally:
        await backend.aclose()
        AsyncSession.post = orig_post
        AsyncSession.get = orig_get
        AsyncSession.stream = orig_stream

    print(f"Captured {len(captured_entries)} calls. Validating and saving...")

    # Real secrets validation list
    real_secrets = [c["value"] for c in jar if len(c.get("value", "")) >= 6]

    # Also load any cached cookies from upstream_cache_dir
    if settings.upstream_cache_dir.exists():
        for cache_file in settings.upstream_cache_dir.glob("*.json"):
            try:
                cached_data = json.loads(cache_file.read_text())
                if isinstance(cached_data, dict):
                    for v in cached_data.values():
                        if isinstance(v, str) and len(v) >= 6:
                            real_secrets.append(v)
            except Exception:
                pass

    # Ensure clean directory
    if FIXTURES_DIR.exists():
        for f in FIXTURES_DIR.glob("*"):
            f.unlink()
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    for idx, entry in enumerate(captured_entries):
        serialized = json.dumps(entry, indent=2, ensure_ascii=False)

        # Pre-save assertion: no real secret can be present
        for sec in real_secrets:
            if sec in serialized:
                print(f"SECURITY FAILURE: Secret leaked in entry {idx}! Aborting save.", file=sys.stderr)
                sys.exit(1)

        tag = "StreamGenerate" if "StreamGenerate" in entry["url"] else entry["method"]
        path = FIXTURES_DIR / f"call_{idx:02d}_{tag}.json"
        path.write_text(serialized, encoding="utf-8")
        print(f"Saved sanitized fixture: {path}")

if __name__ == "__main__":
    asyncio.run(main())
