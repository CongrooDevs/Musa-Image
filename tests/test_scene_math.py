import json
import math
from pathlib import Path

import pytest

from gemini_image_api import scene_math as m

FIXTURE = Path(__file__).parent / "fixtures" / "scene_golden.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def evaluate(c):
    p, t, ty = c["params"], c["t"], c["type"]
    if ty == "bob":
        return {"dy": m.bob(p["amplitude"], p["period"], p["phase"], t)}
    if ty == "sway":
        return {"theta": m.sway(p["amplitude"], p["period"], p["phase"], t)}
    if ty == "pulse":
        return {"s": m.pulse(p["amplitude"], p["period"], p["phase"], t)}
    if ty == "fall":
        dy, op = m.fall(p["amplitude"], p["period"], p["phase"], p["copies"], t)
        return {"dy": dy, "opacity": op}
    if ty == "frames":
        return {"index": m.frames_index(p["n"], p["fps"], p["loop"], t)}
    if ty == "states":
        return {"index": m.states_index(p["m"], p["hold"], p["loop"], t)}
    if ty == "frames_placement":
        return {"box": list(m.frames_placement(p["box"], p["frame_w"], p["frame_h"], p["frame_boxes"]))}
    if ty == "states_placement":
        return {"box": list(m.states_placement(p["bbox_k"], p["offset_layer"], p["offset_k"]))}
    if ty == "pivot":
        return {"pivot": list(m.pivot(p["preset"], p["box"]))}
    if ty == "fall_auto":
        return {"amplitude": m.fall_auto_amplitude(p["height"])}
    raise AssertionError(ty)


def assert_close(actual, expected, path=""):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), path
        for k in expected:
            assert_close(actual[k], expected[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for i, (a, e) in enumerate(zip(actual, expected)):
            assert_close(a, e, f"{path}[{i}]")
    else:
        assert actual == pytest.approx(expected, abs=1e-6), path


def test_fixture_size_and_coverage():
    assert len(CASES) >= 30
    kinds = {c["type"] for c in CASES}
    assert {"bob", "sway", "pulse", "fall", "frames", "states", "frames_placement",
            "states_placement", "pivot", "fall_auto"} <= kinds
    loops = {(c["type"], c["params"].get("loop")) for c in CASES}
    assert {("frames", "loop"), ("frames", "pingpong"), ("states", "loop"),
            ("states", "pingpong"), ("states", "once")} <= loops
    assert any(c["type"] == "bob" and c["params"]["phase"] != 0 for c in CASES)


@pytest.mark.parametrize("i", range(len(CASES)))
def test_golden(i):
    assert_close(evaluate(CASES[i]), CASES[i]["expect"])


def test_hand_checked_values():
    assert m.bob(6, 2.0, 0, 0.5) == pytest.approx(-6)
    assert m.sway(4, 2.4, 0, 0.6) == pytest.approx(4)
    assert m.pulse(0.04, 1.6, 0, 1.2) == pytest.approx(0.96)
    dy, op = m.fall(120, 1.2, 0, 3, 0)
    assert dy[0] == pytest.approx(-120) and op[0] == pytest.approx(0)
    assert m.fall_auto_amplitude(539) == 647


def test_pingpong_sequence():
    seq = [m.frames_index(4, 10, "pingpong", k / 10) for k in range(8)]
    assert seq == [0, 1, 2, 3, 2, 1, 0, 1]
    assert [m.states_index(3, 1, "once", k) for k in range(5)] == [0, 1, 2, 2, 2]


def test_quantization():
    assert m.cycles_for(72, 28.8) == 3          # sway 2.4 s -> 3.0 s
    assert m.cycles_for(72, 144) == 1           # 0.5 -> 1 (empate hacia arriba)
    assert m.cycles_for(72, 1000) == 1
    assert m.cycles_for(72, 48) == 2            # 1.5 -> 2
    assert m.seq_index_tick(0, 3, 4, 72) == 0
    assert m.seq_index_tick(71, 1, 4, 72) == 3
    assert m.motion_phase_tick(72, 3, 72, 0.25) == pytest.approx(0.25)
    assert m.once_index_tick(100, 0.8, 3) == 2
    d = m.tick_durations()
    assert len(d) == 72 and sum(d) == 6000 and set(d) <= {83, 84}


def test_pivot_and_placement():
    assert m.pivot("sway", [100, 200, 300, 500]) == [200, 500]
    assert m.pivot("pulse", [100, 200, 300, 500]) == [200, 350]
    assert m.pivot("fall", [100, 200, 300, 500]) == [0, 0]
    x, y, w, h = m.frames_placement([100, 50, 200, 250], 64, 64, [[8, 4, 40, 60]])
    s = 200 / 56
    assert (x, y) == pytest.approx((100 - 8 * s, 50 - 4 * s))
    assert (w, h) == pytest.approx((64 * s, 64 * s))
    assert not math.isnan(x)
