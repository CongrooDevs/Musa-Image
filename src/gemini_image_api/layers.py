"""Layer image processing for compositions (spec 2 §6). Pillow only; blocking: call from a thread."""
from collections import deque

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .errors import GIAError
from .sprites import alpha_bbox, soft_key

_ASPECT_TOLERANCE = 0.02
_MIN_GAIN = 1.0


def normalize_size(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """`img` at exactly `size`: unchanged if equal, LANCZOS-scaled if the aspect differs < 2 %, else invalid_image."""
    size = (int(size[0]), int(size[1]))
    if img.size == size:
        return img
    w, h = img.size
    if w < 1 or h < 1 or abs((w / h) / (size[0] / size[1]) - 1.0) >= _ASPECT_TOLERANCE:
        raise GIAError("invalid_image", "la capa no tiene la proporción de la imagen completa")
    return img.resize(size, Image.Resampling.LANCZOS)


_GRID = 240            # long side of the coarse grid used to find connected blobs
_KEEP_RATIO = 0.35     # blobs at least this big relative to the largest one are kept too


def keep_main_blobs(rgba: Image.Image) -> Image.Image:
    """Drop stray objects Gemini left next to the piece (e.g. the coins beside Hermes, a shop next
    to the jar): keep the largest connected blob, plus blobs at least _KEEP_RATIO of its area.
    Blobs closer than ~2 % of the image size count as connected (wings stay with the helmet)."""
    w, h = rgba.size
    s = _GRID / max(w, h)
    gw, gh = max(1, round(w * s)), max(1, round(h * s))
    small = rgba.getchannel("A").resize((gw, gh), Image.Resampling.BOX)
    grid = small.point(lambda v: 255 if v > 20 else 0).filter(ImageFilter.MaxFilter(5))
    cells = grid.tobytes()
    label = [0] * (gw * gh)
    areas = [0]
    for start in range(gw * gh):
        if not cells[start] or label[start]:
            continue
        n = len(areas)
        areas.append(0)
        label[start] = n
        q = deque([start])
        while q:
            i = q.popleft()
            areas[n] += 1
            x, y = i % gw, i // gw
            for j in ((i - 1) if x else -1, (i + 1) if x < gw - 1 else -1,
                      (i - gw) if y else -1, (i + gw) if y < gh - 1 else -1):
                if j >= 0 and cells[j] and not label[j]:
                    label[j] = n
                    q.append(j)
    if len(areas) <= 2:
        return rgba
    biggest = max(areas[1:])
    keep = {k for k in range(1, len(areas)) if areas[k] >= _KEEP_RATIO * biggest}
    mask = Image.frombytes("L", (gw, gh), bytes(255 if label[i] in keep else 0 for i in range(gw * gh)))
    mask = mask.resize((w, h), Image.Resampling.NEAREST)
    out = rgba.copy()
    out.putalpha(ImageChops.multiply(rgba.getchannel("A"), mask))
    return out


def prepare_piece(img: Image.Image, key_color: str, tolerance: int):
    """(rgba_full, bbox) of a piece generated on `key_color`; `img` must already be normalized.
    Stray objects beside the piece are removed (keep_main_blobs)."""
    rgba = keep_main_blobs(soft_key(img, key_color, tolerance))
    bbox = alpha_bbox(rgba)
    if bbox is None:
        raise GIAError("invalid_image", "la pieza salió vacía")
    return rgba, tuple(int(v) for v in bbox)


def _score(piece: Image.Image, mask: Image.Image, ref: Image.Image, box) -> float:
    diff = ImageChops.difference(piece, ref.crop(box))
    return float(ImageStat.Stat(diff, mask).mean[0])


def search_offset(master: Image.Image, rgba_full: Image.Image, bbox, radius: int = 12) -> tuple[int, int]:
    """Offset (dx, dy) that best aligns the piece's opaque pixels with `master`; (0, 0) unless another
    position improves the mean gray difference by more than 1.0. Half-resolution search, then ±1 refinement."""
    x0, y0, x1, y1 = (int(v) for v in bbox)
    w, h = x1 - x0, y1 - y0
    piece = rgba_full.convert("RGBA").crop((x0, y0, x1, y1))
    mask = piece.getchannel("A").point(lambda v: 255 if v > 200 else 0)
    if mask.getbbox() is None:
        return 0, 0
    pgray = piece.convert("L")
    mgray = master.convert("L")

    def full(dx: int, dy: int) -> float:
        return _score(pgray, mask, mgray, (x0 + dx, y0 + dy, x1 + dx, y1 + dy))

    base = full(0, 0)
    # coarse: half resolution inside a margin region of the master
    m = 2 * ((radius + 1) // 2)
    w2, h2 = max(1, w // 2), max(1, h // 2)
    p2 = pgray.resize((w2, h2), Image.Resampling.BOX)
    m2 = mask.resize((w2, h2), Image.Resampling.BOX).point(lambda v: 255 if v > 127 else 0)
    best_c, best_s = (0, 0), None
    if m2.getbbox() is not None:
        region = mgray.crop((x0 - m, y0 - m, x1 + m, y1 + m))
        r2 = region.resize((max(1, region.width // 2), max(1, region.height // 2)), Image.Resampling.BOX)
        half = m // 2
        for hy in range(-(radius // 2), radius // 2 + 1):
            for hx in range(-(radius // 2), radius // 2 + 1):
                s = _score(p2, m2, r2, (half + hx, half + hy, half + hx + w2, half + hy + h2))
                if best_s is None or s < best_s:
                    best_c, best_s = (hx, hy), s
    cx, cy = best_c[0] * 2, best_c[1] * 2
    best, best_score = (0, 0), base
    for dy in range(cy - 1, cy + 2):
        for dx in range(cx - 1, cx + 2):
            if max(abs(dx), abs(dy)) > radius:
                continue
            s = full(dx, dy)
            if s < best_score:
                best, best_score = (dx, dy), s
    if base - best_score > _MIN_GAIN:
        return best
    return 0, 0


def layer_png(rgba_full: Image.Image, bbox, offset=(0, 0)) -> Image.Image:
    """The piece cropped to `bbox`; its final position is bbox + offset (kept in the data, not the PNG)."""
    return rgba_full.crop(tuple(int(v) for v in bbox))


def composite(size: tuple[int, int], layers) -> Image.Image:
    """Flat RGB composition. `layers` (bottom first) are dicts with `role`, `image` (PIL), `bbox`, `offset`
    and `visible`: the background fills the canvas, visible pieces are pasted at bbox + offset."""
    size = (int(size[0]), int(size[1]))
    canvas = Image.new("RGB", size, (255, 255, 255))
    for ly in layers:
        img = ly["image"]
        if ly["role"] == "background":
            bg = img.convert("RGB")
            canvas.paste(bg if bg.size == size else bg.resize(size, Image.Resampling.LANCZOS), (0, 0))
            continue
        if not ly.get("visible", True):
            continue
        rgba = img.convert("RGBA")
        x = int(ly["bbox"][0]) + int(ly["offset"][0])
        y = int(ly["bbox"][1]) + int(ly["offset"][1])
        canvas.paste(rgba.convert("RGB"), (x, y), rgba.getchannel("A"))
    return canvas
