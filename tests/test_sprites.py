import time

import pytest
from PIL import Image, ImageDraw
from sprite_synth import grid_lines, jump_sheet, walk_sheet, white_char_sheet

from musa import sprites
from musa.errors import INVALID_ANIMATION, GIAError

GREEN = (0, 255, 0)


def _ficha(img, cols, rows, mode="sprite", key="green", tol=25):
    d = sprites.detect_cells(img, cols, rows, mode, key if mode == "sprite" else None, tol)
    return dict(cols=cols, rows=rows, mode=mode, key_color=key if mode == "sprite" else None,
                tolerance=tol, **d)


def _cuts(cells):
    return sorted({c[0] for c in cells} - {0}), sorted({c[1] for c in cells} - {0})


def _near(got, want, tol=4):
    assert len(got) == len(want), (got, want)
    for g, w in zip(got, want):
        assert abs(g - w) <= tol, (got, want)


@pytest.mark.parametrize("make,cols,rows,size,gutters", [
    (lambda: walk_sheet(True), 4, 2, (1376, 768), True),
    (lambda: walk_sheet(False), 4, 2, (1376, 768), False),
    (jump_sheet, 3, 3, (1024, 1024), True),
])
def test_synthetic_sheet_cuts(make, cols, rows, size, gutters):
    img = make()
    d = sprites.detect_cells(img, cols, rows, "sprite", "green", 25)
    assert (d["detected_by"], d["status"]) == ("lanes", "ok")
    got_x, got_y = _cuts(d["cells"])
    want_x, want_y = grid_lines(size, cols, rows, gutters, gutter=16 if size[0] == 1024 else 12)
    _near(got_x, want_x)
    _near(got_y, want_y)
    assert len(d["cells"]) == cols * rows


def test_jump_row_change_has_no_artificial_jump():
    img = jump_sheet()
    f = _ficha(img, 3, 3)
    sheet, meta = sprites.render_sheet(img, f)
    ys = [b[1] for b in meta["frame_boxes"]]
    assert abs(ys[3] - ys[2]) < 40 and abs(ys[6] - ys[5]) < 40
    assert sheet.size == (9 * meta["frame_w"], meta["frame_h"])
    assert sheet.mode == "RGBA"
    # the airborne frames really are higher than the crouch/landing ones
    assert ys[3] < ys[0] - 40


def test_walk_gutters_timing_size_and_centering():
    img = walk_sheet(True)
    t = time.perf_counter()
    f = _ficha(img, 4, 2)
    sheet, meta = sprites.render_sheet(img, f)
    dt = time.perf_counter() - t
    print("synthetic 1376x768 detect+render seconds:", round(dt, 3))
    assert dt < 6
    fw = meta["frame_w"]
    assert sheet.size == (8 * fw, meta["frame_h"])
    assert len(meta["frame_boxes"]) == 8 and all(meta["frame_boxes"])
    assert sheet.getpixel((0, 0))[3] == 0  # keyed background is transparent
    for k in range(8):
        frame = sheet.crop((k * fw, 0, (k + 1) * fw, meta["frame_h"]))
        box = sprites.alpha_bbox(frame)
        assert abs(sprites.alpha_centroid_x(frame, box) - fw / 2) <= 1, k


def test_walk_without_gutters_renders():
    img = walk_sheet(False)
    f = _ficha(img, 4, 2)
    sheet, meta = sprites.render_sheet(img, f)
    assert f["status"] == "ok" and sheet.size == (8 * meta["frame_w"], meta["frame_h"])


def test_scene_mode_rgb_and_size():
    img = jump_sheet()
    f = _ficha(img, 3, 3, mode="scene")
    assert f["status"] == "ok"
    sheet, meta = sprites.render_sheet(img, f)
    assert sheet.mode == "RGB"
    assert sheet.size == (9 * meta["frame_w"], meta["frame_h"])
    assert meta["pad"] == 0 and meta["frame_boxes"][0] == [0, 0, meta["frame_w"], meta["frame_h"]]


def _discs(cols=4, rows=2, cell=200, r=25, centers=None, color=(220, 30, 40)):
    img = Image.new("RGB", (cols * cell, rows * cell), GREEN)
    dr = ImageDraw.Draw(img)
    for k, (dx, dy) in enumerate(centers):
        c, rw = k % cols, k // cols
        cx, cy = c * cell + dx, rw * cell + dy
        dr.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    return img


def test_synthetic_discs_centered_and_heights_preserved():
    centers = [(90, 60), (110, 100), (100, 80), (95, 120), (100, 50), (105, 90), (100, 110), (96, 70)]
    img = _discs(centers=centers)
    f = _ficha(img, 4, 2)
    assert (f["detected_by"], f["status"]) == ("lanes", "ok")
    sheet, meta = sprites.render_sheet(img, f)
    assert len(meta["frame_boxes"]) == 8
    fw = meta["frame_w"]
    for k, b in enumerate(meta["frame_boxes"]):
        assert abs((b[0] + b[2]) / 2 - fw / 2) <= 1, (k, b, fw)
    ys = [(b[1] + b[3]) / 2 for b in meta["frame_boxes"]]
    for k in range(1, 4):
        assert abs((ys[k] - ys[0]) - (centers[k][1] - centers[0][1])) <= 1
    for k in range(5, 8):
        assert abs((ys[k] - ys[4]) - (centers[k][1] - centers[4][1])) <= 1


def test_no_gutters_uniform_review_sprite_ok_scene():
    img = Image.new("RGB", (800, 400), (200, 60, 60))
    ImageDraw.Draw(img).rectangle((0, 0, 799, 399), outline=(10, 10, 10), width=3)
    d = sprites.detect_cells(img, 4, 2, "sprite", "green", 25)
    assert (d["detected_by"], d["status"]) == ("uniform", "review")
    assert d["cells"][1] == [200, 0, 400, 200]
    d = sprites.detect_cells(img, 4, 2, "scene", None, 25)
    assert (d["detected_by"], d["status"]) == ("uniform", "ok")


def test_gray_ramp_unblends_within_tolerance():
    bg = (0, 250, 10)
    n = 10
    img = Image.new("RGB", (n, 4))
    for i in range(n):
        c = (i + 1) / n
        px = tuple(round(c * 200 + (1 - c) * b) for b in bg)
        for y in range(4):
            img.putpixel((i, y), px)
    out = sprites.soft_key(img, "green", 25, bg=(bg, 240.0))
    checked = 0
    for i in range(n):
        r, g, b, a = out.getpixel((i, 0))
        if a >= 64:
            checked += 1
            assert max(abs(r - 200), abs(g - 200), abs(b - 200)) <= 12, (i, (r, g, b, a))
    assert checked >= 5
    assert out.getpixel((n - 1, 0))[3] == 255


def test_soft_key_pure_background_is_clear_and_zeroed():
    out = sprites.soft_key(Image.new("RGB", (20, 20), (0, 250, 10)), "green", 25)
    assert out.tobytes() == bytes(20 * 20 * 4)


def test_magenta_key():
    img = Image.new("RGB", (40, 40), (255, 0, 255))
    ImageDraw.Draw(img).rectangle((10, 10, 29, 29), fill=(30, 200, 40))
    out = sprites.soft_key(img, "magenta", 25)
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((20, 20)) == (30, 200, 40, 255)


def test_white_character_on_green_finds_lanes():
    img = white_char_sheet()
    f = _ficha(img, 4, 2)
    assert (f["detected_by"], f["status"]) == ("lanes", "ok")
    xs, ys = _cuts(f["cells"])
    _near(xs, [200, 400, 600], 2)
    _near(ys, [200], 2)
    sheet, meta = sprites.render_sheet(img, f)
    assert all(b is not None for b in meta["frame_boxes"])


def test_sheet_width_cap():
    cell = 600
    img = Image.new("RGB", (4 * cell, cell), GREEN)
    dr = ImageDraw.Draw(img)
    for c in range(4):
        dr.rectangle((c * cell + 50, 50, c * cell + 550, 550), fill=(200, 30, 30))
    f = _ficha(img, 4, 1)
    f2 = dict(f, cols=40, rows=1, cells=f["cells"] * 10)
    sheet, meta = sprites.render_sheet(img, f2)
    assert sheet.size[0] <= 8192
    assert sheet.size == (40 * meta["frame_w"], meta["frame_h"])


def test_thin_dark_grid_lines_win_over_lopsided_lanes_and_are_trimmed():
    # Real Gemini case: 1-2 px dark-green rules between cells and characters sitting left of
    # centre, so the empty lane around each rule is lopsided and its centre misses the rule.
    cols, rows, cell = 4, 2, 200
    img = Image.new("RGB", (cols * cell, rows * cell), (37, 245, 19))
    dr = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * cell + 20, r * cell + 60
            dr.ellipse((x0, y0, x0 + 90, y0 + 90), fill=(230, 110, 30), outline=(60, 30, 10), width=3)
    for k in range(1, cols):
        dr.line((k * cell, 0, k * cell, rows * cell), fill=(0, 125, 0), width=2)
    dr.line((0, cell, cols * cell, cell), fill=(0, 125, 0), width=2)
    f = _ficha(img, cols, rows)
    assert (f["detected_by"], f["status"]) == ("lanes", "ok")
    xs, ys = _cuts(f["cells"])
    _near(xs, [200, 400, 600], tol=2)
    _near(ys, [200], tol=2)
    sheet, meta = sprites.render_sheet(img, f)
    # every frame holds only the disc: no rule fragments widen or heighten the boxes
    for b in meta["frame_boxes"]:
        assert b[2] - b[0] <= 96 and b[3] - b[1] <= 96, b


def test_empty_cell_is_review_and_null_box():
    img = _discs(centers=[(100, 100)] * 8)
    ImageDraw.Draw(img).rectangle((400, 0, 599, 199), fill=GREEN)
    f = _ficha(img, 4, 2)
    sheet, meta = sprites.render_sheet(img, f)
    assert meta["frame_boxes"][2] is None


def test_prompts():
    p = sprites.sheet_prompt("a cat walking", 4, 2, "sprite", "green")
    assert "Subject and action: a cat walking" in p and "4 columns x 2 rows" in p
    assert "chroma green #00FF00" in p and "exactly 8 frames" in p
    assert "FF00FF" in sprites.sheet_prompt("x", 3, 3, "sprite", "magenta")
    s = sprites.sheet_prompt("a beach", 2, 2, "scene", None)
    assert "animated scene" in s and "Background" not in s and "edge to edge" in s
    assert "4x2 grid" in sprites.edit_suffix({"cols": 4, "rows": 2, "mode": "sprite", "key_color": "green"})
    assert "edge to edge" in sprites.edit_suffix({"cols": 2, "rows": 2, "mode": "scene", "key_color": None})
    assert sprites.GRIDS["4x2"] == (4, 2, "16:9")


def test_validate_animation_request():
    assert sprites.validate_animation_request({"grid": "3x3"}) == \
        {"grid": "3x3", "mode": "sprite", "key_color": "green"}
    assert sprites.validate_animation_request({"grid": "2x2", "mode": "scene", "key_color": "magenta"}) == \
        {"grid": "2x2", "mode": "scene", "key_color": None}
    for bad in [{"grid": "5x5"}, {"grid": "4x2", "mode": "x"}, {"grid": "4x2", "key_color": "blue"},
                {"grid": "4x2", "fps": 10}, "4x2", None]:
        with pytest.raises(GIAError) as e:
            sprites.validate_animation_request(bad)
        assert e.value.code == INVALID_ANIMATION


def test_validate_settings():
    assert sprites.validate_settings(cols=4, rows=2, fps=12, loop="pingpong", tolerance=5) == \
        {"cols": 4, "rows": 2, "fps": 12, "loop": "pingpong", "tolerance": 5}
    assert sprites.validate_settings() == {}
    for kw in [dict(cols=0), dict(cols=9), dict(cols=1, rows=1), dict(cols=7, rows=6), dict(tolerance=4),
               dict(tolerance=81), dict(fps=1), dict(fps=31), dict(loop="x"), dict(mode="x"),
               dict(key_color="red"), dict(cols=True), dict(fps=2.5)]:
        with pytest.raises(GIAError) as e:
            sprites.validate_settings(**kw)
        assert e.value.code == INVALID_ANIMATION, kw


def test_cache_write_and_purge(tmp_path):
    key = "g_abcdef123456#1"
    other = "g_abcdef123456#10"
    img = Image.new("RGBA", (4, 4))
    p1 = sprites.write_sheet(tmp_path, key, 100, img)
    po = sprites.write_sheet(tmp_path, other, 7, img)
    assert p1 == sprites.cache_path(tmp_path, key, 100) and p1.name == "g_abcdef123456_1_100.png"
    p2 = sprites.write_sheet(tmp_path, key, 200, img)
    assert p2.exists() and not p1.exists() and po.exists()
    sprites.purge_cache(tmp_path, key)
    assert not p2.exists() and po.exists()


def test_rows_with_uneven_margins_share_the_ground_line():
    from sprite_synth import GREEN as SG
    from sprite_synth import _character
    w, h, top, gut, side, ch = 1376, 768, 27, 12, 12, 358
    cw = (w - 2 * side - 3 * gut) // 4
    img = Image.new("RGB", (w, h), (255, 255, 255))
    dr = ImageDraw.Draw(img)
    for r in range(2):
        for c in range(4):
            x0, y0 = side + c * (cw + gut), top + r * (ch + gut)
            dr.rectangle((x0, y0, x0 + cw - 1, y0 + ch - 1), fill=SG)
            _character(img, x0 + cw // 2 + (c - 2) * 5, y0 + 300)
    f = _ficha(img, 4, 2)
    assert (f["detected_by"], f["status"]) == ("lanes", "ok")
    _, meta = sprites.render_sheet(img, f)
    y1 = [b[3] for b in meta["frame_boxes"]]
    assert max(y1) - min(y1) <= 3, y1


def test_no_gutter_rows_use_the_ideal_origin_when_the_lane_is_asymmetric():
    from sprite_synth import GREEN as SG
    from sprite_synth import _character
    w, h, ground = 1376, 768, 278
    img = Image.new("RGB", (w, h), SG)
    for r in range(2):
        for c in range(4):
            _character(img, round((c + 0.5) * w / 4) + (c - 2) * 5, round(r * h / 2) + ground)
    f = _ficha(img, 4, 2)
    assert (f["detected_by"], f["status"]) == ("lanes", "ok")
    cut = f["cells"][4][1]
    assert abs(cut - 384) >= 8, cut  # the lane centre really is off the ideal cut
    _, meta = sprites.render_sheet(img, f)
    y1 = [b[3] for b in meta["frame_boxes"]]
    assert max(y1) - min(y1) <= 3, y1
