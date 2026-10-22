"""REST endpoints. Read-only apart from the simulator's fault injection; admin changes are in admin.py."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import TOOL_NAME, __version__
from ..catalog import catalog_json
from ..changelog import as_json as changelog_json
from ..maptiles import choice as maptile_choice, tiles_for
from ..model import STATUS_NAMES
from ..report import as_html, as_text, report_from_runtime

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


@router.get("/meta")
async def meta(request: Request):
    r = rt(request)
    return {"tool": TOOL_NAME, "version": __version__, "mode": r.mode,
            "site": {"id": r.inv.site.id, "name": r.inv.site.name, "timezone": r.inv.site.timezone},
            "tsdb": r.tsdb.kind, "notes": r.notes, "server_time": time.time(),
            "backfill": {"pct": round(r.progress.pct, 1), "done": r.progress.done},
            "history_days": r.settings.history_days,
            "statuses": STATUS_NAMES, "catalog": catalog_json(), "thresholds": r.rules.thresholds_json(),
            "enum_rules": r.rules.enum_rules_json(), "started": r.started,
            "hide_quiet": {"min": r.settings_state.get("hide_quiet_min", 60),
                           "tabs": r.settings_state.get("hide_quiet_tabs", [])},
            "mobius_explore": r.mobius is not None}


@router.get("/mobius/summary")
async def mobius_summary(request: Request):
    """Per group: when Mobius last had data for it (Live's "gone quiet" check, the cards), map positions."""
    m = rt(request).mobius
    return m.summary() if m else {"enabled": False}


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
