"""Daily update check that ASKS THE ADMIN before anything changes.

    06:00 (site time)  GitHub releases/latest  ->  newer?  ->  banner "Review update" for everyone
    admin: Approve     download zip + .sha256 -> checks -> /data/updates/ + update-request.json
                       the host's systemd updater (deploy/ng_updater.py) rebuilds the image,
                       restarts the app, health-checks it and rolls back on failure, and
                       writes update-result.json, which becomes an event and a banner here
    admin: Remind me tomorrow / Skip this version
    offline site:      the admin uploads the zip + .sha256 on the admin page (same checks)
developer install: (compose.yaml from a git checkout) the check only reminds; it updates with
                   git pull + docker compose up -d --build, so Approve and Upload are refused

The container has no Docker socket: it only drops files into the shared updates folder.
Everything the container writes there is checked again by the host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets as _rand
import shutil
import time
import zipfile
from pathlib import Path
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from . import __version__
from .config import mask, secret
from .daily import DailyJob, next_due
from .github import (ASSET_RE, UpdateError, download, expected_sha256, fetch_text, is_newer, latest_release,
                     latest_version, pick_asset, sha256_file)
from .state import StateFile, write_json_atomic

log = logging.getLogger("networkguru")

TOKEN_ENV = "NETWORKGURU_GITHUB_TOKEN"
REQUEST, STATUS, RESULT = "update-request.json", "update-status.json", "update-result.json"
MAX_ZIP = 300 * 1024 * 1024
MAX_UNPACKED = 800 * 1024 * 1024
MAX_ENTRIES = 5000
PICKUP_S = 120                       # the host updater should start within this
ACTIVE = ("downloading", "verifying", "requested", "installing")
GIT_UPDATE = "This is a developer install from git: update it with git pull, then docker compose up -d --build"
_TOP = re.compile(r"^NetworkGuru_v(\d+\.\d+\.\d+)/")


# ---------------------------------------------------------------------------
# the release zip itself
def verify_release_zip(path: Path, expected_version: str | None = None, current: str = __version__) -> str:
    """The version inside a release zip, after checking it is a complete, newer Network Guru release.
    Raises UpdateError. The host updater repeats these checks before it builds anything."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateError(f"not a zip file ({e})") from None
    with z:
        infos = z.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise UpdateError(f"the zip has {len(infos)} entries - not a Network Guru release")
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise UpdateError("the zip unpacks to more than 800 MB - refused")
        m = _TOP.match(infos[0].filename)
        if not m:
            raise UpdateError("the zip does not contain a NetworkGuru_v<version>/ folder")
        version, top = m.group(1), m.group(0)
        for i in infos:
            name = i.filename
            parts = name.split("/")
            if (not name.startswith(top) or "\\" in name or ".." in parts or name.startswith("/")
                    or re.match(r"^[A-Za-z]:", name)):
                raise UpdateError(f"unsafe path in the zip: {name[:80]}")
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(f"the zip contains a symbolic link: {name[:80]}")
        names = {i.filename for i in infos}
        for need in ("networkguru/__init__.py", "VERSION.txt", "deploy/Dockerfile", "requirements.txt"):
            if top + need not in names:
                raise UpdateError(f"the zip has no {need} - not a complete release")
        init = z.read(top + "networkguru/__init__.py").decode("utf-8", "replace")
        vm = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.MULTILINE)
        vt = re.search(r"^Version\s*:\s*(\S+)", z.read(top + "VERSION.txt").decode("utf-8", "replace"), re.MULTILINE)
        if not vm or vm.group(1) != version or not vt or vt.group(1) != version:
            raise UpdateError(f"the version inside the zip does not match {version}")
        if expected_version and version != expected_version:
            raise UpdateError(f"the zip holds version {version}, not {expected_version}")
        if not is_newer(version, current):
            raise UpdateError(f"version {version} is not newer than the running {current}")
        bad = z.testzip()
        if bad:
            raise UpdateError(f"the zip is damaged ({bad})")
    return version


def _read_json(path: Path, max_bytes: int = 65536) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
class UpdateManager:
    def __init__(self, data_dir: Path, deploy: str, tz: ZoneInfo, settings: StateFile, events=None,
                 clock=time.time):
        self.dir = Path(data_dir) / "updates"
        self.backups = Path(data_dir) / "backups"
        self.deploy, self.tz, self.settings, self.events, self.clock = deploy, tz, settings, events, clock
        self.state = StateFile(Path(data_dir) / "state" / "updates.json",
                               {"last_check": None, "skipped": [], "remind_after": 0, "job": {"state": "idle"},
                                "consumed": [], "result": None, "announced": [], "scheduled_date": None})
        self.task: asyncio.Task | None = None
        job = self.state.get("job") or {}
        if job.get("state") in ("downloading", "verifying"):        # a restart killed it
            self._set_job("failed", job.get("version"), "Interrupted by a restart - approve again.",
                          by=job.get("by"))

    # -- helpers --------------------------------------------------------------
    def _event(self, text: str, level: int = 0, actor: str | None = None) -> None:
        if self.events is not None:
            self.events.add_system_event(self.clock(), "update", mask(text), level, actor)

    def _set_job(self, state: str, version: str | None = None, message: str = "", **extra) -> None:
        job = {"state": state, "version": version, "message": mask(message), "at": self.clock(), **extra}
        self.state.update(job=job)

    @property
    def job(self) -> dict:
        return self.state.get("job") or {"state": "idle"}

    def latest(self) -> dict | None:
        lc = self.state.get("last_check") or {}
        return lc.get("latest")

    def can_install(self) -> bool:
        return self.deploy == "docker"

    def busy(self) -> bool:
        return self.job.get("state") in ACTIVE

    # -- the check --------------------------------------------------------------
    async def check(self, by: str | None = None) -> dict:
        now = self.clock()
        token = secret(TOKEN_ENV) or None              # optional: the repository is public
        lc = {"at": now, "ok": False, "by": by, "latest": self.latest()}
        try:
            rel = await asyncio.to_thread(latest_release, token)
            zip_asset, sha_asset = pick_asset(rel)
        except UpdateError as e:
            lc["message"] = f"Can't check for updates: {mask(str(e))}"
            self.state.update(last_check=lc)
            return lc
        version = latest_version(rel)
        lc.update(ok=True, latest={
            "version": version, "published_at": rel.get("published_at"), "notes": mask(str(rel.get("body") or ""))[:6000],
            "zip": {k: zip_asset.get(k) for k in ("name", "url", "size")},
            "sha": {k: sha_asset.get(k) for k in ("name", "url", "size")}})
        if is_newer(version):
            lc["message"] = f"Version {version} is available (running {__version__})."
            announced = list(self.state.get("announced") or [])
            if version not in announced:
                self._event(f"Network Guru {version} is available - an admin can review it on the dashboard.")
                self.state.update(announced=(announced + [version])[-20:])
        else:
            lc["message"] = f"Up to date (newest release {version})."
        self.state.update(last_check=lc)
        return lc

    def daily_job(self) -> DailyJob:
        async def run(date: str, late: bool) -> None:
            self.state.update(scheduled_date=date)
            await self.check()
        return DailyJob("update-check", self.tz, lambda: self.settings.get("update_check_time", "06:00"),
                        lambda date: self.state.get("scheduled_date") == date, run, catchup_s=20 * 3600,
                        clock=self.clock)

    async def loop(self) -> None:
        lc = self.state.get("last_check") or {}
        if self.clock() - float(lc.get("at") or 0) > 86400:
            await self.check()
        poll = asyncio.create_task(self.watch())
        try:
            await self.daily_job().loop()
        finally:
            poll.cancel()

    # -- banner decisions ---------------------------------------------------------
    def offered(self) -> str | None:
        """The version the banner offers, or None."""
        latest = self.latest()
        if not latest or not is_newer(latest["version"]) or self.busy():
            return None
        if self.job.get("state") == "staged" and self.job.get("version") == latest["version"]:
            return None                                  # already downloaded (a PC run)
        if latest["version"] in (self.state.get("skipped") or []):
            return None
        if self.clock() < float(self.state.get("remind_after") or 0):
            return None
        return latest["version"]

    def remind_tomorrow(self, by: str) -> float:
        until = next_due(self.settings.get("update_check_time", "06:00"), self.tz, self.clock())
        self.state.update(remind_after=until)
        v = (self.latest() or {}).get("version")
        self._event(f"Update {v} postponed until tomorrow's check", 0, by)
        return until

    def skip(self, by: str) -> str | None:
        v = (self.latest() or {}).get("version")
        if v:
            skipped = list(self.state.get("skipped") or [])
            if v not in skipped:
                self.state.update(skipped=(skipped + [v])[-20:])
            self._event(f"Update {v} skipped - the next newer version will be offered again", 0, by)
        return v

    def dismiss_result(self) -> None:
        res = self.state.get("result")
        if res:
            self.state.update(result={**res, "dismissed": True})
        if self.job.get("state") in ("failed", "staged"):
            self._set_job("idle")

    # -- approve: download, verify, stage ----------------------------------------------
    def _free_ok(self, need: int) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.dir).free
        if free < need * 3 + 100 * 1024 * 1024:
            raise UpdateError(f"not enough free disk space for the update ({free / 1048576:.0f} MB free)")

    def approve(self, by: str) -> str:
        latest = self.latest()
        if self.deploy == "compose":
            raise UpdateError(GIT_UPDATE)
        if self.busy():
            raise UpdateError("an update is already in progress")
        if not latest or not is_newer(latest["version"]):
            raise UpdateError("no newer version to install - check for updates first")
        version = latest["version"]
        self._set_job("downloading", version, "Downloading…", by=by)
        self._event(f"Update to {version} approved", 0, by)
        self.task = asyncio.create_task(self._stage_from_github(latest, by))
        return version

    async def _stage_from_github(self, latest: dict, by: str) -> None:
        version = latest["version"]
        zip_path = self.dir / latest["zip"]["name"]
        try:
            if not ASSET_RE.match(latest["zip"]["name"]):
                raise UpdateError("unexpected asset name")
            token = secret(TOKEN_ENV) or None
            await asyncio.to_thread(self._free_ok, int(latest["zip"].get("size") or 0))
            sha_text = await asyncio.to_thread(fetch_text, latest["sha"], token)
            await asyncio.to_thread(download, latest["zip"], zip_path, token, None, 60.0, MAX_ZIP)
            self._set_job("verifying", version, "Checking the download…", by=by)
            await self._verify_and_request(zip_path, sha_text, version, by)
        except UpdateError as e:
            self._fail(zip_path, version, str(e), by)
        except Exception as e:  # noqa: BLE001
            log.exception("update staging failed")
            self._fail(zip_path, version, f"unexpected error ({e})", by)

    async def _verify_and_request(self, zip_path: Path, sha_text: str, version: str | None, by: str) -> str:
        want = expected_sha256(sha_text)
        actual = await asyncio.to_thread(sha256_file, zip_path)
        if actual != want:
            raise UpdateError("checksum mismatch - the file is not the published one")
        found = await asyncio.to_thread(verify_release_zip, zip_path, version)
        (zip_path.with_name(zip_path.name + ".sha256")).write_text(f"{actual}  {zip_path.name}\n", encoding="utf-8",
                                                                   newline="\n")
        self._backup_db(found)
        if not self.can_install():
            self._set_job("staged", found, f"Version {found} is downloaded and checked. Installing needs the "
                          "Docker server (install.sh) - this copy runs on a PC.", by=by, zip=zip_path.name)
            return found
        request_id = _rand.token_hex(8)
        write_json_atomic(self.dir / REQUEST, {
            "request_id": request_id, "version": found, "zip": zip_path.name, "sha256": actual,
            "current": __version__, "requested_by": by, "requested_at": self.clock()})
        self._set_job("requested", found, "Waiting for the server's updater…", by=by, request_id=request_id,
                      requested_at=self.clock())
        self._event(f"Update to {found} handed to the server's updater", 0, by)
        return found

    def _backup_db(self, version: str) -> None:
        if self.events is None:
            return
        try:
            for old in self.backups.glob("networkguru-before-*.db"):
                old.unlink(missing_ok=True)
            self.events.backup_to(self.backups / f"networkguru-before-{version}.db")
        except OSError as e:
            log.warning("database backup before the update failed: %s", e)

    def _fail(self, zip_path: Path | None, version: str | None, message: str, by: str | None) -> None:
        for p in ([zip_path, zip_path.with_name(zip_path.name + ".sha256"),
                   zip_path.with_suffix(zip_path.suffix + ".part")] if zip_path else []):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        self._set_job("failed", version, f"Update not installed: {message}", by=by)
        self._event(f"Update {version or ''} not installed: {message}", 3, by)

    async def upload(self, filename: str, sha_text: str, chunks: AsyncIterator[bytes], by: str) -> str:
        """An update zip uploaded on the admin page (offline sites). Same checks as a download."""
        if self.deploy == "compose":
            raise UpdateError(GIT_UPDATE)
        m = ASSET_RE.match(filename or "")
        if not m:
            raise UpdateError("the file must be a NetworkGuru_v<version>.zip release")
        if self.busy():
            raise UpdateError("an update is already in progress")
        expected_sha256(sha_text)                                  # fail fast on a wrong .sha256 file
        await asyncio.to_thread(self._free_ok, MAX_ZIP // 3)
        part = self.dir / f".upload-{_rand.token_hex(4)}.part"
        dest = self.dir / filename
        size = 0
        try:
            with open(part, "wb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_ZIP:
                        raise UpdateError("the upload is larger than 300 MB")
                    f.write(chunk)
            os.replace(part, dest)
            self._set_job("verifying", m.group(1), "Checking the uploaded file…", by=by)
            self._event(f"Update zip {filename} uploaded", 0, by)
            return await self._verify_and_request(dest, sha_text, m.group(1), by)
        except UpdateError as e:
            part.unlink(missing_ok=True)
            self._fail(dest, m.group(1), str(e), by)
            raise
        finally:
            part.unlink(missing_ok=True)

    # -- progress and results from the host -------------------------------------------
    async def watch(self, every: float = 10.0) -> None:
        while True:
            try:
                self.poll_host()
            except Exception:  # noqa: BLE001
                log.exception("reading the updater's files failed")
            await asyncio.sleep(every)

    def poll_host(self) -> None:
        job = self.job
        rid = job.get("request_id")
        consumed = list(self.state.get("consumed") or [])
        res = _read_json(self.dir / RESULT)
        if res and res.get("request_id") and res["request_id"] not in consumed:
            if not rid or res["request_id"] == rid:
                self._take_result(res, consumed)
                return
        if job.get("state") not in ("requested", "installing") or not rid:
            return
        st = _read_json(self.dir / STATUS)
        if st and st.get("request_id") == rid and job.get("state") == "requested":
            self.state.update(job={**job, "state": "installing", "message": mask(str(st.get("message") or
                                   "Installing - the dashboard restarts and reconnects by itself."))})
        elif job.get("state") == "requested" and self.clock() - float(job.get("requested_at") or 0) > PICKUP_S \
                and not job.get("stalled"):
            self.state.update(job={**job, "stalled": True, "message": (
                "The server's updater has not picked up the update after 2 minutes. On the server run: "
                "systemctl status networkguru-updater.path")})
            self._event("The server's updater did not pick up the update request", 3, job.get("by"))

    def _take_result(self, res: dict, consumed: list[str]) -> None:
        outcome = str(res.get("outcome") or "failed")
        version, previous = res.get("version"), res.get("previous")
        msg = mask(str(res.get("message") or ""))[:800]
        text = {
            "installed": f"Updated to {version}" + (f" (was {previous})" if previous else ""),
            "rolled_back": f"Update to {version} failed - rolled back to {previous}: {msg}",
            "needs_install": f"Update to {version} needs install.sh on the server: {msg}",
        }.get(outcome, f"Update to {version} not installed ({outcome}): {msg}")
        level = 0 if outcome == "installed" else 4 if outcome == "rolled_back" else 3
        self.state.update(result={"request_id": res["request_id"], "version": version, "previous": previous,
                                  "outcome": outcome, "message": msg, "text": text, "warnings": res.get("warnings") or [],
                                  "at": res.get("finished_at") or self.clock(), "dismissed": False},
                          consumed=(consumed + [res["request_id"]])[-30:])
        job = self.job
        if job.get("request_id") == res["request_id"]:
            self._set_job("idle")
        self._event(text, level, job.get("by"))

    # -- what the pages show -----------------------------------------------------------
    def public_status(self) -> dict:
        lc = self.state.get("last_check") or {}
        latest = self.latest()
        job = dict(self.job)
        res = self.state.get("result")
        banners = []
        state = job.get("state")
        if state in ("requested", "installing"):
            banners.append({"level": 3 if job.get("stalled") else 0, "kind": "installing",
                            "text": job.get("message") or f"Installing {job.get('version')}…"})
        elif state in ("downloading", "verifying"):
            banners.append({"level": 0, "kind": "working", "text": f"Update {job.get('version')}: {job.get('message')}"})
        elif state in ("failed", "staged"):
            banners.append({"level": 3 if state == "failed" else 0, "kind": state, "text": job.get("message")})
        if res and not res.get("dismissed"):
            fresh = self.clock() - float(res.get("at") or 0) < 86400
            if res.get("outcome") != "installed" or fresh:
                banners.append({"level": 0 if res.get("outcome") == "installed" else 4, "kind": "result",
                                "text": res.get("text")})
        offer = self.offered()
        if offer:
            banners.append({"level": 0, "kind": "available", "version": offer,
                            "text": f"Network Guru {offer} is available."})
        return {
            "current": __version__, "can_install": self.can_install(), "deploy": self.deploy,
            "token_set": bool(secret(TOKEN_ENV)),
            "check": {"at": lc.get("at"), "ok": lc.get("ok"), "message": lc.get("message")},
            "latest": ({k: latest.get(k) for k in ("version", "published_at", "notes")} | {
                "size": (latest.get("zip") or {}).get("size"), "newer": is_newer(latest["version"])} if latest else None),
            "offered": offer, "skipped": self.state.get("skipped") or [],
            "remind_after": self.state.get("remind_after") or 0,
            "next_check": next_due(self.settings.get("update_check_time", "06:00"), self.tz, self.clock()),
            "job": job, "result": res, "banners": banners,
        }

