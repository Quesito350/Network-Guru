"""REST endpoints. Read-only apart from the simulator's fault injection, sending feedback and reporting a
problem (0.10.0); admin changes are in admin.py."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import TOOL_NAME, __version__
from .. import feedback as fb, logbuffer, problem_report, problem_ticket as pt
from ..auth import COOKIE
from .admin import client_ip, same_origin, secret_list
from ..catalog import catalog_json
from ..changelog import as_json as changelog_json
from ..maptiles import choice as maptile_choice, tiles_for
from ..model import STATUS_NAMES, Info
from ..report import as_html, as_text, report_from_runtime
from ..runtime import TRACK_S

router = APIRouter(prefix="/api")
MAX_SPAN = 32 * 86400


def rt(request: Request):
    return request.app.state.rt


def _range(frm: float | None, to: float | None, default_span: float = 3600) -> tuple[float, float]:
    now = time.time()
    end = min(to, now) if to else now
    start = frm if frm else end - default_span
    if start >= end:
        raise HTTPException(400, "'from' must be before 'to'")
    if end - start > MAX_SPAN:
        raise HTTPException(400, "range longer than 32 days")
    return start, end


def _ids(r, ids: str) -> list[str]:
    out = [i for i in ids.split(",") if i]
    bad = [i for i in out if i not in r.pipeline.state]
    if bad:
        raise HTTPException(404, f"unknown id(s): {', '.join(bad[:5])}")
    return out


def _nav(r, node_id: str | None) -> dict:
    inv = r.inv
    if node_id is None:                        # system events (updates, reports, admin)
        return {"name": "Network Guru", "group": None, "tab": None}
    if node_id in inv.comps:
        gid = inv.comp_group[node_id]
        return {"name": inv.node_name(node_id), "group": gid, "tab": inv.group_tab[gid]}
    if node_id in inv.groups:
        return {"name": inv.groups[node_id].name, "group": node_id, "tab": inv.group_tab[node_id]}
    return {"name": inv.node_name(node_id), "group": None, "tab": node_id}


@router.get("/health")
async def health(request: Request):
    return rt(request).health()


@router.get("/map")
async def map_settings(request: Request):
    """The Map page's background (Admin → Map) - the tile URL the browser loads, or none."""
    choice = rt(request).settings_state.get("map_background")
    return {"background": maptile_choice(choice), "tiles": tiles_for(choice)}


@router.get("/map/vehicles")
async def map_vehicles(request: Request):
    """Every Mobius vehicle's last position (0.9.3): its inventory vehicle (group) or none, whether it is
    live, which way it last drove and (live) its trail of the last 2 minutes (0.9.5). Live mode with a
    Mobius map only - the simulator's vehicles come with their GNSS info."""
    fleet = rt(request).fleet
    if fleet is None:
        return {"enabled": False, "vehicles": []}
    fleet.watch()                              # the Map is open: positions every few seconds (0.9.5)
    out = fleet.positions.public(fleet.vehicle_of)
    for v in out:                              # what Mobius says it is (0.9.7)
        info = fleet.identity.get(v["name"]) or {}
        v.update({"type": info.get("type"), "model": info.get("model"), "ip": info.get("ip"),
                  "simulated": fleet.simulated(v["name"])})
    return {"enabled": True, "every_s": TRACK_S, "vehicles": out}


@router.get("/mobius/vehicles")
async def mobius_vehicles(request: Request):
    """What Mobius says each vehicle is (0.9.7): per vehicle of the dashboard {name (in Mobius), ip, mac, make,
    model, type, comms, last, simulated, auto (added from Mobius)}, and the ones with no vehicle (the
    simulator's). Live mode with the map's `vehicle` values."""
    mv = rt(request).mobius_vehicles
    return {"enabled": True, **mv.public()} if mv is not None else {"enabled": False, "groups": {}, "others": []}


@router.get("/meta")
async def meta(request: Request):
    r = rt(request)
    return {"tool": TOOL_NAME, "version": __version__, "mode": r.mode,
            "site": {"id": r.inv.site.id, "name": r.inv.site.name, "timezone": r.inv.site.timezone},
            "tsdb": r.tsdb.kind, "server_time": time.time(),
            # to-dos are banners; information goes under the footer's "What's checked" (0.9.4)
            "notes": [n for n in r.notes if not isinstance(n, Info)],
            "info": [str(n) for n in r.notes if isinstance(n, Info)],
            "backfill": {"pct": round(r.progress.pct, 1), "done": r.progress.done},
            "history_days": r.settings.history_days,
            "statuses": STATUS_NAMES, "catalog": catalog_json(), "thresholds": r.rules.thresholds_json(),
            "enum_rules": r.rules.enum_rules_json(), "started": r.started,
            "hide_quiet": {"min": r.settings_state.get("hide_quiet_min", 60),
                           "tabs": r.settings_state.get("hide_quiet_tabs", [])},
            "mobius_explore": r.mobius is not None,
            "ncr": bool(r.settings_state.get("ncr_url"))}      # 0.10.0: Report a problem opens a change request


@router.get("/mobius/summary")
async def mobius_summary(request: Request):
    """Per group: when Mobius last had data for it (Live's "gone quiet" check, the cards), map positions."""
    m = rt(request).mobius
    return m.summary() if m else {"enabled": False}


@router.get("/mobius/clients")
async def mobius_clients(request: Request):
    """Every Mobius client PC Mobius reports (0.9.6): connected or when last seen, its values (the map's
    mobius_client values) and its desk in the inventory, if any. Live mode with a mobius_clients map."""
    r = rt(request)
    mc = r.mobius_clients
    if mc is None:
        return {"enabled": False, "clients": [], "columns": [], "connections": []}
    return {"enabled": True, "columns": mc.columns, "clients": mc.public(r.inv), "connections": mc.public_connections()}


@router.get("/mobius")
async def mobius_all(request: Request):
    """Everything Mobius reports: matched groups, what is not in the inventory, site-wide values."""
    m = rt(request).mobius
    return m.everything() if m else {"enabled": False}


@router.get("/mobius/{group_id}")
async def mobius_group(request: Request, group_id: str):
    r = rt(request)
    if group_id not in r.inv.groups:
        raise HTTPException(404, "unknown group")
    return r.mobius.group(group_id) if r.mobius else {"enabled": False}


@router.get("/changelog")
async def changelog():
    return changelog_json()


@router.get("/inventory")
async def inventory(request: Request):
    return rt(request).inv.public_tree()


@router.get("/live")
async def live(request: Request):
    return rt(request).pipeline.snapshot()


@router.get("/live/{node_id}")
async def live_node(request: Request, node_id: str):
    r = rt(request)
    st = r.pipeline.state.get(node_id)
    if st is None:
        raise HTTPException(404, "unknown id")
    return {"id": node_id, **_nav(r, node_id), **st.public()}


@router.get("/history/charts/{comp_id}")
async def charts(request: Request, comp_id: str, frm: float | None = Query(None, alias="from"),
                 to: float | None = None, primary: bool = False, keys: str = ""):
    r = rt(request)
    if comp_id not in r.inv.comps:
        raise HTTPException(404, "unknown component")
    start, end = _range(frm, to)
    return await r.history.charts(comp_id, start, end, primary, [k for k in keys.split(",") if k] or None)


@router.get("/history/sparklines")
async def sparklines(request: Request, ids: str, frm: float | None = Query(None, alias="from"),
                     to: float | None = None):
    r = rt(request)
    groups = [g for g in _ids(r, ids) if g in r.inv.groups]
    start, end = _range(frm, to)
    return await r.history.sparklines(groups, start, end)


@router.get("/history/status")
async def status_strips(request: Request, ids: str, frm: float | None = Query(None, alias="from"),
                        to: float | None = None):
    r = rt(request)
    start, end = _range(frm, to)
    return r.events.intervals(_ids(r, ids), start, end, time.time())


@router.get("/availability")
async def availability(request: Request, ids: str, frm: float | None = Query(None, alias="from"),
                       to: float | None = None):
    r = rt(request)
    start, end = _range(frm, to)
    return r.events.availability(_ids(r, ids), start, end, time.time())


@router.get("/path/{vehicle_id}")
async def path(request: Request, vehicle_id: str, frm: float | None = Query(None, alias="from"),
               to: float | None = None):
    r = rt(request)
    if vehicle_id not in r.inv.groups or r.inv.group_tab[vehicle_id] != "vehicles":
        raise HTTPException(404, "unknown vehicle")
    if frm is None:
        return r.history.path(vehicle_id)
    start, end = _range(frm, to)
    return await r.history.path_history(vehicle_id, start, end, time.time())


@router.get("/events")
async def events(request: Request, frm: float | None = Query(None, alias="from"), to: float | None = None,
                 node: str = "", min_status: int = 0, suppressed: bool = False, limit: int = 300,
                 kinds: str = "", info: bool = False, system: bool = False):
    r = rt(request)
    start, end = _range(frm, to, default_span=86400)
    ids = None
    if node:
        if node not in r.pipeline.state:
            raise HTTPException(404, "unknown id")
        ids = [node] + ([c.id for c in r.inv.groups[node].components] if node in r.inv.groups else [])
    limit = max(1, min(limit, 2000))
    rows = [] if kinds == "system" else r.events.events(start, end, ids, min_status, suppressed,
                                                        tuple(k for k in kinds.split(",") if k) or None, limit)
    if info:                                   # address / firmware changes, merged in time order
        rows = sorted(rows + r.events.info_events(start, end, ids, limit), key=lambda x: -x["ts"])[:limit]
    if system and not ids:                     # updates, morning reports, admin sign-ins
        rows = sorted(rows + r.events.system_events(start, end, limit), key=lambda x: -x["ts"])[:limit]
    for row in rows:
        row.update(_nav(r, row["node"]))
    return rows


class FaultIn(BaseModel):
    target: str
    kind: str = "outage"
    minutes: float = 5


@router.get("/update/status")
async def update_status(request: Request):
    """What the update banner shows (everyone sees it; only an admin can act on it)."""
    return rt(request).updates.public_status()


@router.get("/report/preview")
async def report_preview(request: Request):
    r = rt(request)
    rep = report_from_runtime(r)
    return {"subject": rep.subject, "text": as_text(rep), "html": as_html(rep), "verdict": rep.verdict,
            "settings": {k: r.settings_state.get(k) for k in ("report_time", "report_teams", "report_email")},
            "sent": r.reports.public_status()["channels"], "sent_date": r.reports.state.get("date"),
            "sent_note": r.reports.state.get("note")}


class FeedbackIn(BaseModel):
    kind: str = "request"
    text: str = ""
    name: str = ""
    page: str = ""


@router.post("/feedback")
async def send_feedback(request: Request, body: FeedbackIn):
    """Anyone viewing (0.9.5, feedback.py): a request, problem or idea for the admins. Same-origin only,
    rate limited per address."""
    same_origin(request)
    r = rt(request)
    if r.events is None:
        raise HTTPException(503, "Feedback can't be saved on this server right now.")
    try:
        kind, text, name, page = fb.clean(body.kind, body.text, body.name, body.page)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not r.feedback_limit.allow(client_ip(request)):
        raise HTTPException(429, "That's a lot of feedback in one hour - please try again later.")
    return {"id": r.events.add_feedback(time.time(), kind, text, name, page)}


class ProblemIn(BaseModel):
    text: str = ""
    priority: str = "Standard"
    page: str = ""
    browser: dict = {}


@router.post("/problem")
async def report_problem(request: Request, body: ProblemIn):
    """Anyone viewing (0.10.0, problem_ticket.py): the full problem report is kept on this server and a
    ticket comes back for the site's Network Change Requests page (Admin → Settings), with an error-log
    excerpt. Same-origin only, rate limited per address."""
    same_origin(request)
    r = rt(request)
    try:
        text = pt.clean_text(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    priority = body.priority if body.priority in pt.PRIORITIES else "Standard"
    page = str(body.page or "")[:200]
    if not r.problem_limit.allow(client_ip(request)):
        raise HTTPException(429, "That's a lot of problem reports in one hour - please try again later.")
    now = time.time()
    session = r.auth.session(request.cookies.get(COOKIE))
    who = session.name if session else f"a viewer at {client_ip(request)}"
    from zoneinfo import ZoneInfo
    rid = pt.new_id(now, ZoneInfo(r.inv.site.timezone))
    ring = logbuffer.install()
    browser = body.browser if isinstance(body.browser, dict) else {}
    name, data = problem_report.build(r, who, f"[{priority}] {text}", browser, secret_list(r), ring.text(), now=now)
    first = text.splitlines()[0][:120]
    r.problem_reports.save(rid, data, {"t": now, "by": who, "first": first, "priority": priority, "file": name})
    if r.events is not None:
        r.events.add_system_event(now, "problem", f"Problem reported ({rid}): {first}", 3, session.name if session else None)
    url = str(r.settings_state.get("ncr_url") or "")
    location = pt.location_for(str(r.settings_state.get("ncr_location") or ""), r.inv.site.name, r.inv.site.id)
    log = pt.excerpt(r, rid, page, browser, ring.entries(), now)
    ng = pt.encode(pt.ticket(r, rid, text, priority, page, location, log))
    return {"id": rid, "ng": ng, "url": f"{url}#ng={ng}" if url else None, "location": location,
            "log_chars": len(log), "admin": bool(session)}


@router.post("/sim/fault")
async def sim_fault(request: Request, body: FaultIn):
    r = rt(request)
    if r.mode != "sim" or r.world is None:
        raise HTTPException(403, "only available in simulator mode")
    if r.docker:                               # on a server, only an admin may inject faults
        from .admin import require_admin
        require_admin(request)
    if not 0 < body.minutes <= 240:
        raise HTTPException(400, "minutes must be 1-240")
    try:
        f = r.world.add_fault(body.target, body.kind, body.minutes)
    except KeyError:
        raise HTTPException(404, "unknown id") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"target": f.target, "kind": f.kind, "start": f.start, "end": f.end}
