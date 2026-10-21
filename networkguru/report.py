"""The morning report: what needs attention now, then what changed since yesterday.

It is sent every day, even when everything is green, so a missing report means the tool (or
its server) is broken. Built from the same live state and event list as the dashboard, and
rendered three ways: plain text, HTML (e-mail and the preview page) and a Teams Adaptive Card.
Statuses always come as a symbol plus a word, never colour alone.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import time
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from .inventory import TABS
from .model import Status

SYMBOL = {Status.OK: "✔", Status.UNKNOWN: "?", Status.OFFLINE: "●", Status.WARNING: "▲", Status.CRITICAL: "✖"}
COLOR = {Status.OK: "#0ca30c", Status.UNKNOWN: "#7b8086", Status.OFFLINE: "#7b8086", Status.WARNING: "#b07800",
         Status.CRITICAL: "#c62828"}
LABEL = {Status.OK: "OK", Status.UNKNOWN: "Unknown", Status.OFFLINE: "Offline", Status.WARNING: "Warning",
         Status.CRITICAL: "Critical"}
CARD_COLOR = {Status.OK: "Good", Status.UNKNOWN: "Default", Status.OFFLINE: "Default", Status.WARNING: "Warning",
              Status.CRITICAL: "Attention"}
MAX_CHANGES = 30
MAX_INFO = 20
MAX_STEPS = 6                 # status changes shown per device; older ones become "…"
CARD_LIMIT = 27 * 1024        # Teams rejects cards near 28 KB


def word(s: int) -> str:
    st = Status(s)
    return f"{SYMBOL[st]} {LABEL[st]}"


@dataclass
class Line:
    status: int
    name: str
    reason: str
    since: float | None = None
    href: str = ""


@dataclass
class Report:
    site_name: str
    site_id: str
    date: str
    generated_at: float
    tz: str
    mode: str
    counts: dict[str, int]
    total: int
    verdict: str                       # problems | all_clear | quiet | no_data
    headline: str
    attention: list[Line] = field(default_factory=list)
    hidden_behind: int = 0
    changes: list[str] = field(default_factory=list)
    changes_more: int = 0
    info: list[str] = field(default_factory=list)
    parked: int = 0
    availability: list[tuple[str, float | None]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    update_line: str = ""
    dashboard_url: str = ""
    late: bool = False
    test: bool = False

    @property
    def subject(self) -> str:
        tags = ("[TEST] " if self.test else "") + ("[SIMULATED] " if self.mode == "sim" else "")
        late = " (late)" if self.late else ""
        return f"{tags}Network Guru {self.site_name}: {self.headline} - {self.date}{late}"

    def as_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "verdict": self.verdict, "headline": self.headline, "counts": self.counts,
                "date": self.date, "generated_at": self.generated_at, "mode": self.mode,
                "attention": [vars(x) for x in self.attention], "changes": self.changes, "info": self.info}


def _fmt_time(ts: float | None, tz: ZoneInfo, now: float) -> str:
    if not ts:
        return ""
    d = dt.datetime.fromtimestamp(ts, tz)
    n = dt.datetime.fromtimestamp(now, tz)
    return d.strftime("%H:%M") if d.date() == n.date() else d.strftime("%a %H:%M")


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.0f} days"


def _same_time_yesterday(now: float, tz: ZoneInfo) -> float:
    d = dt.datetime.fromtimestamp(now, tz)
    return (d - dt.timedelta(days=1)).timestamp()        # wall-clock: 23 or 25 h on DST days


def build_report(inv, state: dict, events, mode: str, now: float, tz: ZoneInfo, *, dashboard_url: str = "",
                 update: dict | None = None, collector_ok: bool = True, late: bool = False,
                 test: bool = False) -> Report:
    comps = list(inv.comps)
    counts = {s.name.lower(): 0 for s in Status}
    hidden = 0
    for cid in comps:
        st = state.get(cid)
        s = Status(st.status) if st else Status.UNKNOWN
        counts[s.name.lower()] += 1
        if st and st.suppressed:
            hidden += 1

    # needs attention now: the dashboard's rule (red/yellow, not hidden behind a failed device)
    attention: list[Line] = []
    for gid, g in inv.groups.items():
        rows = []
        for c in g.components:
            st = state.get(c.id)
            if st and st.status >= Status.WARNING and not st.suppressed:
                rows.append(Line(int(st.status), inv.node_name(c.id), " · ".join(st.reasons[:2]), st.since,
                                 f"#/c/{c.id}"))
        gs = state.get(gid)
        if not rows and gs and gs.status >= Status.WARNING:
            rows.append(Line(int(gs.status), g.name, " · ".join(gs.reasons[:2]), gs.since,
                             f"#/{inv.group_tab[gid]}/{gid}"))
        attention += rows
    attention.sort(key=lambda x: (-x.status, x.since or 0))

    parked = sum(1 for gid in inv.groups if (st := state.get(gid)) and st.status == Status.OFFLINE)
    start = _same_time_yesterday(now, tz)

    # changes since yesterday: anything that went to or came back from Warning/Critical
    rows = sorted(events.events(start, now, None, int(Status.WARNING), False, ("component", "group"), 3000),
                  key=lambda r: r["ts"]) if events else []
    by_node: dict[str, list[dict]] = {}
    groups_with_comp = set()
    for r in rows:
        if r["kind"] == "component":
            groups_with_comp.add(inv.comp_group.get(r["node"]))
        by_node.setdefault(r["node"], []).append(r)
    items = []
    for node_id, evs in by_node.items():
        if node_id in inv.groups and node_id in groups_with_comp:
            continue                                  # its devices already tell the story
        if node_id not in inv.comps and node_id not in inv.groups:
            continue
        shown = evs[-MAX_STEPS:]
        chain = [LABEL[Status(shown[0]["old"] if shown[0]["old"] is not None else Status.UNKNOWN)]]
        if len(evs) > len(shown):
            chain[0] = f"… {len(evs) - len(shown)} earlier changes … {chain[0]}"
        for e in shown:
            why = f" ({e['reason']})" if e["reason"] and e["new"] >= Status.WARNING else ""
            chain.append(f"{LABEL[Status(e['new'])]} {_fmt_time(e['ts'], tz, now)}{why}")
        worst = max(max(e["new"], e["old"] or 0) for e in evs)
        items.append((-worst, evs[0]["ts"], f"{inv.node_name(node_id)}: " + " → ".join(chain)))
    items.sort()
    changes = [t for _w, _ts, t in items[:MAX_CHANGES]]
    info = []
    if events:
        for r in events.info_events(start, now, None, MAX_INFO):
            info.append(f"{inv.node_name(r['node'])}: {r['reason']} ({_fmt_time(r['ts'], tz, now)})")

    availability = []
    if events:
        av = events.availability([t for t, _ in TABS], start, now, now)
        availability = [(title, av.get(t, {}).get("pct")) for t, title in TABS]

    total = len(comps)
    crit, warn, unknown = counts["critical"], counts["warning"], counts["unknown"]
    notes = []
    if not collector_ok:
        notes.append("The collector is not running - the readings below may be stale.")
    if total and unknown == total:
        verdict, headline = "no_data", "no data from any device"
        notes.insert(0, "Every device is Unknown: no device data reaches the dashboard"
                     + (" - check that this server can reach the device networks (live mode pings them and "
                        "checks their ports)." if mode == "live" else "."))
    elif attention:
        verdict = "problems"
        parts = [f"{crit} critical" if crit else "", f"{warn} warning{'s' if warn != 1 else ''}" if warn else ""]
        headline = ", ".join(p for p in parts if p) or f"{len(attention)} need attention"
    elif unknown - hidden > 0:
        verdict, headline = "quiet", f"no alarms, {unknown - hidden} without data"
    else:
        verdict, headline = "all_clear", "all clear"
    if unknown and verdict != "no_data":
        notes.append(f"{unknown} device{'s' if unknown != 1 else ''} without data"
                     + (f" ({hidden} hidden behind a failed device)" if hidden else "") + ".")

    upd = ""
    if update:
        if update.get("offered"):
            upd = f"Network Guru {update['offered']} is available - an admin can approve it on the dashboard."
        elif (update.get("result") or {}).get("text") and not update["result"].get("dismissed"):
            upd = update["result"]["text"]
        elif update.get("check") and update["check"].get("ok") is False and update["check"].get("message"):
            upd = update["check"]["message"]
    return Report(site_name=inv.site.name, site_id=inv.site.id, date=dt.datetime.fromtimestamp(now, tz).strftime(
                  "%a %d %b %Y"), generated_at=now, tz=str(tz.key), mode=mode, counts=counts, total=total,
                  verdict=verdict, headline=headline, attention=attention, hidden_behind=hidden, changes=changes,
                  changes_more=max(0, len(items) - MAX_CHANGES), info=info, parked=parked,
                  availability=availability, notes=notes, update_line=upd, dashboard_url=dashboard_url,
                  late=late, test=test)


def report_from_runtime(rt, *, late: bool = False, test: bool = False, now: float | None = None) -> Report:
    now = now or time.time()
    sch = rt.scheduler
    collector_ok = bool(sch and sch.running) or (rt.mode == "sim" and not rt.progress.done)
    url = rt.settings_state.get("dashboard_url") or rt.settings.public_url
    return build_report(rt.inv, rt.pipeline.state, rt.events, rt.mode, now, rt.pipeline.tz, dashboard_url=url,
                        update=rt.updates.public_status() if rt.updates else None, collector_ok=collector_ok,
                        late=late, test=test)


# ---------------------------------------------------------------------------
# renderers
def _counts_text(r: Report) -> str:
    order = ["critical", "warning", "ok", "offline", "unknown"]
    return " · ".join(f"{word(Status[k.upper()])} {r.counts[k]}" for k in order if r.counts.get(k))


def _pct(p: float | None) -> str:
    return "–" if p is None else f"{p:.1f} %"


def as_text(r: Report) -> str:
    out = [r.subject, "", f"{r.total} devices: {_counts_text(r)}"]
    if r.mode == "sim":
        out.append("SIMULATED DATA - this dashboard is not connected to real devices yet.")
    out += r.notes
    out += ["", "NEEDS ATTENTION NOW"]
    if r.attention:
        for x in r.attention:
            since = f" - since {_fmt_time(x.since, ZoneInfo(r.tz), r.generated_at)}" if x.since else ""
            out.append(f"  {word(x.status)}  {x.name}: {x.reason}{since}")
        if r.hidden_behind:
            out.append(f"  (+{r.hidden_behind} unreachable behind them)")
    else:
        out.append("  Nothing needs attention." if r.verdict != "no_data" else "  Nothing to judge - no device data.")
    out += ["", "CHANGES SINCE YESTERDAY"]
    out += [f"  {c}" for c in r.changes] or ["  No warnings or alarms since yesterday."]
    if r.changes_more:
        out.append(f"  …and {r.changes_more} more on the dashboard.")
    if r.info:
        out += ["", "ADDRESS / FIRMWARE CHANGES"] + [f"  {i}" for i in r.info]
    out += ["", "LAST 24 HOURS"]
    out += [f"  {t}: availability {_pct(p)}" for t, p in r.availability]
    if r.parked:
        out.append(f"  {r.parked} vehicle(s) / station(s) parked or not in service (not an alarm)")
    if r.update_line:
        out += ["", f"Updates: {r.update_line}"]
    if r.dashboard_url:
        out += ["", f"Dashboard: {r.dashboard_url}"]
    out += ["", "This report is sent every morning, even when all is well. No report means the Network Guru "
            "server needs a look."]
    return "\n".join(out) + "\n"


def as_html(r: Report) -> str:
    e = html.escape
    tz = ZoneInfo(r.tz)

    def badge(s: int, width: bool = True) -> str:
        st = Status(s)
        w = "display:inline-block;min-width:78px;" if width else ""
        return f'<span style="{w}font-weight:bold;color:{COLOR[st]}">{SYMBOL[st]} {LABEL[st]}</span>'

    parts = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0b0b0b;max-width:760px">',
             f'<h2 style="margin:0 0 4px;font-size:18px">{e(r.subject)}</h2>',
             f'<div style="color:#4a4f54;margin-bottom:10px">{r.total} devices: '
             + " &nbsp;·&nbsp; ".join(f'{badge(int(Status[k.upper()]), False)} {r.counts[k]}'
                                     for k in ["critical", "warning", "ok", "offline", "unknown"] if r.counts.get(k))
             + "</div>"]
    if r.mode == "sim":
        parts.append('<div style="background:#fff4dc;padding:6px 10px;margin-bottom:8px"><b>Simulated data</b> - '
                     'this dashboard is not connected to real devices yet.</div>')
    for n in r.notes:
        parts.append(f'<div style="background:#f2f4f6;padding:6px 10px;margin-bottom:6px">{e(n)}</div>')
    parts.append('<h3 style="font-size:15px;margin:16px 0 6px">Needs attention now</h3>')
    if r.attention:
        parts.append('<table style="border-collapse:collapse">')
        for x in r.attention:
            since = f"since {_fmt_time(x.since, tz, r.generated_at)}" if x.since else ""
            link = (f'<a href="{e(r.dashboard_url.rstrip("/") + "/" + x.href)}">{e(x.name)}</a>' if r.dashboard_url
                    else e(x.name))
            parts.append(f'<tr><td style="padding:3px 10px 3px 0;vertical-align:top">{badge(x.status)}</td>'
                         f'<td style="padding:3px 10px 3px 0"><b>{link}</b><br><span style="color:#4a4f54">'
                         f'{e(x.reason)}</span></td><td style="padding:3px 0;color:#7b8086;white-space:nowrap">'
                         f'{e(since)}</td></tr>')
        parts.append("</table>")
        if r.hidden_behind:
            parts.append(f'<div style="color:#7b8086">+{r.hidden_behind} unreachable behind them</div>')
    else:
        parts.append(f'<div>{badge(0) if r.verdict == "all_clear" else ""} '
                     f'{"Nothing needs attention." if r.verdict != "no_data" else "Nothing to judge - no device data."}'
                     "</div>")
    parts.append('<h3 style="font-size:15px;margin:16px 0 6px">Changes since yesterday</h3>')
    if r.changes:
        parts.append("<ul style=\"margin:0;padding-left:18px\">" + "".join(f"<li>{e(c)}</li>" for c in r.changes)
                     + "</ul>")
        if r.changes_more:
            parts.append(f'<div style="color:#7b8086">…and {r.changes_more} more on the dashboard.</div>')
    else:
        parts.append("<div>No warnings or alarms since yesterday.</div>")
    if r.info:
        parts.append('<h3 style="font-size:15px;margin:16px 0 6px">Address / firmware changes</h3>'
                     "<ul style=\"margin:0;padding-left:18px\">" + "".join(f"<li>{e(i)}</li>" for i in r.info) + "</ul>")
    parts.append('<h3 style="font-size:15px;margin:16px 0 6px">Last 24 hours</h3><div>'
                 + " &nbsp;·&nbsp; ".join(f"{e(t)}: availability <b>{_pct(p)}</b>" for t, p in r.availability)
                 + (f"<br>{r.parked} vehicle(s) / station(s) parked or not in service (not an alarm)" if r.parked else "")
                 + "</div>")
    if r.update_line:
        parts.append(f'<p style="margin:14px 0 0"><b>Updates:</b> {e(r.update_line)}</p>')
    if r.dashboard_url:
        parts.append(f'<p style="margin:14px 0 0"><a href="{e(r.dashboard_url)}">Open the dashboard</a></p>')
    parts.append('<p style="color:#7b8086;font-size:12px;margin-top:18px">This report is sent every morning, even '
                 "when all is well. No report means the Network Guru server needs a look.</p></div>")
    return "\n".join(parts)


def as_teams_card(r: Report) -> dict:
    tz = ZoneInfo(r.tz)
    worst = max([x.status for x in r.attention], default=0 if r.verdict == "all_clear" else 1)
    body: list[dict] = [
        {"type": "TextBlock", "text": r.subject, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": f"{r.total} devices: {_counts_text(r)}", "wrap": True, "spacing": "Small",
         "color": CARD_COLOR[Status(worst)]},
    ]
    if r.mode == "sim":
        body.append({"type": "TextBlock", "text": "**Simulated data** - not connected to real devices yet.",
                     "wrap": True})
    for n in r.notes:
        body.append({"type": "TextBlock", "text": n, "wrap": True, "isSubtle": True})
    body.append({"type": "TextBlock", "text": "Needs attention now", "weight": "Bolder", "spacing": "Medium"})
    if r.attention:
        for x in r.attention[:25]:
            since = f" - since {_fmt_time(x.since, tz, r.generated_at)}" if x.since else ""
            body.append({"type": "TextBlock", "wrap": True, "spacing": "Small", "color": CARD_COLOR[Status(x.status)],
                         "text": f"{word(x.status)} **{x.name}**: {x.reason}{since}"})
        if len(r.attention) > 25 or r.hidden_behind:
            more = [f"+{len(r.attention) - 25} more" if len(r.attention) > 25 else "",
                    f"+{r.hidden_behind} unreachable behind them" if r.hidden_behind else ""]
            body.append({"type": "TextBlock", "text": ", ".join(m for m in more if m), "isSubtle": True, "wrap": True})
    else:
        body.append({"type": "TextBlock", "text": "Nothing needs attention." if r.verdict != "no_data"
                     else "Nothing to judge - no device data.", "wrap": True,
                     "color": "Good" if r.verdict == "all_clear" else "Default"})
    body.append({"type": "TextBlock", "text": "Changes since yesterday", "weight": "Bolder", "spacing": "Medium"})
    for c in (r.changes or ["No warnings or alarms since yesterday."]):
        body.append({"type": "TextBlock", "text": c, "wrap": True, "spacing": "Small"})
    if r.changes_more:
        body.append({"type": "TextBlock", "text": f"…and {r.changes_more} more on the dashboard.", "isSubtle": True})
    if r.info:
        body.append({"type": "TextBlock", "text": "Address / firmware changes", "weight": "Bolder", "spacing": "Medium"})
        body += [{"type": "TextBlock", "text": i, "wrap": True, "spacing": "Small"} for i in r.info]
    facts = [{"title": t, "value": f"availability {_pct(p)}"} for t, p in r.availability]
    if r.parked:
        facts.append({"title": "Parked / not in service", "value": str(r.parked)})
    if facts:
        body.append({"type": "FactSet", "facts": facts, "spacing": "Medium"})
    if r.update_line:
        body.append({"type": "TextBlock", "text": f"Updates: {r.update_line}", "wrap": True, "isSubtle": True})
    content: dict[str, Any] = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                               "version": "1.4", "body": body, "msteams": {"width": "Full"}}
    if r.dashboard_url:
        content["actions"] = [{"type": "Action.OpenUrl", "title": "Open the dashboard", "url": r.dashboard_url}]
    card = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": content}]}
    while len(json.dumps(card)) > CARD_LIMIT and len(body) > 6:     # keep the head, drop the tail
        body.pop(-2)
    return card
