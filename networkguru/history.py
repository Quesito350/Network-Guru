"""History queries built from the catalog's chart specs - the browser never sends raw queries."""

from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

from .catalog import ACCESS_TYPES, VEHICLE_RADIO_TYPES, VEHICLE_ROUTER_TYPES, ChartSpec, metric_spec, type_spec
from .model import Status

STEPS = [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]


def pick_step(span: float, min_step: float = 10, max_points: int = 600) -> int:
    need = max(span / max_points, min_step)
    return next((s for s in STEPS if s >= need), STEPS[-1])


def local_midnight(ts: float, tz: ZoneInfo) -> float:
    d = dt.datetime.fromtimestamp(ts, tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class History:
    def __init__(self, inv, rules, tsdb, events):
        self.inv = inv
        self.rules = rules
        self.tsdb = tsdb
        self.events = events
        self.tz = ZoneInfo(inv.site.timezone)

    # -- charts -----------------------------------------------------------
    async def charts(self, comp_id: str, start: float, end: float, primary_only: bool = False,
                     keys: list[str] | None = None) -> list[dict]:
        comp = self.inv.comps[comp_id]
        spec = type_spec(comp.type)
        charts = [c for c in spec.charts if (not primary_only or c.primary) and (not keys or c.key in keys)]
        return list(await asyncio.gather(*(self._chart(comp, c, start, end) for c in charts)))

    async def _chart(self, comp, chart: ChartSpec, start: float, end: float) -> dict:
        interval = type_spec(comp.type).interval
        match = {"comp": comp.id}
        out = {"comp": comp.id, "key": chart.key, "title": chart.title, "kind": chart.kind,
               "ymin": chart.ymin, "ymax": chart.ymax, "series": [], "thresholds": {}}
        first = chart.series[0].metric
        spec = metric_spec(first)
        out["unit"] = spec.unit
        out["enum"] = {str(k): v for k, v in (spec.enum or {}).items()} or None
        if chart.kind == "daily":
            # one bar per local day, the last 30 days up to `end`
            last = local_midnight(end, self.tz)
            first_day = local_midnight(end - 29 * 86400 - 3600, self.tz)
            series = []
            for s in chart.series:
                res = await self.tsdb.query_range(s.metric, match, first_day + 86400, last + 86400, 86400, "max")
                pts = [[t - 86400, v] for t, v in (res[0]["points"] if res else [])]
                series.append({"metric": s.metric, "label": s.label, "points": pts})
            out.update(series=series, step=86400, start=first_day, end=last + 86400)
            return out
        step = pick_step(end - start, interval)
        agg = "last" if chart.kind == "state" else "avg"
        queries = [self.tsdb.query_range(s.metric, match, start, end, step, agg) for s in chart.series]
        want_peak = chart.kind == "line" and len(chart.series) == 1 and step >= 3 * interval
        if want_peak:
            queries.append(self.tsdb.query_range(first, match, start, end, step, "max"))
        res = await asyncio.gather(*queries)
        for i, s in enumerate(chart.series):
            pts = res[i][0]["points"] if res[i] else []
            item = {"metric": s.metric, "label": s.label, "points": [[t, round(v, 4)] for t, v in pts]}
            if want_peak and i == 0 and res[-1]:
                item["peak"] = [[t, round(v, 4)] for t, v in res[-1][0]["points"]]
            out["series"].append(item)
            rule = self.rules.metric_rule(s.metric, comp.rules)
            if rule and chart.kind != "state":
                t = {k: rule[k] for k in ("warn", "crit", "low_warn", "low_crit") if k in rule}
                if t:
                    out["thresholds"][s.metric] = t
        out.update(step=step, start=start, end=end)
        return out

    # -- sparklines for cards ------------------------------------------------
    def headline_of(self, group_id: str) -> tuple[str, str] | None:
        """(component id, metric) a group's card sparkline shows."""
        g = self.inv.groups[group_id]
        prefs = [("vcu", "vcu_comms_latency_ms"), ("victron_cerbo", "power_soc_pct"),
                 ("mobius_server", "mobius_clients"), ("mobius_client", "host_cpu_pct"),
                 ("rajant", "ping_rtt_ms"), ("cell_modem", "ping_rtt_ms")]
        for typ, m in prefs:
            c = next((c for c in g.components if c.type == typ), None)
            if c:
                return c.id, m
        c = g.components[0]
        return c.id, type_spec(c.type).headline[0]

    async def sparklines(self, group_ids: list[str], start: float, end: float) -> dict:
        step = pick_step(end - start, 10, max_points=72)

        async def one(gid):
            h = self.headline_of(gid)
            if not h:
                return gid, None
            cid, m = h
            res = await self.tsdb.query_range(m, {"comp": cid}, start, end, step, "avg")
            return gid, {"comp": cid, "metric": m, "points": [[t, round(v, 3)] for t, v in (res[0]["points"] if res else [])]}
        return dict(await asyncio.gather(*(one(g) for g in group_ids)))

    # -- vehicle path ---------------------------------------------------------
    def path(self, vehicle_id: str) -> list[dict]:
        g = self.inv.groups[vehicle_id]
        access = (g.access or "rajant").lower()
        hops: list[dict] = []
        server = next((c for c in self.inv.comps.values() if c.type == "mobius_server"), None)
        if server:
            hops.append({"key": "server", "label": "Mobius Server", "comp": server.id,
                         "group": self.inv.comp_group[server.id]})
        core = next((x for x in self.inv.tab_groups("infrastructure") if x.kind == "core"), None)
        if core:
            hops.append({"key": "core", "label": "Site network", "group": core.id})
        acc_types = ACCESS_TYPES.get(access, ("rajant",))
        acc = [c.id for c in self.inv.comps.values()
               if c.type in acc_types and self.inv.group_tab[self.inv.comp_group[c.id]] == "infrastructure"]
        hops.append({"key": "access", "label": "Celona private LTE" if access == "celona" else "Rajant mesh",
                     "comps": acc})
        radio = next((c for c in g.components if c.type in VEHICLE_RADIO_TYPES), None)
        router = next((c for c in g.components if c.type in VEHICLE_ROUTER_TYPES), None)
        if radio:
            hops.append({"key": "radio", "label": radio.label, "comp": radio.id, "edge": "ping_rtt_ms"})
        if router:
            hops.append({"key": "router", "label": router.label, "comp": router.id, "edge": "ping_rtt_ms"})
        for typ, edge in (("vcu", "vcu_comms_latency_ms"), ("vai", "vai_heartbeat_age_s")):
            c = next((c for c in g.components if c.type == typ), None)
            if c:
                hops.append({"key": typ, "label": c.label, "comp": c.id, "edge": edge})
        return hops

    async def path_history(self, vehicle_id: str, start: float, end: float, now: float) -> list[dict]:
        hops = self.path(vehicle_id)
        ids = []
        for h in hops:
            ids.extend([h["comp"]] if h.get("comp") else [h["group"]] if h.get("group") else h.get("comps", []))
        av = self.events.availability(ids, start, end, now)

        async def edge(h):
            if not h.get("edge"):
                return None
            return await self.tsdb.aggregate(h["edge"], {"comp": h["comp"]}, start, end, "p95")
        p95 = await asyncio.gather(*(edge(h) for h in hops))
        for h, p in zip(hops, p95):
            if h.get("comps"):                      # access hop: average over the mesh / LTE nodes
                vals = [av[c]["pct"] for c in h["comps"] if av.get(c) and av[c]["pct"] is not None]
                h["availability"] = sum(vals) / len(vals) if vals else None
            else:
                nid = h.get("comp") or h.get("group")
                h["availability"] = av.get(nid, {}).get("pct")
            h["edge_p95"] = p
        return hops


def status_name(v: int) -> str:
    return Status(v).name.lower()
