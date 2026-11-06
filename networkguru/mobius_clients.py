"""Every Mobius client PC Mobius reports (0.9.6), for the list on the Control Room tab and the Mobius
Server's page - also the PCs that are not desks in the inventory.

The Mobius Server reader (drivers/readers.py) fills it from the table the map names for
mobius_server / mobius_clients - one row per client and moment, the PC's name in a field - with the
values the map gives for the type mobius_client (CPU, memory, disk, frame rate, uptime). A PC is
connected while it wrote in the last CONNECTED_S (as the server card's count); one unseen for FORGET_S
is dropped. Memory only: the names never go into files or problem reports.
"""

from __future__ import annotations

import re
import time

CONNECTED_S = 300.0
FORGET_S = 86400.0


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


class MobiusClients:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.data: dict[str, dict] = {}            # PC name -> {"t": newest point, "values": {metric: value}}
        self.columns: list[str] = []               # the map's mobius_client values, in its order

    def seen(self, name: str, t: float, values: dict[str, float]) -> None:
        e = self.data.setdefault(str(name), {"t": 0.0, "values": {}})
        if t >= e["t"]:
            e["t"] = t
            e["values"].update(values)

    def forget_old(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self.data = {n: e for n, e in self.data.items() if now - e["t"] <= FORGET_S}

    def public(self, inv, now: float | None = None) -> list[dict]:
        """[{name, connected, last, values, group}] - connected first, then by name. `group`: the inventory desk
        (kind client) whose Mobius machine id, name or host name is this PC's name."""
        now = self.clock() if now is None else now
        desks = {}
        for g in inv.groups.values():
            if g.kind != "client":
                continue
            for n in [g.name, *(x for c in g.components for x in (c.mobius_machine_id, c.hostname, c.name))]:
                if n:
                    desks.setdefault(norm(n), g.id)
        out = [{"name": n, "connected": now - e["t"] <= CONNECTED_S, "last": e["t"], "values": dict(e["values"]),
                "group": desks.get(norm(n))} for n, e in self.data.items() if now - e["t"] <= FORGET_S]
        return sorted(out, key=lambda x: (not x["connected"], x["name"].lower()))

    def counts(self, now: float | None = None) -> dict:
        """For the problem report - never a name."""
        now = self.clock() if now is None else now
        return {"seen": sum(1 for e in self.data.values() if now - e["t"] <= FORGET_S),
                "connected": sum(1 for e in self.data.values() if now - e["t"] <= CONNECTED_S)}
