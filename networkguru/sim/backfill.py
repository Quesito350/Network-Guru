"""Fast-forward the simulated site through the real pipeline to build history.

Rules, events and stored metrics all come out exactly as they would have live, so the
History view has 30 days of believable data the moment the dashboard starts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("networkguru.backfill")


@dataclass
class Progress:
    start: float = 0.0
    end: float = 0.0
    at: float = 0.0
    done: bool = True
    wall_s: float = 0.0

    @property
    def pct(self) -> float:
        if self.done or self.end <= self.start:
            return 100.0
        return max(0.0, min(100.0, 100.0 * (self.at - self.start) / (self.end - self.start)))


async def backfill(world, pipeline, events, tsdb, start: float, end: float, progress: Progress | None = None,
                   coarse_step: float = 300.0, fine_step: float = 60.0, fine_window: float = 6 * 3600.0,
                   yield_every: int = 6) -> int:
    """Simulate [start, end) in steps; returns the number of steps."""
    progress = progress or Progress()
    progress.start, progress.end, progress.at, progress.done = start, end, start, False
    t0 = time.perf_counter()
    comps = list(world.inv.comps)
    t, n = start, 0
    next_beat = start
    while t < end:
        step = fine_step if end - t <= fine_window else coarse_step
        pipeline.ingest([world.sample(cid, t) for cid in comps], now=t, fast=True)
        if t >= next_beat:
            events.beat(t)
            next_beat = t + 3600
        n += 1
        t += step
        progress.at = t
        if n % yield_every == 0:
            await asyncio.sleep(0)          # keep the web server responsive
        if n % 288 == 0:
            await tsdb.flush()
    events.beat(end)
    await tsdb.flush()
    progress.done = True
    progress.wall_s = time.perf_counter() - t0
    log.info("backfill: %d steps (%.1f days) in %.1f s", n, (end - start) / 86400, progress.wall_s)
    return n
