"""Wires the pieces together for one running dashboard (store, events, pipeline, pollers)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

import httpx

from . import __version__, logbuffer
from .auth import AdminAuth, hash_password
from .drivers import live_pollers
from .drivers.mobius import MobiusPoller
from .drivers.readers import MobiusServerReader
from .drivers.reach import icmp_available
from .events import EventStore
from .feedback import RateLimit
from .history import History
from .inventory import load_inventory
from .model import Info
from .pipeline import Pipeline
from .positions import VehiclePositions
from .problem_ticket import PER_HOUR as PROBLEMS_PER_HOUR, ReportStore
from .rules import RuleSet, load_rules
from .scheduler import Scheduler, sim_pollers
from .sim.backfill import Progress, backfill
from .sim.world import SimWorld
from .stalls import METER
from .notify import ReportService
from .report import report_from_runtime
from .site_settings import open_settings
from .tsdb import MemoryTSDB, VictoriaTSDB, periodic_flush
from .updates import UpdateManager

log = logging.getLogger("networkguru")
TRACK_S = 3.0               # the map's vehicle positions while it is open (0.9.5)
STALL_NOTE = "The server was busy:"
VEHICLES_EVERY_S = 60.0     # Mobius's vehicles compared with the inventory (0.9.7)
STALL_NOTE_COUNT = 10
TRACK_SLOW_S = 15.0
DAY = 86400.0


def _from_mobius(text: str) -> bool:
    return text.startswith("Added from Mobius") or " in Mobius (" in text or "added from Mobius" in text


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.mode = settings.mode
        self.inv_changes: list[str] = []       # for the Events list once it is open (start)
        self.mobius_vehicles = None            # live mode: the vehicles straight from Mobius (0.9.7)
        if self.mode == "live":            # + devices added from the discovery scan, + followed addresses
            from .found import load_site_inventory
            from .mobius_vehicles import MobiusVehicles
            self.mobius_vehicles = MobiusVehicles(settings.data_dir)
            self.inv, self.base_hosts, self.inv_changes = load_site_inventory(settings.inventory, settings.data_dir,
                                                                              mobius=self.mobius_vehicles)
        else:
            self.inv = load_inventory(settings.inventory)
            self.base_hosts = {cid: c.host for cid, c in self.inv.comps.items()}
        self.rules = RuleSet(load_rules(settings.rules))
        self.tsdb = None
        self.events: EventStore | None = None
        self.pipeline: Pipeline | None = None
        self.history: History | None = None
        self.world: SimWorld | None = None
        self.scheduler: Scheduler | None = None
        self.progress = Progress()
        logbuffer.install()                  # the last log lines, for the problem report
        logbuffer.install_file(Path(settings.data_dir) / "logs")    # 0.10.0: kept across restarts (Admin → Download the log)
        logging.getLogger("httpx").setLevel(logging.WARNING)   # a line per request pushed everything else out
        self.notes: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.started = time.time()
        self.settings_state = open_settings(settings.data_dir, settings.public_url)
        self.auth = AdminAuth(dev_hash=hash_password(settings.dev_admin_password, n=16384)
                              if settings.dev_admin_password else None)
        self.updates: UpdateManager | None = None
        self.reports: ReportService | None = None
        self.mobius = None                    # live mode: the Mobius explorer (drivers/mobius_explore.py)
        self.fleet = None                     # live mode: the Mobius reader, for every vehicle's position (map)
        self.mobius_clients = None            # live mode: every Mobius client PC (mobius_clients.py, 0.9.6)
        self.discovery = None                 # live mode: the discovery scan (found.py)
        self.feedback_limit = RateLimit()     # the Feedback button (feedback.py)
        self.problem_limit = RateLimit(PROBLEMS_PER_HOUR)    # Report a problem, anyone (problem_ticket.py, 0.10.0)
        self.problem_reports = ReportStore(settings.data_dir)
        from .drivers.snmp_walk import SnmpWalks
        self.snmp_walks = SnmpWalks()         # a device page's All SNMP values (0.11.0)
        self.can_ping = False

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
        for text in self.inv_changes:        # an added device the inventory has now, a followed address it overrides
            self.events.add_system_event(time.time(), "mobius" if _from_mobius(text) else "discovery", text)
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
        self.tasks.append(asyncio.create_task(METER.run()))      # how long the loop is blocked (stalls.py)
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
            can_ping, why = icmp_available()
            self.can_ping = can_ping
            pollers, self.pipeline.unchecked, notes = live_pollers(self.inv, can_ping)
            self.fleet = next((p for p in pollers if isinstance(p, MobiusPoller)), None)
            self.mobius_clients = next((p.reader.clients for p in pollers
                                        if isinstance(getattr(p, "reader", None), MobiusServerReader)), None)
            if self.fleet is not None:                # last positions survive a restart (positions.py)
                self.fleet.positions = VehiclePositions(Path(s.data_dir) / "state" / "positions.json")
            self.pipeline.sweep()            # show "Not checked yet" at once, not after the first sweep
            self.notes.extend(notes)
            if not can_ping:
                self.notes.append(why[0].upper() + why[1:] + ".")
            self.scheduler = Scheduler(self.pipeline, pollers, self.events)
            self.tasks.append(asyncio.create_task(self.scheduler.run()))
            if self.fleet is not None:
                self.tasks.append(asyncio.create_task(self._track_vehicles()))
                self.tasks.append(asyncio.create_task(self._vehicles_from_mobius()))
            if self.inv.data.mobius.influx_url:       # everything else Mobius has, for the pages
                from .drivers.mobius_explore import MobiusExplorer
                self.mobius = MobiusExplorer(self.inv)
                self.tasks.append(asyncio.create_task(self.mobius.run()))
            from .found import Discovery
            self.discovery = Discovery(self)
            self.tasks.append(asyncio.create_task(self.discovery.daily_job().loop()))

    def swap_inventory(self, inv, base_hosts: dict) -> list[str]:
        """Go on with a bigger inventory (a device added from the discovery scan) without a restart.
        Returns the new component ids."""
        old = self.inv
        self.inv, self.base_hosts = inv, base_hosts
        self.pipeline.swap_inventory(inv)
        if self.history is not None:
            self.history.inv = inv
        if self.mobius is not None:
            self.mobius.inv, self.mobius.names = inv, self.mobius._names(inv)
        new = {cid for cid in inv.comps if cid not in old.comps}
        if new and self.scheduler is not None:
            pollers, unchecked, _notes = live_pollers(inv, self.can_ping, only=new)
            if self.fleet is not None:                # the one Mobius reader takes them on (0.9.7)
                for p in [p for p in pollers if isinstance(p, MobiusPoller)]:
                    self.fleet.extend(inv, p.comps)
                    pollers.remove(p)
            self.pipeline.unchecked.update(unchecked)
            self.scheduler.add(pollers)
        if self.fleet is not None:
            self.fleet.extend(inv, [])                # Mobius names given to vehicles (by IP) link up
        if self.scheduler is not None:                # a device at a new address (a vehicle's Mobius IP): pinged there
            for cid, c in inv.comps.items():
                if cid in old.comps and c.host and c.host != old.comps[cid].host:
                    p = self.scheduler.poller_of(cid)
                    if p is not None and hasattr(p, "retarget"):
                        p.retarget(c.host)
        self.pipeline.sweep()
        return sorted(new)

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
            self.notes.append(Info("No VictoriaMetrics configured - history is kept in memory only."))
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

    async def _vehicles_from_mobius(self) -> None:
        """Every VEHICLES_EVERY_S: a vehicle Mobius knows that the dashboard has not got yet, or a new IP, and the
        inventory is built again with it - its card, its Mobius values, its IP pinged - without a restart
        (mobius_vehicles.py). Each change is one Events line."""
        from .found import load_site_inventory
        while True:
            await asyncio.sleep(VEHICLES_EVERY_S)
            try:
                if not self.mobius_vehicles.update(self.fleet):
                    continue
                added = self.discovery.added if self.discovery is not None else None
                inv, base, changes = load_site_inventory(self.settings.inventory, self.settings.data_dir, added=added,
                                                         mobius=self.mobius_vehicles)
                for text in changes:
                    self.events.add_system_event(time.time(), "mobius" if _from_mobius(text) else "discovery", text)
                self.swap_inventory(inv, base)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never stop over one bad vehicle
                log.exception("vehicles from Mobius not updated")

    async def _track_vehicles(self) -> None:
        """While someone has the Map open: the vehicles' positions every TRACK_S (MobiusPoller.poll_positions),
        on their own - a slow device poll never holds them up. An error is logged once; then every TRACK_SLOW_S
        until it reads again. Never a status: the Mobius reader's own poll gives those."""
        failing = False
        while True:
            await asyncio.sleep(TRACK_SLOW_S if failing else TRACK_S)
            if not self.fleet.watched():
                continue
            try:
                await asyncio.wait_for(self.fleet.poll_positions(), timeout=TRACK_SLOW_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the map's extra read must never stop
                if not failing:
                    log.warning("map positions not read (%s) - trying every %.0f s", e or type(e).__name__, TRACK_SLOW_S)
                failing = True
            else:
                if failing:
                    log.info("map positions read again")
                failing = False

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
            self._stall_note()

    def _stall_note(self) -> None:
        """Information under "What's checked" while the server is often blocked (stalls.py)."""
        st = METER.summary()
        self.notes[:] = [n for n in self.notes if not (isinstance(n, Info) and n.startswith(STALL_NOTE))]
        if st["stalls_250ms"] >= STALL_NOTE_COUNT:
            self.notes.append(Info(f"{STALL_NOTE} {st['stalls_250ms']} times over 0.25 s in the last 10 minutes "
                                   f"(longest {st['stall_max_ms'] / 1000:.1f} s). Ping and port times measured meanwhile "
                                   "are left out. Admin → Server speed finds what slows it."))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.fleet is not None:
            self.fleet.positions.save(force=True)
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
                "uptime_s": round(time.time() - self.started), **METER.summary()}
