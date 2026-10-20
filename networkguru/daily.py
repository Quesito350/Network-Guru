"""Once-a-day jobs at a site-local time (update check 06:00, morning report 06:30).

A job looks every 20 s whether today's run is due and not yet done. That one rule covers a
changed time (no restart needed), DST days (times are site-local dates, never "+24 h") and a
server that was down at the set time (it runs late, within `catchup_s`, marked late).
"Done" is decided by the job's owner from its saved state, so a restart never runs it twice.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("networkguru")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def valid_hhmm(value: str) -> bool:
    return bool(_HHMM.match(value or ""))


def local_date(ts: float, tz: ZoneInfo) -> str:
    return dt.datetime.fromtimestamp(ts, tz).date().isoformat()


def due_ts(date: str, hhmm: str, tz: ZoneInfo) -> float:
    """The moment HH:MM happens on that site-local date. A time skipped by the spring DST change
    (02:30) resolves to the hour after; a time repeated in autumn uses the first one."""
    h, m = (int(x) for x in hhmm.split(":"))
    d = dt.date.fromisoformat(date)
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()


def next_due(hhmm: str, tz: ZoneInfo, after: float) -> float:
    date = local_date(after, tz)
    t = due_ts(date, hhmm, tz)
    if t <= after:
        nxt = dt.date.fromisoformat(date) + dt.timedelta(days=1)
        t = due_ts(nxt.isoformat(), hhmm, tz)
    return t


class DailyJob:
    def __init__(self, name: str, tz: ZoneInfo, when: Callable[[], str],
                 is_done: Callable[[str], bool], run: Callable[[str, bool], Awaitable[None]],
                 ready: Callable[[], bool] = lambda: True, catchup_s: float = 6 * 3600,
                 clock: Callable[[], float] = time.time, every_s: float = 20.0):
        self.name, self.tz, self.when, self.is_done, self.run = name, tz, when, is_done, run
        self.ready, self.catchup_s, self.clock, self.every_s = ready, catchup_s, clock, every_s

    async def tick(self) -> bool:
        """Run today's job if it is due; True when it ran."""
        hhmm = self.when()
        if not valid_hhmm(hhmm):
            return False
        now = self.clock()
        date = local_date(now, self.tz)
        due = due_ts(date, hhmm, self.tz)
        if now < due or now - due > self.catchup_s or self.is_done(date) or not self.ready():
            return False
        await self.run(date, now - due > 300)
        return True

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken run must not stop tomorrow's
                log.exception("daily job %s failed", self.name)
            await asyncio.sleep(self.every_s)
