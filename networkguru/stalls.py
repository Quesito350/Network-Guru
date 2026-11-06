"""How long the server's event loop was blocked (0.9.6).

Everything - the readers, the web pages, the ping and port timers - shares one asyncio loop. When a
step blocks it (a slow calculation, a TLS context being built), nothing else runs, and every ping or
port time measured across that moment includes the stall: on the test PC that turned into Critical
"Ping RTT 900 ms" alarms on devices that answer in 5 ms. The meter sleeps TICK_S again and again; how
late it wakes is how long the loop was blocked. The ping and port checks ask `blocked(t0, t1)` and
leave a time out when the loop stalled while it was measured (drivers/reach.py).

`slow_steps(minutes)` turns on asyncio's own report of each step that blocked over SLOW_STEP_S
(Admin → "Find what slows the dashboard"); the lines go to the log and so into the problem report.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger("networkguru")

TICK_S = 0.1
KEEP_S = 600.0              # the stalls of the last 10 minutes
MIN_S = 0.05                # shorter is just scheduling
REPORT_S = 0.25             # health / the problem report count stalls over this
SLOW_STEP_S = 0.25


class StallMeter:
    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.stalls: deque[tuple[float, float]] = deque()      # (start, end), on `clock`
        self.last_wake: float | None = None                     # None: the meter is not running
        self.slow_until = 0.0                                    # wall time (slow_steps)

    async def run(self) -> None:
        self.last_wake = self.clock()
        try:
            while True:
                before = self.clock()
                await asyncio.sleep(TICK_S)
                now = self.clock()
                self.note(before + TICK_S, now)
                self.last_wake = now
                if self.slow_until and time.time() > self.slow_until:
                    self._slow_off()
        finally:
            self.last_wake = None

    def note(self, due: float, woke: float) -> None:
        if woke - due >= MIN_S:
            self.stalls.append((due, woke))
        while self.stalls and woke - self.stalls[0][1] > KEEP_S:
            self.stalls.popleft()

    def blocked(self, t0: float, t1: float) -> float:
        """Seconds of [t0, t1] (on `clock`) the loop was blocked - including a stall that is ending just
        now, before the meter itself has woken up to note it."""
        spans = list(self.stalls)
        if self.last_wake is not None and t1 - (self.last_wake + TICK_S) >= MIN_S:
            spans.append((self.last_wake + TICK_S, t1))
        return sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in spans)

    def summary(self) -> dict:
        now = self.clock()
        recent = [b - a for a, b in self.stalls if now - b <= KEEP_S]
        return {"running": self.last_wake is not None, "stall_max_ms": round(max(recent, default=0.0) * 1000),
                "stalls_250ms": sum(1 for d in recent if d >= REPORT_S),
                "slow_steps_until": self.slow_until or None}

    # -- Admin → "Find what slows the dashboard" ----------------------------------------
    def slow_steps(self, minutes: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = SLOW_STEP_S
        loop.set_debug(True)
        self.slow_until = time.time() + minutes * 60
        log.warning("finding slow steps for %.0f minutes: every step that blocks the server over %.2f s is logged",
                    minutes, SLOW_STEP_S)

    def _slow_off(self) -> None:
        try:
            asyncio.get_running_loop().set_debug(False)
        except RuntimeError:
            pass
        self.slow_until = 0.0
        log.warning("finding slow steps: done")


METER = StallMeter()          # one loop per server process
