"""Sending the morning report: Teams (Workflows webhook) and e-mail (Microsoft Graph or SMTP).

Secrets come only from secrets.env (typed during install.sh):
  NETWORKGURU_TEAMS_WEBHOOK_URL     the Teams Workflows "When a Teams webhook request is received" URL
  NETWORKGURU_GRAPH_CLIENT_SECRET   Entra app with Mail.Send (application), limited to the sender mailbox
  NETWORKGURU_SMTP_USER / _PASSWORD SMTP sign-in (not used when security is "none" - a relay)
Everything else (recipients, hosts, times) is on the admin page.

Each channel is tried up to 3 times, 5 minutes apart. What happened is saved per channel and
site-local date right after each try, so a restart never sends twice and never forgets a retry.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

import httpx

from .config import mask, secret
from .daily import DailyJob
from .report import Report, as_html, as_teams_card, as_text
from .state import StateFile

log = logging.getLogger("networkguru")

TEAMS_ENV = "NETWORKGURU_TEAMS_WEBHOOK_URL"
GRAPH_ENV = "NETWORKGURU_GRAPH_CLIENT_SECRET"
SMTP_USER_ENV, SMTP_PASS_ENV = "NETWORKGURU_SMTP_USER", "NETWORKGURU_SMTP_PASSWORD"
TRIES = 3
RETRY_S = 300


class SendError(Exception):
    pass


# ---------------------------------------------------------------------------
# senders
async def send_teams(url: str, card: dict, client: httpx.AsyncClient | None = None) -> str:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        r = await client.post(url, json=card)
    except httpx.HTTPError as e:
        raise SendError(f"Teams webhook not reachable ({type(e).__name__})") from None
    finally:
        if own:
            await client.aclose()
    if r.status_code // 100 != 2:
        raise SendError(f"Teams webhook answered {r.status_code}: {mask(r.text[:200])}")
    # Workflows answers 202: the request is accepted, the flow posts the card a moment later.
    return "accepted" if r.status_code == 202 else "posted"


def build_email(rep: Report, sender: str, to: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = rep.subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain=(sender.split("@")[-1] or "networkguru.local"))
    msg.set_content(as_text(rep))
    msg.add_alternative(as_html(rep), subtype="html")
    return msg


def send_smtp(host: str, port: int, security: str, user: str | None, password: str | None,
              msg: EmailMessage, timeout: float = 30.0) -> str:
    """Blocking; run in a thread. Certificates are verified; no sign-in over an unencrypted line."""
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            s = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=timeout)
        with s:
            s.ehlo()
            if security == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
            if security != "none" and user:
                s.login(user, password or "")
            s.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise SendError(f"SMTP {host}:{port}: {mask(str(e))[:200]}") from None
    return "sent"


async def send_graph(tenant: str, client_id: str, client_secret: str, sender: str, rep: Report, to: list[str],
                     client: httpx.AsyncClient | None = None) -> str:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        tok = await client.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data={
            "client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default"})
        if tok.status_code != 200:
            raise SendError(f"Microsoft sign-in failed ({tok.status_code}): {mask(tok.text[:200])}")
        access = tok.json().get("access_token")
        mail = {"message": {"subject": rep.subject, "body": {"contentType": "HTML", "content": as_html(rep)},
                            "toRecipients": [{"emailAddress": {"address": a}} for a in to]},
                "saveToSentItems": False}
        r = await client.post(f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail", json=mail,
                              headers={"Authorization": f"Bearer {access}"})
        if r.status_code != 202:
            raise SendError(f"Graph sendMail answered {r.status_code}: {mask(r.text[:200])}")
        return "sent"
    except httpx.HTTPError as e:
        raise SendError(f"Microsoft Graph not reachable ({type(e).__name__})") from None
    finally:
        if own:
            await client.aclose()


# ---------------------------------------------------------------------------
class ReportService:
    """Builds and sends the daily report; remembers per date and channel what was sent."""

    def __init__(self, data_dir: Path, settings: StateFile, build: Callable[..., Report], tz, events=None,
                 ready: Callable[[], bool] = lambda: True, clock=time.time,
                 client_factory: Callable[[], httpx.AsyncClient] | None = None,
                 smtp: Callable[..., str] = send_smtp):
        self.settings, self.build, self.tz, self.events, self.ready, self.clock = settings, build, tz, events, ready, clock
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30.0))
        self.smtp = smtp
        self.state = StateFile(Path(data_dir) / "state" / "report.json", {"date": None, "done": False, "channels": {},
                                                                        "last_test": None})

    # -- which channels are set up ------------------------------------------------
    def channels(self) -> dict[str, str | None]:
        """{channel: None when ready, or why it can't send}. Only enabled channels are listed."""
        s = self.settings
        out: dict[str, str | None] = {}
        if s.get("report_teams"):
            out["teams"] = None if secret(TEAMS_ENV) else "no Teams webhook URL in secrets.env"
        if s.get("report_email"):
            why = None
            if not s.get("email_to"):
                why = "no recipients set on the admin page"
            elif s.get("email_via") == "graph":
                if not (s.get("graph_tenant") and s.get("graph_client_id") and s.get("graph_sender")):
                    why = "Graph tenant, app ID or sender not set on the admin page"
                elif not secret(GRAPH_ENV):
                    why = "no Graph client secret in secrets.env"
            else:
                if not (s.get("smtp_host") and s.get("email_from")):
                    why = "SMTP host or sender not set on the admin page"
                elif s.get("smtp_security") != "none" and not (secret(SMTP_USER_ENV) and secret(SMTP_PASS_ENV)):
                    why = "no SMTP user / password in secrets.env"
            out["email"] = why
        return out

    async def send_one(self, channel: str, rep: Report) -> str:
        s = self.settings
        if channel == "teams":
            async with self.client_factory() as c:
                return await send_teams(secret(TEAMS_ENV) or "", as_teams_card(rep), c)
        to = list(s.get("email_to") or [])
        if s.get("email_via") == "graph":
            async with self.client_factory() as c:
                return await send_graph(s.get("graph_tenant"), s.get("graph_client_id"), secret(GRAPH_ENV) or "",
                                        s.get("graph_sender"), rep, to, c)
        msg = build_email(rep, s.get("email_from"), to)
        return await asyncio.to_thread(self.smtp, s.get("smtp_host"), int(s.get("smtp_port") or 587),
                                       s.get("smtp_security") or "starttls", secret(SMTP_USER_ENV),
                                       secret(SMTP_PASS_ENV), msg)

    def _event(self, text: str, level: int = 0, actor: str | None = None) -> None:
        if self.events is not None:
            self.events.add_system_event(self.clock(), "report", mask(text), level, actor)

    # -- the daily run ------------------------------------------------------------
    def is_done(self, date: str) -> bool:
        return self.state.get("date") == date and bool(self.state.get("done"))

    async def run(self, date: str, late: bool) -> None:
        if self.state.get("date") != date:
            self.state.replace({"date": date, "done": False, "channels": {}, "late": late,
                                "last_test": self.state.get("last_test")})
        chans = self.channels()
        ready = {c: why for c, why in chans.items() if why is None}
        if not ready:
            why = "; ".join(f"{c}: {w}" for c, w in chans.items()) or "Teams and e-mail are both switched off"
            self._event(f"Morning report built but NOT sent - no channel is set up ({why})", 3)
            self.state.update(done=True, note=why)
            return
        rep = self.build(late=late)
        states = dict(self.state.get("channels") or {})
        now = self.clock()
        for ch in ready:
            c = dict(states.get(ch) or {"ok": False, "tries": 0, "next_try": 0})
            if c["ok"] or c["tries"] >= TRIES or now < c.get("next_try", 0):
                continue
            try:
                result = await self.send_one(ch, rep)
                c.update(ok=True, at=self.clock(), message=result)
            except SendError as e:
                c.update(tries=c["tries"] + 1, at=self.clock(), next_try=self.clock() + RETRY_S, message=mask(str(e)))
            except Exception as e:  # noqa: BLE001
                log.exception("morning report via %s failed", ch)
                c.update(tries=c["tries"] + 1, at=self.clock(), next_try=self.clock() + RETRY_S,
                         message=mask(f"unexpected error ({type(e).__name__})"))
            states[ch] = c
            self.state.update(channels=states)               # saved at once: no double send after a restart
        finished = all(states.get(ch, {}).get("ok") or states.get(ch, {}).get("tries", 0) >= TRIES for ch in ready)
        if finished:
            self.state.update(done=True)
            parts = [f"{ch} ✓ {st.get('message')}" if st.get("ok") else f"{ch} ✗ {st.get('message')}"
                     for ch, st in states.items()]
            parts += [f"{ch}: {why}" for ch, why in chans.items() if why]
            failed = [ch for ch, st in states.items() if not st.get("ok")] or [ch for ch, why in chans.items() if why]
            self._event(("Morning report sent" if not failed else "Morning report NOT fully sent")
                        + (" (late)" if late else "") + ": " + "; ".join(parts), 3 if failed else 0)

    def daily_job(self) -> DailyJob:
        return DailyJob("morning-report", self.tz, lambda: self.settings.get("report_time", "06:30"),
                        self.is_done, self.run, ready=self.ready, catchup_s=6 * 3600, clock=self.clock)

    async def send_test(self, by: str) -> dict[str, str]:
        rep = self.build(test=True)
        out: dict[str, str] = {}
        for ch, why in self.channels().items():
            if why:
                out[ch] = f"not set up: {why}"
                continue
            try:
                out[ch] = "✓ " + await self.send_one(ch, rep)
            except SendError as e:
                out[ch] = "✗ " + mask(str(e))
        if not out:
            out["report"] = "Teams and e-mail are both switched off on this page"
        self.state.update(last_test={"at": self.clock(), "by": by, "results": out})
        self._event("Test report: " + "; ".join(f"{k} {v}" for k, v in out.items()),
                    0 if all(v.startswith("✓") for v in out.values()) else 3, by)
        return out

    def public_status(self) -> dict:
        return {"date": self.state.get("date"), "done": self.state.get("done"), "late": self.state.get("late"),
                "channels": self.state.get("channels") or {}, "setup": self.channels(),
                "note": self.state.get("note"), "last_test": self.state.get("last_test"),
                "secrets": {"teams_webhook": bool(secret(TEAMS_ENV)), "graph_secret": bool(secret(GRAPH_ENV)),
                            "smtp_user": bool(secret(SMTP_USER_ENV)), "smtp_password": bool(secret(SMTP_PASS_ENV))}}
