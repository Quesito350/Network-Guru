"""Morning report: content and order, always sent, Teams / SMTP / Graph with fakes, retries,
no double send after a restart, the daily timing (DST, catch-up) and the preview page."""

import asyncio
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from networkguru.config import Settings
from networkguru.daily import DailyJob, due_ts, local_date, next_due
from networkguru.events import EventStore
from networkguru.model import Status
from networkguru.notify import ReportService, send_smtp
from networkguru.pipeline import Pipeline
from networkguru.report import as_html, as_teams_card, as_text, build_report
from networkguru.rules import RuleSet, load_rules
from networkguru.site_settings import open_settings
from networkguru.tsdb import MemoryTSDB
from networkguru.web.app import create_app

from .conftest import EXAMPLE

TZ = ZoneInfo("America/Denver")
NOW = dt.datetime(2026, 10, 15, 6, 30, tzinfo=TZ).timestamp()
WEBHOOK = "https://prod-11.westus." + "logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=" + "Q" * 43


def world(mini_inv, states=None, changes=()):
    ev = EventStore()
    p = Pipeline(mini_inv, RuleSet(load_rules()), MemoryTSDB(), ev)
    for nid, st in p.state.items():
        st.status, st.reasons = Status.OK, ["OK"]
    for nid, (s, reason, extra) in (states or {}).items():
        st = p.state[nid]
        st.status, st.reasons, st.since = s, [reason], NOW - 3600
        for k, v in extra.items():
            setattr(st, k, v)
    ev.add_changes(list(changes))
    return p, ev


def report(mini_inv, states=None, changes=(), mode="live", **kw):
    p, ev = world(mini_inv, states, changes)
    return build_report(mini_inv, p.state, ev, mode, NOW, TZ, dashboard_url="http://10.0.0.5:8080/", **kw)


def test_red_before_yellow_and_hidden_counted(mini_inv):
    r = report(mini_inv, {
        "t1-60g": (Status.WARNING, "RSSI -70 dBm", {}),
        "t1-bc": (Status.CRITICAL, "Offline - not responding", {}),
        "tr1-vcu": (Status.UNKNOWN, "Unreachable via Truck 1 · Router", {"suppressed": True}),
        "t1": (Status.CRITICAL, "Trailer down", {}),
    })
    assert [x.status for x in r.attention] == [4, 3]
    assert r.attention[0].name.endswith("Rajant") or "t1-bc" in r.attention[0].href
    assert r.hidden_behind == 1 and r.verdict == "problems" and r.headline == "1 critical, 1 warning"
    text = as_text(r)
    assert text.index("✖ Critical") < text.index("▲ Warning") and "+1 unreachable behind them" in text
    assert "[SIMULATED]" not in r.subject


def test_all_green_is_still_a_report(mini_inv):
    r = report(mini_inv, mode="sim")
    assert r.verdict == "all_clear" and "all clear" in r.subject and r.subject.startswith("[SIMULATED]")
    assert "Nothing needs attention." in as_text(r) and "No warnings or alarms since yesterday." in as_text(r)


def test_everything_unknown_says_so(mini_inv):
    p, ev = world(mini_inv)
    for st in p.state.values():
        st.status = Status.UNKNOWN
    r = build_report(mini_inv, p.state, ev, "live", NOW, TZ)
    assert r.verdict == "no_data" and "no data from any device" in r.subject
    assert "can reach the device networks" in as_text(r) and "All clear" not in as_text(r)


def test_changes_since_yesterday(mini_inv):
    changes = [(NOW - 7200, "t1-60g", "component", 0, 3, "60 GHz link down", 0),
               (NOW - 6000, "t1-60g", "component", 3, 0, "", 0),
               (NOW - 5000, "t1", "group", 0, 3, "", 0),                   # its device already tells it
               (NOW - 4000, "tr1-vcu", "component", 0, 4, "hidden", 1),     # suppressed: left out
               (NOW - 2 * 86400, "t1-bc", "component", 0, 4, "too old", 0)]
    r = report(mini_inv, changes=changes)
    assert len(r.changes) == 1
    line = r.changes[0]
    assert line.startswith("Trailer 1 · ") and "OK → Warning 04:30 (60 GHz link down) → OK 04:50" in line


def test_html_and_teams_card(mini_inv):
    r = report(mini_inv, {"t1-bc": (Status.CRITICAL, "Offline", {})})
    html = as_html(r)
    assert "✖ Critical" in html and "href=\"http://10.0.0.5:8080/#/c/t1-bc\"" in html
    card = as_teams_card(r)
    assert card["type"] == "message"
    att = card["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive" and att["contentUrl"] is None
    content = att["content"]
    assert content["type"] == "AdaptiveCard" and content["version"] == "1.4"
    assert content["actions"][0] == {"type": "Action.OpenUrl", "title": "Open the dashboard", "url": "http://10.0.0.5:8080/"}
    assert any("Critical" in b.get("text", "") and b.get("color") == "Attention" for b in content["body"])
    assert len(json.dumps(card)) < 28 * 1024


# ---------------------------------------------------------------------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def service(tmp_path, mini_inv, handler=None, smtp=None, clock=None, **settings):
    s = open_settings(tmp_path)
    s.update(**settings)
    p, ev = world(mini_inv)
    build = lambda **kw: build_report(mini_inv, p.state, ev, "sim", NOW, TZ, **kw)  # noqa: E731
    factory = (lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))) if handler else None
    return ReportService(tmp_path, s, build, TZ, ev, clock=clock or Clock(NOW), client_factory=factory,
                         smtp=smtp or send_smtp), ev


def test_teams_payload_and_always_sent(tmp_path, mini_inv, monkeypatch):
    monkeypatch.setenv("NETWORKGURU_TEAMS_WEBHOOK_URL", WEBHOOK)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(202)
    svc, ev = service(tmp_path, mini_inv, handler, report_email=False)
    date = local_date(NOW, TZ)
    asyncio.run(svc.run(date, False))
    assert len(seen) == 1 and str(seen[0].url) == WEBHOOK
    body = json.loads(seen[0].content)
    assert body["attachments"][0]["content"]["type"] == "AdaptiveCard"
    assert svc.is_done(date) and svc.state.get("channels")["teams"]["message"] == "accepted"
    assert "Morning report sent" in ev.system_events(0, NOW + 1)[0]["reason"]


def test_smtp_with_tls_login_and_headers(tmp_path, mini_inv, monkeypatch):
    monkeypatch.setenv("NETWORKGURU_SMTP_USER", "ng@asirobots.com")
    monkeypatch.setenv("NETWORKGURU_SMTP_PASSWORD", "smtp-secret-pw")
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls["connect"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            calls["tls_context"] = context

        def login(self, user, pw):
            calls["login"] = (user, pw)

        def send_message(self, msg):
            calls["msg"] = msg

    monkeypatch.setattr("networkguru.notify.smtplib.SMTP", FakeSMTP)
    svc, _ = service(tmp_path, mini_inv, report_teams=False, email_via="smtp", smtp_host="smtp.example.com",
                     email_from="ng@asirobots.com", email_to=["ops@asirobots.com", "casey@asirobots.com"])
    asyncio.run(svc.run(local_date(NOW, TZ), False))
    assert calls["connect"] == ("smtp.example.com", 587, 30.0)
    assert calls["tls_context"] is not None and calls["tls_context"].check_hostname        # certificates verified
    assert calls["login"] == ("ng@asirobots.com", "smtp-secret-pw")
    msg = calls["msg"]
    assert msg["To"] == "ops@asirobots.com, casey@asirobots.com" and msg["From"] == "ng@asirobots.com"
    assert msg["Subject"].startswith("[SIMULATED] Network Guru") and msg.is_multipart()
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}


def test_graph_send(tmp_path, mini_inv, monkeypatch):
    monkeypatch.setenv("NETWORKGURU_GRAPH_CLIENT_SECRET", "graph-client-secret-value")
    seen = []

    def handler(req):
        seen.append(req)
        if "oauth2" in str(req.url):
            assert b"client_secret=graph-client-secret-value" in req.content and b"client_credentials" in req.content
            return httpx.Response(200, json={"access_token": "AT"})
        assert req.headers["Authorization"] == "Bearer AT"
        return httpx.Response(202)
    svc, _ = service(tmp_path, mini_inv, handler, report_teams=False, email_via="graph",
                     graph_tenant="11111111-2222-3333-4444-555555555555", graph_client_id="app-id-1234",
                     graph_sender="ng@asirobots.com", email_to=["ops@asirobots.com"])
    asyncio.run(svc.run(local_date(NOW, TZ), False))
    assert str(seen[1].url) == "https://graph.microsoft.com/v1.0/users/ng@asirobots.com/sendMail"
    mail = json.loads(seen[1].content)["message"]
    assert mail["toRecipients"] == [{"emailAddress": {"address": "ops@asirobots.com"}}]
    assert mail["body"]["contentType"] == "HTML" and svc.state.get("channels")["email"]["ok"]


def test_retries_three_times_then_reports_masked(tmp_path, mini_inv, monkeypatch):
    monkeypatch.setenv("NETWORKGURU_TEAMS_WEBHOOK_URL", WEBHOOK)
    clock = Clock(NOW)
    tries = []

    def handler(req):
        tries.append(1)
        return httpx.Response(500, text=f"failed for {WEBHOOK}")
    svc, ev = service(tmp_path, mini_inv, handler, clock=clock, report_email=False)
    date = local_date(NOW, TZ)
    asyncio.run(svc.run(date, False))
    asyncio.run(svc.run(date, False))                 # too early for the second try
    assert len(tries) == 1 and not svc.is_done(date)
    for _ in range(2):
        clock.t += 301
        asyncio.run(svc.run(date, False))
    assert len(tries) == 3 and svc.is_done(date)
    ev_text = json.dumps(ev.system_events(0, clock.t + 1))
    assert "NOT fully sent" in ev_text and WEBHOOK not in ev_text and "Q" * 43 not in ev_text


def test_no_double_send_after_restart_and_daily_timing(tmp_path, mini_inv, monkeypatch):
    monkeypatch.setenv("NETWORKGURU_TEAMS_WEBHOOK_URL", WEBHOOK)
    sent = []
    clock = Clock(due_ts(local_date(NOW, TZ), "06:30", TZ) - 60)
    svc, _ = service(tmp_path, mini_inv, lambda r: sent.append(1) or httpx.Response(202), clock=clock, report_email=False)
    job = svc.daily_job()
    assert asyncio.run(job.tick()) is False                  # 06:29: not yet
    clock.t += 120
    assert asyncio.run(job.tick()) is True and sent == [1]   # 06:31
    svc2, _ = service(tmp_path, mini_inv, lambda r: sent.append(1) or httpx.Response(202), clock=clock,
                      report_email=False)                     # the app restarted
    assert asyncio.run(svc2.daily_job().tick()) is False and sent == [1]


def test_catch_up_late_and_too_late(tmp_path, mini_inv):
    ran = []

    async def run(date, late):
        ran.append(late)
    due = due_ts("2026-10-15", "06:30", TZ)
    clock = Clock(due + 2 * 3600)                                 # server was down at 06:30
    job = DailyJob("t", TZ, lambda: "06:30", lambda d: False, run, clock=clock)
    assert asyncio.run(job.tick()) and ran == [True]
    clock.t = due + 7 * 3600                                      # more than 6 h late: skip the day
    ran.clear()
    assert not asyncio.run(job.tick()) and ran == []
    job2 = DailyJob("t", TZ, lambda: "06:30", lambda d: False, run, clock=Clock(due + 60), ready=lambda: False)
    assert not asyncio.run(job2.tick())                          # waits for the first data


def test_dst_days():
    # 2026-03-08: clocks jump 02:00 -> 03:00 in Denver; 2026-11-01: 02:00 -> 01:00
    assert dt.datetime.fromtimestamp(due_ts("2026-03-08", "06:30", TZ), TZ).hour == 6
    assert dt.datetime.fromtimestamp(due_ts("2026-11-01", "06:30", TZ), TZ).strftime("%H:%M") == "06:30"
    a = due_ts("2026-03-08", "06:30", TZ)
    assert next_due("06:30", TZ, a) - a == 23 * 3600 + 3600       # the day after the change is 24 h
    assert due_ts("2026-03-08", "06:30", TZ) - due_ts("2026-03-07", "06:30", TZ) == 23 * 3600
    assert dt.datetime.fromtimestamp(due_ts("2026-03-08", "02:30", TZ), TZ).hour == 3     # skipped hour


def test_no_channel_set_up_is_an_event(tmp_path, mini_inv):
    svc, ev = service(tmp_path, mini_inv)
    asyncio.run(svc.run(local_date(NOW, TZ), False))
    rows = ev.system_events(0, NOW + 1)
    assert rows[0]["level"] == 3 and "NOT sent" in rows[0]["reason"]


def test_preview_page_keeps_its_own_csp(tmp_path):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, data_dir=tmp_path))
    with TestClient(app) as c:
        for _ in range(200):
            if c.get("/api/meta").json()["backfill"]["done"]:
                break
            time.sleep(0.1)
        r = c.get("/report/preview.html")
        assert r.status_code == 200 and r.headers["content-security-policy"].startswith("default-src 'none'")
        assert "<script" not in r.text and "Needs attention now" in r.text
        p = c.get("/api/report/preview").json()
        assert p["subject"].startswith("[SIMULATED]") and "NEEDS ATTENTION NOW" in p["text"]
        assert c.get("/static/js/app.js").headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("bad", ["24:00", "6:30", "", "06:60"])
def test_bad_times_never_run(bad):
    job = DailyJob("t", TZ, lambda: bad, lambda d: False, None, clock=Clock(NOW))
    assert asyncio.run(job.tick()) is False
