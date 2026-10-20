"""Deterministic randomness: every simulated value is a pure function of (seed, time).

That makes the 30-day backfill and the live run join up exactly, and makes tests
repeatable.
"""

from __future__ import annotations

import math
import zlib

MASK = 0xFFFFFFFF


def seed_of(text: str) -> int:
    return zlib.crc32(text.encode("utf-8")) & MASK


def h32(*vals: int) -> int:
    x = 0x9E3779B9
    for v in vals:
        x = ((x ^ (int(v) & MASK)) * 0x85EBCA6B) & MASK
        x ^= x >> 13
        x = (x * 0xC2B2AE35) & MASK
        x ^= x >> 16
    return x


def rnd(*vals: int) -> float:
    """Uniform [0, 1)."""
    return h32(*vals) / 4294967296.0


def vnoise(seed: int, t: float, period: float) -> float:
    """Smooth value noise in [-1, 1] that changes over roughly `period` seconds."""
    x = t / period
    i = math.floor(x)
    f = x - i
    a = rnd(seed, i) * 2 - 1
    b = rnd(seed, i + 1) * 2 - 1
    s = f * f * (3 - 2 * f)
    return a + (b - a) * s


def fbm(seed: int, t: float, period: float) -> float:
    """Value noise with finer detail, still in about [-1, 1]."""
    return (0.6 * vnoise(seed, t, period) + 0.3 * vnoise(seed + 1, t, period / 3)
            + 0.1 * vnoise(seed + 2, t, period / 9))


def episode(seed: int, t: float, per_day: float, min_s: float, max_s: float) -> float | None:
    """Random episodes (outages, fades ...): returns seconds since the episode began when t
    is inside one, else None. Episodes start in hourly slots with probability per_day/24 and
    last a log-uniform time between min_s and max_s."""
    slot = math.floor(t / 3600)
    p = per_day / 24.0
    for k in range(slot - int(max_s // 3600) - 1, slot + 1):
        if rnd(seed, k, 1) >= p:
            continue
        start = k * 3600 + rnd(seed, k, 2) * 3600
        dur = min_s * (max_s / min_s) ** rnd(seed, k, 3)
        if start <= t < start + dur:
            return t - start
    return None
