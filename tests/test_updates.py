"""Daily update check against a fake GitHub: newer / same / older, bad checksum, no token,
redirects without the token, the checks on the zip, remind / skip, results from the host."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from networkguru import __version__
from networkguru.events import EventStore
from networkguru.github import UpdateError, is_newer, version_tuple
from networkguru.site_settings import open_settings
from networkguru.updates import REQUEST, RESULT, STATUS, UpdateManager, verify_release_zip

from .conftest import make_release_zip, sha_text

TZ = ZoneInfo("America/Denver")
TOKEN = "t0k-fake-github-token-for-tests"
NEWER = "99.0.0"


class FakeGitHub:
    """releases/latest + assets. Asset downloads answer 302 to a relative CDN path, like GitHub."""

    def __init__(self):
        self.version = NEWER
        self.zip_bytes = b""
        self.sha = ""
        self.latest_status = 200
        self.with_sha_asset = True
        self.seen: dict[str, str | None] = {}
        self.hits = 0
        gh = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                gh.hits += 1
                gh.seen[self.path] = self.headers.get("Authorization")
                base = f"http://127.0.0.1:{gh.port}"
                if self.path == "/latest":
                    if gh.latest_status != 200:
                        return self._send(gh.latest_status, b'{"message":"nope"}')
                    name = f"NetworkGuru_v{gh.version}.zip"
                    assets = [{"name": name, "url": f"{base}/assets/1", "size": len(gh.zip_bytes)}]
                    if gh.with_sha_asset:
                        assets.append({"name": name + ".sha256", "url": f"{base}/assets/2", "size": len(gh.sha)})
                    rel = {"tag_name": f"v{gh.version}", "published_at": "2026-10-20T12:00:00Z",
                           "body": "## Network Guru\n- a fix", "assets": assets}
                    return self._send(200, json.dumps(rel).encode())
                if self.path == "/assets/1":
                    return self._send(302, headers={"Location": "/cdn/zip?sig=abc"})
                if self.path == "/assets/2":
                    return self._send(302, headers={"Location": f"{base}/cdn/sha"})
                if self.path.startswith("/cdn/zip"):
                    return self._send(200, gh.zip_bytes, "application/octet-stream")
                if self.path == "/cdn/sha":
                    return self._send(200, gh.sha.encode(), "application/octet-stream")
                self._send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, tmp: Path, version: str, **zip_opts) -> None:
        z = make_release_zip(tmp / f"NetworkGuru_v{version}.zip", version, **zip_opts)
        self.version = version
        self.zip_bytes = z.read_bytes()
        self.sha = sha_text(z)
        z.unlink()


@pytest.fixture
def gh(monkeypatch):
    g = FakeGitHub()
    monkeypatch.setenv("NETWORKGURU_UPDATE_API", f"http://127.0.0.1:{g.port}/latest")
    monkeypatch.setenv("NETWORKGURU_GITHUB_TOKEN", TOKEN)
    yield g
    g.server.shutdown()


class Clock:
    def __init__(self, t=None):
        self.t = t or time.time()

    def __call__(self):
        return self.t


def manager(tmp_path, deploy="docker", clock=None):
    ev = EventStore()
    um = UpdateManager(tmp_path, deploy, TZ, open_settings(tmp_path), ev, clock=clock or time.time)
    return um, ev


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
def test_versions_compare_numerically():
    assert version_tuple("0.10.0") > version_tuple("0.9.9")
    assert is_newer("0.5.1", "0.5.0") and not is_newer("0.5.0", "0.5.0") and not is_newer("0.4.9", "0.5.0")


@pytest.mark.parametrize("version,offered", [(NEWER, True), (__version__, False), ("0.0.1", False)])
def test_check_newer_same_older(tmp_path, gh, version, offered):
    gh.publish(tmp_path, version)
    um, ev = manager(tmp_path)
    lc = run(um.check())
    assert lc["ok"] and lc["latest"]["version"] == version
    st = um.public_status()
    assert (st["offered"] == version) is offered
    assert any(b["kind"] == "available" for b in st["banners"]) is offered
    assert gh.seen["/latest"] == f"Bearer {TOKEN}"
    assert bool(ev.system_events(0, time.time() + 1)) is offered        # "X is available" once


def test_no_token_checks_and_installs_from_the_public_repo(tmp_path, gh, monkeypatch):
    """The repository is public (0.5.3): no token needed, and no Authorization header is sent."""
    monkeypatch.delenv("NETWORKGURU_GITHUB_TOKEN")
    gh.publish(tmp_path, NEWER)
    um, _ = manager(tmp_path)

    async def flow():
        lc = await um.check()
        assert lc["ok"] and um.public_status()["offered"] == NEWER and um.public_status()["token_set"] is False
        um.approve("Casey")
        await um.task
    run(flow())
    assert um.job["state"] == "requested" and (tmp_path / "updates" / REQUEST).is_file()
    assert set(gh.seen) >= {"/latest", "/assets/1", "/assets/2"} and not any(gh.seen.values())


def test_no_internet_cant_check(tmp_path, monkeypatch):
    monkeypatch.setenv("NETWORKGURU_UPDATE_API", "http://127.0.0.1:9/latest")     # nothing listens
    monkeypatch.setenv("NETWORKGURU_GITHUB_TOKEN", TOKEN)
    um, _ = manager(tmp_path)
    lc = run(um.check())
    assert not lc["ok"] and lc["message"].startswith("Can't check for updates") and "no internet" in lc["message"]


def test_token_refused_401(tmp_path, gh):
    gh.latest_status = 401
    um, _ = manager(tmp_path)
    lc = run(um.check())
    assert not lc["ok"] and "401" in lc["message"] and TOKEN not in json.dumps(lc)


def test_release_without_checksum_is_not_offered(tmp_path, gh):
    gh.publish(tmp_path, NEWER)
    gh.with_sha_asset = False
    um, _ = manager(tmp_path)
    lc = run(um.check())
    assert not lc["ok"] and ".sha256" in lc["message"]


def test_approve_downloads_verifies_and_requests(tmp_path, gh):
    gh.publish(tmp_path, NEWER)
    um, ev = manager(tmp_path)

    async def flow():
        await um.check()
        assert um.approve("Casey") == NEWER
        await um.task
    run(flow())
    # the token went to the API, never to the CDN the redirects pointed at (one of them relative)
    assert gh.seen["/assets/1"] == f"Bearer {TOKEN}" and gh.seen["/cdn/zip?sig=abc"] is None
    assert gh.seen["/cdn/sha"] is None
    req = json.loads((tmp_path / "updates" / REQUEST).read_text())
    assert req["version"] == NEWER and req["zip"] == f"NetworkGuru_v{NEWER}.zip" and req["requested_by"] == "Casey"
    assert len(req["sha256"]) == 64 and len(req["request_id"]) == 16
    assert (tmp_path / "updates" / req["zip"]).is_file()
    assert um.job["state"] == "requested" and um.public_status()["offered"] is None
    texts = [e["reason"] for e in ev.system_events(0, time.time() + 1)]
    assert any("approved" in t for t in texts) and any("handed to the server" in t for t in texts)


def test_approve_on_a_pc_only_stages(tmp_path, gh):
    gh.publish(tmp_path, NEWER)
    um, _ = manager(tmp_path, deploy="dev")

    async def flow():
        await um.check()
        um.approve("Casey")
        await um.task
    run(flow())
    assert um.job["state"] == "staged" and not (tmp_path / "updates" / REQUEST).exists()


def test_developer_install_only_reminds(tmp_path, gh):
    """compose.yaml from a git checkout: the check still offers a newer release, but it updates
    with git pull - Approve and Upload are refused and nothing is staged."""
    gh.publish(tmp_path, NEWER)
    um, _ = manager(tmp_path, deploy="compose")
    run(um.check())
    st = um.public_status()
    assert st["offered"] == NEWER and st["deploy"] == "compose" and st["can_install"] is False
    with pytest.raises(UpdateError, match="git pull, then docker compose up -d --build"):
        um.approve("Casey")

    async def chunks():
        yield b"PK"
    with pytest.raises(UpdateError, match="git pull"):
        run(um.upload(f"NetworkGuru_v{NEWER}.zip", "1" * 64, chunks(), "Casey"))
    staged = tmp_path / "updates"
    assert um.job["state"] == "idle" and (not staged.exists() or not any(staged.iterdir()))
    um.skip("Casey")
    assert um.public_status()["offered"] is None                   # Skip still works


@pytest.mark.parametrize("problem", ["bad_checksum", "wrong_inner_version", "zipslip", "symlink", "missing_dockerfile"])
def test_bad_downloads_are_refused_and_nothing_staged(tmp_path, gh, problem):
    opts = {"wrong_inner_version": {"inner_version": "98.0.0"}, "zipslip": {"zipslip": True},
            "symlink": {"symlink": True}, "missing_dockerfile": {"missing": ("deploy/Dockerfile",)}}.get(problem, {})
    gh.publish(tmp_path, NEWER, **opts)
    if problem == "bad_checksum":
        gh.sha = "0" * 64 + f"  NetworkGuru_v{NEWER}.zip\n"
    um, ev = manager(tmp_path)

    async def flow():
        await um.check()
        um.approve("Casey")
        await um.task
    run(flow())
    assert um.job["state"] == "failed"
    staged = [p.name for p in (tmp_path / "updates").iterdir()]
    assert staged == [], staged
    assert any(e["level"] >= 3 for e in ev.system_events(0, time.time() + 1))


def test_size_mismatch_is_refused(tmp_path, gh):
    gh.publish(tmp_path, NEWER)
    um, _ = manager(tmp_path)

    async def flow():
        await um.check()                        # GitHub lists the full size...
        gh.zip_bytes = gh.zip_bytes[:-10]       # ...but fewer bytes arrive
        um.approve("Casey")
        await um.task
    run(flow())
    assert um.job["state"] == "failed" and "incomplete" in um.job["message"]
    assert [p.name for p in (tmp_path / "updates").iterdir()] == []


def test_verify_release_zip_rules(tmp_path):
    ok = make_release_zip(tmp_path / "a.zip", NEWER)
    assert verify_release_zip(ok) == NEWER
    with pytest.raises(UpdateError, match="not newer"):
        verify_release_zip(make_release_zip(tmp_path / "b.zip", "0.0.1"))
    with pytest.raises(UpdateError, match="not .*99.0.1|holds version"):
        verify_release_zip(ok, expected_version="99.0.1")
    (tmp_path / "c.zip").write_bytes(b"not a zip")
    with pytest.raises(UpdateError, match="not a zip"):
        verify_release_zip(tmp_path / "c.zip")


def test_remind_tomorrow_and_skip(tmp_path, gh):
    gh.publish(tmp_path, NEWER)
    clock = Clock()
    um, _ = manager(tmp_path, clock=clock)
    run(um.check())
    assert um.offered() == NEWER
    until = um.remind_tomorrow("Casey")
    assert um.offered() is None and until > clock.t
    clock.t = until + 1                         # the next day's check time has passed
    assert um.offered() == NEWER
    um.skip("Casey")
    assert um.offered() is None and NEWER in um.public_status()["skipped"]
    gh.publish(tmp_path, "99.0.1")              # a newer one is offered again
    run(um.check())
    assert um.offered() == "99.0.1"


def test_host_result_becomes_event_and_banner_once(tmp_path):
    clock = Clock()
    um, ev = manager(tmp_path, clock=clock)
    um._set_job("requested", NEWER, "Waiting…", by="Casey", request_id="abcd1234abcd1234", requested_at=clock.t)
    up = tmp_path / "updates"
    up.mkdir(parents=True, exist_ok=True)
    (up / STATUS).write_text(json.dumps({"request_id": "abcd1234abcd1234", "state": "installing", "message": "x"}))
    um.poll_host()
    assert um.job["state"] == "installing"
    (up / RESULT).write_text(json.dumps({"request_id": "abcd1234abcd1234", "outcome": "rolled_back", "version": NEWER,
                                         "previous": __version__, "message": "not healthy within 60 s"}))
    um.poll_host()
    st = um.public_status()
    assert um.job["state"] == "idle" and st["result"]["outcome"] == "rolled_back"
    assert any(b["kind"] == "result" and b["level"] == 4 for b in st["banners"])
    n = len(ev.system_events(0, time.time() + 10))
    um2 = UpdateManager(tmp_path, "docker", TZ, open_settings(tmp_path), ev, clock=clock)    # after a restart
    um2.poll_host()
    assert len(ev.system_events(0, time.time() + 10)) == n            # the same result never fires twice
    um2.dismiss_result()
    assert not [b for b in um2.public_status()["banners"] if b["kind"] == "result"]


def test_an_old_result_for_another_request_is_ignored(tmp_path):
    um, ev = manager(tmp_path)
    um._set_job("requested", NEWER, "Waiting…", request_id="1111222233334444", requested_at=time.time())
    up = tmp_path / "updates"
    up.mkdir(parents=True, exist_ok=True)
    (up / RESULT).write_text(json.dumps({"request_id": "9999888877776666", "outcome": "installed", "version": "1.0.0"}))
    um.poll_host()
    assert um.job["state"] == "requested" and not ev.system_events(0, time.time() + 1)


def test_updater_that_never_answers_is_reported(tmp_path):
    clock = Clock()
    um, ev = manager(tmp_path, clock=clock)
    um._set_job("requested", NEWER, "Waiting…", request_id="1111222233334444", requested_at=clock.t)
    clock.t += 121
    um.poll_host()
    assert um.job.get("stalled") and "systemctl status networkguru-updater.path" in um.job["message"]


def test_restart_resets_a_download_in_progress(tmp_path):
    um, _ = manager(tmp_path)
    um._set_job("downloading", NEWER, "Downloading…", by="Casey")
    um2, _ = manager(tmp_path)
    assert um2.job["state"] == "failed" and "restart" in um2.job["message"]


def test_upload_runs_the_same_checks(tmp_path):
    um, _ = manager(tmp_path)
    good = make_release_zip(tmp_path / f"NetworkGuru_v{NEWER}.zip", NEWER)
    data, sha = good.read_bytes(), sha_text(good)
    good.unlink()

    async def chunks(b):
        for i in range(0, len(b), 1000):
            yield b[i:i + 1000]

    with pytest.raises(UpdateError, match="checksum"):
        run(um.upload(f"NetworkGuru_v{NEWER}.zip", "1" * 64, chunks(data), "Casey"))
    assert [p.name for p in (tmp_path / "updates").iterdir()] == []
    with pytest.raises(UpdateError, match="NetworkGuru_v"):
        run(um.upload("evil.zip", sha, chunks(data), "Casey"))
    um._set_job("idle")
    assert run(um.upload(f"NetworkGuru_v{NEWER}.zip", sha, chunks(data), "Casey")) == NEWER
    assert json.loads((tmp_path / "updates" / REQUEST).read_text())["version"] == NEWER
