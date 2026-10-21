"""Admin endpoints: sign-in, update approval / upload, report settings and test sends, and the
problem report (Report a problem).

Every change needs a signed-in admin (session cookie: HttpOnly, SameSite=Strict) AND the header
X-Requested-With: networkguru, which a page on another site cannot send - with a same-origin
check on top. The dashboard pages themselves stay open to everyone.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ..auth import COOKIE, HASH_ENV, MAX_AGE_S, Session
from .. import logbuffer, problem_report
from ..config import secret
from ..github import UpdateError
from ..notify import GRAPH_ENV, SMTP_PASS_ENV, SMTP_USER_ENV, TEAMS_ENV
from ..site_settings import validate
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
    return [{"name": n, "label": label, "set": bool(secret(n)) or (n == HASH_ENV and r.auth.configured)}
            for n, label in SECRET_NAMES]


@router.get("/status")
async def status(request: Request, who: Session = Depends(require_admin)):
    r = rt(request)
    return {"name": who.name, "updates": r.updates.public_status(), "report": r.reports.public_status(),
            "settings": r.settings_state.data, "secrets": secret_list(r)}


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


@router.post("/report/test")
async def report_test(request: Request, who: Session = Depends(require_admin)):
    return await rt(request).reports.send_test(who.name)
