"""Admin sign-in: hashing (app and host updater agree), lockout, cookie flags, the request
checks, non-admins refused, and secrets shown by name only."""

import asyncio
import importlib.util
import json

import pytest
from fastapi.testclient import TestClient

from networkguru.auth import AdminAuth, hash_password, lockout_seconds, verify_password
from networkguru.config import Settings
from networkguru.web.app import create_app

from .conftest import EXAMPLE, ROOT

PW = "correct-horse-battery"
HDR = {"X-Requested-With": "networkguru"}


def load_updater():
    spec = importlib.util.spec_from_file_location("ng_updater_auth", ROOT / "deploy" / "ng_updater.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, deploy="docker",
                              data_dir=tmp_path_factory.mktemp("adm"), dev_admin_password=PW))
    with TestClient(app) as c:
        yield c


def signed_in(client):
    client.cookies.clear()
    r = client.post("/api/admin/login", json={"password": PW, "name": "Casey"}, headers=HDR)
    assert r.status_code == 200
    return r


def test_hash_round_trip_and_host_updater_hash_is_accepted():
    h = hash_password("a-long-password")
    assert h.startswith("scrypt:32768:8:1:") and verify_password("a-long-password", h)
    assert not verify_password("a-long-passwore", h) and not verify_password("x", "garbage") and not verify_password("x", None)
    host = load_updater().hash_password("made-on-the-server")       # install.sh hashes on the host
    assert verify_password("made-on-the-server", host) and not verify_password("made-on-the-serve", host)


def test_lockout_grows():
    assert [lockout_seconds(n) for n in range(1, 7)] == [0, 0, 10, 60, 300, 300]

    class C:
        t = 1000.0

        def __call__(self):
            return self.t
    clock = C()
    auth = AdminAuth(dev_hash=hash_password("secret-pass-1", n=1024), clock=clock)
    for _ in range(3):
        assert asyncio.run(auth.login("10.0.0.5", "wrong", "x")) is None
    assert auth.wait_for("10.0.0.5") == 10 and auth.wait_for("10.0.0.6") == 0
    clock.t += 11
    tok = asyncio.run(auth.login("10.0.0.5", "secret-pass-1", "Casey"))
    assert tok and auth.session(tok).name == "Casey" and auth.wait_for("10.0.0.5") == 0
    clock.t += 8 * 3600 + 1                                  # idle too long
    assert auth.session(tok) is None


def test_login_cookie_flags_and_logout(client):
    r = signed_in(client)
    cookie = r.headers["set-cookie"]
    assert "ng_admin=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert client.get("/api/admin/session").json()["signed_in"] is True
    assert client.get("/api/admin/status").status_code == 200
    assert client.post("/api/admin/logout", headers=HDR).status_code == 200
    client.cookies.clear()
    assert client.get("/api/admin/status").status_code == 401


def test_wrong_password_then_locked(client):
    client.cookies.clear()
    codes = [client.post("/api/admin/login", json={"password": "nope"}, headers=HDR).status_code for _ in range(4)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429
    rows = client.get("/api/events", params={"kinds": "system", "system": "true"}).json()
    assert any("sign-in failed" in r["reason"] for r in rows)
    assert not any("nope" in json.dumps(r) for r in rows)              # the typed password is never stored
    client.app.state.rt.auth.tries.clear()


def test_requests_without_the_header_or_from_another_site_are_refused(client):
    client.cookies.clear()
    assert client.post("/api/admin/login", json={"password": PW}).status_code == 403
    assert client.post("/api/admin/login", json={"password": PW},
                       headers={**HDR, "Origin": "http://evil.example"}).status_code == 403
    signed_in(client)
    assert client.post("/api/admin/update/skip").status_code == 403
    assert client.post("/api/admin/update/skip", headers={**HDR, "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/admin/update/skip", headers={**HDR, "Origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("path", ["/api/admin/update/check", "/api/admin/update/approve", "/api/admin/update/remind",
                                  "/api/admin/update/skip", "/api/admin/update/dismiss", "/api/admin/report/test",
                                  "/api/admin/update/upload?name=NetworkGuru_v99.0.0.zip"])
def test_non_admin_refused(client, path):
    client.cookies.clear()
    assert client.post(path, headers=HDR, content=b"x").status_code == 401
    assert client.put("/api/admin/settings", json={"report_time": "07:00"}, headers=HDR).status_code == 401


def test_sim_fault_needs_admin_on_a_server(client):
    client.cookies.clear()
    body = {"target": "t48-gx", "kind": "outage", "minutes": 1}
    assert client.post("/api/sim/fault", json=body, headers=HDR).status_code == 401
    signed_in(client)
    assert client.post("/api/sim/fault", json=body, headers=HDR).status_code == 200


def test_settings_are_validated(client):
    signed_in(client)
    assert client.put("/api/admin/settings", json={"report_time": "25:00"}, headers=HDR).status_code == 400
    assert client.put("/api/admin/settings", json={"email_to": "not-an-address"}, headers=HDR).status_code == 400
    r = client.put("/api/admin/settings", json={"report_time": "07:15", "email_to": "a@asirobots.com; b@asirobots.com"},
                   headers=HDR)
    assert r.status_code == 200 and r.json()["email_to"] == ["a@asirobots.com", "b@asirobots.com"]


def test_secrets_listed_by_name_only(client, monkeypatch):
    url = "https://prod-00.westus." + "logic.azure.com/workflows/x/triggers/manual/paths/invoke?sig=" + "S" * 40
    monkeypatch.setenv("NETWORKGURU_TEAMS_WEBHOOK_URL", url)
    signed_in(client)
    st = client.get("/api/admin/status").json()
    teams = next(s for s in st["secrets"] if s["name"] == "NETWORKGURU_TEAMS_WEBHOOK_URL")
    assert teams["set"] is True
    assert url not in json.dumps(st) and PW not in json.dumps(st)


def test_developer_install_prints_a_one_time_password(tmp_path, monkeypatch, capsys):
    """compose.yaml from git has no install.sh: serve prints a one-time password, unless a hash is set."""
    import networkguru.__main__ as cli
    seen = []
    monkeypatch.setattr(cli, "_serve", lambda s, open_browser: seen.append(s) or 0)
    monkeypatch.setenv("NETWORKGURU_DEPLOY", "compose")
    monkeypatch.setenv("NETWORKGURU_SECRETS", str(tmp_path / "missing.env"))
    monkeypatch.delenv("NETWORKGURU_ADMIN_PASSWORD_HASH", raising=False)
    assert cli.main(["serve", "--inventory", str(EXAMPLE), "--data-dir", str(tmp_path)]) == 0
    pw = seen[-1].dev_admin_password
    assert seen[-1].deploy == "compose" and pw and f"one-time password for this start: {pw}" in capsys.readouterr().out
    monkeypatch.setenv("NETWORKGURU_ADMIN_PASSWORD_HASH", hash_password(PW, n=1024))
    assert cli.main(["serve", "--inventory", str(EXAMPLE), "--data-dir", str(tmp_path)]) == 0
    assert seen[-1].dev_admin_password is None and "one-time" not in capsys.readouterr().out
    monkeypatch.setenv("NETWORKGURU_DEPLOY", "docker")                 # a site server never gets one
    monkeypatch.delenv("NETWORKGURU_ADMIN_PASSWORD_HASH")
    assert cli.main(["serve", "--inventory", str(EXAMPLE), "--data-dir", str(tmp_path)]) == 0
    assert seen[-1].dev_admin_password is None


def test_test_install_shows_the_demo_without_an_inventory(tmp_path, monkeypatch, capsys):
    """compose.yaml with an empty site folder shows the demo site; a site server never does."""
    import networkguru.__main__ as cli
    from networkguru.inventory import InventoryError
    seen = []
    monkeypatch.setattr(cli, "_serve", lambda s, open_browser: seen.append(s) or 0)
    monkeypatch.setenv("NETWORKGURU_DEPLOY", "compose")
    monkeypatch.setenv("NETWORKGURU_SECRETS", str(tmp_path / "missing.env"))
    missing = str(tmp_path / "site" / "inventory.yaml")
    assert cli.main(["serve", "--inventory", missing, "--data-dir", str(tmp_path)]) == 0
    assert seen[-1].inventory == cli.EXAMPLE and "showing the demo site" in capsys.readouterr().out
    monkeypatch.setenv("NETWORKGURU_DEPLOY", "docker")
    with pytest.raises(InventoryError, match="cannot read"):
        cli.main(["serve", "--inventory", missing, "--data-dir", str(tmp_path)])
    assert len(seen) == 1


def test_developer_install_session_and_server_rules(tmp_path):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, deploy="compose",
                              data_dir=tmp_path, dev_admin_password=PW))
    with TestClient(app) as c:
        s = c.get("/api/admin/session").json()
        assert s["configured"] and s["dev_password"] is True and s["deploy"] == "compose"
        target = next(iter(app.state.rt.inv.comps))
        r = c.post("/api/sim/fault", json={"target": target, "minutes": 5}, headers=HDR)
        assert r.status_code == 401                                       # like a server: admins only
        assert c.post("/api/admin/login", json={"password": PW, "name": "Casey"}, headers=HDR).status_code == 200
        r = c.post("/api/admin/update/approve", headers=HDR)
        assert r.status_code == 409 and "docker compose pull" in r.json()["detail"]
        st = c.get("/api/admin/status").json()["updates"]
        assert st["deploy"] == "compose" and st["can_install"] is False
    assert app.state.rt.docker


def test_no_admin_password_means_admin_is_off(tmp_path):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, data_dir=tmp_path))
    with TestClient(app) as c:
        assert c.get("/api/admin/session").json()["configured"] is False
        assert c.post("/api/admin/login", json={"password": "anything-long"}, headers=HDR).status_code == 503
