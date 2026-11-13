import time

import pytest
from PIL import Image, ImageChops

from musa import scene_math as sm
from musa import scene_render as sr
from musa.errors import GIAError


def _solid(size, color):
    return Image.new("RGBA", size, color)


def _sprite(size, color):
    """Pieza con borde transparente y un parche asimétrico para ver movimiento."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (w - 8, h - 8), color), (4, 4))
    img.paste(Image.new("RGBA", (w // 4, h // 4), (255, 255, 255, 255)), (w // 2, 8))
    return img


def make_scene(scale=1):
    W, H = 400 * scale, 300 * scale
    s = scale
    files = {
        "bg.png": _solid((W, H), (30, 60, 90, 255)),
        "bob.png": _sprite((40 * s, 60 * s), (200, 40, 40, 255)),
        # hoja de 4 frames de 30x40, cada uno de un color
        "sheet.png": _strip([(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255)], 30, 40),
        "jar.png": _solid((50 * s, 50 * s), (10, 200, 200, 255)),
        "jar-1.png": _solid((54 * s, 54 * s), (200, 200, 10, 255)),
        "coin.png": _sprite((20 * s, 20 * s), (240, 200, 0, 255)),
    }
    layers = [
        {"id": "a", "name": "Fondo", "role": "background", "visible": True, "file": "bg.png",
         "x": 0, "y": 0, "w": W, "h": H, "animation": {"type": "static"}},
        {"id": "b", "name": "Bob", "role": "piece", "visible": True, "file": "bob.png",
         "x": 50 * s, "y": 100 * s, "w": 40 * s, "h": 60 * s,
         "animation": {"type": "motion", "preset": "bob", "amplitude": 12, "period": 2.0,
                       "phase": 0.1, "pivot": [0, 0]}},
        {"id": "c", "name": "Frames", "role": "piece", "visible": True, "file": "bob.png",
         "x": 150 * s, "y": 100 * s, "w": 30 * s, "h": 40 * s,
         "animation": {"type": "frames", "file": "sheet.png", "frames": 4, "fps": 10,
                       "loop": "pingpong", "x": 150 * s, "y": 100 * s, "w": 30 * s, "h": 40 * s}},
        {"id": "d", "name": "States", "role": "piece", "visible": True, "file": "jar.png",
         "x": 250 * s, "y": 100 * s, "w": 50 * s, "h": 50 * s,
         "animation": {"type": "states", "hold": 0.8, "loop": "loop",
                       "states": [{"file": "jar-1.png", "x": 248 * s, "y": 98 * s, "w": 54 * s, "h": 54 * s}]}},
        {"id": "e", "name": "Fall", "role": "piece", "visible": True, "file": "coin.png",
         "x": 330 * s, "y": 120 * s, "w": 20 * s, "h": 20 * s,
         "animation": {"type": "motion", "preset": "fall", "amplitude": 100 * s, "period": 1.2,
                       "phase": 0.2, "copies": 3, "pivot": [0, 0]}},
        {"id": "f", "name": "Sway", "role": "piece", "visible": True, "file": "bob.png",
         "x": 50 * s, "y": 200 * s, "w": 40 * s, "h": 60 * s,
         "animation": {"type": "motion", "preset": "sway", "amplitude": 8, "period": 2.4,
                       "phase": 0.3, "pivot": [70 * s, 260 * s]}},
        {"id": "g", "name": "Pulse", "role": "piece", "visible": True, "file": "bob.png",
         "x": 150 * s, "y": 200 * s, "w": 40 * s, "h": 60 * s,
         "animation": {"type": "motion", "preset": "pulse", "amplitude": 0.2, "period": 1.6,
                       "phase": 0.15, "pivot": [170 * s, 230 * s]}},
    ]
    scene = {"version": 1, "name": "t", "width": W, "height": H, "layers": layers}
    return scene, files


def _strip(colors, fw, fh):
    sheet = Image.new("RGBA", (fw * len(colors), fh))
    for i, c in enumerate(colors):
        sheet.paste(Image.new("RGBA", (fw, fh), c), (i * fw, 0))
    return sheet


def _opener(files):
    return lambda name: files[name].copy()


def _only(scene, lid):
    """Escena con fondo + una sola capa animada."""
    return {**scene, "layers": [ly for ly in scene["layers"] if ly["id"] in ("a", lid)]}


def _maxdiff(a, b):
    return max(hi for _, hi in ImageChops.difference(a, b).getextrema())


@pytest.mark.parametrize("scale", [1, 3])
def test_render_webp_frames_width_duration(tmp_path, scale):
    scene, files = make_scene(scale)
    out = tmp_path / "s.webp"
    t0 = time.perf_counter()
    sr.render_webp(scene, _opener(files), out)
    elapsed = time.perf_counter() - t0
    print(f"render_webp scale={scale} ({scene['width']}px -> <=800): {elapsed:.2f}s")
    with Image.open(out) as im:
        assert 1 < im.n_frames <= 72   # libwebp may merge identical consecutive ticks (durations are summed)
        assert im.width <= 800
        total = 0
        for i in range(im.n_frames):
            im.seek(i)
            im.load()
            total += im.info["duration"]
        assert total == 6000
    assert elapsed < 10


def test_render_webp_every_tick_differs_keeps_72_frames(tmp_path):
    scene, files = make_scene()
    sub = _only(scene, "e")
    anim = sub["layers"][1]["animation"]
    anim.update(amplitude=110, period=6.0, phase=0, copies=1)   # ~1.5 px per tick, inside the canvas
    out = tmp_path / "fall.webp"
    sr.render_webp(sub, _opener(files), out)
    with Image.open(out) as im:
        assert im.n_frames == 72
        total = 0
        for i in range(im.n_frames):
            im.seek(i)
            im.load()
            total += im.info["duration"]
        assert total == 6000


def test_check_webp_rejects_wrong_duration_sum_and_non_webp(tmp_path):
    scene, files = make_scene()
    out = tmp_path / "ok.webp"
    sr.render_webp(scene, _opener(files), out)
    sr._check_webp(out, 72, 6000)
    for planned in (5999, 6001):
        with pytest.raises(GIAError) as e:
            sr._check_webp(out, 72, planned)
        assert e.value.code == "invalid_composition"
    png = tmp_path / "x.webp"
    Image.new("RGB", (4, 4)).save(png, "PNG")
    with pytest.raises(GIAError):
        sr._check_webp(png, 72, 6000)
    with pytest.raises(GIAError):   # a static scene must be a single frame
        sr._check_webp(out, 1, 6000)


def test_scale_applies(tmp_path):
    scene, files = make_scene(3)  # 1200 px
    img = sr.render_tick(scene, _opener(files), 0)
    assert img.width == 800 and img.height == 600
    assert sr.composite_at_t0(scene, _opener(files)).size == (1200, 900)


@pytest.mark.parametrize("lid", ["b", "c", "d", "e", "f", "g"])
def test_loop_closes(lid):
    scene, files = make_scene()
    sub = _only(scene, lid)
    a = sr.render_tick(sub, _opener(files), 0)
    b = sr.render_tick(sub, _opener(files), 72)
    assert _maxdiff(a, b) <= 1


def test_bob_moves():
    scene, files = make_scene()
    sub = _only(scene, "b")
    a = sr.render_tick(sub, _opener(files), 0)
    b = sr.render_tick(sub, _opener(files), 9)  # 1/4 de ciclo aprox.
    assert _maxdiff(a, b) > 50
    assert a.getpixel((60, 100 + 20)) != b.getpixel((60, 100 + 20)) or a.tobytes() != b.tobytes()


def test_frames_and_states_change():
    scene, files = make_scene()
    f = _only(scene, "c")
    assert sr.render_tick(f, _opener(files), 0).tobytes() != sr.render_tick(f, _opener(files), 10).tobytes()
    # el frame 0 (rojo) cae dentro de L
    assert sr.render_tick(f, _opener(files), 0).getpixel((165, 120))[:3] == (255, 0, 0)
    s = _only(scene, "d")
    p0 = sr.render_tick(s, _opener(files), 0).getpixel((275, 125))
    p1 = sr.render_tick(s, _opener(files), 10).getpixel((275, 125))
    assert p0[:3] == (10, 200, 200) and p1[:3] == (200, 200, 10)


def test_states_once_holds_last():
    scene, files = make_scene()
    sub = _only(scene, "d")
    sub["layers"][1]["animation"]["loop"] = "once"
    last = sr.render_tick(sub, _opener(files), 71).getpixel((275, 125))
    assert last[:3] == (200, 200, 10)


def test_static_scene_one_frame(tmp_path):
    scene, files = make_scene()
    scene["layers"] = [scene["layers"][0]]
    out = tmp_path / "static.webp"
    sr.render_webp(scene, _opener(files), out)
    with Image.open(out) as im:
        assert im.n_frames == 1


def test_hidden_animated_layer_is_static(tmp_path):
    scene, files = make_scene()
    for ly in scene["layers"][1:]:
        ly["visible"] = False
    assert not sr.scene_is_animated(scene)


def test_composite_at_t0_matches_tick0():
    scene, files = make_scene()
    a = sr.composite_at_t0(scene, _opener(files))
    b = sr.render_tick(scene, _opener(files), 0)
    assert a.size == b.size and a.tobytes() == b.tobytes()


def test_webp_failure_raises(tmp_path, monkeypatch):
    scene, files = make_scene()
    monkeypatch.setattr(sm, "tick_durations", lambda T, fps: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(GIAError) as ei:
        sr.render_webp(scene, _opener(files), tmp_path / "x.webp")
    assert ei.value.code == "invalid_composition"
