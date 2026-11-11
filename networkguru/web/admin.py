"""Admin endpoints: sign-in, update approval / upload, report settings and test sends, the
problem report (Report a problem) and the feedback list (0.9.5); the kept problem reports and the log
download (0.10.0).

Every change needs a signed-in admin (session cookie: HttpOnly, SameSite=Strict) AND the header
X-Requested-With: networkguru, which a page on another site cannot send - with a same-origin
check on top. The dashboard pages themselves stay open to everyone.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..auth import COOKIE, HASH_ENV, MAX_AGE_S, Session
from .. import feedback as fb, logbuffer, problem_report
from ..config import mask, secret
from ..github import UpdateError
from ..notify import GRAPH_ENV, SMTP_PASS_ENV, SMTP_USER_ENV, TEAMS_ENV
from ..site_settings import validate
from ..stalls import METER
from ..updates import TOKEN_ENV

router = APIRouter(prefix="/api/admin")
SECRET_NAMES = [(TOKEN_ENV, "GitHub token (optional)"), (HASH_ENV, "Admin password"),
                (TEAMS_ENV, "Teams webhook URL"), (GRAPH_ENV, "Microsoft Graph client secret"),
                (SMTP_USER_ENV, "SMTP user"), (SMTP_PASS_ENV, "SMTP password")]


def rt(request: Request):
    return request.app.state.rt


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def same_origin(request: Request) -> None:
    if request.headers.get("x-requested-with") != "networkguru":
        raise HTTPException(403, "missing X-Requested-With header")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "cross-site request refused")


def require_admin(request: Request) -> Session:
    s = rt(request).auth.session(request.cookies.get(COOKIE))
    if s is None:
        raise HTTPException(401, "Sign in as admin first")
    if request.method != "GET":
        same_origin(request)
    return s


def _event(request: Request, text: str, level: int = 0, actor: str | None = None) -> None:
    r = rt(request)
    if r.events is not None:
        r.events.add_system_event(time.time(), "admin", text, level, actor)


class LoginIn(BaseModel):
    password: str
    name: str = ""


@router.get("/session")
async def session(request: Request):
    r = rt(request)
    s = r.auth.session(request.cookies.get(COOKIE))
    return {"configured": r.auth.configured, "signed_in": s is not None, "name": s.name if s else None,
            "dev_password": r.settings.deploy in ("dev", "compose") and bool(r.settings.dev_admin_password),
            "deploy": r.settings.deploy}


@router.post("/login")
async def login(request: Request, body: LoginIn, response: Response):
    same_origin(request)
    r = rt(request)
    if not r.auth.configured:
        raise HTTPException(503, "No admin password is set on this server - run install.sh --secrets on the server.")
    ip = client_ip(request)
    wait = r.auth.wait_for(ip)
    if wait > 0:
        raise HTTPException(429, f"Too many wrong passwords - try again in {int(wait) + 1} s.")
    token = await r.auth.login(ip, body.password, body.name)
    if token is None:
        _event(request, f"Admin sign-in failed from {ip}", 3, body.name.strip()[:40] or None)
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(COOKIE, token, max_age=MAX_AGE_S, httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https")
    name = r.auth.sessions[token].name
    _event(request, f"Admin signed in from {ip}", 0, name)
    return {"signed_in": True, "name": name}


@router.post("/logout")
async def logout(request: Request, response: Response):
    same_origin(request)
    rt(request).auth.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"signed_in": False}


def secret_list(r) -> list[dict]:
    """Which secrets are set - names only, never values."""
    out = [{"name": n, "label": label, "set": bool(secret(n)) or (n == HASH_ENV and r.auth.configured)}
           for n, label in SECRET_NAMES]
    known = {n for n, _l in SECRET_NAMES}
    refs = {ref for c in r.inv.comps.values() for ref in c.secret_refs().values()}
    if r.inv.data.netmaker.url and r.inv.data.netmaker.token_ref:
        refs.add(r.inv.data.netmaker.token_ref)
    out += [{"name": n, "label": "Device login (named in the inventory)", "set": bool(secret(n))}
            for n in sorted(refs - known)]
    return out


@router.get("/status")
async def status(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    return {"name": who.name, "updates": r.updates.public_status(), "report": r.reports.public_status(),
            "settings": r.settings_state.data, "secrets": secret_list(r), "speed": METER.summary()}


# -- Server speed (stalls.py) ------------------------------------------------------
class SlowIn(BaseModel):
    minutes: float = 5


@router.post("/slow-steps")
async def slow_steps(request: Request, body: SlowIn, who: Session = Depends(require_admin)):
    """Log every step that blocks the server over 0.25 s, for a few minutes - the next problem report has them."""
    if not 1 <= body.minutes <= 30:
        raise HTTPException(400, "minutes must be 1-30")
    METER.slow_steps(body.minutes)
    _event(request, f"Finding slow steps for {body.minutes:.0f} minutes", 0, who.name)
    return METER.summary()


# -- Report a problem ------------------------------------------------------------
class ProblemIn(BaseModel):
    note: str = ""
    browser: dict = {}


@router.post("/problem-report")
async def problem_report_zip(request: Request, body: ProblemIn, who: Session = Depends(require_admin)):
    r = rt(request)
    name, data = problem_report.build(r, who.name, body.note, body.browser, secret_list(r), logbuffer.install().text())
    _event(request, "Problem report downloaded", 0, who.name)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/problem-reports")
async def problem_reports(request: Request, who: Session = Depends(require_admin)):
    """The reports Report a problem kept (0.10.0, problem_ticket.ReportStore), newest first."""
    return {"reports": rt(request).problem_reports.list()}


@router.get("/problem-reports/{rid}")
async def problem_report_file(request: Request, rid: str, who: Session = Depends(require_admin)):
    r = rt(request)
    p = r.problem_reports.path(rid)
    if p is None:
        raise HTTPException(404, "No such report (they are kept for 30 days)")
    _event(request, f"Problem report {rid} downloaded", 0, who.name)
    return FileResponse(p, media_type="application/zip", filename=f"networkguru-report-{rid}.zip")


@router.get("/log")
async def download_log(request: Request, who: Session = Depends(require_admin)):
    """The server's log (0.10.0): the newest 4 MB of data/logs/networkguru.log and the file before it,
    secrets masked; the in-memory lines when the file can't be written."""
    import datetime as dt
    from pathlib import Path
    from zoneinfo import ZoneInfo
    r = rt(request)
    text = logbuffer.file_text(Path(r.settings.data_dir) / "logs") or logbuffer.install().text()
    stamp = dt.datetime.now(ZoneInfo(r.inv.site.timezone))
    _event(request, "Log downloaded", 0, who.name)
    return Response(content=(mask(text) or "(the log is empty)").encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="networkguru-log-{r.inv.site.id}-{stamp:%Y%m%d-%H%M}.txt"'})


# -- updates -------------------------------------------------------------------
@router.post("/update/check")
async def update_check(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    await r.updates.check(by=who.name)
    return r.updates.public_status()


@router.post("/update/approve")
async def update_approve(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    try:
        r.updates.approve(who.name)
    except UpdateError as e:
        raise HTTPException(409, str(e)) from None
    return r.updates.public_status()


@router.post("/update/remind")
async def update_remind(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    r.updates.remind_tomorrow(who.name)
    return r.updates.public_status()


@router.post("/update/skip")
async def update_skip(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    r.updates.skip(who.name)
    return r.updates.public_status()


@router.post("/update/dismiss")
async def update_dismiss(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    r.updates.dismiss_result()
    return r.updates.public_status()


@router.post("/update/upload")
async def update_upload(request: Request, name: str = Query(...), who: Session = Depends(require_admin)):
    """Raw zip in the body; the .sha256 file's text in the X-Update-Sha256 header."""
    r = rt(request)
    sha_text = request.headers.get("x-update-sha256", "")
    try:
        version = await r.updates.upload(name, sha_text, request.stream(), who.name)
    except UpdateError as e:
        raise HTTPException(400, str(e)) from None
    return {"version": version, **r.updates.public_status()}


# -- settings and the report ---------------------------------------------------------
@router.put("/settings")
async def put_settings(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("send a JSON object")
        clean = validate(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    r.settings_state.update(**clean)
    _event(request, "Settings changed: " + ", ".join(sorted(clean)), 0, who.name)
    return r.settings_state.data


# -- the discovery scan (found.py) ---------------------------------------------------------------
def _discovery(request: Request):
    d = rt(request).discovery
    if d is None:
        raise HTTPException(409, "the network scan runs in live mode only")
    return d


@router.get("/discovery")
async def discovery_status(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    return r.discovery.public() if r.discovery else {"enabled": False, "why": "The network scan runs in live mode only."}


@router.post("/discovery/scan")
async def discovery_scan(request: Request, who: Session = Depends(require_admin)):
    d = _discovery(request)
    if not d.start(who.name):
        raise HTTPException(409, "a scan is already running")
    _event(request, "Network scan started", 0, who.name)
    return {"started": True}


class AddIn(BaseModel):
    key: str
    name: str
    type: str
    group_id: str | None = None
    group_name: str = ""
    kind: str = ""


@router.post("/discovery/add")
async def discovery_add(request: Request, body: AddIn, who: Session = Depends(require_admin)):
    d = _discovery(request)
    try:
        cid = d.add(body.key, name=body.name, ctype=body.type, group_id=body.group_id or None,
                    group_name=body.group_name, kind=body.kind, who=who.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    r = rt(request)
    _event(request, f"Added from the network scan: {r.inv.node_name(cid)} ({r.inv.comps[cid].host})", 0, who.name)
    return {"id": cid, "group": r.inv.comp_group[cid], "tab": r.inv.group_tab[r.inv.comp_group[cid]]}


class IgnoreIn(BaseModel):
    key: str
    ignore: bool = True


@router.post("/discovery/ignore")
async def discovery_ignore(request: Request, body: IgnoreIn, who: Session = Depends(require_admin)):
    _discovery(request).ignore(body.key, body.ignore)
    return {"ok": True}


@router.get("/discovery/export.csv")
async def discovery_export(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    return Response(_discovery(request).export_csv(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="networkguru-found-{r.inv.site.id}.csv"'})


@router.post("/report/test")
async def report_test(request: Request, who: Session = Depends(require_admin)):
    return await rt(request).reports.send_test(who.name)


# -- feedback (feedback.py; anyone sends it - POST /api/feedback) --------------------------------------
@router.get("/feedback")
async def feedback_list(request: Request, show: str = "open", who: Session = Depends(require_admin)):
    """show: open | closed | all."""
    r = rt(request)
    if r.events is None:
        return {"items": [], "counts": None, "kinds": fb.KINDS, "statuses": fb.STATUSES}
    statuses = {"open": fb.OPEN, "closed": fb.CLOSED}.get(show)
    return {"items": r.events.feedback(statuses), "counts": r.events.feedback_counts(time.time() - 86400, fb.OPEN),
            "kinds": fb.KINDS, "statuses": fb.STATUSES}


class FeedbackPatch(BaseModel):
    status: str | None = None
    note: str | None = None


@router.put("/feedback/{fid}")
async def feedback_update(request: Request, fid: int, body: FeedbackPatch, who: Session = Depends(require_admin)):
    r = rt(request)
    if r.events is None:
        raise HTTPException(503, "no event database on this server")
    if body.status is not None and body.status not in fb.STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(fb.STATUSES)}")
    try:
        note = None if body.note is None else fb.clean_note(body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    old = r.events.get_feedback(fid)
    if old is None:
        raise HTTPException(404, "no such feedback")
    new = r.events.update_feedback(fid, time.time(), who.name, body.status, note)
    if new["status"] != old["status"]:
        _event(request, f"Feedback #{fid}: {fb.STATUSES[old['status']]} → {fb.STATUSES[new['status']]}", 0, who.name)
    elif new["note"] != old["note"]:
        _event(request, f"Feedback #{fid}: note changed", 0, who.name)
    return new
