"""Polling loop: asks every poller for results when it is due, one pipeline batch per tick.

A poller is anything with `comp_ids`, `interval`, `timeout` and `async poll(now)`. The
simulator's pollers and (from 0.4) the real device drivers both plug in here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from .catalog import type_spec
from .model import CompResult

log = logging.getLogger("networkguru.scheduler")


class Poller(Protocol):
    comp_ids: list[str]
    interval: float
    timeout: float

    async def poll(self, now: float) -> list[CompResult]: ...


class SimPoller:
    """One component of the simulated world."""

    def __init__(self, world, comp_id: str, interval: float):
        self.world = world
        self.comp_ids = [comp_id]
        self.interval = interval
        self.timeout = 5.0

    async def poll(self, now: float) -> list[CompResult]:
        return [self.world.sample(self.comp_ids[0], now)]


def sim_pollers(world) -> list[SimPoller]:
    return [SimPoller(world, cid, type_spec(c.type).interval) for cid, c in world.inv.comps.items()]


class Scheduler:
    def __init__(self, pipeline, pollers: list, events=None, clock=time.time, tick: float = 1.0,
                 sweep_every: float = 10.0, beat_every: float = 30.0, recheck_after: float = 5.0):
        self.recheck_after = recheck_after       # parent re-check at most this often
        self.pipeline = pipeline
        self.pollers = pollers
        self.events = events
        self.clock = clock
        self.tick = tick
        self.sweep_every = sweep_every
        self.beat_every = beat_every
        self.running = False
        self.last_tick: float | None = None
        self.loop_lag_ms = 0.0

    async def _poll(self, p, now: float) -> list[CompResult]:
        try:
            return await asyncio.wait_for(p.poll(now), timeout=p.timeout)
        except asyncio.TimeoutError:
            return [CompResult(cid, now, False, error="timeout") for cid in p.comp_ids]
        except Exception as e:  # noqa: BLE001 - a broken driver must never stop the loop
            log.exception("poller for %s failed", p.comp_ids)
            return [CompResult(cid, now, False, error=f"driver error: {type(e).__name__}") for cid in p.comp_ids]

    async def run(self) -> None:
        self.running = True
        start = self.clock()
        # spread first polls over each interval so devices aren't all hit at once
        due = {id(p): start + (i * 0.37) % max(p.interval, 1.0) for i, p in enumerate(self.pollers)}
        last = {id(p): 0.0 for p in self.pollers}
        poller_of = {cid: p for p in self.pollers for cid in p.comp_ids}
        comps = self.pipeline.inv.comps
        next_sweep, next_beat = start + self.sweep_every, start
        try:
            while True:
                t0 = time.perf_counter()
                now = self.clock()
                ready = [p for p in self.pollers if due[id(p)] <= now]
                if ready:
                    batches = await asyncio.gather(*(self._poll(p, now) for p in ready))
                    results = [r for b in batches for r in b]
                    self.pipeline.ingest(results, now)
                    for p in ready:
                        due[id(p)], last[id(p)] = now + p.interval, now
                    # a device stopped answering: check the device in front of it right away,
                    # so the real failure is found before the devices behind it raise alarms
                    for r in results:
                        dep = comps[r.comp_id].depends_on if not r.reachable and r.comp_id in comps else None
                        up = poller_of.get(dep) if dep else None
                        if up is not None and now - last[id(up)] >= self.recheck_after:
                            due[id(up)] = min(due[id(up)], now)
                if now >= next_sweep:
                    self.pipeline.sweep(now)
                    next_sweep = now + self.sweep_every
                if self.events is not None and now >= next_beat:
                    self.events.beat(now)
                    next_beat = now + self.beat_every
                self.last_tick = now
                spent = time.perf_counter() - t0
                self.loop_lag_ms = spent * 1000
                await asyncio.sleep(max(0.05, self.tick - spent))
        finally:
            self.running = False
