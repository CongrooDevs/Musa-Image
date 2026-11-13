"""Temporary uploads for the web composer, stored under <state_dir>/uploads.

Clients only ever send the upload id; the file path is resolved server-side.
Files are written 0600 inside a 0700 directory and purged after 24 h.
"""
import os
import re
import time
import uuid
from pathlib import Path

from .config import Settings
from .errors import GIAError
from .refs import sniff_image_format, svg_to_png


class UploadStore:
    MAX_BYTES = 20 * 1024 * 1024
    ID_RE = re.compile(r"^u_[0-9a-f]{12}$")

    def __init__(self, state_dir: Path, settings: Settings):
        self.settings = settings
        self.dir = Path(state_dir) / "uploads"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def save(self, data: bytes) -> str:
        """Validate size + magic bytes, store as <id>.<ext> (0600), return the id."""
        if not data:
            raise GIAError("invalid_image", "uploaded data is empty")
        data = bytes(data)
        if len(data) > self.MAX_BYTES:
            raise GIAError("invalid_image",
                           f"upload exceeds {self.MAX_BYTES // (1024 * 1024)} MB")
        ext = sniff_image_format(data)
        if ext is None:
            raise GIAError("invalid_image", "uploaded bytes are not an image (png/jpeg/webp/gif/svg)")
        if ext == "svg":
            try:
                data = svg_to_png(data)
            except Exception as e:
                raise GIAError("invalid_image", f"SVG could not be converted to PNG: {e}") from e
            ext = "png"
        upload_id = f"u_{uuid.uuid4().hex[:12]}"
        path = self.dir / f"{upload_id}.{ext}"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return upload_id

    def path(self, upload_id: str) -> Path:
        """Resolve an upload id to its file, or GIAError("unknown_upload")."""
        if not isinstance(upload_id, str) or not self.ID_RE.match(upload_id):
            raise GIAError("unknown_upload", f"bad upload id {upload_id!r}; expected u_<12 hex>")
        matches = sorted(f for f in self.dir.glob(f"{upload_id}.*")
                         if f.is_file() and not f.is_symlink())
        if not matches:
            raise GIAError("unknown_upload", f"upload {upload_id!r} does not exist")
        return matches[0]

    def purge(self, max_age_s: int = 86400) -> int:
        """Delete files older than max_age_s (by mtime); returns how many were removed."""
        now = time.time()
        removed = 0
        for f in self.dir.iterdir():
            try:
                if f.is_file() and now - f.stat().st_mtime > max_age_s:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
