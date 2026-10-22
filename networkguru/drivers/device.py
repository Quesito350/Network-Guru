"""One live poller per device: ping + ports (reach.py) and its reader (readers.py), in one result -
the pipeline takes a component's metrics from one result at a time.

The reader runs at most every READ_EVERY_S; between reads its last values go out again, so a fast
ping (Rajant: 10 s) stays fast. A failed read on a device that answers ping is `read_ok{via}` = 0
(Warning "Device data (snmp): Not read") with the reason in info.read_error.
"""

from __future__ import annotations

import asyncio
import logging

from ..model import CompResult, Metric
from .reach import ReachPoller
from .readers import READ_EVERY_S, ReadError

log = logging.getLogger("networkguru.drivers")


class DevicePoller(ReachPoller):
    def __init__(self, comp, can_ping: bool, reader=None, interval: float | None = None):
        super().__init__(comp, can_ping, interval)
        self.reader = reader
        self.last_read = -1e18
        self.cached: tuple[list[Metric], dict] | None = None
        if reader:
            self.timeout += 12.0

    @property
    def has_work(self) -> bool:
        return bool(self.ping_host or self.ports or self.reader)

    async def poll(self, now: float) -> list[CompResult]:
        due = bool(self.reader) and now - self.last_read >= READ_EVERY_S and not self.reader.missing()
        jobs = [super().poll(now)]
        if due:
            jobs.append(self._read(now))
        got = await asyncio.gather(*jobs)
        base = got[0][0]
        read = got[1] if due else None
        return [self.merge(base, read)]

    async def _read(self, now: float):
        try:
            return await self.reader.read(now)
        except ReadError as e:
            return e
        except Exception as e:  # noqa: BLE001 - a reader bug must not stop the ping
            log.exception("reader %s failed on %s", self.reader.via, self.comp.id)
            return ReadError(f"reader error ({type(e).__name__})")

    def merge(self, base: CompResult, read) -> CompResult:
        if not self.reader:
            return base
        info = dict(base.info)
        metrics = list(base.metrics)
        missing = self.reader.missing()
        if missing:
            info["reader"] = f"{self.reader.label} not set up - add {', '.join(missing)} to secrets.env"
            return CompResult(base.comp_id, base.ts, base.reachable, metrics, info, base.error)
        if read is not None:
            self.last_read = base.ts
            if isinstance(read, ReadError):
                self.cached = None
                info["read_error"] = str(read)
                if not (self.ping_host or self.ports):    # read from a cloud (ALMS): the reader is all there is
                    return CompResult(base.comp_id, base.ts, False, [], info, str(read))
                metrics.append(Metric("read_ok", 0.0, {"via": self.reader.via}))
                return CompResult(base.comp_id, base.ts, base.reachable, metrics, info, base.error)
            self.cached = read
        if self.cached is None:
            return base
        r_metrics, r_info = self.cached
        info.update(r_info)
        info.pop("read_error", None)
        metrics += r_metrics + [Metric("read_ok", 1.0, {"via": self.reader.via})]
        # the device answered its reader even if ping and ports did not (ICMP filtered)
        return CompResult(base.comp_id, base.ts, True, metrics, info, None)
