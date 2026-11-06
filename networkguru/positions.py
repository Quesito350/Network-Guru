"""The last known position of every Mobius vehicle, for the map (0.9.3).

Kept per Mobius vehicle name - the vehicles not in the inventory yet too - with when the vehicle was
last heard and whether it last said it was talking to Mobius. Saved in <data dir>/state/positions.json
(at most every SAVE_EVERY_S, only when something changed), so a parked vehicle keeps its place on the
map across a restart. The file stays on the server; problem reports get counts only.

0.9.5: the way a vehicle last drove (`heading`, degrees from north, once it has moved MOVED_M - saved, so
a parked one keeps pointing that way) and its trail: the points of the last TRAIL_S, in memory only, for
the map's fading line behind a live vehicle.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from pathlib import Path

from .state import StateFile

log = logging.getLogger("networkguru")

SAVE_EVERY_S = 60.0
LIVE_LINK_S = 600.0         # as the Mobius reader: link true, but nothing for this long - not live
LIVE_S = 120.0              # no link value: live while something arrived this recently
TRAIL_S = 120.0             # the map's trail behind a live vehicle
MOVED_M = 2.0               # moved this far: a new heading (GPS wanders less than this standing still)
JUMP_M = 500.0              # further than this between two points: a new trail, not a line across the site


def metres(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """(east, north) from a to b, in metres - flat earth, fine over a site."""
    return ((b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2)), (b[0] - a[0]) * 110_540.0)


def valid(lat, lon) -> bool:
    return (all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (lat, lon))
            and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0))


class VehiclePositions:
    def __init__(self, path: str | Path | None = None, clock=time.time):
        self.file = StateFile(Path(path)) if path else None
        self.clock = clock
        saved = self.file.get("vehicles") if self.file else None
        self.data: dict[str, dict] = {str(k): dict(v) for k, v in (saved or {}).items() if isinstance(v, dict)}
        self.dirty = False
        self.saved_at = clock()
        self.trails: dict[str, deque] = {}         # name -> (t, lat, lon), the last TRAIL_S
        self.anchor: dict[str, tuple[float, float]] = {}     # where the heading was last measured from

    def _entry(self, name: str) -> dict:
        return self.data.setdefault(str(name), {})

    def heard(self, name: str, t: float) -> None:
        e = self._entry(name)
        if t > e.get("heard", 0.0):
            e["heard"] = t
            self.dirty = True

    def position(self, name: str, lat, lon, t: float) -> None:
        if not valid(lat, lon):
            return
        e = self._entry(name)
        if t >= e.get("t", 0.0) and (e.get("lat"), e.get("lon"), e.get("t")) != (lat, lon, t):
            e.update(lat=float(lat), lon=float(lon), t=float(t))
            self.dirty = True
            self._moved(name, e)

    def _moved(self, name: str, e: dict) -> None:
        here = (e["lat"], e["lon"])
        trail = self.trails.setdefault(name, deque())
        if trail and math.hypot(*metres(trail[-1][1:], here)) > JUMP_M:
            trail.clear()
        trail.append((e["t"], *here))
        while trail and e["t"] - trail[0][0] > TRAIL_S:
            trail.popleft()
        start = self.anchor.setdefault(name, here)
        east, north = metres(start, here)
        if math.hypot(east, north) >= MOVED_M:
            if math.hypot(east, north) <= JUMP_M:
                e["heading"] = round(math.degrees(math.atan2(east, north)) % 360.0, 1)
            self.anchor[name] = here

    def link(self, name: str, on: bool, t: float) -> None:
        e = self._entry(name)
        if t >= e.get("link_t", 0.0) and (e.get("link"), e.get("link_t")) != (on, t):
            e.update(link=bool(on), link_t=float(t))
            self.dirty = True

    def live(self, e: dict, now: float) -> bool:
        if e.get("link") is False:
            return False
        heard = e.get("heard") or e.get("t") or 0.0
        return now - heard <= (LIVE_LINK_S if e.get("link") else LIVE_S)

    def save(self, force: bool = False) -> None:
        now = self.clock()
        if not self.file or not self.dirty or (not force and now - self.saved_at < SAVE_EVERY_S):
            return
        try:
            self.file.update(vehicles=self.data)
            self.dirty, self.saved_at = False, now
        except OSError as e:
            log.warning("vehicle positions not saved (%s)", e)

    def public(self, match, now: float | None = None) -> list[dict]:
        """Every vehicle with a position: {name, group (id or None), lat, lon, t, live, heading, trail}.
        `match`: name -> group id. The trail (oldest first, ending at the position) for live vehicles only."""
        now = self.clock() if now is None else now
        out = []
        for n, e in self.data.items():
            if "lat" not in e:
                continue
            live = self.live(e, now)
            trail = [[round(la, 7), round(lo, 7)] for _t, la, lo in self.trails.get(n, ())] if live else []
            out.append({"name": n, "group": match(n), "lat": e["lat"], "lon": e["lon"], "t": e["t"], "live": live,
                        "heading": e.get("heading"), "trail": trail if len(trail) > 1 else []})
        return sorted(out, key=lambda v: v["name"].lower())

    def counts(self, match, now: float | None = None) -> dict:
        """For the problem report - never a position."""
        vs = self.public(match, now)
        now = self.clock() if now is None else now
        return {"with_position": len(vs), "matched": sum(1 for v in vs if v["group"]),
                "live": sum(1 for v in vs if v["live"]),
                "newest_age_s": round(now - max(v["t"] for v in vs)) if vs else None}
