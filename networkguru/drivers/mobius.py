"""The Mobius reader: VCU, GNSS, VAI (and other Mobius-sourced) values from the Mobius InfluxDB.

One poller for every mapped component. Per mapped measurement it asks for the newest point of each
vehicle (`... GROUP BY "<asset tag>" ORDER BY time DESC LIMIT 1`) and hands it to the component of
that type in the vehicle whose `mobius_asset` matches the tag. Numbers become metrics; a GNSS
component's lat / lon become info (for the map). Data older than STALE_S means the vehicle is quiet.
"""

from __future__ import annotations

import time

from ..config import secret
from ..model import CompResult, Metric
from .influx import InfluxClient, InfluxError, q_ident
from .mobius_map import mobius_map

STALE_S = 60.0
WINDOW = "2m"
INFO_KEYS = ("lat", "lon")


class MobiusPoller:
    def __init__(self, inv, comps: list, interval: float = 10.0):
        self.inv = inv
        self.map = mobius_map(inv)
        self.comps = comps
        self.comp_ids = [c.id for c in comps]
        self.interval = interval
        self.timeout = 15.0
        cfg = inv.data.mobius
        self.url, self.database = cfg.influx_url, cfg.database
        self.default_tag = cfg.asset_tag or "asset"
        self.extra = cfg.model_extra or {}
        self.by_asset: dict[tuple[str, str], str] = {}         # (type, mobius asset) -> component id
        for c in comps:
            g = inv.groups[inv.comp_group[c.id]]
            self.by_asset[(c.type, str(g.mobius_asset or g.name))] = c.id

    async def poll(self, now: float) -> list[CompResult]:
        got: dict[str, tuple[float, dict, dict]] = {}
        try:
            async with InfluxClient(self.url, self.database, secret(self.extra.get("user_ref") or ""),
                                    secret(self.extra.get("password_ref") or "")) as c:
                for typ, spec in self.map.items():
                    if not any(t == typ for t, _a in self.by_asset):
                        continue
                    tag = spec.get("asset_tag") or self.default_tag
                    ours = list(spec["fields"].items())
                    cols = ", ".join(q_ident(f) for _o, f in ours)
                    rows = await c.query(f"SELECT {cols} FROM {q_ident(spec['measurement'])} WHERE time > now() - {WINDOW} "
                                         f"GROUP BY {q_ident(tag)} ORDER BY time DESC LIMIT 1")
                    for s in rows:
                        cid = self.by_asset.get((typ, str((s.get("tags") or {}).get(tag, ""))))
                        if not cid or not s.get("values"):
                            continue
                        row = dict(zip(s.get("columns") or [], s["values"][0]))
                        metrics, info = {}, {}
                        for ours_name, field in ours:
                            v = row.get(field)
                            if isinstance(v, bool):
                                v = 1.0 if v else 0.0
                            if not isinstance(v, (int, float)):
                                continue
                            (info if ours_name in INFO_KEYS else metrics)[ours_name] = float(v)
                        got[cid] = (float(row.get("time") or 0), metrics, info)
        except InfluxError as e:
            return [CompResult(cid, now, False, error=f"Mobius InfluxDB: {e}") for cid in self.comp_ids]
        out = []
        wall = time.time()
        for cid in self.comp_ids:
            if cid not in got:
                out.append(CompResult(cid, now, False, error="no Mobius data in the last 2 minutes (vehicle off, or "
                                                            "its mobius_asset does not match the Mobius name)"))
                continue
            t, metrics, info = got[cid]
            age = wall - t
            if age > STALE_S:
                out.append(CompResult(cid, now, False, error=f"no Mobius data for {age:.0f} s"))
                continue
            out.append(CompResult(cid, now, True, [Metric(k, v) for k, v in metrics.items()]
                                  + [Metric("read_ok", 1.0, {"via": "mobius"})], info))
        return out
