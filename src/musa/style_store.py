"""User-defined styles persisted under <state_dir>/styles/ (YAML + optional reference image)."""
import logging
import os
import unicodedata
from pathlib import Path

import yaml
from filelock import FileLock

from .errors import GIAError, INVALID_STYLE, UNKNOWN_STYLE
from .refs import sniff_image_format, svg_to_png
from .storage import slugify
from .styles import Style

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _slug(name: str) -> str:
    """ASCII-safe slug: fold accents (Neón -> neon) before storage.slugify."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return slugify(folded)


class UserStyleStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / "styles"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(Path(state_dir) / "styles.lock"))

    @property
    def path(self) -> Path:
        return self.dir / "styles.yaml"

    # -- io -------------------------------------------------------------
    def _read(self) -> dict:
        if not self.path.exists():
            return {"styles": {}}
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {"styles": {}}
        if not isinstance(data.get("styles"), dict):
            data["styles"] = {}
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- api ------------------------------------------------------------
    def create(self, *, name: str, description: str = "", append: str = "", family: str = "otros",
               image: tuple[bytes, str] | None = None, taken_ids: set[str] = frozenset()) -> Style:
        name = (name or "").strip()
        if not name:
            raise GIAError(INVALID_STYLE, "style name is empty")
        with self._lock:
            data = self._read()
            taken = set(taken_ids) | set(data["styles"])
            base = _slug(name)
            sid, n = base, 1
            while sid in taken:
                n += 1
                sid = f"{base}-{n}"
            ref_rel = None
            if image is not None:
                blob, ext = self._prepare_image(image)
                ref_rel = f"{sid}.{ext}"
                (self.dir / ref_rel).write_bytes(blob)
            family = (family or "").strip() or "otros"
            entry = {"name": name, "family": family, "description": description,
                     "append": append or description}
            if ref_rel:
                entry["ref"] = ref_rel
            data["styles"][sid] = entry
            self._write(data)
            ref = (self.dir / ref_rel).resolve() if ref_rel else None
            return Style(id=sid, name=name, family=family, description=description,
                         append=append or description, ref=ref, credit="", source="user")

    @staticmethod
    def _prepare_image(image: tuple[bytes, str]) -> tuple[bytes, str]:
        """Validate an upload; return (bytes, extension). SVG is converted to PNG."""
        blob, _fname = image
        if len(blob) > MAX_IMAGE_BYTES:
            raise GIAError(INVALID_STYLE, f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        fmt = sniff_image_format(blob)
        if fmt is None:
            raise GIAError(INVALID_STYLE, "uploaded bytes are not a png/jpeg/webp/gif/svg image")
        if fmt == "svg":
            try:
                blob = svg_to_png(blob)
            except Exception as e:
                raise GIAError(INVALID_STYLE, f"SVG could not be converted to PNG: {e}") from e
            fmt = "png"
        return blob, ("jpg" if fmt == "jpeg" else fmt)

    def _unlink_ref(self, sid: str, rel: str) -> None:
        ref = (self.dir / rel).resolve()
        if ref.is_relative_to(self.dir.resolve()):
            ref.unlink(missing_ok=True)
        else:
            log.warning("style %r ref %r escapes %s; not deleting it", sid, rel, self.dir)

    def update(self, sid: str, *, name: str | None = None, description: str | None = None,
               append: str | None = None, family: str | None = None,
               image: tuple[bytes, str] | None = None, remove_image: bool = False) -> Style:
        """Edit a user style in place (id is immutable). Unset fields are kept.

        An id not in the store raises unknown_style; rejecting built-in styles is the service's job.
        """
        if name is not None and not name.strip():
            raise GIAError(INVALID_STYLE, "style name is empty")
        with self._lock:
            data = self._read()
            entry = data["styles"].get(sid)
            if not isinstance(entry, dict):
                raise GIAError(UNKNOWN_STYLE, f"style {sid!r} does not exist")
            new_blob = new_rel = None
            if image is not None:
                new_blob, ext = self._prepare_image(image)
                new_rel = f"{sid}.{ext}"
            if name is not None:
                entry["name"] = name.strip()
            if description is not None:
                entry["description"] = description
            if append is not None:
                entry["append"] = append
            if family is not None:
                entry["family"] = (family or "").strip() or "otros"
            old_rel = entry.get("ref")
            tmp = None
            if new_blob is not None:
                tmp = self.dir / f".{new_rel}.tmp"
                tmp.write_bytes(new_blob)
                entry["ref"] = new_rel
            elif remove_image:
                entry.pop("ref", None)
            try:
                self._write(data)
                if tmp is not None:
                    os.replace(tmp, self.dir / new_rel)
            except Exception:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
                raise
            if old_rel and (not entry.get("ref")
                            or (self.dir / old_rel).resolve() != (self.dir / entry["ref"]).resolve()):
                self._unlink_ref(sid, old_rel)
            ref = (self.dir / entry["ref"]).resolve() if entry.get("ref") else None
            return Style(id=sid, name=entry.get("name", sid), family=entry.get("family", "otros"),
                         description=entry.get("description", ""),
                         append=entry.get("append") or entry.get("description", ""),
                         ref=ref, credit="", source="user")

    def delete(self, style_id: str) -> None:
        with self._lock:
            data = self._read()
            entry = data["styles"].pop(style_id, None)
            if entry is None:
                raise GIAError(INVALID_STYLE, f"not a user style: {style_id!r}")
            if entry.get("ref"):
                ref = (self.dir / entry["ref"]).resolve()
                if ref.is_relative_to(self.dir.resolve()):
                    ref.unlink(missing_ok=True)
                else:
                    log.warning("style %r ref %r escapes %s; not deleting it", style_id, entry["ref"], self.dir)
            self._write(data)

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0
