"""The last known position of every Mobius vehicle, for the map (0.9.3).

Kept per Mobius vehicle name - the vehicles not in the inventory yet too - with when the vehicle was
last heard and whether it last said it was talking to Mobius. Saved in <data dir>/state/positions.json
(at most every SAVE_EVERY_S, only when something changed), so a parked vehicle keeps its place on the
map across a restart. The file stays on the server; problem reports get counts only.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .state import StateFile

log = logging.getLogger("networkguru")

SAVE_EVERY_S = 60.0
LIVE_LINK_S = 600.0         # as the Mobius reader: link true, but nothing for this long - not live
LIVE_S = 120.0              # no link value: live while something arrived this recently


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
        """Every vehicle with a position: {name, group (id or None), lat, lon, t, live}. `match`: name -> group id."""
        now = self.clock() if now is None else now
        return sorted(({"name": n, "group": match(n), "lat": e["lat"], "lon": e["lon"], "t": e["t"],
                        "live": self.live(e, now)} for n, e in self.data.items() if "lat" in e),
                      key=lambda v: v["name"].lower())

    def counts(self, match, now: float | None = None) -> dict:
        """For the problem report - never a position."""
        vs = self.public(match, now)
        now = self.clock() if now is None else now
        return {"with_position": len(vs), "matched": sum(1 for v in vs if v["group"]),
                "live": sum(1 for v in vs if v["live"]),
                "newest_age_s": round(now - max(v["t"] for v in vs)) if vs else None}
