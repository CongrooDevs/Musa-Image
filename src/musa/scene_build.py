"""Per-layer animation data and scene.json building for compositions (spec 3 §1, §3, §5, §7).

Pure functions: no I/O. The manager supplies `names`, an object that turns layer/sheet/state images
into the `file` strings of the scene (archive names for the export, URLs for Musa).
"""
import math

from . import scene_math as sm
from .errors import GIAError, INVALID_COMPOSITION

STATIC = {"type": "static"}
MAX_STATES = 6
DEFAULT_HOLD = 0.8
STATE_LOOPS = ("loop", "pingpong", "once")
_AMPLITUDE_RANGE = {"bob": (1, 60), "sway": (1, 30), "pulse": (0.01, 0.3), "fall": (10, 2000)}
_MOTION_KEYS = {"type", "preset", "amplitude", "period", "phase", "copies"}


def _bad(msg: str, **details) -> GIAError:
    return GIAError(INVALID_COMPOSITION, msg, **details)


def _num(value, label: str, lo: float, hi: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not lo <= value <= hi:
        raise _bad(f"{label} debe ser un número entre {lo} y {hi}", field=label)
    return value


def animation_of(layer: dict) -> dict:
    """The layer's stored animation (static when it has none)."""
    return layer.get("animation") or STATIC


def validate_animation_patch(layer: dict, body) -> dict:
    """The new stored animation for PATCH `animation`: static, motion, or (when already states) hold/loop."""
    if not isinstance(body, dict):
        raise _bad("animation debe ser un objeto")
    kind = body.get("type")
    if kind == "static":
        if set(body) - {"type"}:
            raise _bad("static no admite más campos")
        return dict(STATIC)
    if layer["role"] == "background":
        raise _bad("el fondo solo admite animación static")
    if kind == "motion":
        extra = sorted(set(body) - _MOTION_KEYS)
        if extra:
            raise _bad(f"campos de movimiento desconocidos: {', '.join(extra)}", fields=extra)
        if not layer.get("file"):
            raise _bad("la capa aún no tiene imagen para animar")
        preset = body.get("preset")
        if preset not in sm.PRESET_DEFAULTS:
            raise _bad(f"preset no válido {preset!r}", valid=sorted(sm.PRESET_DEFAULTS))
        defaults = sm.PRESET_DEFAULTS[preset]
        amp = body.get("amplitude")
        if amp is None:
            amp = defaults["amplitude"]  # None only for fall: auto
        else:
            amp = _num(amp, "amplitude", *_AMPLITUDE_RANGE[preset])
        out = {"type": "motion", "preset": preset, "amplitude": amp,
               "period": _num(body.get("period", defaults["period"]), "period", 0.3, 10),
               "phase": _num(body.get("phase", 0), "phase", 0, 1)}
        if preset == "fall":
            copies = body.get("copies", 3)
            if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 5:
                raise _bad("copies debe ser un entero entre 1 y 5", field="copies")
            out["copies"] = copies
        elif "copies" in body:
            raise _bad("copies solo existe en el preset fall")
        return out
    if kind == "states":
        cur = animation_of(layer)
        if cur.get("type") != "states":
            raise _bad("los estados se crean con «Añadir estado»; por PATCH solo se ajusta hold/loop")
        extra = sorted(set(body) - {"type", "hold", "loop"})
        if extra:
            raise _bad(f"campos de estados desconocidos: {', '.join(extra)}", fields=extra)
        out = dict(cur)
        if "hold" in body:
            out["hold"] = _num(body["hold"], "hold", 0.2, 10)
        if "loop" in body:
            if body["loop"] not in STATE_LOOPS:
                raise _bad("loop debe ser loop, pingpong u once", valid=list(STATE_LOOPS))
            out["loop"] = body["loop"]
        return out
    raise _bad("animation.type debe ser static, motion o (para ajustar) states; frames y estados se generan "
               "con sus propias acciones")


def layer_box(layer: dict) -> list[int]:
    """L = bbox + offset as [x0, y0, x1, y1]."""
    b, o = layer["bbox"], layer["offset"]
    return [b[0] + o[0], b[1] + o[1], b[2] + o[0], b[3] + o[1]]


def frames_view(anim: dict, ficha: dict | None) -> dict:
    """Read view of a frames animation: the stored data + the ficha summary, or static+broken."""
    if ficha is None or not any(b is not None for b in ficha.get("frame_boxes") or []):
        return {"type": "static", "broken": True}
    return {**anim, "frames": ficha["frames"], "fps": ficha.get("fps", 10), "loop": ficha.get("loop", "loop"),
            "frame_w": ficha["frame_w"], "frame_h": ficha["frame_h"], "sheet_rev": ficha.get("rev")}


def _scene_animation(comp: dict, ly: dict, box: list[int], ficha_of, names) -> dict:
    anim = animation_of(ly)
    kind = anim.get("type")
    if ly["role"] == "background":
        return dict(STATIC)
    if kind == "motion":
        preset = anim["preset"]
        amp = anim["amplitude"]
        if preset == "fall" and amp is None:
            amp = sm.fall_auto_amplitude(box[3] - box[1])
        out = {"type": "motion", "preset": preset, "amplitude": amp, "period": anim["period"],
               "phase": anim.get("phase", 0), "pivot": sm.pivot(preset, box)}
        if preset == "fall":
            out["copies"] = anim.get("copies", 3)
        return out
    if kind == "frames":
        ficha = ficha_of(anim["image"])
        if frames_view(anim, ficha).get("broken"):
            return dict(STATIC)
        try:
            fname = names.sheet(ly, anim["image"], ficha)
        except GIAError:
            return dict(STATIC)
        x, y, w, h = sm.frames_placement(box, ficha["frame_w"], ficha["frame_h"], ficha["frame_boxes"])
        return {"type": "frames", "file": fname, "frames": ficha["frames"], "fps": ficha.get("fps", 10),
                "loop": ficha.get("loop", "loop"), "x": round(x, 2), "y": round(y, 2),
                "w": round(w, 2), "h": round(h, 2)}
    if kind == "states" and anim.get("states"):
        states = []
        for k, st in enumerate(anim["states"], 1):
            x, y, w, h = sm.states_placement(st["bbox"], ly["offset"], st["offset"])
            states.append({"file": names.state(ly, k, st), "x": x, "y": y, "w": w, "h": h})
        return {"type": "states", "hold": anim.get("hold", DEFAULT_HOLD), "loop": anim.get("loop", "loop"),
                "states": states}
    return dict(STATIC)


def build_scene(comp: dict, ficha_of, names) -> dict:
    """scene.json v1 with animation. Only `done` layers with a file are included (bottom first).
    `ficha_of(key)` returns the animation ficha of an image; `names` maps images to `file` strings."""
    m = comp["master"]
    W, H = m["width"], m["height"]
    layers = []
    for ly in comp["layers"]:
        if ly["status"] != "done" or not ly.get("file"):
            continue
        box = layer_box(ly)
        bg = ly["role"] == "background"
        layers.append({"id": ly["id"], "name": ly["name"], "role": ly["role"], "visible": ly["visible"],
                       "file": names.layer(ly), "x": 0 if bg else box[0], "y": 0 if bg else box[1],
                       "w": W if bg else box[2] - box[0], "h": H if bg else box[3] - box[1],
                       "animation": _scene_animation(comp, ly, box, ficha_of, names)})
    return {"version": 1, "name": comp["name"], "width": W, "height": H, "layers": layers}
