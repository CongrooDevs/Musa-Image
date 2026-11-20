import io
import os
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from .errors import GIAError, INVALID_REF, PATH_NOT_ALLOWED
from .refs import sniff_image_format


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "img")[:max_len].strip("-")


def safe_output_dir(output_root: Path, project: str | None, subdir: str | None) -> Path:
    root = output_root.expanduser().resolve()
    parts = [p for p in (project, subdir) if p]
    for p in parts:
        if Path(p).is_absolute() or ".." in Path(p).parts:
            raise GIAError(INVALID_REF, f"invalid project/subdir: {p!r}")
    target = root.joinpath(*parts) if parts else root
    target = target.resolve()
    if not (target == root or root in target.parents):
        raise GIAError(INVALID_REF, f"output outside output_root: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


@dataclass(frozen=True)
class SavedImage:
    path: Path
    width: int
    height: int
    nbytes: int
    format: str
    full_size: bool
    thumbnail: bytes | None


def make_thumbnail(data: bytes, size: int) -> bytes:
    """JPEG thumbnail: a 512px photo is ~40 KB as JPEG vs ~450 KB as PNG, and it travels to the agent."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()


def save_image(data: bytes, *, output_root: Path, project: str | None, subdir: str | None, prompt: str,
               index: int, meta: dict, write_sidecar: bool, thumb_size: int) -> SavedImage:
    fmt = sniff_image_format(data)
    if fmt is None:
        raise GIAError(INVALID_REF, "received bytes are not an image")
    ext = "jpg" if fmt == "jpeg" else fmt
    out_dir = safe_output_dir(output_root, project, subdir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = slugify(prompt)
    path = out_dir / f"{stamp}_{slug}_{index}.{ext}"
    n = 0
    while path.exists():
        n += 1
        path = out_dir / f"{stamp}_{slug}_{index}_{n}.{ext}"
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if fmt == "png":  # embed metadata as tEXt chunks (re-encodes the PNG)
            info = PngInfo()
            for k, v in meta.items():
                info.add_text(k, str(v))
            im.save(path, "PNG", pnginfo=info)
        else:
            path.write_bytes(data)
    if write_sidecar:
        path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    nbytes = path.stat().st_size  # real size on disk (PNGs are re-encoded with metadata)
    return SavedImage(path=path, width=w, height=h, nbytes=nbytes, format=fmt, full_size=True,
                      thumbnail=make_thumbnail(data, thumb_size))


def check_copy_dest(dest: str, settings) -> Path:
    """Resolve a copy_to destination and reject anything outside the allowlist (state_dir always excluded)."""
    raw = Path(dest).expanduser()
    target = (raw if raw.is_absolute() else Path.cwd() / raw).resolve()
    state = settings.state_dir.resolve()
    if target == state or state in target.parents or not any(
            target == d or d in target.parents for d in settings.allowed_ref_dirs_list()):
        raise GIAError(PATH_NOT_ALLOWED, f"copy_to {dest!r} resolves outside the allowed directories",
                       allowed=[str(d) for d in settings.allowed_ref_dirs_list()])
    return target


def copy_file(src: Path, target: Path) -> Path:
    """Copy src to target; a dir target keeps the src name; never overwrites (_n suffix).

    The destination is created with O_CREAT|O_EXCL, so an existing name (including a dangling
    symlink planted there) is skipped atomically instead of being overwritten or followed."""
    is_dir = target.is_dir() or not target.suffix
    out = (target / src.name) if is_dir else target
    out.parent.mkdir(parents=True, exist_ok=True)
    stem, n = out.stem, 0
    while True:
        try:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            n += 1
            out = out.with_name(f"{stem}_{n}{out.suffix}")
    with os.fdopen(fd, "wb") as dst, open(src, "rb") as s:
        shutil.copyfileobj(s, dst)
    shutil.copystat(src, out)
    return out
