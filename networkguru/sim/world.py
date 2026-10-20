"""The simulated site: vehicles on shifts, solar trailers, radio links, the Mobius server.

`SimWorld.sample(comp_id, t)` returns the same CompResult a real driver would, for any
time t. Everything is deterministic in t (see noise.py) except the trailer batteries,
which are integrated on a 5-minute grid from a fixed anchor 40 days back.

Knobs come from `sim:` in inventory.yaml (per group or component), for example
  trailer:  sim: {battery_wh: 9600, battery_health: 0.25, pv_peak_w: 300, extra_load_w: 25}
  vehicle:  sim: {flaky: 0.6, vcu_hot: true, park_chance: 0.1}
  client:   sim: {schedule: "Mon-Fri 06:00-18:00", disk_free: 12}
"""

from __future__ import annotations

import datetime as dt
import math
import time
from array import array
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..catalog import TYPES
from ..inventory import Inventory, Schedule
from ..model import CompResult, Metric
from .noise import episode, fbm, rnd, seed_of, vnoise

DAY = 86400.0


@dataclass
class Fault:
    target: str            # component or group id
    kind: str              # outage | degrade
    start: float
    end: float


def sun_height(lat: float, lon: float, t: float) -> float:
    """sin(solar elevation), 0 at night."""
    g = time.gmtime(t)
    hours = g.tm_hour + g.tm_min / 60 + g.tm_sec / 3600
    decl = math.radians(-23.44 * math.cos(2 * math.pi * (g.tm_yday + 10) / 365))
    h = math.radians(15 * (hours + lon / 15 - 12))
    la = math.radians(lat)
    return max(0.0, math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(h))


LFP_CELL = [(0, 2.50), (5, 3.00), (10, 3.15), (20, 3.22), (40, 3.26), (60, 3.29), (80, 3.32),
            (95, 3.36), (100, 3.45)]


def lfp_voltage(soc: float, cells: int = 8) -> float:
    for (s0, v0), (s1, v1) in zip(LFP_CELL, LFP_CELL[1:]):
        if soc <= s1:
            return cells * (v0 + (v1 - v0) * (soc - s0) / (s1 - s0))
    return cells * LFP_CELL[-1][1]


class PowerModel:
    """One trailer's battery, integrated every 5 minutes from the anchor."""
    STEP = 300.0

    def __init__(self, world: "SimWorld", gid: str, knobs: dict, load_w: float):
        self.w = world
        self.seed = seed_of(gid + ":power")
        self.cap_wh = float(knobs.get("battery_wh", 9600)) * float(knobs.get("battery_health", 1.0))
        self.pv_peak = float(knobs.get("pv_peak_w", 720))
        self.load_w = load_w + float(knobs.get("extra_load_w", 10))
        self.soc = array("d", [float(knobs.get("soc_start", 80))])
        self.pv = array("d", [0.0])
        self.load = array("d", [self.load_w])
        self.state = array("b", [0])
        self.yld = array("d", [0.0])
        self.lvd = array("b", [0])
        self._day = dt.datetime.fromtimestamp(world.anchor, world.tz).date()

    def _pv_at(self, t: float) -> float:
        sun = min(1.0, sun_height(self.w.lat, self.w.lon, t) * 1.45) ** 1.1
        return self.pv_peak * 0.9 * sun * self.w.cloud(t) * (1 + 0.04 * vnoise(self.seed, t, 900))

    def _step(self) -> None:
        i = len(self.soc)
        t = self.w.anchor + i * self.STEP
        soc, lvd = self.soc[-1], bool(self.lvd[-1])
        pv = self._pv_at(t)
        fault = episode(self.seed, t, 0.02, 1800, 4 * 3600) is not None
        if fault:
            pv = 0.0
        load = 0.0 if lvd else self.load_w * (1 + 0.08 * vnoise(self.seed + 1, t, 1800))
        net = pv * 0.95 - load
        if soc >= 99.5 and net > 0:                       # full: charger throttles to the load
            pv, net = load / 0.95, 0.0
        soc = min(100.0, max(0.0, soc + net * self.STEP / 3600 / self.cap_wh * 100))
        if soc <= 5:
            lvd = True
        elif lvd and soc >= 20:
            lvd = False
        day = dt.datetime.fromtimestamp(t, self.w.tz).date()
        yld = 0.0 if day != self._day else self.yld[-1]
        self._day = day
        yld += pv * self.STEP / 3600 / 1000
        if fault:
            state = 2
        elif pv < 3:
            state = 0
        elif soc < 85:
            state = 3
        elif soc < 98.5:
            state = 4
        else:
            state = 5
        self.soc.append(soc)
        self.pv.append(pv)
        self.load.append(load)
        self.state.append(state)
        self.yld.append(yld)
        self.lvd.append(1 if lvd else 0)

    def at(self, t: float) -> dict:
        i = max(0, int((t - self.w.anchor) // self.STEP))
        while len(self.soc) <= i:
            self._step()
        soc, pv, load, state = self.soc[i], self.pv[i], self.load[i], self.state[i]
        jit = vnoise(self.seed + 2, t, 60)
        pv_now = max(0.0, pv * (1 + 0.03 * jit)) if pv > 0 else 0.0
        load_now = load * (1 + 0.02 * jit)
        v = lfp_voltage(soc) + (0.5 if state in (3, 4) else 0.15 if state == 5 else -0.1) * min(1.0, pv_now / 200)
        w = pv_now * 0.95 - load_now
        return {"soc": soc, "pv": pv_now, "load": load_now, "state": state, "yield": self.yld[i],
                "lvd": bool(self.lvd[i]), "v": v, "a": w / v if v else 0.0, "w": w}


class SimWorld:
    def __init__(self, inv: Inventory, anchor: float | None = None):
        self.inv = inv
        self.tz = ZoneInfo(inv.site.timezone)
        self.lat, self.lon = inv.site.lat, inv.site.lon
        self.site_seed = seed_of(inv.site.id)
        self.anchor = anchor if anchor is not None else (math.floor(time.time() / DAY) - 40) * DAY
        self.faults: list[Fault] = []
        self.seed = {k: seed_of(k) for k in list(inv.comps) + list(inv.groups)}
        self.vehicles = [g.id for g in inv.tab_groups("vehicles")]
        self.power: dict[str, PowerModel] = {}
        for g in inv.tab_groups("infrastructure"):
            if any(c.type == "victron_cerbo" for c in g.components):
                load = sum(TYPES[c.type].load_w for c in g.components)
                self.power[g.id] = PowerModel(self, g.id, g.sim, load)
        self.client_sched = {}
        for g in inv.tab_groups("control"):
            if g.kind == "client":
                self.client_sched[g.id] = Schedule.parse(g.sim.get("schedule", "Mon-Fri 06:00-18:00"))
        self._t: float | None = None
        self._cache: dict = {}

    # -- faults injected from the API (sim only) -------------------------
    def add_fault(self, target: str, kind: str, minutes: float, now: float | None = None) -> Fault:
        if target not in self.inv.comps and target not in self.inv.groups:
            raise KeyError(target)
        if kind not in ("outage", "degrade", "link"):
            raise ValueError("kind must be outage, degrade or link")
        if kind == "link" and not (target in self.inv.comps and self.inv.comps[target].link):
            raise ValueError("a link fault needs a 60 GHz radio that has a link")
        now = time.time() if now is None else now
        f = Fault(target, kind, now, now + minutes * 60)
        self.faults.append(f)
        return f

    def _fault(self, ids: tuple[str, ...], t: float) -> str | None:
        for f in self.faults:
            if f.target in ids and f.start <= t < f.end:
                return f.kind
        return None

    # -- cached per time step ---------------------------------------------
    def _c(self, key, fn):
        v = self._cache.get(key, self._cache)
        if v is self._cache:
            v = self._cache[key] = fn()
        return v

    def local(self, t: float) -> dt.datetime:
        return self._c("local", lambda: dt.datetime.fromtimestamp(t, self.tz))

    def ambient(self, t: float) -> float:
        def f():
            lt = self.local(t)
            h = lt.hour + lt.minute / 60
            return 8 + 9 * math.sin(2 * math.pi * (h - 9) / 24) + 4 * vnoise(self.site_seed + 7, t, 3 * DAY)
        return self._c("ambient", f)

    def cloud(self, t: float) -> float:
        """Weather shared by the whole site: clear, partly cloudy or overcast spells."""
        spell = vnoise(self.site_seed + 9, t, 2.5 * DAY)
        day = math.floor(t / DAY)
        c = 0.55 * (spell + 1) / 2 + 0.45 * rnd(self.site_seed, day, 77)
        if c < 0.22:
            return 0.12 + 0.08 * vnoise(self.site_seed + 10, t, 1800)
        if c < 0.5:
            return 0.55 + 0.35 * vnoise(self.site_seed + 11, t, 1200)
        return 0.97 + 0.02 * vnoise(self.site_seed + 12, t, 600)

    def mobius_up(self, t: float) -> bool:
        if self._fault(("mobius-svc", "mobius-server"), t) == "outage":
            return False
        return self._c("mob", lambda: episode(self.site_seed + 100, t, 0.04, 120, 600) is None)

    def ntrip_outage_age(self, t: float) -> float | None:
        if self._fault(("mobius-ntrip",), t) == "outage":
            return 120.0
        return self._c("ntrip", lambda: episode(self.site_seed + 200, t, 0.3, 60, 900))

    # -- vehicles -----------------------------------------------------------
    def veh_on(self, gid: str, t: float) -> bool:
        def f():
            g = self.inv.groups[gid]
            if g.sim.get("always_on"):
                return True
            lt = self.local(t)
            s, day, wd = self.seed[gid], lt.toordinal(), lt.weekday()
            minute = lt.hour * 60 + lt.minute + lt.second / 60
            if wd < 5:
                if rnd(s, day, 1) < float(g.sim.get("park_chance", 0.08)):
                    return False
                start, end = 360 + rnd(s, day, 2) * 45, 1020 + rnd(s, day, 3) * 90
            elif wd == 5:
                if rnd(s, day, 4) > 0.3:
                    return False
                start, end = 420 + rnd(s, day, 5) * 30, 720 + rnd(s, day, 6) * 60
            else:
                return False
            return start <= minute < end
        return self._c(("on", gid), f)

    def veh_link(self, gid: str, t: float) -> bool:
        def f():
            if not self.veh_on(gid, t) or self._fault((gid,), t) == "outage":
                return False
            flaky = float(self.inv.groups[gid].sim.get("flaky", 0.1))
            return episode(self.seed[gid] + 1, t, 0.15 + 1.5 * flaky, 30, 600) is None
        return self._c(("link", gid), f)

    def veh_dist(self, gid: str, t: float) -> float:
        s = self.seed[gid]
        lap = 1200 + rnd(s, 3) * 900
        return 0.5 + 0.5 * math.sin(2 * math.pi * t / lap + rnd(s, 4) * 6.28)

    def veh_bad(self, gid: str, t: float) -> bool:
        def f():
            if self._fault((gid,), t) == "degrade":
                return True
            flaky = float(self.inv.groups[gid].sim.get("flaky", 0.1))
            n = vnoise(self.seed[gid] + 5, t, 240)
            return n > 0.965 - 0.3 * flaky or (self.veh_dist(gid, t) > 0.95 and n > 0.7)
        return self._c(("bad", gid), f)

    def veh_radio_rtt(self, gid: str, t: float) -> float:
        def f():
            s = self.seed[gid]
            if self.veh_bad(gid, t):
                return 170 + 380 * rnd(s, math.floor(t / 20), 9) ** 2
            return 6 + 32 * self.veh_dist(gid, t) + 4 * abs(fbm(s + 6, t, 90))
        return self._c(("rtt", gid), f)

    def active_vehicles(self, t: float, access: str | None = None) -> int:
        return self._c(("active", access), lambda: sum(
            1 for v in self.vehicles if self.veh_link(v, t)
            and (access is None or (self.inv.groups[v].access or "rajant") == access)))

    # -- infrastructure ------------------------------------------------------
    def loc_down(self, gid: str, t: float) -> bool:
        if self._fault((gid,), t) == "outage":
            return True
        pm = self.power.get(gid)
        return bool(pm and pm.at(t)["lvd"])

    # ------------------------------------------------------------------
    def sample(self, cid: str, t: float) -> CompResult:
        if t != self._t:
            self._t, self._cache = t, {}
        c = self.inv.comps[cid]
        gid = self.inv.comp_group[cid]
        if self._fault((cid,), t) == "outage":
            return CompResult(cid, t, False, error="timeout")
        # infrastructure is reached through its upstream device (switch, Tachyon backhaul ...)
        if c.depends_on and self.inv.group_tab[gid] != "vehicles":
            if not self.sample_cached(c.depends_on, t).reachable:
                return CompResult(cid, t, False, error="timeout")
        degrade = self._fault((cid, gid), t) == "degrade"
        fn = getattr(self, "_s_" + c.type, self._s_host)
        res = fn(c, gid, t, degrade)
        if res.reachable and (c.host or c.address == "dhcp"):
            res.info.setdefault("address", self.address_of(c, t))
            fw = (c.model_extra or {}).get("firmware")
            if fw:
                res.info["firmware"] = str(fw)
        return res

    LEASE_S = 4 * DAY

    def address_of(self, c, t: float) -> str:
        """Static: the inventory address. DHCP: a lease that sometimes changes, and always changes
        after an injected outage - so address-change events can be seen without hardware."""
        if c.address != "dhcp":
            return c.host
        s = self.seed[c.id]
        base = c.host or f"10.10.{(s >> 8) % 250}.{20 + s % 200}"
        net = base.rsplit(".", 1)[0]
        epoch = math.floor(t / self.LEASE_S + rnd(s, 21))
        e = next((k for k in range(epoch, epoch - 12, -1) if rnd(s, k, 22) < 0.45), None)
        outages = sum(1 for f in self.faults if f.target in (c.id, self.inv.comp_group[c.id])
                      and f.kind == "outage" and f.end <= t)
        if e is None and outages == 0:
            return base
        return f"{net}.{20 + int(rnd(s, e or 0, outages, 23) * 200)}"

    # helpers
    @staticmethod
    def _ok(c, t, metrics: dict, info: dict | None = None) -> CompResult:
        return CompResult(c.id, t, True, [Metric(k, float(v)) for k, v in metrics.items() if v is not None],
                          info or {})

    @staticmethod
    def _down(c, t, error="timeout") -> CompResult:
        return CompResult(c.id, t, False, error=error)

    def _ping(self, s: int, t: float, base: float, bad: bool) -> tuple[float, float]:
        rtt = base * (1 + 0.25 * abs(fbm(s, t, 120))) + 0.5 * rnd(s, math.floor(t / 10))
        loss = 0.0
        if bad:
            rtt = rtt * 5 + 120 + 300 * rnd(s, math.floor(t / 20), 3)
            loss = 3 + 30 * rnd(s, math.floor(t / 20), 4)
        return rtt, loss

    def _uptime(self, s: int, t: float, period_days: float = 23) -> float:
        p = period_days * DAY
        return (t + rnd(s, 8) * p) % p

    def _infra_bad(self, s: int, t: float, degrade: bool) -> bool:
        return degrade or vnoise(s + 3, t, 300) > 0.95

    # -- component types ----------------------------------------------------
    def _s_rajant(self, c, gid, t, degrade):
        s = self.seed[c.id]
        info = {"model": c.model or "", "firmware": c.sim.get("firmware", "11.31.1")}
        if self.inv.group_tab[gid] == "vehicles":
            if not self.veh_link(gid, t):
                return self._down(c, t)
            rtt = self.veh_radio_rtt(gid, t) + (60 if degrade else 0)
            bad = self.veh_bad(gid, t)
            loss = (4 + 25 * rnd(s, math.floor(t / 20))) if bad else 0.0
            tx = (3.0 + 2.5 * (1 + vnoise(s, t, 300)) / 2) * 1e6
            return self._ok(c, t, {
                "ping_rtt_ms": rtt, "ping_loss_pct": loss, "mobius_radio_rtt_ms": rtt + 1.5 * rnd(s, math.floor(t / 10), 2),
                "rajant_temp_c": self.ambient(t) + 24 + 3 * vnoise(s + 1, t, 1800),
                "rajant_peers": 2 + round(1.5 * (1 + vnoise(s + 2, t, 600))),
                "if_rx_bps": 0.4e6 * (1.2 + vnoise(s + 3, t, 120)), "if_tx_bps": tx,
                "uptime_s": self._uptime(s, t, 0.4)}, info)
        if self.loc_down(gid, t):
            return self._down(c, t, "timeout (no power at site?)")
        if episode(s, t, 0.06, 120, 1800) is not None:
            return self._down(c, t)
        kind = self.inv.groups[gid].kind
        base = {"core": 2, "building": 3, "tower": 6, "trailer": 8, "repeater": 12}.get(kind, 6)
        rtt, loss = self._ping(s, t, base, self._infra_bad(s, t, degrade))
        active = self.active_vehicles(t)
        return self._ok(c, t, {
            "ping_rtt_ms": rtt, "ping_loss_pct": loss,
            "rajant_temp_c": self.ambient(t) + 18 + 3 * fbm(s + 1, t, 3600),
            "rajant_input_v": 53.6 + 0.25 * vnoise(s + 2, t, 900),
            "rajant_peers": max(1, round(float(c.sim.get("peers", 4)) + 0.4 * active + 0.6 * vnoise(s + 4, t, 900))),
            "if_rx_bps": (0.3 + 0.8 * active * (0.6 + 0.4 * vnoise(s + 5, t, 300))) * 1e6,
            "if_tx_bps": (0.2 + 0.3 * active * (0.6 + 0.4 * vnoise(s + 6, t, 300))) * 1e6,
            "uptime_s": self._uptime(s, t)}, info)

    def _vehicle_router(self, c, gid, t, degrade, wg: bool):
        s = self.seed[c.id]
        if not self.veh_link(gid, t):
            return self._down(c, t)
        rtt = self.veh_radio_rtt(gid, t) + 1.2 + 0.6 * rnd(s, math.floor(t / 10)) + (60 if degrade else 0)
        m = {"ping_rtt_ms": rtt, "ping_loss_pct": 0.0,
             "router_cpu_pct": 9 + 12 * (1 + vnoise(s, t, 300)) / 2,
             "router_mem_used_pct": 36 + 3 * vnoise(s + 1, t, 3600),
             "router_temp_c": self.ambient(t) + 22 + 2 * vnoise(s + 2, t, 900),
             "wan_rx_bps": 0.3e6 * (1.5 + vnoise(s + 3, t, 120)),
             "wan_tx_bps": 4.0e6 * (1.2 + 0.5 * vnoise(s + 4, t, 120)),
             "uptime_s": self._uptime(s, t, 0.4)}
        if c.type == "mikrotik":
            m["router_supply_v"] = 13.9 + 0.15 * vnoise(s + 5, t, 600)
        if wg:
            m["wg_handshake_age_s"] = (t + rnd(s, 9) * 120) % 120
        return self._ok(c, t, m, {"model": c.model or "", "firmware": c.sim.get("firmware", "7.23.5")})

    def _s_mikrotik(self, c, gid, t, degrade):
        return self._vehicle_router(c, gid, t, degrade, bool(c.sim.get("wireguard")))

    def _s_westermo(self, c, gid, t, degrade):
        return self._vehicle_router(c, gid, t, degrade, False)

    def _s_cell_modem(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if not self.veh_link(gid, t):
            return self._down(c, t)
        d = self.veh_dist(gid, t)
        bad = self.veh_bad(gid, t) or degrade
        return self._ok(c, t, {
            "ping_rtt_ms": (28 + 14 * d + 3 * abs(fbm(s, t, 60))) * (6 if bad else 1),
            "ping_loss_pct": 6.0 if bad else 0.0,
            "cell_rsrp_dbm": -82 - 22 * d - (12 if bad else 0) + 2 * vnoise(s + 1, t, 60),
            "cell_sinr_db": 22 - 14 * d - (8 if bad else 0) + 1.5 * vnoise(s + 2, t, 60)})

    def _s_vcu(self, c, gid, t, degrade):
        s = self.seed[c.id]
        g = self.inv.groups[gid]
        if not self.mobius_up(t):
            return self._down(c, t, "Mobius not reporting")
        if not self.veh_link(gid, t):
            return self._ok(c, t, {"vcu_communicating": 0}, {"asset_state": "Not communicating"})
        bad = self.veh_bad(gid, t) or degrade
        lat = self.veh_radio_rtt(gid, t) + 18 + 6 * abs(fbm(s, t, 60))
        if bad:
            lat = lat * 1.4 + 250 * rnd(s, math.floor(t / 15))
        hot = 16 if g.sim.get("vcu_hot") else 0
        lt = self.local(t)
        mode = "Autonomous" if rnd(s, lt.toordinal(), 1) > 0.2 else "Manual"
        return self._ok(c, t, {
            "vcu_communicating": 1, "vcu_comms_latency_ms": lat,
            "vcu_comms_drop_pct": (6 + 30 * rnd(s, math.floor(t / 15), 2)) if bad else 0.3 * rnd(s, math.floor(t / 30), 3),
            "vcu_cpu_temp_c": 52 + hot + 0.6 * self.ambient(t) + 3 * vnoise(s + 1, t, 600),
            "vcu_temp_c": 38 + hot / 2 + 0.6 * self.ambient(t) + 2 * vnoise(s + 2, t, 900),
            "vcu_supply_v": 27.5 + 0.3 * vnoise(s + 3, t, 300),
            "vcu_cpu_pct": 32 + 18 * (1 + vnoise(s + 4, t, 180)) / 2},
            {"asset_state": mode, "active_errors": "COMMS_SLOW" if bad else "", "estop": "Run"})

    def _s_gnss(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if not self.mobius_up(t):
            return self._down(c, t, "Mobius not reporting")
        if not self.veh_link(gid, t):
            return self._down(c, t, "vehicle not communicating")
        age = 0.6 + 0.7 * rnd(s, math.floor(t / 10))
        if self.veh_bad(gid, t) or degrade:
            age += 3 + 7 * rnd(s, math.floor(t / 20), 2)
        nt = self.ntrip_outage_age(t)
        if nt is not None:
            age = nt + 1
        multipath = episode(s, t, 1.0, 60, 400) is not None
        mode = 4 if age < 10 else 5 if age < 60 else 2
        if multipath and mode == 4:
            mode = 5
        rms = {4: 0.015, 5: 0.16, 2: 0.8}[mode] * (1 + 0.3 * vnoise(s + 1, t, 60))
        return self._ok(c, t, {
            "gnss_mode": mode, "gnss_corr_age_s": age,
            "gnss_sats": round(18 + 3 * vnoise(s + 2, t, 1800)),
            "gnss_hdop": 0.75 + 0.15 * vnoise(s + 3, t, 600) + (0.6 if multipath else 0),
            "gnss_rms_m": rms})

    def _s_vai(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if not self.mobius_up(t):
            return self._down(c, t, "Mobius not reporting")
        if not self.veh_link(gid, t):
            return self._down(c, t, "vehicle not communicating")
        crashed = degrade or episode(s, t, 0.12, 60, 900) is not None
        return self._ok(c, t, {
            "vai_connected": 0 if crashed else 1,
            "vai_heartbeat_age_s": (30 + t % 60) if crashed else 0.3 + 0.6 * rnd(s, math.floor(t / 10))},
            {"helm": "Stopped" if crashed else "All subsystems OK"})

    def _s_victron_cerbo(self, c, gid, t, degrade):
        pm = self.power.get(gid)
        if pm is None:
            return self._down(c, t, "no power model")
        p = pm.at(t)
        if p["lvd"] or self._fault((gid,), t) == "outage":
            return self._down(c, t, "timeout (battery disconnected?)")
        return self._ok(c, t, {
            "power_soc_pct": p["soc"], "power_batt_v": p["v"], "power_batt_a": p["a"], "power_batt_w": p["w"],
            "power_pv_w": p["pv"], "power_load_w": p["load"], "power_mppt_state": p["state"],
            "power_yield_today_kwh": p["yield"]},
            {"battery_wh": round(pm.cap_wh), "pv_peak_w": round(pm.pv_peak)})

    def _s_starlink(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.inv.group_tab[gid] == "infrastructure" and self.loc_down(gid, t):
            return self._down(c, t, "timeout (no power at site?)")
        searching = episode(s, t, 0.25, 40, 360) is not None or degrade
        burst = episode(s + 1, t, 2.0, 60, 240) is not None
        active = self.active_vehicles(t)
        alerts = 1 if (self.ambient(t) > 30 and vnoise(s + 9, t, 3600) > 0.5) else 0
        m = {"starlink_state": 0 if searching else 1,
             "starlink_pop_latency_ms": None if searching else 32 + 6 * abs(fbm(s, t, 300)) + (45 if burst else 0),
             "starlink_pop_drop_pct": 100.0 if searching else (4 + 10 * rnd(s, math.floor(t / 30))) if burst
             else 0.4 * rnd(s, math.floor(t / 30), 2),
             "starlink_obstructed_pct": float(c.sim.get("obstruction", 0.6)) * (1 + 0.2 * vnoise(s + 2, t, 3 * DAY)),
             "starlink_down_bps": 0 if searching else (2 + 3 * active) * 1e6 * (1 + 0.4 * vnoise(s + 3, t, 120)),
             "starlink_up_bps": 0 if searching else (1 + 1.5 * active) * 1e6 * (1 + 0.4 * vnoise(s + 4, t, 120)),
             "starlink_alerts": alerts,
             "starlink_power_w": 52 + 8 * vnoise(s + 5, t, 600)}
        return self._ok(c, t, m, {"software": c.sim.get("software", "2026.10.06.mr1"),
                                  "alerts": "thermal throttle" if alerts else ""})

    def _dead(self, cid: str, t: float, depth: int = 0) -> bool:
        """Structurally gone (outage, no power, or behind something that is) - without sampling,
        so the two ends of a link can ask about each other."""
        gid = self.inv.comp_group[cid]
        if self._fault((cid, gid), t) == "outage" or self.loc_down(gid, t):
            return True
        dep = self.inv.comps[cid].depends_on
        return bool(dep and depth < 16 and self._dead(dep, t, depth + 1))

    def _tachyon_base(self, c) -> float:
        if c.install_rssi is not None:
            return float(c.install_rssi)
        return float(c.sim.get("rssi", -50))

    def _s_tachyon(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t):
            return self._down(c, t, "timeout (no power at site?)")
        partner = self.inv.link_partner(c.id)
        ls = seed_of(c.link) if c.link else s            # both ends of a link fade together
        fade = degrade or episode(ls, t, 0.3, 120, 1200) is not None
        info = {"firmware": c.sim.get("firmware", "1.12.3"), "link": c.link or "", "role": c.role or ""}
        rtt, _ = self._ping(s, t, 2.5, False)
        # Link down while this radio still answers: the far end is gone (outage / no power), or the
        # 60 GHz path itself failed (link fault - both radios stay up). Over the Rajant mesh this end
        # is still reachable, so it reports the link down rather than going silent.
        link_fault = any(self._fault((x,), t) == "link" for x in (c.id, partner) if x)
        far_dead = bool(partner) and (c.role == "master" or self.inv.backhaul == "redundant") and self._dead(partner, t)
        if partner and (link_fault or far_dead):
            return self._ok(c, t, {"tachyon_link_up": 0, "ping_rtt_ms": rtt, "ping_loss_pct": 0.0,
                                   "tachyon_radio_temp_c": self.ambient(t) + 22}, info)
        base = self._tachyon_base(c)
        far = self._tachyon_base(self.inv.comps[partner]) if partner else base
        drop = 12 + 4 * rnd(ls, math.floor(t / 60)) if fade else 0
        rssi = base - drop + 1.2 * vnoise(s + 1, t, 300)
        snr = 27 - drop * 1.2 + vnoise(s + 2, t, 300)
        mcs = max(0, min(12, round(12 - max(0.0, (24 - snr) / 2))))
        rtt, loss = self._ping(s, t, 2.5, fade and drop > 14)
        return self._ok(c, t, {
            "tachyon_link_up": 1,
            "tachyon_rssi_dbm": rssi, "tachyon_remote_rssi_dbm": far - drop + 1.2 * vnoise(s + 3, t, 300),
            "tachyon_snr_db": snr, "tachyon_tx_mcs": mcs, "tachyon_phy_rate_mbps": 2600 * (mcs + 1) / 13,
            "tachyon_radio_temp_c": self.ambient(t) + 26 + 2 * vnoise(s + 4, t, 900),
            "ping_rtt_ms": rtt, "ping_loss_pct": loss}, info)

    def _s_switch(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t):
            return self._down(c, t, "timeout (no power at site?)")
        g = self.inv.groups[gid]
        others = [x for x in g.components if x.id != c.id]
        powered = [x for x in others if x.type != "victron_cerbo" and self._fault((x.id,), t) != "outage"]
        rtt, loss = self._ping(s, t, 1.5, degrade)
        return self._ok(c, t, {
            "ping_rtt_ms": rtt, "ping_loss_pct": loss,
            "switch_ports_up": 1 + sum(1 for x in others if self._fault((x.id,), t) != "outage"),
            "switch_poe_w": sum(TYPES[x.type].load_w for x in powered) * (1 + 0.05 * vnoise(s, t, 600)),
            "uptime_s": self._uptime(s, t, 60)}, {"model": c.model or ""})

    def _s_airmax(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t):
            return self._down(c, t, "timeout (no power at site?)")
        interf = degrade or episode(s, t, 0.5, 120, 1500) is not None
        sig = float(c.sim.get("signal", -58)) + 2 * vnoise(s + 1, t, 600) - (9 if interf else 0)
        rtt, loss = self._ping(s, t, 3, interf and vnoise(s + 5, t, 60) > 0.3)
        return self._ok(c, t, {
            "airmax_signal_dbm": sig, "airmax_noise_dbm": -96 + 2 * vnoise(s + 2, t, 900) + (14 if interf else 0),
            "airmax_quality_pct": (62 if interf else 92) + 4 * vnoise(s + 3, t, 300),
            "airmax_capacity_pct": (55 if interf else 86) + 5 * vnoise(s + 4, t, 300),
            "ping_rtt_ms": rtt, "ping_loss_pct": loss})

    def _s_unifi(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t):
            return self._down(c, t)
        active = self.active_vehicles(t)
        rtt, loss = self._ping(s, t, 1.0, degrade)
        return self._ok(c, t, {
            "ping_rtt_ms": rtt, "ping_loss_pct": loss,
            "unifi_cpu_pct": 9 + 6 * (1 + vnoise(s, t, 300)), "unifi_mem_pct": 48 + 4 * vnoise(s + 1, t, 3600),
            "unifi_uplink_rx_bps": (4 + 5 * active) * 1e6 * (1 + 0.3 * vnoise(s + 2, t, 120)),
            "unifi_uplink_tx_bps": (2 + 2 * active) * 1e6 * (1 + 0.3 * vnoise(s + 3, t, 120)),
            "uptime_s": self._uptime(s, t, 41)}, {"model": c.model or ""})

    def _s_celona_ap(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t) or episode(s, t, 0.05, 120, 900) is not None:
            return self._down(c, t)
        lt = self.local(t)
        handhelds = 3 + round(2 * vnoise(s + 1, t, 1800)) if 6 <= lt.hour < 18 and lt.weekday() < 5 else 0
        rtt, loss = self._ping(s, t, 2.5, degrade)
        return self._ok(c, t, {"ping_rtt_ms": rtt, "ping_loss_pct": loss,
                               "celona_ues": handhelds + self.active_vehicles(t, "celona")})

    def _s_celona_edge(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t):
            return self._down(c, t)
        aps = [x for x in self.inv.comps.values() if x.type == "celona_ap"]
        up = sum(1 for x in aps if self.sample_cached(x.id, t).reachable)
        rtt, loss = self._ping(s, t, 1.2, degrade)
        return self._ok(c, t, {"ping_rtt_ms": rtt, "ping_loss_pct": loss, "celona_aps_up": up})

    def sample_cached(self, cid: str, t: float) -> CompResult:
        return self._c(("sample", cid), lambda: self.sample(cid, t))

    def _s_mobius_server(self, c, gid, t, degrade):
        s = self.seed[c.id]
        up = self.mobius_up(t)
        clients = sum(1 for g in self.client_sched if self._client_connected(g, t)) if up else 0
        assets = self.active_vehicles(t) if up else 0
        return self._ok(c, t, {
            "mobius_up": 1 if up else 0, "tcp_ms": (1.2 + 0.8 * rnd(s, math.floor(t / 10))) if up else None,
            "mobius_clients": clients, "mobius_assets": assets,
            "host_cpu_pct": 14 + 3.5 * assets + 6 * (1 + vnoise(s, t, 300)) / 2 + (35 if degrade else 0),
            "host_mem_used_pct": 54 + 4 * vnoise(s + 1, t, 6 * 3600),
            "mobius_clock_skew_s": 0.08 + 0.12 * (1 + vnoise(s + 2, t, 3600)) / 2
            + (3.5 if episode(s + 3, t, 0.05, 1800, 7200) is not None else 0)},
            {"version": c.sim.get("version", "Mobius 2026.3"), "host": c.host or ""})

    def _s_influxdb(self, c, gid, t, degrade):
        s = self.seed[c.id]
        rtt, loss = self._ping(s, t, 0.8, degrade)
        age = 2 + 3 * rnd(s, math.floor(t / 10))
        if not self.mobius_up(t):
            age = 60 + (t % 300)
        return self._ok(c, t, {"ping_rtt_ms": rtt, "ping_loss_pct": loss,
                               "tcp_ms": 1.5 + rnd(s, math.floor(t / 10), 2), "influx_write_age_s": age})

    def _s_mobius_ntrip(self, c, gid, t, degrade):
        if not self.mobius_up(t):
            return self._down(c, t, "Mobius not reporting")
        return self._ok(c, t, {"ntrip_connected": 0 if self.ntrip_outage_age(t) is not None else 1})

    def _client_connected(self, gid: str, t: float) -> bool:
        def f():
            sch = self.client_sched.get(gid)
            if sch is None or not self.mobius_up(t):
                return False
            lt = self.local(t)
            if not sch.active(lt):
                return False
            return rnd(self.seed[gid], lt.toordinal(), 1) > 0.05
        return self._c(("client", gid), f)

    def _s_mobius_client(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self._fault((gid,), t) == "outage":
            return self._down(c, t)
        g = self.inv.groups[gid]
        on = self._client_connected(gid, t)
        rtt, loss = self._ping(s, t, 1.0, degrade)
        return self._ok(c, t, {
            "client_connected": 1 if on else 0, "ping_rtt_ms": rtt, "ping_loss_pct": loss,
            "host_cpu_pct": (18 if on else 4) + 12 * (1 + vnoise(s, t, 240)) / 2,
            "host_mem_used_pct": (58 if on else 35) + 6 * vnoise(s + 1, t, 3600),
            "host_disk_free_pct": float(g.sim.get("disk_free", 55)) + 0.5 * vnoise(s + 2, t, DAY)},
            {"user": g.sim.get("user", "operator") if on else ""})

    def _s_gnss_base(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.loc_down(gid, t) or episode(s, t, 0.03, 300, 1800) is not None:
            return self._down(c, t)
        rtt, loss = self._ping(s, t, 3.5, degrade)
        return self._ok(c, t, {"ping_rtt_ms": rtt, "ping_loss_pct": loss,
                               "tcp_ms": 3 + 2 * rnd(s, math.floor(t / 10), 5)})

    def _s_host(self, c, gid, t, degrade):
        s = self.seed[c.id]
        if self.inv.group_tab[gid] == "infrastructure" and self.loc_down(gid, t):
            return self._down(c, t)
        rtt, loss = self._ping(s, t, 2.0, degrade)
        return self._ok(c, t, {"ping_rtt_ms": rtt, "ping_loss_pct": loss})
