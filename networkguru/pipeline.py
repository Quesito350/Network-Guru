"""Results in -> live state, status changes, stored metrics, browser updates out.

Order for every batch of poll results:
  1. per component: metrics + rules -> raw status (Offline after N failed polls)
  2. per affected group: escalation (Offline that matters = Critical), dependency
     suppression (behind a dead upstream device = Unknown), staleness, critical:false caps
  3. roll-up component -> group -> tab, status changes recorded in SQLite
  4. metrics + status written to the time-series store
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from .catalog import type_spec
from .inventory import TABS, Inventory
from .model import CompResult, Metric, Status
from .rules import RuleMemory, RuleSet

WATCH_INFO = ("address", "firmware", "software")   # text facts whose changes become events


@dataclass
class NodeState:
    id: str
    kind: str                          # component | group | tab
    status: Status = Status.UNKNOWN
    reasons: list[str] = field(default_factory=lambda: ["No data yet"])
    since: float | None = None
    suppressed: bool = False
    version: int = 0
    # components only
    raw: Status = Status.UNKNOWN
    raw_reasons: list[str] = field(default_factory=list)
    down: bool = False                 # not responding / absent (drives suppression)
    fail_count: int = 0
    metrics: dict[str, float] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    last_seen: float | None = None
    last_poll: float | None = None
    error: str | None = None
    # groups / tabs
    counts: dict[str, int] = field(default_factory=dict)

    def public(self) -> dict:
        d: dict[str, Any] = {"s": int(self.status), "r": self.reasons[:4], "since": self.since}
        if self.kind == "component":
            d.update({"m": self.metrics, "i": self.info, "seen": self.last_seen, "poll": self.last_poll,
                      "err": self.error, "sup": self.suppressed})
        else:
            d["counts"] = self.counts
        return d


class Pipeline:
    def __init__(self, inv: Inventory, rules: RuleSet, tsdb, events, clock=time.time):
        self.inv = inv
        self.rules = rules
        self.tsdb = tsdb
        self.events = events
        self.clock = clock
        self.tz = ZoneInfo(inv.site.timezone)
        self.version = 0
        self.state: dict[str, NodeState] = {}
        self.memory: dict[str, RuleMemory] = {}
        for cid in inv.comps:
            self.state[cid] = NodeState(cid, "component")
            self.memory[cid] = RuleMemory()
        for gid in inv.groups:
            self.state[gid] = NodeState(gid, "group")
        for tab, _ in TABS:
            self.state[tab] = NodeState(tab, "tab")
        self.dependents: dict[str, set[str]] = {}
        for c in inv.comps.values():
            if c.depends_on:
                self.dependents.setdefault(c.depends_on, set()).add(inv.comp_group[c.id])
        self.site_labels = {"site": inv.site.id}
        self.pending_points: list = []
        self.unchecked: dict[str, str] = {}      # live mode: comp id -> why no driver checks it yet

    # ------------------------------------------------------------------
    def ingest(self, results: list[CompResult], now: float | None = None, fast: bool = False) -> None:
        now = self.clock() if now is None else now
        self.version += 1
        dirty_groups: set[str] = set()
        points = []
        info_changes: list[tuple] = []
        for r in results:
            c = self.inv.comps.get(r.comp_id)
            if c is None:
                continue
            st = self.state[r.comp_id]
            st.last_poll = r.ts
            st.version = self.version
            was_down = st.down
            if r.reachable:
                st.fail_count = 0
                st.error = None
                st.metrics = {m.key: m.value for m in r.metrics}
                for key in WATCH_INFO:                 # an address or firmware change is an event
                    new, old = r.info.get(key), st.info.get(key)
                    if new not in (None, "") and old not in (None, "") and new != old:
                        info_changes.append((r.ts, r.comp_id, key, str(old), str(new)))
                st.info.update(r.info)
                ev = self.rules.evaluate(r.metrics, r.ts, self.memory[r.comp_id], c.rules)
                st.raw, st.raw_reasons, st.down = ev.status, ev.reasons, ev.offline
                if c.link:                             # name the link: "60 GHz link COW-T46 down"
                    st.raw_reasons = [w.replace("60 GHz link: Down", f"60 GHz link {c.link} down")
                                      for w in st.raw_reasons]
                if not ev.offline:                 # e.g. Mobius answered but says the VCU is silent
                    st.last_seen = r.ts
                labels = {**self.site_labels, "comp": c.id, "type": c.type}
                points.extend((m.name, {**labels, **m.labels}, m.value, r.ts) for m in r.metrics)
            else:
                st.fail_count += 1
                st.error = r.error
                st.metrics = {}
                if fast or st.fail_count >= self.rules.offline_after:
                    st.raw, st.down = Status.OFFLINE, True
                    st.raw_reasons = [f"Not responding ({r.error})" if r.error else "Not responding"]
            gid = self.inv.comp_group[r.comp_id]
            dirty_groups.add(gid)
            if st.down != was_down:
                dirty_groups.update(self.dependents.get(r.comp_id, ()))
        self._recompute(dirty_groups, now, points)
        self.tsdb.write(points)
        if info_changes:
            self.events.add_info_changes(info_changes)

    def sweep(self, now: float | None = None) -> None:
        """Re-evaluate everything that depends on the clock (staleness, schedules)."""
        now = self.clock() if now is None else now
        self.version += 1
        points: list = []
        self._recompute(set(self.inv.groups), now, points)
        self.tsdb.write(points)

    # ------------------------------------------------------------------
    def _recompute(self, groups: set[str], now: float, points: list) -> None:
        changes: list[tuple] = []
        local = dt.datetime.fromtimestamp(now, self.tz)
        dirty_tabs: set[str] = set()
        # "down" for hiding devices behind a failure: declared offline, or its last check failed -
        # an upstream device polled less often must not lose the race to the devices behind it
        down = {cid for cid in self.inv.comps if self.state[cid].down or self.state[cid].fail_count > 0}
        for gid in groups:
            g = self.inv.groups[gid]
            sev_critical = (self.inv.offline_severity(gid) == "critical"
                            or self.inv.expected_online(gid, local))
            worst, reasons, hidden = Status.OK, [], []
            counts: dict[str, int] = {}
            for c in g.components:
                st = self.state[c.id]
                status, why, sup = self._effective(c, st, now, sev_critical, down)
                if sup and why and why[0] not in hidden:
                    hidden.append(why[0])
                if status != st.status or why != st.reasons:
                    if status != st.status:
                        changes.append((now, c.id, "component", int(st.status), int(status),
                                        "; ".join(why), int(sup)))
                        st.since = now
                    st.status, st.reasons, st.suppressed = status, why, sup
                    st.version = self.version
                points.append(("status", {**self.site_labels, "comp": c.id, "type": c.type}, int(status), now))
                counts[status.name.lower()] = counts.get(status.name.lower(), 0) + 1
                if status > worst:
                    worst, reasons = status, []
                if status == worst and status > Status.OK and not sup:
                    reasons.extend(f"{c.label}: {w}" for w in why[:2])
            if worst == Status.UNKNOWN and not reasons:
                reasons = hidden or ["Waiting for data"]       # e.g. "Unreachable via Trailer 1846 · Tachyon 2"
            if (self.inv.group_tab[gid] == "infrastructure" and len(g.components) > 1 and worst >= Status.OFFLINE
                    and all(c.id in down for c in g.components)):
                why = "power, or both backhauls down" if self.inv.backhaul == "redundant" else "power or backhaul down"
                reasons = [f"No device at {g.name} answers - {why}"] + reasons
            if worst == Status.OFFLINE:
                seen = [self.state[c.id].last_seen for c in g.components if self.state[c.id].last_seen]
                reasons = [_last_seen_text(max(seen), self.tz, now) if seen else "Never seen"]
            dirty_tabs.add(self.inv.group_tab[gid])
            self._set(gid, "group", worst, reasons[:4], counts, now, changes)
            points.append(("status", {**self.site_labels, "comp": gid, "type": "group"}, int(worst), now))
        for tab in dirty_tabs:
            gs = self.inv.tab_groups(tab)
            counts: dict[str, int] = {}
            worst, reasons = Status.OK, []
            for g in gs:
                s = self.state[g.id].status
                counts[s.name.lower()] = counts.get(s.name.lower(), 0) + 1
                if s == Status.OFFLINE:
                    continue                     # parked vehicles / idle desks don't colour the tab
                if s > worst:
                    worst, reasons = s, []
                if s == worst and s > Status.OK:
                    reasons.append(g.name)
            if gs and all(self.state[g.id].status == Status.OFFLINE for g in gs):
                worst = Status.OFFLINE
            self._set(tab, "tab", worst, reasons[:6], counts, now, changes)
            points.append(("status", {**self.site_labels, "comp": tab, "type": "tab"}, int(worst), now))
        if changes:
            self.events.add_changes(changes)

    def _set(self, node_id, kind, status, reasons, counts, now, changes) -> None:
        st = self.state[node_id]
        if status != st.status:
            changes.append((now, node_id, kind, int(st.status), int(status), "; ".join(reasons), 0))
            st.since = now
        if status != st.status or reasons != st.reasons or counts != st.counts:
            st.version = self.version
        st.status, st.reasons, st.counts = status, reasons, counts

    def _effective(self, c, st: NodeState, now: float, sev_critical: bool,
                   down: set[str]) -> tuple[Status, list[str], bool]:
        if st.last_poll is None:
            if c.address == "dhcp" and not c.host:
                return Status.UNKNOWN, ["No address yet (DHCP)"], False
            return Status.UNKNOWN, [self.unchecked.get(c.id, "No data yet")], False
        interval = type_spec(c.type).interval
        if now - st.last_poll > self.rules.stale_after * interval:
            return Status.UNKNOWN, [f"No data since {_ago(now - st.last_poll)}"], False
        status, why = st.raw, list(st.raw_reasons)
        if c.depends_on and st.down and c.depends_on in down:
            root = self.inv.root_cause(c.id, down)        # name the first failure, not the next hop
            where = self.inv.comp_group[root]
            label = self.inv.comps[root].label
            if where != self.inv.comp_group[c.id]:
                label = f"{self.inv.groups[where].name} · {label}"
            return Status.UNKNOWN, [f"Unreachable via {label}"], True
        if status == Status.OFFLINE and sev_critical:
            status, why = Status.CRITICAL, [f"Offline - {w}" for w in why] or ["Offline"]
        if not c.critical and status > Status.WARNING:
            status = Status.WARNING
        if status == Status.UNKNOWN and not why:
            why = ["Waiting for data"]
        return status, why, False

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {"version": self.version, "ts": self.clock(),
                "nodes": {nid: st.public() for nid, st in self.state.items()}}

    def delta_since(self, version: int) -> dict:
        return {"version": self.version, "ts": self.clock(),
                "nodes": {nid: st.public() for nid, st in self.state.items() if st.version > version}}


def _ago(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _last_seen_text(ts: float, tz, now: float) -> str:
    when = dt.datetime.fromtimestamp(ts, tz)
    today = dt.datetime.fromtimestamp(now, tz).date()
    if when.date() == today:
        return f"Offline - last seen {when:%H:%M}"
    return f"Offline - last seen {when:%a %d %b %H:%M}"


def metric(name: str, value: float, **labels: str) -> Metric:
    return Metric(name, float(value), dict(labels))
