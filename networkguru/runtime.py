"""Wires the pieces together for one running dashboard (store, events, pipeline, pollers)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

import httpx

from . import __version__
from .auth import AdminAuth, hash_password
from .events import EventStore
from .history import History
from .inventory import load_inventory
from .pipeline import Pipeline
from .rules import RuleSet, load_rules
from .scheduler import Scheduler, sim_pollers
from .sim.backfill import Progress, backfill
from .sim.world import SimWorld
from .notify import ReportService
from .report import report_from_runtime
from .site_settings import open_settings
from .tsdb import MemoryTSDB, VictoriaTSDB, periodic_flush
from .updates import UpdateManager

log = logging.getLogger("networkguru")
DAY = 86400.0


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.inv = load_inventory(settings.inventory)
        self.rules = RuleSet(load_rules(settings.rules))
        self.mode = settings.mode
        self.tsdb = None
        self.events: EventStore | None = None
        self.pipeline: Pipeline | None = None
        self.history: History | None = None
        self.world: SimWorld | None = None
        self.scheduler: Scheduler | None = None
        self.progress = Progress()
        self.notes: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.started = time.time()
        self.settings_state = open_settings(settings.data_dir, settings.public_url)
        self.auth = AdminAuth(dev_hash=hash_password(settings.dev_admin_password, n=16384)
                              if settings.dev_admin_password else None)
        self.updates: UpdateManager | None = None
        self.reports: ReportService | None = None

    @property
    def docker(self) -> bool:
        """In a container: a site server (install.sh) or the test install (compose.yaml)."""
        return self.settings.deploy in ("docker", "compose")

    @property
    def persistent(self) -> bool:
        return isinstance(self.tsdb, VictoriaTSDB)

    async def start(self) -> None:
        s = self.settings
        self.tsdb = await self._open_tsdb()
        db_path: str | Path = ":memory:"
        if self.persistent or self.docker:              # the server always keeps its events on /data
            db_path = Path(s.data_dir) / f"{self.mode}-{self.inv.site.id}.db"
            if s.fresh and self.mode == "sim":
                await self._wipe_sim_history(Path(db_path))
        self.events = EventStore(db_path)
        self.pipeline = Pipeline(self.inv, self.rules, self.tsdb, self.events)
        self.history = History(self.inv, self.rules, self.tsdb, self.events)
        tz = self.pipeline.tz
        self.updates = UpdateManager(Path(s.data_dir), s.deploy, tz, self.settings_state, self.events)
        self.reports = ReportService(Path(s.data_dir), self.settings_state,
                                     lambda **kw: report_from_runtime(self, **kw), tz, self.events,
                                     ready=self._report_ready)
        self.tasks.append(asyncio.create_task(self.updates.loop()))
        self.tasks.append(asyncio.create_task(self.reports.daily_job().loop()))
        if self.persistent:
            self.tasks.append(asyncio.create_task(periodic_flush(self.tsdb)))
        self.tasks.append(asyncio.create_task(self._housekeeping()))
        if self.mode == "sim":
            self.notes.insert(0, "Simulated data - this dashboard is not connected to any device.")
            self.world = SimWorld(self.inv)
            now = time.time()
            start = now - s.history_days * DAY
            resume = self.events.get_meta("sim_until") if self.persistent else None
            if resume and await self.tsdb.has_data_since(now - 31 * DAY):
                start = max(float(resume), start)
            self.events.start_run(start, __version__, "sim")
            self.tasks.append(asyncio.create_task(self._sim_main(start)))
        else:
            self.events.start_run(time.time(), __version__, "live")
            self.notes.append("No device drivers are installed in this version yet (they arrive in 0.6.0) - "
                              "every device shows Unknown.")
            self.scheduler = Scheduler(self.pipeline, [], self.events)
            self.tasks.append(asyncio.create_task(self.scheduler.run()))

    async def _open_tsdb(self):
        url = self.settings.vm_url
        if url and self.docker:
            # On the server VictoriaMetrics may still be starting (both containers start together
            # after a reboot). Never fall back to memory there: writes are buffered until it answers.
            vm = VictoriaTSDB(url)
            if not await vm.ping():
                note = f"VictoriaMetrics at {url} is not answering yet - history is buffered until it does."
                self.notes.append(note)
                self.tasks.append(asyncio.create_task(self._await_vm(vm, note)))
            return vm
        if url:
            vm = VictoriaTSDB(url)
            if await vm.ping():
                return vm
            await vm.close()
            self.notes.append(f"VictoriaMetrics at {url} is not answering - history is kept in memory only "
                              "and is lost on restart.")
        elif self.mode != "sim":
            self.notes.append("No VictoriaMetrics configured - history is kept in memory only.")
        return MemoryTSDB()

    async def _await_vm(self, vm: VictoriaTSDB, note: str) -> None:
        while not await vm.ping():
            await asyncio.sleep(5)
        with contextlib.suppress(ValueError):
            self.notes.remove(note)
        log.info("VictoriaMetrics is answering")

    def _report_ready(self) -> bool:
        """The morning report waits for real numbers: the end of the simulator's backfill, or the
        collector's first minute (a report sent the moment the server starts would be all Unknown)."""
        if self.mode == "sim":
            return self.progress.done
        sch = self.scheduler
        return bool(sch and sch.last_tick) and time.time() - self.started > 60

    async def _wipe_sim_history(self, db_path: Path) -> None:
        with contextlib.suppress(OSError):
            for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
                if p.exists():
                    p.unlink()
        with contextlib.suppress(httpx.HTTPError):
            await self.tsdb.client.post(f"{self.tsdb.url}/api/v1/admin/tsdb/delete_series",
                                        params={"match[]": f'{{site="{self.inv.site.id}"}}'})

    async def _sim_main(self, start: float) -> None:
        try:
            await backfill(self.world, self.pipeline, self.events, self.tsdb, start, time.time(), self.progress)
            self.scheduler = Scheduler(self.pipeline, sim_pollers(self.world), self.events)
            await self.scheduler.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("simulator stopped")
            self.notes.append("The simulator stopped with an error - see the server log.")

    async def _housekeeping(self) -> None:
        last_prune = 0.0
        while True:
            await asyncio.sleep(30)
            now = time.time()
            if self.mode == "sim" and self.progress.done:
                self.events.set_meta("sim_until", str(now))
            if self.world:
                self.world.faults = [f for f in self.world.faults if f.end > now]
            if now - last_prune > 3600:
                self.tsdb.prune(now)
                self.events.prune(now - 90 * DAY)
                last_prune = now

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.events and self.mode == "sim" and self.progress.done:
            self.events.set_meta("sim_until", str(time.time()))
        if self.tsdb:
            await self.tsdb.close()
        if self.events:
            self.events.close()

    def dead_tasks(self) -> list[str]:
        out = []
        for t in self.tasks:
            if t.done() and not t.cancelled() and t.exception() is not None:
                out.append(f"{t.get_coro().__qualname__}: {type(t.exception()).__name__}")
        return out

    def health(self) -> dict:
        sch = self.scheduler
        dead = self.dead_tasks()
        return {"ok": not dead and self.events is not None, "dead_tasks": dead, "version": __version__,
                "mode": self.mode, "deploy": self.settings.deploy, "tsdb": self.tsdb.kind if self.tsdb else None,
                "tsdb_error": getattr(self.tsdb, "last_error", None),
                "tsdb_buffer": len(getattr(self.tsdb, "buffer", []) or []),
                "backfill_pct": round(self.progress.pct, 1), "collector_running": bool(sch and sch.running),
                "last_tick": sch.last_tick if sch else None, "loop_ms": round(sch.loop_lag_ms, 1) if sch else None,
                "uptime_s": round(time.time() - self.started)}
