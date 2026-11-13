"""Matemática común de la composición animada (spec 3 §2 y §6).

Funciones puras. Hay una copia idéntica en ``web/musa-scene.js`` (``MusaScene._math``);
ambas pasan los vectores de ``tests/fixtures/scene_golden.json``.

Convenciones: una caja es ``[x0, y0, x1, y1]``; una colocación es ``(x, y, w, h)``.
"""

from __future__ import annotations

import math

TWO_PI = 2.0 * math.pi
TICK_FPS = 12
DEFAULT_T = 72
FALL_FADE = 0.15

PRESET_DEFAULTS = {
    "bob": {"amplitude": 6, "period": 2.0},
    "sway": {"amplitude": 4, "period": 2.4},
    "pulse": {"amplitude": 0.04, "period": 1.6},
    "fall": {"amplitude": None, "period": 1.2},
}


def frac(x: float) -> float:
    return x - math.floor(x)


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# --- Fase y presets -------------------------------------------------------

def phase_at(t: float, period: float, phase: float) -> float:
    """p = frac(t/period + phase)."""
    return frac(t / period + phase)


def bob_from_p(amplitude: float, p: float) -> float:
    return -amplitude * math.sin(TWO_PI * p)


def sway_from_p(amplitude: float, p: float) -> float:
    return amplitude * math.sin(TWO_PI * p)


def pulse_from_p(amplitude: float, p: float) -> float:
    return 1.0 + amplitude * math.sin(TWO_PI * p)


def fall_from_p(amplitude: float, copies: int, p: float) -> tuple[list[float], list[float]]:
    """(dy por copia, opacidad por copia)."""
    dys: list[float] = []
    ops: list[float] = []
    for c in range(copies):
        pc = frac(p + c / copies)
        dys.append(-amplitude * (1.0 - pc))
        ops.append(min(1.0, pc / FALL_FADE, (1.0 - pc) / FALL_FADE))
    return dys, ops


def bob(amplitude: float, period: float, phase: float, t: float) -> float:
    return bob_from_p(amplitude, phase_at(t, period, phase))


def sway(amplitude: float, period: float, phase: float, t: float) -> float:
    return sway_from_p(amplitude, phase_at(t, period, phase))


def pulse(amplitude: float, period: float, phase: float, t: float) -> float:
    return pulse_from_p(amplitude, phase_at(t, period, phase))


def fall(amplitude: float, period: float, phase: float, copies: int, t: float):
    return fall_from_p(amplitude, copies, phase_at(t, period, phase))


def fall_auto_amplitude(height: float) -> int:
    """Amplitud automática de ``fall``: round(1.2·alto de la caja)."""
    return round_half_up(1.2 * height)


def pivot(preset: str, box: list[float]) -> list[float]:
    """Pivote en px de escena a partir de la caja L=[x0,y0,x1,y1]."""
    x0, y0, x1, y1 = box
    if preset == "sway":
        return [(x0 + x1) / 2, y1]
    if preset == "pulse":
        return [(x0 + x1) / 2, (y0 + y1) / 2]
    return [0, 0]


# --- Secuencias (frames / states) -----------------------------------------

def seq_length(n: int, loop: str) -> int:
    """Longitud de la secuencia de índices: n (loop) o 2n-2 (pingpong, n>1)."""
    if loop == "pingpong" and n > 1:
        return 2 * n - 2
    return max(n, 1)


def seq_to_index(s: int, n: int, loop: str) -> int:
    """Índice de elemento para la posición s de la secuencia (ya módulo longitud)."""
    if loop == "pingpong" and n > 1:
        return s if s < n else 2 * n - 2 - s
    return s


def frames_index(n: int, fps: float, loop: str, t: float) -> int:
    k = int(math.floor(t * fps))
    length = seq_length(n, loop)
    return seq_to_index(k % length, n, loop)


def states_index(m: int, hold: float, loop: str, t: float) -> int:
    k = int(math.floor(t / hold))
    if loop == "once":
        return min(k, m - 1)
    length = seq_length(m, loop)
    return seq_to_index(k % length, m, loop)


# --- Colocación -----------------------------------------------------------

def frames_placement(box, frame_w, frame_h, frame_boxes):
    """(x, y, w, h) de un frame sobre la capa de caja L; F = primer frame no nulo."""
    f = next((b for b in frame_boxes if b is not None), None)
    if f is None:
        raise ValueError("frame_boxes sin ningún frame")
    s = (box[3] - box[1]) / (f[3] - f[1])
    return (box[0] - f[0] * s, box[1] - f[1] * s, frame_w * s, frame_h * s)


def states_placement(bbox_k, offset_layer, offset_k):
    """(x, y, w, h) del estado k ≥ 1: bbox_k + offset_capa + offset_k."""
    x = bbox_k[0] + offset_layer[0] + offset_k[0]
    y = bbox_k[1] + offset_layer[1] + offset_k[1]
    return (x, y, bbox_k[2] - bbox_k[0], bbox_k[3] - bbox_k[1])


# --- Cuantización del WebP (§6) -------------------------------------------

def cycles_for(T: int, ticks: float) -> int:
    """max(1, floor(T/ticks + 0.5))."""
    return max(1, round_half_up(T / ticks))


def motion_phase_tick(j: int, cycles: int, T: int, phase: float) -> float:
    """p = frac(j·cycles/T + phase) con la parte entera en aritmética de enteros."""
    return frac((j * cycles) % T / T + phase)


def seq_index_tick(j: int, cycles: int, length: int, T: int) -> int:
    """floor(j·cycles·L/T) mod L, todo en enteros."""
    return (j * cycles * length // T) % length


def once_index_tick(j: int, hold: float, m: int, tick_fps: int = TICK_FPS) -> int:
    return min(int(math.floor(j / (hold * tick_fps))), m - 1)


def motion_ticks(period: float, tick_fps: int = TICK_FPS) -> float:
    return period * tick_fps


def sequence_ticks(length: int, seconds_per_item: float, tick_fps: int = TICK_FPS) -> float:
    """Ciclo en ticks: length · segundos por elemento · tick_fps."""
    return length * seconds_per_item * tick_fps


def tick_durations(T: int = DEFAULT_T, tick_fps: int = TICK_FPS) -> list[int]:
    """Duraciones en ms por tick; la suma es round(T·1000/tick_fps) (6000 para T=72)."""
    return [
        round_half_up((j + 1) * 1000 / tick_fps) - round_half_up(j * 1000 / tick_fps)
        for j in range(T)
    ]
