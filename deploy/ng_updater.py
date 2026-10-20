#!/usr/bin/env python3
"""
Network Guru host updater (runs as root on the site server, NOT in the container)
==================================================================================
Installed by install.sh to /opt/networkguru/bin/ng_updater.py.

    ng_updater.py run                        systemd: networkguru-updater.path saw update-request.json
    ng_updater.py install-local --src DIR    install.sh: build this release, check it, switch to it
    ng_updater.py rollback                   swap NG_VERSION and NG_PREVIOUS by hand
    ng_updater.py hash-password              admin password on stdin -> hash for secrets.env

How an approved update is installed
  1. The app (container) drops NetworkGuru_v<ver>.zip, its .sha256 and update-request.json into
     /var/lib/networkguru/updates/ - the only folder shared with the container. Nothing it wrote
     there is trusted: the request is renamed first, read without following links, and the zip
     is copied into a root-only folder and checked again (checksum, paths, version, newer).
  2. The image is built from /opt/networkguru/Dockerfile (never the zip's own), offline
     (--network none). If the builder refuses, the same steps run in a plain container and
     are committed as the image.
  3. .env gets NG_PREVIOUS=<running> and NG_VERSION=<new>; `docker compose up -d --no-deps app`.
  4. /api/health must answer ok with the new version 3 times in a row within 60 s. Otherwise
     NG_VERSION goes back, the old version is started again and the failed image is removed.
  5. update-result.json tells the dashboard what happened (an event and a banner).

compose.yaml, the Dockerfile and these units always come from /opt/networkguru (install.sh),
so an update can never change mounts, capabilities or build steps. A release whose
deploy/HOST_VERSION number is higher than the installed one needs install.sh first.
Never `docker compose down -v`: the volumes hold the history and the event log.

Standard library only, Python 3.10+ (Ubuntu 22.04 and 24.04 ship it).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

BASE = Path("/opt/networkguru")
UPDATES = Path("/var/lib/networkguru/updates")
LOCK = Path("/run/networkguru-updater.lock")
APP_UID = 10001
IMAGE = "networkguru-app"
REQUEST, STATUS, RESULT = "update-request.json", "update-status.json", "update-result.json"
PROCESSING = "update-request.processing"
MAX_REQUEST = 4096
MAX_ZIP = 300 * 1024 * 1024
MAX_UNPACKED = 800 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_AGE_S = 24 * 3600
HEALTH_S = 60
HEALTH_EVERY_S = 3
HEALTH_STREAK = 3
KEEP = 2                                    # releases / images kept: current and previous
HOST_FILES = ["compose.yaml", "Dockerfile", "ng_updater.py", "networkguru-updater.path",
              "networkguru-updater.service"]
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_TOP = re.compile(r"^NetworkGuru_v(\d+\.\d+\.\d+)/")

# The builder-free fallback: the Dockerfile's steps in a plain container, then `docker commit`.
FALLBACK_SCRIPT = (
    "set -e; "
    "pip install --no-index --find-links /src/deploy/wheels --require-hashes -r /src/deploy/requirements.lock; "
    "groupadd --gid 10001 networkguru; "
    "useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin networkguru; "
    "mkdir -p /data/updates /app; chown -R 10001:10001 /data; "
    "cp -r /src/networkguru /app/networkguru; cp /src/VERSION.txt /app/VERSION.txt; "
    "find /app -name __pycache__ -prune -exec rm -rf {} +"
)
FALLBACK_CHANGES = [
    "USER 10001:10001", "WORKDIR /app", "EXPOSE 8080",
    "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 NETWORKGURU_DATA=/data NETWORKGURU_DEPLOY=docker",
    'CMD ["python", "-m", "networkguru", "serve", "--inventory", "/site/inventory.yaml"]',
]

# admin password hash - the same format as networkguru/auth.py (a test checks both agree)
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_MAXMEM = 32768, 8, 1, 64 * 1024 * 1024


class Refused(Exception):
    """The request or the zip is not acceptable; nothing was changed."""


class Failed(Exception):
    """Building failed; the running version was not touched."""


def version_tuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v or "")) or (0,)


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                        maxmem=SCRYPT_MAXMEM)
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()}"


def host_digest(folder: Path) -> str:
    """sha256 over the server-side files (deploy/HOST_VERSION records it; a test keeps it honest)."""
    h = hashlib.sha256()
    for name in HOST_FILES:
        h.update(name.encode() + b"\0" + (Path(folder) / name).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def host_version(path: Path) -> int:
    try:
        return int(Path(path).read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# reading what the container wrote, without trusting it
def read_untrusted(path: Path, max_bytes: int) -> bytes:
    """A regular file's bytes. Refuses links, FIFOs, devices and anything too large."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{path.name} is not a regular file")
    if st.st_size > max_bytes:
        raise Refused(f"{path.name} is larger than {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
            raise Refused(f"{path.name} changed while it was read")
        chunks, total = [], 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            total += len(b)
            if total > max_bytes:
                raise Refused(f"{path.name} is larger than {max_bytes} bytes")
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def copy_untrusted(src: Path, dest: Path, max_bytes: int) -> str:
    """Copy a regular file (no links) into a root-only folder; returns its sha256."""
    st = os.lstat(src)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{src.name} is not a regular file")
    if st.st_size > max_bytes:
        raise Refused(f"{src.name} is larger than {max_bytes // 1048576} MB")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(src, flags)
    h = hashlib.sha256()
    total = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{src.name} is not a regular file")
        with open(dest, "wb") as out:
            while True:
                b = os.read(fd, 1 << 20)
                if not b:
                    break
                total += len(b)
                if total > max_bytes:
                    raise Refused(f"{src.name} is larger than {max_bytes // 1048576} MB")
                h.update(b)
                out.write(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def verify_zip(path: Path, expected_version: str, current: str | None) -> str:
    """Same checks as the app's verify_release_zip, repeated here because the app is not trusted."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise Refused(f"not a zip file ({e})") from None
    with z:
        infos = z.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise Refused("not a Network Guru release (entry count)")
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise Refused("the zip unpacks to more than 800 MB")
        m = _TOP.match(infos[0].filename)
        if not m or m.group(1) != expected_version:
            raise Refused(f"the zip does not hold NetworkGuru_v{expected_version}/")
        top = m.group(0)
        for i in infos:
            n = i.filename
            if (not n.startswith(top) or "\\" in n or ".." in n.split("/") or n.startswith("/")
                    or re.match(r"^[A-Za-z]:", n)):
                raise Refused(f"unsafe path in the zip: {n[:80]}")
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise Refused(f"the zip contains a symbolic link: {n[:80]}")
        names = {i.filename for i in infos}
        for need in ("networkguru/__init__.py", "VERSION.txt", "deploy/Dockerfile", "deploy/HOST_VERSION",
                     "deploy/requirements.lock"):
            if top + need not in names:
                raise Refused(f"the zip has no {need}")
        init = z.read(top + "networkguru/__init__.py").decode("utf-8", "replace")
        vm = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.MULTILINE)
        vt = re.search(r"^Version\s*:\s*(\S+)", z.read(top + "VERSION.txt").decode("utf-8", "replace"), re.MULTILINE)
        if not vm or vm.group(1) != expected_version or not vt or vt.group(1) != expected_version:
            raise Refused(f"the version inside the zip is not {expected_version}")
        if current and version_tuple(expected_version) <= version_tuple(current):
            raise Refused(f"{expected_version} is not newer than the running {current}")
        if z.testzip():
            raise Refused("the zip is damaged")
    return top


def extract(zip_path: Path, top: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for i in z.infolist():
            rel = i.filename[len(top):]
            if not rel:
                continue
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise Refused(f"unsafe path in the zip: {i.filename[:80]}")
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


# ---------------------------------------------------------------------------
class Runner:
    """Runs commands; tests replace it with a fake."""

    def run(self, args: list[str], timeout: float = 600, env: dict | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout:.0f} s"
        except OSError as e:
            return 127, str(e)
        return p.returncode, (p.stdout or "") + (p.stderr or "")


def http_get_json(url: str, timeout: float = 4.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # never via a proxy
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read(65536).decode("utf-8"))


def tail(text: str, lines: int = 30) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


class Updater:
    def __init__(self, base: Path = BASE, updates: Path = UPDATES, runner: Runner | None = None,
                 http_get: Callable[[str], dict] = http_get_json, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, uid: int = APP_UID):
        self.base, self.updates = Path(base), Path(updates)
        self.runner = runner or Runner()
        self.http_get, self.clock, self.sleep, self.uid = http_get, clock, sleep, uid

    # -- .env (no secrets in it) ---------------------------------------------------
    def read_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        p = self.base / ".env"
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        return out

    def write_env(self, env: dict[str, str]) -> None:
        text = "# Written by install.sh / ng_updater.py - no secrets here (they are in secrets/secrets.env)\n"
        text += "".join(f"{k}={v}\n" for k, v in env.items())
        fd, tmp = tempfile.mkstemp(dir=self.base, prefix=".env.")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.base / ".env")

    def compose(self, *args: str) -> list[str]:
        return ["docker", "compose", "--project-directory", str(self.base), "-f", str(self.base / "compose.yaml"),
                *args]

    # -- files for the dashboard -----------------------------------------------------
    def write_shared(self, name: str, data: dict) -> None:
        """Atomic, owned by the app user, never following a link the container may have planted."""
        self.updates.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.updates, prefix=".ng-")
        try:
            if hasattr(os, "fchown") and hasattr(os, "geteuid") and os.geteuid() == 0:
                os.fchown(fd, self.uid, self.uid)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.updates / name)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def status(self, rid: str | None, state: str, message: str) -> None:
        self.write_shared(STATUS, {"request_id": rid, "state": state, "message": message, "at": self.clock()})
        log(f"[{state}] {message}")

    # -- the systemd entry point -------------------------------------------------------
    def process(self) -> int:
        stale = self.updates / PROCESSING
        if os.path.lexists(stale):                         # a reboot or crash in the middle of an update
            rid = None
            try:
                rid = json.loads(read_untrusted(stale, MAX_REQUEST)).get("request_id")
            except (Refused, OSError, ValueError, AttributeError):
                pass
            self._finish(stale, False)
            self.write_shared(RESULT, {"request_id": rid, "outcome": "interrupted", "finished_at": self.clock(),
                                       "message": "The server restarted during the update. The version in .env "
                                                  "is running; approve the update again."})
        req_path = self.updates / REQUEST
        if not os.path.lexists(req_path):
            return 0
        os.replace(req_path, stale)                         # rename first: the path unit must not fire again
        ok = False
        rid = None
        result: dict = {}
        try:
            req = self.validate(json.loads(read_untrusted(stale, MAX_REQUEST)))
            rid = req["request_id"]
            result = self.install_request(req)
            ok = result.get("outcome") == "installed"
        except Refused as e:
            result = {"outcome": "rejected", "message": str(e)}
        except (ValueError, TypeError, AttributeError) as e:
            result = {"outcome": "rejected", "message": f"unreadable request ({e})"}
        except Exception as e:  # noqa: BLE001 - always answer the dashboard
            result = {"outcome": "failed", "message": f"updater error: {e}"}
        finally:
            self._finish(stale, ok)
        result.update(request_id=rid, finished_at=self.clock())
        self.write_shared(RESULT, result)
        log(f"result: {result.get('outcome')} - {result.get('message', '')}")
        return 0          # the outcome goes to the dashboard; a failed unit would only stop the path unit

    def _finish(self, path: Path, ok: bool) -> None:
        try:
            os.replace(path, path.with_name("update-request." + ("done" if ok else "rejected")))
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass

    def validate(self, req: object) -> dict:
        if not isinstance(req, dict):
            raise Refused("the request is not a JSON object")
        rid, ver, zip_name, sha = req.get("request_id"), req.get("version"), req.get("zip"), req.get("sha256")
        if not (isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{8,32}", rid)):
            raise Refused("bad request id")
        if not (isinstance(ver, str) and _VERSION.match(ver)):
            raise Refused("bad version")
        if zip_name != f"NetworkGuru_v{ver}.zip":
            raise Refused("the zip name does not match the version")
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
            raise Refused("bad checksum in the request")
        at = req.get("requested_at")
        if not isinstance(at, (int, float)) or not (-300 <= self.clock() - at <= MAX_AGE_S):
            raise Refused("the request is older than 24 hours - approve it again")
        current = self.read_env().get("NG_VERSION")
        if current and version_tuple(ver) <= version_tuple(current):
            raise Refused(f"{ver} is not newer than the running {current}")
        return {"request_id": rid, "version": ver, "zip": zip_name, "sha256": sha,
                "requested_by": str(req.get("requested_by") or "")[:40]}

    # -- install an approved request --------------------------------------------------
    def install_request(self, req: dict) -> dict:
        rid, ver = req["request_id"], req["version"]
        current = self.read_env().get("NG_VERSION", "")
        self.status(rid, "installing", f"Installing {ver} - the dashboard restarts and reconnects by itself.")
        work = self.base / "releases" / f".work-{rid}"
        staged = [self.updates / req["zip"], self.updates / (req["zip"] + ".sha256")]
        try:
            self.free_space(MAX_ZIP)
            work.mkdir(parents=True, exist_ok=True)
            os.chmod(work, 0o700)
            zip_copy = work / req["zip"]
            got = copy_untrusted(self.updates / req["zip"], zip_copy, MAX_ZIP)
            sha_text = read_untrusted(self.updates / (req["zip"] + ".sha256"), 4096).decode("utf-8", "replace")
            m = re.search(r"\b([0-9a-fA-F]{64})\b", sha_text)
            if got != req["sha256"] or not m or m.group(1).lower() != got:
                raise Refused("checksum mismatch - the zip is not the one the dashboard checked")
            top = verify_zip(zip_copy, ver, current or None)
            dest = self.base / "releases" / ver
            extract(zip_copy, top, dest)
            need = host_version(dest / "deploy" / "HOST_VERSION")
            have = host_version(self.base / "HOST_VERSION")
            if need > have:
                return {"outcome": "needs_install", "version": ver, "previous": current,
                        "message": f"This release changes the server's own files (host version {need}, installed "
                                   f"{have}). On the server: cd {dest} && sudo bash deploy/install.sh"}
            self.status(rid, "building", f"Building {ver} (a few minutes on a slow server)…")
            self.build(ver, dest)
            self.status(rid, "switching", f"Starting {ver}…")
            return self.switch(ver, current)
        except Failed as e:
            return {"outcome": "failed", "version": ver, "previous": current, "message": str(e)}
        finally:
            shutil.rmtree(work, ignore_errors=True)
            for p in staged:
                try:
                    if os.path.lexists(p):
                        os.unlink(p)
                except OSError:
                    pass

    def free_space(self, need: int) -> None:
        for where, want in ((self.base, need * 4 + 300 * 1024 * 1024),):
            free = shutil.disk_usage(where).free
            if free < want:
                raise Failed(f"not enough disk space in {where} ({free // 1048576} MB free)")
        docker_root = Path("/var/lib/docker")
        if docker_root.is_dir() and shutil.disk_usage(docker_root).free < 1024 * 1024 * 1024:
            raise Failed("less than 1 GB free for Docker (/var/lib/docker)")

    def base_image(self) -> str:
        text = (self.base / "Dockerfile").read_text(encoding="utf-8")
        m = re.search(r"^FROM\s+(\S+)", text, re.MULTILINE)
        if not m:
            raise Failed("the installed Dockerfile has no FROM line")
        return m.group(1)

    def build(self, ver: str, src: Path) -> str:
        tag = f"{IMAGE}:{ver}"
        base_image = self.base_image()
        rc, _ = self.runner.run(["docker", "image", "inspect", base_image], timeout=60)
        if rc != 0:
            raise Failed(f"the base image {base_image} is not on this server - run install.sh with the new images")
        rc, out = self.runner.run(["docker", "build", "--network", "none", "--label", f"networkguru.version={ver}",
                                   "-f", str(self.base / "Dockerfile"), "-t", tag, str(src)], timeout=1800)
        if rc == 0:
            return tag
        log("docker build failed - trying the builder-free fallback:\n" + tail(out, 15))
        name = f"ng-build-{ver}"
        self.runner.run(["docker", "rm", "-f", name], timeout=60)
        rc2, out2 = self.runner.run(["docker", "run", "--name", name, "--network", "none", "--user", "0:0",
                                     "-v", f"{src}:/src:ro", base_image, "sh", "-c", FALLBACK_SCRIPT], timeout=1800)
        if rc2 == 0:
            changes = [a for c in FALLBACK_CHANGES + [f"LABEL networkguru.version={ver}"] for a in ("--change", c)]
            rc2, out2 = self.runner.run(["docker", "commit", *changes, name, tag], timeout=600)
        self.runner.run(["docker", "rm", "-f", name], timeout=60)
        if rc2 != 0:
            raise Failed(f"building {ver} failed:\n{tail(out, 10)}\n{tail(out2, 10)}")
        return tag

    def health(self, version: str) -> tuple[bool, str, list[str]]:
        flag = self.base / "test-fail-next-update"
        if flag.exists():
            flag.unlink()
            return False, "forced to fail by test-fail-next-update (hardware test)", []
        port = self.read_env().get("NG_PORT") or "8080"
        deadline = self.clock() + HEALTH_S
        streak, last, warnings = 0, "no answer yet", []
        while self.clock() < deadline:
            try:
                h = self.http_get(f"http://127.0.0.1:{port}/api/health")
                if h.get("version") == version and h.get("ok"):
                    streak += 1
                    if h.get("tsdb") == "memory":
                        warnings = ["History is kept in memory only - VictoriaMetrics is not answering."]
                    if streak >= HEALTH_STREAK:
                        return True, "healthy", warnings
                else:
                    streak = 0
                    last = f"answers as {h.get('version')} with ok={h.get('ok')} {h.get('dead_tasks') or ''}".strip()
            except Exception as e:  # noqa: BLE001
                streak = 0
                last = f"no answer ({type(e).__name__})"
            self.sleep(HEALTH_EVERY_S)
        return False, f"not healthy within {HEALTH_S} s: {last}", warnings

    def switch(self, new: str, current: str) -> dict:
        env = self.read_env()
        before = dict(env)
        env["NG_PREVIOUS"], env["NG_VERSION"] = current, new
        self.write_env(env)
        rc, out = self.runner.run(self.compose("up", "-d", "--no-deps", "app"), timeout=300)
        ok, why, warnings = (False, f"compose up failed: {tail(out, 5)}", []) if rc != 0 else self.health(new)
        if ok:
            self.prune({new, current})
            return {"outcome": "installed", "version": new, "previous": current, "warnings": warnings,
                    "message": f"{new} is running and healthy."}
        _, logs = self.runner.run(self.compose("logs", "--no-color", "--tail", "30", "app"), timeout=60)
        if not current:
            return {"outcome": "failed", "version": new, "previous": "", "message": f"{why} (first install: left "
                    "running for a look - docker compose logs app)", "log_tail": tail(logs)}
        self.write_env(before)
        self.runner.run(self.compose("up", "-d", "--no-deps", "app"), timeout=300)
        ok2, why2, _ = self.health(current)
        self.runner.run(["docker", "image", "rm", f"{IMAGE}:{new}"], timeout=120)
        shutil.rmtree(self.base / "releases" / new, ignore_errors=True)
        if not ok2:
            return {"outcome": "failed", "version": new, "previous": current, "log_tail": tail(logs),
                    "message": f"{why}; going back to {current} did not come up healthy either ({why2}) - "
                               "look at: docker compose logs app"}
        return {"outcome": "rolled_back", "version": new, "previous": current, "message": why, "log_tail": tail(logs)}

    def prune(self, keep: set[str]) -> None:
        """Only our own old tags and dangling layers of our builds - never the base image."""
        keep = {k for k in keep if k}
        rc, out = self.runner.run(["docker", "image", "ls", IMAGE, "--format", "{{.Tag}}"], timeout=60)
        if rc == 0:
            for tag in out.split():
                if _VERSION.match(tag) and tag not in keep:
                    self.runner.run(["docker", "image", "rm", f"{IMAGE}:{tag}"], timeout=120)
        self.runner.run(["docker", "image", "prune", "-f", "--filter", "label=networkguru.version"], timeout=300)
        rel = self.base / "releases"
        if rel.is_dir():
            for d in rel.iterdir():
                if d.is_dir() and _VERSION.match(d.name) and d.name not in keep:
                    shutil.rmtree(d, ignore_errors=True)

    # -- install.sh and by hand ----------------------------------------------------------
    def install_local(self, src: Path, rebuild: bool = False) -> int:
        init = (src / "networkguru" / "__init__.py").read_text(encoding="utf-8")
        ver = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.MULTILINE).group(1)
        env = self.read_env()
        current = env.get("NG_VERSION", "")
        tag = f"{IMAGE}:{ver}"
        dest = self.base / "releases" / ver
        if src.resolve() != dest.resolve():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        have, _ = self.runner.run(["docker", "image", "inspect", tag], timeout=60)
        try:
            if have != 0 or rebuild:
                log(f"Building {tag} …")
                self.build(ver, dest)
        except Failed as e:
            log(f"ERROR: {e}")
            return 1
        rc, out = self.runner.run(["docker", "run", "--rm", "--network", "none", "--user", "10001:10001",
                                   "-e", f"NG_SUBNET={env.get('NG_SUBNET') or '172.29.250.0/24'}",
                                   "-v", f"{self.base / 'site'}:/site:ro", tag, "python", "-m", "networkguru",
                                   "check-inventory", "/site/inventory.yaml"], timeout=120)
        print(out.strip())
        if rc != 0:
            log("ERROR: the site inventory has problems - nothing was switched.")
            return 1
        if not current:
            env.update(NG_VERSION=ver, NG_PREVIOUS="")
            self.write_env(env)
            rc, out = self.runner.run(self.compose("up", "-d"), timeout=600)
            if rc != 0:
                log("ERROR: docker compose up failed:\n" + tail(out, 20))
                return 1
            ok, why, warnings = self.health(ver)
            for w in warnings:
                log("warning: " + w)
            log(f"{ver} is running and healthy." if ok else f"ERROR: {why} - look at: docker compose logs app")
            return 0 if ok else 1
        if ver == current:
            rc, out = self.runner.run(self.compose("up", "-d"), timeout=600)
            ok, why, _ = self.health(ver) if rc == 0 else (False, tail(out, 10), [])
            log(f"{ver} is running and healthy." if ok else f"ERROR: {why}")
            return 0 if ok else 1
        self.runner.run(self.compose("up", "-d", "victoriametrics"), timeout=300)
        res = self.switch(ver, current)
        log(f"{res['outcome']}: {res['message']}")
        if res.get("log_tail") and res["outcome"] != "installed":
            print(res["log_tail"])
        return 0 if res["outcome"] == "installed" else 1

    def rollback(self) -> int:
        env = self.read_env()
        cur, prev = env.get("NG_VERSION", ""), env.get("NG_PREVIOUS", "")
        if not prev:
            log("No previous version is kept - nothing to roll back to.")
            return 1
        rc, _ = self.runner.run(["docker", "image", "inspect", f"{IMAGE}:{prev}"], timeout=60)
        if rc != 0:
            log(f"The image for {prev} is gone - nothing to roll back to.")
            return 1
        env["NG_VERSION"], env["NG_PREVIOUS"] = prev, cur
        self.write_env(env)
        self.runner.run(self.compose("up", "-d", "--no-deps", "app"), timeout=300)
        ok, why, _ = self.health(prev)
        log(f"Rolled back to {prev}." if ok else f"Rolled back to {prev}, but: {why}")
        return 0 if ok else 1


def _locked(fn: Callable[[], int]) -> int:
    if os.environ.get("NG_LOCK_HELD") == "1":               # install.sh already holds the lock
        return fn()
    try:
        import fcntl
    except ImportError:                                     # not Linux (tests on Windows)
        return fn()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fn()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Network Guru host updater (root, on the site server)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="process update-request.json (systemd)")
    il = sub.add_parser("install-local", help="build and switch to an unpacked release (install.sh)")
    il.add_argument("--src", required=True)
    il.add_argument("--rebuild", action="store_true")
    sub.add_parser("rollback", help="go back to NG_PREVIOUS")
    sub.add_parser("hash-password", help="admin password on stdin -> hash")
    hd = sub.add_parser("host-digest", help="digest of the server-side files in a folder")
    hd.add_argument("folder")
    args = ap.parse_args(argv)
    if args.cmd == "hash-password":
        pw = sys.stdin.readline().rstrip("\r\n")
        if len(pw) < 10:
            print("The admin password needs at least 10 characters.", file=sys.stderr)
            return 1
        print(hash_password(pw))
        return 0
    if args.cmd == "host-digest":
        print(host_digest(Path(args.folder)))
        return 0
    u = Updater()
    if args.cmd == "run":
        return _locked(u.process)
    if args.cmd == "install-local":
        return _locked(lambda: u.install_local(Path(args.src), args.rebuild))
    return _locked(u.rollback)


if __name__ == "__main__":
    sys.exit(main())
