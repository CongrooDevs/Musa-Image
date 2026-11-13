import json

import pytest

from gemini_image_api import scene_math as sm
from gemini_image_api.compositions import scene_index_html
from gemini_image_api.errors import GIAError
from gemini_image_api.scene_build import build_scene, frames_view, validate_animation_patch

LAYER = {"id": "ly_1", "role": "piece", "name": "Hermes", "file": "ly_1_1.png", "status": "done", "visible": True,
         "bbox": [100, 50, 160, 170], "offset": [3, -2], "rev": 5, "animation": {"type": "static"}}
BG = {"id": "ly_0", "role": "background", "name": "Fondo", "file": "ly_0_1.png", "status": "done", "visible": True,
      "bbox": [0, 0, 800, 600], "offset": [0, 0], "rev": 1}


class Names:
    def layer(self, ly):
        return f"{ly['id']}.png"

    def sheet(self, ly, key, ficha):
        return f"{key}-sheet.png"

    def state(self, ly, k, st):
        return f"{ly['id']}-{k}.png"


def comp(layer):
    return {"name": "Escena", "master": {"width": 800, "height": 600}, "layers": [dict(BG), layer]}


FICHA = {"frames": 4, "fps": 10, "loop": "loop", "frame_w": 120, "frame_h": 200, "rev": 9,
         "frame_boxes": [None, [20, 30, 80, 150], [20, 30, 80, 150], [20, 30, 80, 150]]}


def test_validate_motion_defaults_and_ranges():
    for preset, expect in (("bob", (6, 2.0)), ("sway", (4, 2.4)), ("pulse", (0.04, 1.6)), ("fall", (None, 1.2))):
        a = validate_animation_patch(LAYER, {"type": "motion", "preset": preset})
        assert (a["amplitude"], a["period"], a["phase"]) == (expect[0], expect[1], 0) and ("copies" in a) == (preset == "fall")
    assert validate_animation_patch(LAYER, {"type": "motion", "preset": "fall", "amplitude": 10, "copies": 1})["copies"] == 1
    assert validate_animation_patch(LAYER, {"type": "motion", "preset": "fall", "amplitude": None})["amplitude"] is None
    assert validate_animation_patch(LAYER, {"type": "motion", "preset": "pulse", "amplitude": 0.3, "phase": 1})["phase"] == 1
    for bad in ({"type": "motion", "preset": "bob", "amplitude": float("nan")}, {"type": "motion", "preset": "fall", "amplitude": 2001},
                {"type": "motion", "preset": "bob", "pivot": [0, 0]}):
        with pytest.raises(GIAError) as e:
            validate_animation_patch(LAYER, bad)
        assert e.value.code == "invalid_composition"
    with pytest.raises(GIAError):
        validate_animation_patch({**LAYER, "file": None}, {"type": "motion", "preset": "bob"})
    with pytest.raises(GIAError):
        validate_animation_patch(BG, {"type": "motion", "preset": "bob"})
    assert validate_animation_patch(BG, {"type": "static"}) == {"type": "static"}


def test_validate_states_adjustment_only_when_states():
    states = {"type": "states", "hold": 0.8, "loop": "loop", "states": [{"file": "a.png"}]}
    out = validate_animation_patch({**LAYER, "animation": states}, {"type": "states", "hold": 2, "loop": "once"})
    assert (out["hold"], out["loop"], out["states"]) == (2, "once", states["states"])
    with pytest.raises(GIAError):
        validate_animation_patch(LAYER, {"type": "states", "hold": 2})


def test_frames_view_broken():
    a = {"type": "frames", "image": "g_a#0"}
    assert frames_view(a, None) == {"type": "static", "broken": True}
    assert frames_view(a, {**FICHA, "frame_boxes": [None, None]}) == {"type": "static", "broken": True}
    v = frames_view(a, FICHA)
    assert (v["type"], v["frames"], v["fps"], v["sheet_rev"]) == ("frames", 4, 10, 9)


def test_build_scene_shapes():
    motion = {"type": "motion", "preset": "sway", "amplitude": 4, "period": 2.4, "phase": 0}
    s = build_scene(comp({**LAYER, "animation": motion}), lambda k: None, Names())
    assert s["version"] == 1 and (s["width"], s["height"]) == (800, 600) and s["name"] == "Escena"
    bg, ly = s["layers"]
    assert bg["animation"] == {"type": "static"} and (bg["x"], bg["y"], bg["w"], bg["h"]) == (0, 0, 800, 600)
    assert (ly["x"], ly["y"], ly["w"], ly["h"]) == (103, 48, 60, 120)
    assert ly["animation"]["pivot"] == [133.0, 168] and ly["animation"]["type"] == "motion"
    fall = {"type": "motion", "preset": "fall", "amplitude": None, "period": 1.2, "phase": 0, "copies": 2}
    a = build_scene(comp({**LAYER, "animation": fall}), lambda k: None, Names())["layers"][1]["animation"]
    assert a["amplitude"] == sm.fall_auto_amplitude(120) == 144 and a["copies"] == 2 and a["pivot"] == [0, 0]


def test_build_scene_frames_placement_matches_layer_box():
    layer = {**LAYER, "animation": {"type": "frames", "image": "g_a#0"}}
    s = build_scene(comp(layer), lambda k: FICHA, Names())
    an = s["layers"][1]["animation"]
    L = [103, 48, 163, 168]
    # frame 0 (first non-null box) lands on the static piece
    k = an["w"] / FICHA["frame_w"]
    assert abs(an["x"] + 20 * k - L[0]) <= 1 and abs(an["y"] + 30 * k - L[1]) <= 1
    assert abs(an["y"] + 150 * k - L[3]) <= 1 and an["file"] == "g_a#0-sheet.png" and an["frames"] == 4
    broken = build_scene(comp(layer), lambda k: None, Names())["layers"][1]["animation"]
    assert broken == {"type": "static"}


def test_build_scene_states_boxes_and_undone_layers():
    st = {"image": "g_b#0", "bbox": [98, 48, 164, 172], "offset": [1, 2], "file": "s.png", "rev": 3}
    layer = {**LAYER, "animation": {"type": "states", "hold": 0.8, "loop": "loop", "states": [st]}}
    s = build_scene(comp(layer), lambda k: None, Names())
    an = s["layers"][1]["animation"]
    assert an["states"] == [{"file": "ly_1-1.png", "x": 98 + 3 + 1, "y": 48 - 2 + 2, "w": 66, "h": 124}]
    pending = {**LAYER, "status": "pending", "file": None}
    assert [x["id"] for x in build_scene(comp(pending), lambda k: None, Names())["layers"]] == ["ly_0"]


def test_scene_index_html_escapes_script_breakers_and_title():
    name = "</script><b>x & \u2028 y\u2029"
    scene = {"version": 1, "name": name, "width": 800, "height": 600, "layers": []}
    page = scene_index_html(scene)
    data = page[page.index('id="scene-data">') + len('id="scene-data">'):page.index('</script>', page.index('id="scene-data">'))]
    assert "<" not in data and ">" not in data and "&" not in data and "\u2028" not in data and "\u2029" not in data
    assert json.loads(data) == scene
    assert "<title>&lt;/script&gt;&lt;b&gt;x &amp; \u2028 y\u2029</title>" in page
    assert page.count("</script>") == 3
