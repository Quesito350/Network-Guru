"""0.9.5 Feedback: anyone sends it (no sign-in, same origin, rate limited, secrets masked), admins track
it (status + note, each change an event), the morning report lists the new and open ones, and the
database stays one a 0.9.4 rollback can open (an added table, kept by prune)."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from networkguru import feedback as fb
from networkguru.config import Settings, register_secret
from networkguru.events import EventStore
from networkguru.report import as_html, as_teams_card, as_text, report_from_runtime
from networkguru.web.app import create_app

from .conftest import EXAMPLE

PW = "correct-horse-battery"
HDR = {"X-Requested-With": "networkguru"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, deploy="docker",
                              data_dir=tmp_path_factory.mktemp("fb"), dev_admin_password=PW))
    with TestClient(app) as c:
        yield c


def _admin(client):
    client.cookies.clear()
    assert client.post("/api/admin/login", json={"password": PW, "name": "Sam"}, headers=HDR).status_code == 200


def test_store_add_list_update_and_counts():
    ev = EventStore()
    now = time.time()
    a = ev.add_feedback(now - 2 * 86400, "idea", "older idea")
    b = ev.add_feedback(now - 60, "request", "add the COW Cerbo", "Sam", "#/infrastructure")
    assert [x["id"] for x in ev.feedback()] == [b, a]                          # newest first
    assert ev.feedback_counts(now - 86400, fb.OPEN) == {"new": 2, "new_since": 1, "open": 2, "total": 2}
    row = ev.update_feedback(a, now, "Sam", "done", "shipped")
    assert (row["status"], row["note"], row["updated_by"]) == ("done", "shipped", "Sam")
    assert [x["id"] for x in ev.feedback(fb.OPEN)] == [b] and [x["id"] for x in ev.feedback(fb.CLOSED)] == [a]
    assert ev.update_feedback(a, now + 1, "Other", None, None)["updated_by"] == "Sam"   # nothing changed: not touched
    ev.update_feedback(b, now, "Sam", "planned")
    assert ev.feedback_counts(now - 86400, fb.OPEN) == {"new": 0, "new_since": 1, "open": 1, "total": 2}   # sent today
    assert ev.update_feedback(999, now, "Sam", "done") is None
    ev.prune(now + 1)                                                          # the 90-day clean-up: feedback stays
    assert ev.feedback_counts(0, fb.OPEN)["total"] == 2


def test_an_older_database_gets_the_table_and_keeps_its_rows(tmp_path):
    path = tmp_path / "ev.db"
    EventStore(path).close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE feedback")                                          # as 0.9.4 left it
    db.execute("INSERT INTO system_events(ts,kind,text) VALUES(1,'admin','kept')")
    db.commit()
    db.close()
    ev = EventStore(path)
    assert ev.feedback() == [] and ev.system_events(0, 2)[0]["reason"] == "kept"
    ev.add_feedback(5, "idea", "x")
    ev.close()


def test_clean_rejects_and_masks():
    register_secret("tok-ABC123xyz-secret")
    kind, text, name, page = fb.clean("problem", "  my key is tok-ABC123xyz-secret\x00\r\nline 2  ", "  Sam  ", "#/map")
    assert (kind, name, page) == ("problem", "Sam", "#/map")
    assert text == "my key is *****\nline 2" and "tok-ABC" not in text
    assert fb.clean("idea", "x", "", "https://elsewhere")[3] is None             # only the dashboard's own pages
    for bad in [("nope", "x"), ("idea", "   "), ("idea", "x" * (fb.MAX_TEXT + 1))]:
        with pytest.raises(ValueError):
            fb.clean(*bad)
    assert fb.clean_note("password=hunter2 ok") == "password=***** ok"


def test_rate_limit_per_address():
    clock = [0.0]
    lim = fb.RateLimit(per_hour=2, clock=lambda: clock[0])
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a") and lim.allow("b")
    clock[0] += 3601
    assert lim.allow("a")


def test_anyone_sends_feedback(client):
    client.cookies.clear()
    body = {"kind": "request", "text": "Show the COW Cerbo", "name": "Sam", "page": "#/infrastructure"}
    assert client.post("/api/feedback", json=body).status_code == 403            # not from the dashboard's page
    assert client.post("/api/feedback", json=body, headers={**HDR, "Origin": "http://evil.example"}).status_code == 403
    r = client.post("/api/feedback", json=body, headers=HDR)
    assert r.status_code == 200 and r.json()["id"] >= 1
    assert client.post("/api/feedback", json={**body, "text": " "}, headers=HDR).status_code == 400
    assert client.post("/api/feedback", json={**body, "text": "x" * 2001}, headers=HDR).status_code == 400
    assert client.post("/api/feedback", json={**body, "kind": "spam"}, headers=HDR).status_code == 400
    assert client.get("/api/admin/feedback").status_code == 401                 # the list is for admins


def test_too_many_in_an_hour(client):
    rt = client.app.state.rt
    old = rt.feedback_limit
    rt.feedback_limit = fb.RateLimit(per_hour=1)
    try:
        assert client.post("/api/feedback", json={"text": "one"}, headers=HDR).status_code == 200
        assert client.post("/api/feedback", json={"text": "two"}, headers=HDR).status_code == 429
    finally:
        rt.feedback_limit = old


def test_admins_track_it_and_every_change_is_an_event(client):
    fid = client.post("/api/feedback", json={"kind": "idea", "text": "trail on the map"}, headers=HDR).json()["id"]
    _admin(client)
    d = client.get("/api/admin/feedback").json()
    assert fid in [x["id"] for x in d["items"]] and d["statuses"]["doing"] == "In progress" and d["counts"]["new"] >= 1
    assert client.put(f"/api/admin/feedback/{fid}", json={"status": "doing"}).status_code == 403    # header needed
    r = client.put(f"/api/admin/feedback/{fid}", json={"status": "doing", "note": "in 0.9.5"}, headers=HDR)
    assert r.status_code == 200 and (r.json()["status"], r.json()["note"]) == ("doing", "in 0.9.5")
    client.put(f"/api/admin/feedback/{fid}", json={"note": "in 0.9.5, with heading"}, headers=HDR)
    assert client.put(f"/api/admin/feedback/{fid}", json={"status": "maybe"}, headers=HDR).status_code == 400
    assert client.put("/api/admin/feedback/99999", json={"status": "done"}, headers=HDR).status_code == 404
    texts = [e["reason"] for e in client.app.state.rt.events.system_events(time.time() - 60, time.time() + 1)]
    assert f"Feedback #{fid}: New → In progress" in texts and f"Feedback #{fid}: note changed" in texts
    assert fid not in [x["id"] for x in client.get("/api/admin/feedback", params={"show": "closed"}).json()["items"]]
    client.put(f"/api/admin/feedback/{fid}", json={"status": "done"}, headers=HDR)
    assert fid in [x["id"] for x in client.get("/api/admin/feedback", params={"show": "closed"}).json()["items"]]


def test_the_morning_report_lists_new_and_open(client):
    rt = client.app.state.rt
    rt.events.add_feedback(time.time(), "problem", "The [map](http://x) **jumps** " + "y" * 200, "Pat")
    rep = report_from_runtime(rt)
    c = rt.events.feedback_counts(time.time() - 86400, fb.OPEN)
    line = f"{c['new_since']} new since yesterday, {c['open']} open"
    assert rep.feedback_line == line and len(rep.feedback) <= 5
    newest = rep.feedback[0]
    assert newest.startswith("#") and "Problem: The [map]" in newest and newest.endswith("… - Pat")
    assert f"FEEDBACK: {line} (Admin page)" in as_text(rep)
    assert "<h3 style=\"font-size:15px;margin:16px 0 6px\">Feedback</h3>" in as_html(rep)
    card = str(as_teams_card(rep))
    assert f"Feedback: {line}" in card and "[map]" not in card and "**jumps**" not in card
