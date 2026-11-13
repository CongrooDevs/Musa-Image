"""Scotty resumable uploader for reference images."""
import json
from .session import UpstreamSession
from .exceptions import APIError

UPLOAD_URL = "https://content-push.googleapis.com/upload"

async def upload_reference_image(session: UpstreamSession, image_bytes: bytes, filename: str = "ref.png") -> str:
    """Uploads an image via Google's content-push server, returning the file identifier."""
    import mimetypes
    from curl_cffi import CurlMime

    http = await session._get_session()
    content_type = mimetypes.guess_type(filename)[0] or "image/png"
    mime = CurlMime()
    mime.addpart(name="file", content_type=content_type, filename=filename, data=image_bytes)

    headers = {
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "Push-ID": getattr(session, "push_id", None) or "feeds/mcudyrk2a4khkz",
    }
    try:
        resp = await http.post("https://content-push.googleapis.com/upload", headers=headers, multipart=mime)
        if resp.status_code != 200:
            raise APIError(f"Upload failed: HTTP {resp.status_code} - {resp.text[:200]}")
        url = resp.text.strip()
        if not url.startswith("/contrib_service/"):
            raise APIError(f"Unexpected upload response: {url[:200]}")
        return url
    finally:
        mime.close()
