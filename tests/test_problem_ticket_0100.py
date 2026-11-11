"""0.10.0 Report a problem for anyone: the full report is kept on the server (admins list and download it),
and a ticket comes back for the Network Change Requests page (#ng= contract, problem_ticket.py) with a
masked error-log excerpt. Admin → Download the log: the log file kept across restarts, masked."""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from networkguru import logbuffer, problem_ticket as pt
from networkguru.config import Settings, register_secret
from networkguru.web.app import create_app

from .conftest import EXAMPLE

PW = "correct-horse-battery"
HDR = {"X-Requested-With": "networkguru"}
NCR = "https://claude.ai/artifact/example-page"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, deploy="docker",
                              data_dir=tmp_path_factory.mktemp("ncr"), dev_admin_password=PW))
    with TestClient(app) as c:
        yield c


def _admin(client):
    client.cookies.clear()
    assert client.post("/api/admin/login", json={"password": PW, "name": "Sam"}, headers=HDR).status_code == 200


def test_location_guess_and_text():
    assert pt.location_for("", "Mendon - proving grounds", "mendon") == "Mendon"
    assert pt.location_for("", "Example site", "dfw-yard") == "Dallas/Fort Worth"
    assert pt.location_for("", "Example site", "example") == "Other"
    assert pt.location_for("Denver", "Mendon", "mendon") == "Denver"                 # the admin's choice wins
    assert pt.clean_text("  a\x00b \n") == "ab"
    for bad in ("   ", "x" * (pt.TEXT_MAX + 1)):
        with pytest.raises(ValueError):
            pt.clean_text(bad)


def test_warning_lines_keep_tracebacks_and_the_last_ones():
    entries = [f"2026-01-01 10:00:{i:02d},000 INFO a: fine {i}" for i in range(5)]
    entries += [f"2026-01-01 10:01:{i:02d},000 WARNING b: slow {i}" for i in range(70)]
    entries += ["2026-01-01 10:02:00,000 ERROR c: boom\nTraceback (most recent call last):\n  File x"]
    out = pt.warn_lines(entries)
    assert len(out) == pt.LOG_LINES and out[-1].startswith("2026-01-01 10:02:00,000 ERROR c: boom\nTraceback")
    assert not any(" INFO " in e for e in out)


def test_anyone_reports_a_problem_and_gets_a_ticket(client):
    register_secret("tok-NCR-secret-123")
    client.cookies.clear()
    st = client.put("/api/admin/settings", json={"ncr_url": NCR}, headers=HDR)
    assert st.status_code == 401                                                     # settings stay admin-only
    _admin(client)
    assert client.put("/api/admin/settings", json={"ncr_url": "http://not-https/x"}, headers=HDR).status_code == 400
    assert client.put("/api/admin/settings", json={"ncr_url": NCR + "#x"}, headers=HDR).status_code == 400
    assert client.put("/api/admin/settings", json={"ncr_location": "Mars"}, headers=HDR).status_code == 400
    assert client.put("/api/admin/settings", json={"ncr_url": NCR, "ncr_location": "Logan"}, headers=HDR).status_code == 200
    assert client.get("/api/meta").json()["ncr"] is True
    client.cookies.clear()
    logging.getLogger("networkguru.test").warning("reader failed with token tok-NCR-secret-123")
    comp = next(iter(client.app.state.rt.inv.comps))
    body = {"text": "The trailer radio is down\nsince lunch", "priority": "Urgent", "page": f"#/c/{comp}",
            "browser": {"url": "http://x/", "errors": [{"message": "TypeError: x is undefined", "source": "app.js:1:2"}]}}
    assert client.post("/api/problem", json=body).status_code == 403                 # same origin only
    r = client.post("/api/problem", json=body, headers=HDR)
    assert r.status_code == 200, r.text
    d = r.json()
    assert pt.ID_RE.match(d["id"]) and d["url"] == f"{NCR}#ng={d['ng']}" and d["location"] == "Logan"
    t = pt.decode(d["ng"])
    assert (t["v"], t["from"], t["type"], t["site"], t["priority"], t["ngReport"]) == (1, "network-guru", "Other", "Logan", "Urgent", d["id"])
    assert t["title"].endswith(": The trailer radio is down") and t["devices"]
    assert "since lunch" in t["description"] and d["id"] in t["justification"]
    log = t["log"]
    assert len(log) <= pt.EXCERPT_MAX and "TypeError: x is undefined" in log and "reader failed" in log
    assert "tok-NCR-secret" not in log and "tok-NCR-secret" not in d["ng"]          # masked
    assert len(d["ng"]) < 16_000                                                     # a link a browser takes
    assert client.post("/api/problem", json={"text": "  "}, headers=HDR).status_code == 400
    ev = client.get("/api/events", params={"from": time.time() - 60, "system": "true", "kinds": "group"}).json()
    assert any(e.get("sys_kind") == "problem" and d["id"] in e["reason"] for e in ev)


def test_admins_list_and_download_kept_reports(client):
    client.cookies.clear()
    rid = client.post("/api/problem", json={"text": "kept one"}, headers=HDR).json()["id"]
    assert client.get("/api/admin/problem-reports").status_code == 401
    assert client.get(f"/api/admin/problem-reports/{rid}").status_code == 401
    _admin(client)
    items = client.get("/api/admin/problem-reports").json()["reports"]
    assert items[0]["id"] == rid and items[0]["first"] == "kept one" and items[0]["by"].startswith("a viewer")
    z = client.get(f"/api/admin/problem-reports/{rid}")
    assert z.status_code == 200 and z.content[:2] == b"PK"
    assert client.get("/api/admin/problem-reports/NG-000000-ZZZZ").status_code == 404
    assert client.get("/api/admin/problem-reports/..%2Fsecrets").status_code == 404


def test_store_prunes_old_and_too_many(tmp_path):
    now = [1_000_000.0]
    s = pt.ReportStore(tmp_path, clock=lambda: now[0])
    s.save("NG-260101-AAAA", b"PK old", {"t": now[0] - 31 * 86400, "first": "old"})
    assert s.list() == [] and not (tmp_path / "reports" / "NG-260101-AAAA.zip").exists()
    for i in range(pt.KEEP_MAX + 3):
        s.save(f"NG-260101-B{i:03d}", b"PK", {"t": now[0] + i})
    assert len(s.list()) == pt.KEEP_MAX and s.list()[0]["id"] == f"NG-260101-B{pt.KEEP_MAX + 2:03d}"
    assert len(list((tmp_path / "reports").glob("*.zip"))) == pt.KEEP_MAX
    assert s.path("../index") is None


def test_rate_limit(client):
    client.cookies.clear()
    rl = client.app.state.rt.problem_limit
    rl.hits.clear()
    codes = [client.post("/api/problem", json={"text": f"n{i}"}, headers=HDR).status_code for i in range(pt.PER_HOUR + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {200}
    rl.hits.clear()


def test_without_a_page_reports_are_only_kept(client):
    _admin(client)
    client.put("/api/admin/settings", json={"ncr_url": ""}, headers=HDR)
    client.cookies.clear()
    d = client.post("/api/problem", json={"text": "no page yet"}, headers=HDR).json()
    assert d["url"] is None and d["ng"] and client.get("/api/meta").json()["ncr"] is False


def test_log_file_and_download(client, tmp_path):
    register_secret("pw-LOG-secret-99")
    logging.getLogger("networkguru.test").warning("login pw-LOG-secret-99 refused")
    assert client.get("/api/admin/log").status_code == 401
    _admin(client)
    r = client.get("/api/admin/log")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "refused" in r.text and "pw-LOG-secret" not in r.text
    # the file rotates and the download keeps to the newest part, starting on a whole line
    h = logbuffer.install_file(tmp_path / "logs")
    try:
        for i in range(3000):
            logging.getLogger("networkguru.rot").info("line %05d %s", i, "x" * 900)
        files = sorted(p.name for p in (tmp_path / "logs").iterdir())
        assert "networkguru.log.1" in files
        text = logbuffer.file_text(tmp_path / "logs", limit=50_000)
        assert len(text.encode()) <= 50_000 and text[:4].isdigit() and "line 02999" in text
    finally:
        logbuffer.install_file(client.app.state.rt.settings.data_dir / "logs")      # back to the app's own
        assert h is not None
