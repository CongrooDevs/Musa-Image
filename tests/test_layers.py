import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from musa.errors import GIAError
from musa.layers import composite, layer_png, normalize_size, prepare_piece, search_offset
from sprite_synth import composition_set


@pytest.fixture(scope="module")
def cs():
    return composition_set()


def test_prepare_piece_bbox(cs):
    _, _, piece, box = cs
    rgba, bbox = prepare_piece(piece, "green", 25)
    assert rgba.size == piece.size and rgba.mode == "RGBA"
    assert all(abs(a - b) <= 3 for a, b in zip(bbox, box))


def test_prepare_piece_empty_raises():
    with pytest.raises(GIAError) as e:
        prepare_piece(Image.new("RGB", (100, 80), (0, 255, 0)), "green", 25)
    assert e.value.code == "invalid_image"


def test_prepare_piece_drops_stray_objects_but_keeps_attached_parts():
    # Real Gemini case: asked for "only Hermes", it also kept the coin pile beside him.
    img = Image.new("RGB", (800, 600), (37, 245, 19))
    dr = ImageDraw.Draw(img)
    dr.rectangle((100, 200, 300, 500), fill=(220, 180, 100), outline=(10, 10, 10), width=4)  # body
    dr.ellipse((300, 150, 340, 190), fill=(240, 240, 240), outline=(10, 10, 10), width=3)   # wing, 10 px gap
    dr.ellipse((600, 420, 680, 480), fill=(200, 150, 60), outline=(10, 10, 10), width=3)     # stray coins
    rgba, bbox = prepare_piece(img, "green", 25)
    assert bbox[2] <= 345 and bbox[1] <= 152, bbox             # wing kept, coins dropped
    assert rgba.getpixel((640, 450))[3] == 0


def test_search_offset_zero(cs):
    master, _, piece, _ = cs
    rgba, bbox = prepare_piece(piece, "green", 25)
    assert search_offset(master, rgba, bbox) == (0, 0)


def test_search_offset_shifted_piece(cs):
    master, _, piece, _ = cs
    shifted = ImageChops.offset(piece, -5, 0)  # the piece was drawn 5 px to the left of the master
    rgba, bbox = prepare_piece(shifted, "green", 25)
    dx, dy = search_offset(master, rgba, bbox)
    assert abs(dx - 5) <= 1 and abs(dy) <= 1
    down = ImageChops.offset(piece, 0, 4)
    rgba, bbox = prepare_piece(down, "green", 25)
    dx, dy = search_offset(master, rgba, bbox)
    assert abs(dx) <= 1 and abs(dy + 4) <= 1


def test_normalize_size(cs):
    master = cs[0]
    assert normalize_size(master, master.size) is master
    scaled = normalize_size(master.resize((810, 600)), master.size)
    assert scaled.size == master.size
    with pytest.raises(GIAError) as e:
        normalize_size(Image.new("RGB", (600, 600)), master.size)
    assert e.value.code == "invalid_image"


def test_composite_reproduces_master_outside_piece(cs):
    master, plate, piece, _ = cs
    rgba, bbox = prepare_piece(piece, "green", 25)
    png = layer_png(rgba, bbox, (0, 0))
    assert png.size == (bbox[2] - bbox[0], bbox[3] - bbox[1])
    flat = composite(master.size, [
        {"role": "background", "image": plate, "bbox": [0, 0, *master.size], "offset": [0, 0], "visible": True},
        {"role": "piece", "image": png, "bbox": list(bbox), "offset": [0, 0], "visible": True}])
    assert flat.size == master.size and flat.mode == "RGB"
    mask = Image.new("L", master.size, 255)
    ImageDraw.Draw(mask).rectangle((bbox[0] - 6, bbox[1] - 6, bbox[2] + 6, bbox[3] + 6), fill=0)
    outside = ImageStat.Stat(ImageChops.difference(flat, master).convert("L"), mask).mean[0]
    assert outside < 1.0
    inside = Image.new("L", master.size, 0)
    ImageDraw.Draw(inside).rectangle(bbox, fill=255)
    assert ImageStat.Stat(ImageChops.difference(flat, master).convert("L"), inside).mean[0] < 12


def test_composite_hidden_piece_and_offset(cs):
    master, plate, piece, _ = cs
    rgba, bbox = prepare_piece(piece, "green", 25)
    png = layer_png(rgba, bbox, (0, 0))
    bg = {"role": "background", "image": plate, "bbox": [0, 0, *master.size], "offset": [0, 0], "visible": True}
    hidden = composite(master.size, [bg, {"role": "piece", "image": png, "bbox": list(bbox), "offset": [0, 0],
                                          "visible": False}])
    assert ImageChops.difference(hidden, plate).getbbox() is None
    moved = composite(master.size, [bg, {"role": "piece", "image": png, "bbox": list(bbox), "offset": [30, -20],
                                         "visible": True}])
    box = ImageChops.difference(moved, plate).getbbox()
    assert abs(box[0] - (bbox[0] + 30)) <= 3 and abs(box[1] - (bbox[1] - 20)) <= 3
