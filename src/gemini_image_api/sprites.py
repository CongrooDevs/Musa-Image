"""Sprite-sheet analysis and normalisation (Pillow only; no numpy, no Image.getdata)."""
import array
import math
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageMath

from .errors import INVALID_ANIMATION, GIAError

GRIDS = {"4x2": (4, 2, "16:9"), "3x3": (3, 3, "1:1"), "2x2": (2, 2, "1:1")}
MODES = ("sprite", "scene")
KEY_COLORS = ("green", "magenta")
LOOPS = ("loop", "pingpong")
MAX_SHEET_W = 8192
_PURE = {"green": (0, 255, 0), "magenta": (255, 0, 255)}


# -- validation ----------------------------------------------------------
def _bad(msg: str, **details) -> GIAError:
    return GIAError(INVALID_ANIMATION, msg, **details)


def _int_in(name: str, v, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise _bad(f"{name} must be an integer between {lo} and {hi}", field=name)
    return v


def validate_animation_request(data) -> dict:
    """Normalise the generate-time `animation` argument: {grid, mode, key_color}."""
    if not isinstance(data, dict):
        raise _bad("animation must be an object with grid, mode and key_color")
    extra = sorted(set(data) - {"grid", "mode", "key_color"})
    if extra:
        raise _bad(f"unknown animation keys: {', '.join(map(str, extra))}", fields=extra)
    grid = data.get("grid", "4x2")
    if not isinstance(grid, str) or grid not in GRIDS:
        raise _bad(f"grid must be one of {', '.join(GRIDS)}", field="grid", valid=list(GRIDS))
    mode = data.get("mode", "sprite")
    if mode not in MODES:
        raise _bad("mode must be sprite or scene", field="mode", valid=list(MODES))
    kc = data.get("key_color")
    if mode == "sprite":
        kc = "green" if kc is None else kc
        if kc not in KEY_COLORS:
            raise _bad("key_color must be green or magenta", field="key_color", valid=list(KEY_COLORS))
    else:
        if kc is not None and kc not in KEY_COLORS:
            raise _bad("key_color must be green or magenta", field="key_color", valid=list(KEY_COLORS))
        kc = None
    return {"grid": grid, "mode": mode, "key_color": kc}


def validate_settings(*, cols=None, rows=None, mode=None, key_color=None, tolerance=None,
                      fps=None, loop=None) -> dict:
    """Validate the provided (non-None) set_animation values; returns only those."""
    out: dict = {}
    if cols is not None:
        out["cols"] = _int_in("cols", cols, 1, 8)
    if rows is not None:
        out["rows"] = _int_in("rows", rows, 1, 8)
    if "cols" in out and "rows" in out:
        validate_grid(out["cols"], out["rows"])
    if mode is not None:
        if mode not in MODES:
            raise _bad("mode must be sprite or scene", field="mode", valid=list(MODES))
        out["mode"] = mode
    if key_color is not None:
        if key_color not in KEY_COLORS:
            raise _bad("key_color must be green or magenta", field="key_color", valid=list(KEY_COLORS))
        out["key_color"] = key_color
    if tolerance is not None:
        out["tolerance"] = _int_in("tolerance", tolerance, 5, 80)
    if fps is not None:
        out["fps"] = _int_in("fps", fps, 2, 30)
    if loop is not None:
        if loop not in LOOPS:
            raise _bad("loop must be loop or pingpong", field="loop", valid=list(LOOPS))
        out["loop"] = loop
    return out


def validate_grid(cols: int, rows: int) -> None:
    _int_in("cols", cols, 1, 8)
    _int_in("rows", rows, 1, 8)
    if not 2 <= cols * rows <= 36:
        raise _bad("cols x rows must be between 2 and 36 frames", field="grid")


# -- prompts -------------------------------------------------------------
def bg_name(key_color: str | None) -> str:
    return "magenta #FF00FF" if key_color == "magenta" else "chroma green #00FF00"


def sheet_prompt(prompt: str, cols: int, rows: int, mode: str, key_color: str | None) -> str:
    n = cols * rows
    bg = bg_name(key_color)
    if mode == "scene":
        first = (f"a sprite sheet for a looping animated scene. Scene and action: {prompt}, "
                 "as one seamless loop.")
        consistency = ("Consistency: the same scene in every frame, with identical composition, framing, "
                       "colors and lighting; only the described motion changes. "
                       "Fixed camera: no zoom, no pan, no perspective change.")
        placement = "Placement: every frame fills its whole cell edge to edge."
        background = None
    else:
        first = (f"a sprite sheet for a looping animation. Subject and action: {prompt}, "
                 "as one seamless loop.")
        consistency = ("Consistency: the same character in every frame, with identical design, proportions, "
                       "colors, outfit, size and camera angle. Fixed camera: no zoom, no pan, "
                       "no perspective change.")
        placement = ("Placement: in every cell the character is centered horizontally and fully inside "
                     "the cell with generous empty padding on all sides. The ground level is the same "
                     "in every cell; the character rises above it only while airborne. "
                     "Nothing touches or crosses the cell edges.")
        background = (f"Background: solid flat {bg} in every cell. No gradient, no floor, no cast shadow, "
                      "no scenery. The character itself does not use that color.")
    lines = [
        first,
        (f"Layout: exactly {n} frames in a grid of {cols} columns x {rows} rows, read left to right, "
         "top to bottom. All cells are exactly the same size and the grid fills the whole image evenly."),
        consistency,
        placement,
        ("Motion: each frame is the next evenly spaced step of the motion. Changes between consecutive "
         "frames are small but clearly visible, and the last frame flows naturally back into the first."),
    ]
    if background:
        lines.append(background)
    lines.append("Do not draw grid lines, borders, frame numbers, labels, text or watermarks.")
    return "\n".join(lines)


def edit_suffix(ficha: dict) -> str:
    cols, rows = ficha["cols"], ficha["rows"]
    if ficha.get("mode") == "scene":
        return (f" Keep the exact same sprite sheet layout: a {cols}x{rows} grid of equal cells, "
                "each filling its cell edge to edge.")
    return (f" Keep the exact same sprite sheet layout: a {cols}x{rows} grid of equal cells, "
            "the same character size and position in each cell, and the same solid flat "
            f"{bg_name(ficha.get('key_color'))} background.")


# -- pixel helpers -------------------------------------------------------
def _rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "P", "PA") and (img.mode != "P" or "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return img.convert("RGB")


def _excess(img: Image.Image, key_color: str) -> Image.Image:
    """Key-colour excess S as an 'L' image (clamped 0-255)."""
    r, g, b = img.split()
    if key_color == "magenta":
        return ImageChops.subtract(ImageChops.darker(r, b), g)
    return ImageChops.subtract(g, ImageChops.lighter(r, b))


def _hist_median(hist: list[int]) -> int:
    total = sum(hist)
    if not total:
        return 0
    half, acc = total / 2, 0
    for i, n in enumerate(hist):
        acc += n
        if acc >= half:
            return i
    return len(hist) - 1


def _fractions(mask: Image.Image, size: tuple[int, int]) -> list[float]:
    """Mean of an 'L' 0/255 mask resampled by BOX to `size` (one axis is 1) -> 0..1 floats."""
    f = mask.convert("F").resize(size, Image.Resampling.BOX)
    a = array.array("f")
    a.frombytes(f.tobytes())
    return [v / 255.0 for v in a]


def _white_mask(img: Image.Image) -> Image.Image:
    r, g, b = img.split()
    return ImageChops.darker(ImageChops.darker(r, g), b).point(lambda v: 255 if v > 200 else 0)


def _dark_mask(img: Image.Image) -> Image.Image:
    """Dark pixels (every channel < 170): the thin grid lines Gemini sometimes draws between cells,
    often a darker shade of the key colour such as (0, 120, 0) on a green sheet."""
    r, g, b = img.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v < 170 else 0)


def _rule_mask(img: Image.Image) -> Image.Image:
    """White gutter or dark grid-line pixels: what may be trimmed at a cell edge."""
    return ImageChops.lighter(_white_mask(img), _dark_mask(img))


def estimate_key(img: Image.Image, key_color: str) -> tuple[tuple[int, int, int], float]:
    img = _rgb(img)
    w, h = img.size
    s = _excess(img, key_color)
    border = Image.new("L", (w, h), 255)
    if w > 16 and h > 16:
        border.paste(0, (8, 8, w - 8, h - 8))
    strong = ImageChops.darker(border, s.point(lambda v: 255 if v >= 40 else 0))
    n = strong.histogram()[255]
    if n < 50:
        # No key colour on the outer frame (e.g. a white margin around the sheet):
        # fall back to every strongly keyed pixel of the sheet, if there are enough of them.
        strong = s.point(lambda v: 255 if v >= 40 else 0)
        n = strong.histogram()[255]
        if n < max(50, w * h // 50):
            return _PURE[key_color], 255.0
    hist = img.histogram(mask=strong)
    rgb = tuple(_hist_median(hist[i * 256:(i + 1) * 256]) for i in range(3))
    return rgb, float(_hist_median(s.histogram(mask=strong)))


def _clamp(a, lo, hi, args):
    return args["max"](args["min"](a, hi), lo)


def soft_key(img: Image.Image, key_color: str, tolerance: float, bg=None) -> Image.Image:
    img = _rgb(img)
    (bg_rgb, k) = bg if bg is not None else estimate_key(img, key_color)
    k = float(k)
    tol = min(float(tolerance), max(0.0, k - 20))
    kf = max(tol + 10, k - 15)
    s = _excess(img, key_color)
    # alpha depends only on S: build it from a 256-entry LUT, with the 0.02/0.98 snaps.
    lut = []
    for v in range(256):
        a = min(1.0, max(0.0, (kf - v) / (kf - tol)))
        lut.append(255 if a >= 0.98 else 0 if a <= 0.02 else int(round(255 * a)))
    alpha = s.point(lut)

    sf = s.convert("F")
    au = ImageMath.lambda_eval(
        lambda a: _clamp(1.0 - a["s"] / max(k, 1.0), 0.02, 1.0, a), s=sf)
    chans = []
    for c, bgc in zip(img.split(), bg_rgb):
        cf = c.convert("F")
        chans.append(ImageMath.lambda_eval(
            lambda a, bgc=bgc: _clamp((a["c"] - (1.0 - a["au"]) * bgc) / a["au"], 0.0, 255.0, a),
            c=cf, au=au))
    rf, gf, bf = chans
    if key_color == "magenta":
        m = ImageMath.lambda_eval(lambda a: a["max"](a["min"](a["r"], a["b"]) - a["g"], 0.0),
                                  r=rf, g=gf, b=bf)
        rf = ImageMath.lambda_eval(lambda a: a["r"] - a["m"], r=rf, m=m)
        bf = ImageMath.lambda_eval(lambda a: a["b"] - a["m"], b=bf, m=m)
    else:
        gf = ImageMath.lambda_eval(lambda a: a["min"](a["g"], a["max"](a["r"], a["b"])),
                                   r=rf, g=gf, b=bf)
    out = [ch.convert("L") for ch in (rf, gf, bf)]
    nz = alpha.point(lambda v: 255 if v else 0)
    out = [ImageChops.multiply(ch, nz) for ch in out]
    return Image.merge("RGBA", (*out, alpha))


def _mask(rgba: Image.Image, threshold: int = 128) -> Image.Image:
    m = rgba.getchannel("A").point(lambda v: 255 if v >= threshold else 0)
    return m.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))


def alpha_bbox(rgba: Image.Image, threshold: int = 128):
    return _mask(rgba, threshold).getbbox()


def alpha_centroid_x(rgba: Image.Image, box) -> float:
    x0, y0, x1, y1 = box
    w = x1 - x0
    m = _mask(rgba).crop(box)
    cols = _fractions(m, (w, 1))
    tot = sum(cols)
    if tot <= 0:
        return x0 + w / 2.0
    return x0 + sum((i + 0.5) * v for i, v in enumerate(cols)) / tot


# -- cell detection ------------------------------------------------------
def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    out, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - 1))
            start = None
    return out


_LINE_FRACTION = 0.6   # a column/row that is at least this dark is a drawn grid line
_LINE_MAX_WIDTH = 8


def _axis_cuts(flags: list[bool], n: int, length: int,
               line_flags: list[bool] | None = None) -> tuple[list[int], bool]:
    """Cuts (len n+1, including 0 and length) and whether all inner cuts came from lanes.

    A thin drawn grid line near the ideal cut wins over an empty lane: the lane around a line is
    usually lopsided (characters sit at different distances from it), so its centre is off."""
    if n <= 1:
        return [0, length], True
    runs = [r for r in _runs(flags) if r[0] > 0 and r[1] < length - 1]
    lines = [r for r in _runs(line_flags or []) if r[0] > 0 and r[1] < length - 1
             and r[1] - r[0] + 1 <= _LINE_MAX_WIDTH]
    step = length / n
    cuts, ok = [0], True
    for k in range(1, n):
        ideal = k * step
        best = None
        for group in (lines, runs):
            bd = None
            for a, b in group:
                d = abs((a + b + 1) / 2.0 - ideal)
                if d <= 0.25 * step and (bd is None or d < bd):
                    best, bd = (a, b), d
            if best is not None:
                break
        if best is None:
            ok = False
            break
        cuts.append(int(round((best[0] + best[1] + 1) / 2.0)))
    if not ok:
        return [int(round(k * step)) for k in range(n)] + [length], False
    return cuts + [length], True


def _trim_extents(white_cols: list[float], white_rows: list[float]) -> tuple[int, int, int, int]:
    """Gutter lines to trim from (left, top, right, bottom): up to 15 % while white >= 0.90."""
    def side(vals: list[float]) -> tuple[int, int]:
        cap = int(0.15 * len(vals))
        a = 0
        while a < cap and vals[a] >= 0.90:
            a += 1
        b = 0
        while b < cap and vals[-1 - b] >= 0.90:
            b += 1
        return a, b
    l, r = side(white_cols)
    t, b = side(white_rows)
    return l, t, r, b


def _cell_trim(cell_rgb: Image.Image) -> tuple[int, int, int, int]:
    w, h = cell_rgb.size
    rm = _rule_mask(cell_rgb)
    return _trim_extents(_fractions(rm, (w, 1)), _fractions(rm, (1, h)))


def _clear_outside(rgba: Image.Image, trim: tuple[int, int, int, int]) -> Image.Image:
    l, t, r, b = trim
    if not (l or t or r or b):
        return rgba
    w, h = rgba.size
    keep = Image.new("L", (w, h), 0)
    keep.paste(255, (l, t, w - r, h - b))
    a = ImageChops.multiply(rgba.getchannel("A"), keep)
    rgba = rgba.copy()
    rgba.putalpha(a)
    return rgba


def detect_cells(img: Image.Image, cols: int, rows: int, mode: str, key_color: str | None,
                 tolerance: int) -> dict:
    img = _rgb(img)
    w, h = img.size
    sprite = mode == "sprite"
    rgba = soft_key(img, key_color or "green", tolerance) if sprite else None
    white = _white_mask(img)
    wc, wr = _fractions(white, (w, 1)), _fractions(white, (1, h))
    if sprite:
        transp = rgba.getchannel("A").point(lambda v: 255 if v < 128 else 0)
        tc, tr = _fractions(transp, (w, 1)), _fractions(transp, (1, h))
        col_flags = [a >= 0.996 or b >= 0.90 for a, b in zip(tc, wc)]
        row_flags = [a >= 0.996 or b >= 0.90 for a, b in zip(tr, wr)]
    else:
        col_flags = [b >= 0.90 for b in wc]
        row_flags = [b >= 0.90 for b in wr]
    dark = _dark_mask(img)
    dc, dr = _fractions(dark, (w, 1)), _fractions(dark, (1, h))
    xs, ok_x = _axis_cuts(col_flags, cols, w, [v >= _LINE_FRACTION for v in dc])
    ys, ok_y = _axis_cuts(row_flags, rows, h, [v >= _LINE_FRACTION for v in dr])
    cells = [[xs[c], ys[r], xs[c + 1], ys[r + 1]] for r in range(rows) for c in range(cols)]
    lanes = (ok_x or cols <= 1) and (ok_y or rows <= 1)
    detected_by = "lanes" if lanes else "uniform"
    if not sprite:
        status = "ok"
    else:
        status = "ok" if lanes else "review"
        if status == "ok":
            for x0, y0, x1, y1 in cells:
                cell = rgba.crop((x0, y0, x1, y1))
                trim = _cell_trim(img.crop((x0, y0, x1, y1)))
                if alpha_bbox(_clear_outside(cell, trim)) is None:
                    status = "review"
                    break
    return {"cells": cells, "detected_by": detected_by, "status": status}


# -- rendering -----------------------------------------------------------
def _scale_sheet(sheet: Image.Image, n: int, fw: int, fh: int, pad: int, boxes):
    if n * fw <= MAX_SHEET_W:
        return sheet, fw, fh, pad, boxes
    nfw = max(1, MAX_SHEET_W // n)
    nfh = max(1, round(fh * nfw / fw))
    sx, sy = nfw / fw, nfh / fh
    sheet = sheet.resize((nfw * n, nfh), Image.Resampling.LANCZOS)
    nb = [None if b is None else [round(b[0] * sx), round(b[1] * sy), round(b[2] * sx), round(b[3] * sy)]
          for b in boxes]
    return sheet, nfw, nfh, round(pad * sx), nb


def render_sheet(img: Image.Image, ficha: dict) -> tuple[Image.Image, dict]:
    img = _rgb(img)
    cells = ficha["cells"]
    n = len(cells)
    if ficha.get("mode") == "scene":
        return _render_scene(img, cells, n)
    return _render_sprite(img, ficha, cells, n)


def _render_scene(img, cells, n):
    trims = [_cell_trim(img.crop(tuple(c))) for c in cells]
    l = max(t[0] for t in trims)
    t_ = max(t[1] for t in trims)
    r = max(t[2] for t in trims)
    b = max(t[3] for t in trims)
    inner = [(c[0] + l, c[1] + t_, c[2] - r, c[3] - b) for c in cells]
    fw = max(1, min(x1 - x0 for x0, _, x1, _ in inner))
    fh = max(1, min(y1 - y0 for _, y0, _, y1 in inner))
    sheet = Image.new("RGB", (n * fw, fh))
    for k, (x0, y0, _, _) in enumerate(inner):
        sheet.paste(img.crop((x0, y0, x0 + fw, y0 + fh)), (k * fw, 0))
    boxes = [[0, 0, fw, fh] for _ in range(n)]
    sheet, fw, fh, pad, boxes = _scale_sheet(sheet, n, fw, fh, 0, boxes)
    boxes = [[0, 0, fw, fh] for _ in range(n)]
    return sheet, {"frame_w": fw, "frame_h": fh, "pad": 0, "frame_boxes": boxes}


def _render_sprite(img, ficha, cells, n):
    key_color = ficha.get("key_color") or "green"
    tol = ficha.get("tolerance", 25)
    H = img.size[1]
    bg = estimate_key(img, key_color)
    items = []  # (rgba, box|None, cx, y_rel, exp_box)
    for k, (x0, y0, x1, y1) in enumerate(cells):
        crop = img.crop((x0, y0, x1, y1))
        trim = _cell_trim(crop)
        rgba = _clear_outside(soft_key(crop, key_color, tol, bg=bg), trim)
        box = alpha_bbox(rgba)
        if box is None:
            items.append((rgba, None, 0.0, 0))
            continue
        cx = alpha_centroid_x(rgba, box)
        if trim[1] > 0:
            y_rel = box[1] - trim[1]
        else:
            y_rel = (y0 + box[1]) - round((k // ficha["cols"]) * H / ficha["rows"])
        items.append((rgba, box, cx, y_rel))
    live = [it for it in items if it[1] is not None]
    if live:
        half = max(math.ceil(max(cx - b[0], b[2] - cx)) for _, b, cx, _ in live)
        top = min(y for _, _, _, y in live)
        bottom = max(y + (b[3] - b[1]) for _, b, _, y in live)
    else:
        half, top, bottom = 1, 0, 1
    half = max(1, half)
    pad = max(2, round(0.04 * 2 * half))
    fw, fh = 2 * half + 2 * pad, bottom - top + 2 * pad
    sheet = Image.new("RGBA", (n * fw, fh), (0, 0, 0, 0))
    boxes: list = []
    for k, (rgba, box, cx, y_rel) in enumerate(items):
        if box is None:
            boxes.append(None)
            continue
        cw, ch = rgba.size
        ex = (max(0, box[0] - 2), max(0, box[1] - 2), min(cw, box[2] + 2), min(ch, box[3] + 2))
        px = round(pad + half - (cx - box[0]))
        py = y_rel - top + pad
        sheet.paste(rgba.crop(ex), (k * fw + px - (box[0] - ex[0]), py - (box[1] - ex[1])))
        bw, bh = box[2] - box[0], box[3] - box[1]
        boxes.append([px, py, px + bw, py + bh])
    sheet, fw, fh, pad, boxes = _scale_sheet(sheet, n, fw, fh, pad, boxes)
    return sheet, {"frame_w": fw, "frame_h": fh, "pad": pad, "frame_boxes": boxes}


# -- cache ---------------------------------------------------------------
def _gen_i(key: str) -> str:
    gen, _, i = key.partition("#")
    return f"{gen}_{i or '0'}"


def cache_path(state_dir, key: str, rev: int) -> Path:
    return Path(state_dir) / "anim_cache" / f"{_gen_i(key)}_{rev}.png"


def purge_cache(state_dir, key: str, keep_rev: int | None = None) -> None:
    d = Path(state_dir) / "anim_cache"
    if not d.is_dir():
        return
    prefix = _gen_i(key) + "_"
    keep = None if keep_rev is None else f"{prefix}{keep_rev}.png"
    for p in d.glob(f"{prefix}*.png"):
        if p.name != keep and p.name[len(prefix):-4].isdigit():
            p.unlink(missing_ok=True)


def write_sheet(state_dir, key: str, rev: int, img: Image.Image) -> Path:
    dest = cache_path(state_dir, key, rev)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    img.save(tmp, format="PNG")
    os.replace(tmp, dest)
    purge_cache(state_dir, key, keep_rev=rev)
    return dest
