"""WebP animado aplanado de una escena (spec 3 §6).

``scene`` es el ``scene.json`` v1 con animación (§5) con nombres de archivo;
``open_file(nombre) -> PIL.Image`` resuelve cada nombre. Las hojas de ``frames`` son
tiras horizontales de N frames iguales (``background-size: N·100% 100%`` en JS).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from . import scene_math as sm
from .errors import GIAError

_INVALID = "invalid_composition"


def _is_animated(layer: dict) -> bool:
    if layer.get("visible", True) is False:
        return False
    anim = layer.get("animation") or {"type": "static"}
    kind = anim.get("type")
    if kind == "motion":
        return True
    if kind == "frames":
        return anim["frames"] > 1
    if kind == "states":
        return len(anim.get("states") or []) > 0
    return False


def scene_is_animated(scene: dict) -> bool:
    return any(_is_animated(ly) for ly in scene["layers"])


def _resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    size = (max(1, size[0]), max(1, size[1]))
    if img.size == size:
        return img
    # Premultiplicado: evita el halo oscuro de los píxeles transparentes.
    return img.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


def _over(canvas: Image.Image, img: Image.Image, x: int, y: int) -> None:
    """alpha_composite de img sobre canvas en (x, y), recortando lo que se sale."""
    cw, ch = canvas.size
    iw, ih = img.size
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(iw, cw - x), min(ih, ch - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if (sx0, sy0, sx1, sy1) != (0, 0, iw, ih):
        img = img.crop((sx0, sy0, sx1, sy1))
    canvas.alpha_composite(img, (x + sx0, y + sy0))


def _with_opacity(img: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return img
    lut = [int(math.floor(v * max(opacity, 0.0) + 0.5)) for v in range(256)]
    r, g, b, a = img.split()
    return Image.merge("RGBA", (r, g, b, a.point(lut)))


class _Renderer:
    def __init__(self, scene: dict, open_file: Callable[[str], Image.Image],
                 T: int, max_width: int):
        self.scene = scene
        self.T = T
        self.W = scene["width"]
        self.H = scene["height"]
        self.k = min(1.0, max_width / self.W)
        self.size = (max(1, round(self.W * self.k)), max(1, round(self.H * self.k)))
        self._open = open_file
        self._files: dict[str, Image.Image] = {}
        self._scaled: dict[tuple, Image.Image] = {}
        self._frames: dict[tuple, Image.Image] = {}
        self.layers = [ly for ly in scene["layers"] if ly.get("visible", True) is not False]
        self.animated = scene_is_animated(scene)

    # -- imágenes ----------------------------------------------------------
    def file(self, name: str) -> Image.Image:
        img = self._files.get(name)
        if img is None:
            img = self._open(name).convert("RGBA")
            self._files[name] = img
        return img

    def px_box(self, x, y, w, h) -> tuple[int, int, int, int]:
        """Caja de escena → (x, y, w, h) enteros en px de salida."""
        k = self.k
        x0, y0 = round(x * k), round(y * k)
        x1, y1 = round((x + w) * k), round((y + h) * k)
        return x0, y0, max(1, x1 - x0), max(1, y1 - y0)

    def scaled(self, name: str, box) -> tuple[Image.Image, int, int]:
        x, y, w, h = self.px_box(*box)
        key = (name, w, h)
        img = self._scaled.get(key)
        if img is None:
            img = _resize(self.file(name), (w, h))
            self._scaled[key] = img
        return img, x, y

    def frame(self, name: str, n: int, idx: int, box) -> tuple[Image.Image, int, int]:
        x, y, w, h = self.px_box(*box)
        key = (name, n, idx, w, h)
        img = self._frames.get(key)
        if img is None:
            sheet = self.file(name)
            fw = sheet.width // n
            img = _resize(sheet.crop((idx * fw, 0, (idx + 1) * fw, sheet.height)), (w, h))
            self._frames[key] = img
        return img, x, y

    # -- fases cuantizadas -------------------------------------------------
    def _cycles(self, ticks: float) -> int:
        return sm.cycles_for(self.T, ticks)

    def motion_p(self, anim: dict, j: int) -> float:
        cycles = self._cycles(sm.motion_ticks(anim["period"]))
        return sm.motion_phase_tick(j, cycles, self.T, anim.get("phase", 0))

    def seq_index(self, j: int, count: int, loop: str, seconds_per_item: float) -> int:
        length = sm.seq_length(count, loop)
        cycles = self._cycles(sm.sequence_ticks(length, seconds_per_item))
        s = sm.seq_index_tick(j, cycles, length, self.T)
        return sm.seq_to_index(s, count, loop)

    # -- dibujo ------------------------------------------------------------
    def render(self, j: int) -> Image.Image:
        canvas = Image.new("RGBA", self.size, (0, 0, 0, 0))
        for ly in self.layers:
            self._draw_layer(canvas, ly, j)
        return canvas

    def _draw_layer(self, canvas, ly, j):
        anim = ly.get("animation") or {"type": "static"}
        kind = anim.get("type")
        box = (ly["x"], ly["y"], ly["w"], ly["h"])

        if kind == "motion":
            self._draw_motion(canvas, ly, anim, box, j)
        elif kind == "frames":
            n = anim["frames"]
            idx = self.seq_index(j, n, anim.get("loop", "loop"), 1.0 / anim["fps"])
            img, x, y = self.frame(anim["file"], n, idx, (anim["x"], anim["y"], anim["w"], anim["h"]))
            _over(canvas, img, x, y)
        elif kind == "states":
            states = anim.get("states") or []
            m = 1 + len(states)
            loop = anim.get("loop", "loop")
            hold = anim["hold"]
            if loop == "once":
                idx = sm.once_index_tick(j, hold, m)
            else:
                idx = self.seq_index(j, m, loop, hold)
            if idx == 0:
                img, x, y = self.scaled(ly["file"], box)
            else:
                st = states[idx - 1]
                img, x, y = self.scaled(st["file"], (st["x"], st["y"], st["w"], st["h"]))
            _over(canvas, img, x, y)
        else:
            img, x, y = self.scaled(ly["file"], box)
            _over(canvas, img, x, y)

    def _draw_motion(self, canvas, ly, anim, box, j):
        k = self.k
        preset = anim["preset"]
        p = self.motion_p(anim, j)
        base, bx, by = self.scaled(ly["file"], box)
        amp = anim["amplitude"]
        if preset == "bob":
            dy = sm.bob_from_p(amp, p)
            _over(canvas, base, bx, by + round(dy * k))
        elif preset == "fall":
            copies = anim.get("copies", 3)
            dys, ops = sm.fall_from_p(amp, copies, p)
            for dy, op in zip(dys, ops):
                if op <= 0:
                    continue
                _over(canvas, _with_opacity(base, op), bx, by + round(dy * k))
        elif preset == "pulse":
            s = sm.pulse_from_p(amp, p)
            px, py = anim["pivot"]
            ox, oy = px * k, py * k
            bw, bh = base.size
            nw, nh = max(1, round(bw * s)), max(1, round(bh * s))
            nx = round(ox - (ox - bx) * s)
            ny = round(oy - (oy - by) * s)
            _over(canvas, _resize(base, (nw, nh)), nx, ny)
        elif preset == "sway":
            theta = sm.sway_from_p(amp, p)
            px, py = anim["pivot"]
            bw, bh = base.size
            pad = int(math.ceil(math.hypot(bw, bh) * math.radians(abs(amp)))) + 2
            tile = Image.new("RGBA", (bw + 2 * pad, bh + 2 * pad), (0, 0, 0, 0))
            tile.paste(base, (pad, pad))
            center = (px * k - bx + pad, py * k - by + pad)
            rot = (
                tile.convert("RGBa")
                .rotate(-theta, resample=Image.BICUBIC, expand=False, center=center)
                .convert("RGBA")
            )
            _over(canvas, rot, bx - pad, by - pad)
        else:
            raise GIAError(_INVALID, f"preset desconocido: {preset}")


def render_tick(scene: dict, open_file: Callable[[str], Image.Image], j: int,
                *, T: int = sm.DEFAULT_T, max_width: int = 800) -> Image.Image:
    """Escena en el tick ``j`` (entero; 0…T), a ancho ≤ max_width."""
    return _Renderer(scene, open_file, T, max_width).render(j)


def composite_at_t0(scene: dict, open_file: Callable[[str], Image.Image]) -> Image.Image:
    """Escena en t = 0, a tamaño completo (composite.png)."""
    return render_tick(scene, open_file, 0, max_width=scene["width"])


def _check_webp(out: Path, count: int, planned_ms: int) -> None:
    """Reopen the saved file: it must be a WebP, a static scene must have exactly 1 frame, and an animated one
    at least 1 whose durations add up to the planned ones (libwebp may merge identical consecutive frames,
    summing their durations, so the frame count can be lower)."""
    with Image.open(out) as check:
        if check.format != "WEBP":
            raise GIAError(_INVALID, "este Pillow no puede crear WebP animado")
        n = getattr(check, "n_frames", 1)
        if n < 1 or (count == 1 and n != 1):
            raise GIAError(_INVALID, "este Pillow no puede crear WebP animado")
        if count > 1:
            total = 0
            for k in range(n):
                check.seek(k)
                check.load()
                total += check.info.get("duration", 0)
            if total != planned_ms:
                raise GIAError(_INVALID, "este Pillow no puede crear WebP animado")


def render_webp(scene: dict, open_file: Callable[[str], Image.Image], out_path,
                *, T: int = sm.DEFAULT_T, tick_fps: int = sm.TICK_FPS,
                max_width: int = 800) -> None:
    """Escribe el WebP animado (1 frame si no hay capas animadas)."""
    r = _Renderer(scene, open_file, T, max_width)
    out = Path(out_path)
    count = T if r.animated else 1
    try:
        durations = sm.tick_durations(T, tick_fps)
        first = r.render(0)
        if count == 1:
            first.save(out, "WEBP", quality=85, lossless=False)
        else:
            rest = (r.render(j) for j in range(1, count))
            first.save(
                out, "WEBP", save_all=True, append_images=rest,
                duration=durations, loop=0,
                quality=85, lossless=False,
            )
        _check_webp(out, count, sum(durations))
    except GIAError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GIAError(_INVALID, "este Pillow no puede crear WebP animado") from exc
