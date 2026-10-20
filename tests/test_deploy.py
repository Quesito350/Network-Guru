"""The server-side files. install.sh and systemd cannot run on this PC (no Docker / WSL), so:
static checks of compose.yaml, the Dockerfile and install.sh, plus the host updater's logic
against a fake Docker. deploy/HARDWARE_TEST.md covers the real server."""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from .conftest import ROOT, make_release_zip, sha_text

DEPLOY = ROOT / "deploy"


def load_updater():
    spec = importlib.util.spec_from_file_location("ng_updater_test", DEPLOY / "ng_updater.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


U = load_updater()


# ---------------------------------------------------------------------------
# static checks
def test_compose_file():
    c = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
    assert c["name"] == "networkguru"
    app, vm = c["services"]["app"], c["services"]["victoriametrics"]
    for svc in (app, vm):
        assert svc["restart"] == "unless-stopped"
        assert svc["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svc["security_opt"]
        assert svc["logging"]["options"]["max-size"]
    assert app["image"].startswith("networkguru-app:${NG_VERSION:?") and app["pull_policy"] == "never"
    assert app["user"] == "10001:10001" and app["read_only"] is True
    assert app["sysctls"]["net.ipv4.ping_group_range"] == "0 2147483647"
    assert app["ports"] == ["0.0.0.0:${NG_PORT:-8080}:8080"]
    assert all(p.startswith("127.0.0.1:") for p in vm["ports"])           # VictoriaMetrics: this server only
    assert "-retentionPeriod=31d" in vm["command"] and "-storageDataPath=/storage" in vm["command"]
    assert c["networks"]["ng"]["ipam"]["config"][0]["subnet"] == "${NG_SUBNET:-172.29.250.0/24}"
    assert set(c["volumes"]) == {"appdata", "vmdata"}
    assert "appdata:/data" in app["volumes"] and "vmdata:/storage" in vm["volumes"]
    assert "./secrets:/run/networkguru:ro" in app["volumes"] and "./site:/site:ro" in app["volumes"]
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "docker.sock" not in text and "privileged" not in text and "env_file" not in text


def test_dockerfile():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert not text.startswith("# syntax") and "--mount" not in "\n".join(lines)
    assert "--no-index" in text and "--require-hashes" in text and "deploy/requirements.lock" in text
    assert "USER 10001:10001" in text and "chown -R 10001:10001 /data" in text
    from_line = next(ln for ln in lines if ln.startswith("FROM "))
    fetch = (ROOT / "tools" / "fetch_images.py").read_text(encoding="utf-8")
    assert from_line.split()[1] in fetch                                # the base image is in the offline bundle
    for needle in ("--require-hashes", "10001", "/data/updates"):       # the fallback does the same steps
        assert needle in U.FALLBACK_SCRIPT


# ---------------------------------------------------------------------------
# the developer install from git: compose.yaml at the top of the repository
def test_dev_compose_has_the_server_lockdown():
    dev = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    prod = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
    assert dev["name"] == "networkguru-dev" != prod["name"]
    app, vm = dev["services"]["app"], dev["services"]["victoriametrics"]
    papp, pvm = prod["services"]["app"], prod["services"]["victoriametrics"]
    for key in ("user", "read_only", "tmpfs", "cap_drop", "security_opt", "sysctls", "healthcheck", "logging",
                "restart", "ports", "networks"):
        assert app[key] == papp[key], key
    for key in ("image", "command", "volumes", "cap_drop", "security_opt", "restart", "stop_grace_period", "logging"):
        assert vm[key] == pvm[key], key
    assert "ports" not in vm                         # never clashes with an install.sh install's 127.0.0.1:8428
    assert app["build"] == {"context": ".", "dockerfile": "deploy/Dockerfile.dev"} and app["pull_policy"] == "build"
    assert (ROOT / app["build"]["dockerfile"]).is_file()
    env = app["environment"]
    assert env["NETWORKGURU_DEPLOY"] == "compose" and env["NETWORKGURU_SECRETS"] == "/run/networkguru/secrets.env"
    assert env["NETWORKGURU_VM_URL"] == papp["environment"]["NETWORKGURU_VM_URL"]
    subnet = dev["networks"]["ng"]["ipam"]["config"][0]["subnet"]
    assert subnet == env["NG_SUBNET"] != prod["networks"]["ng"]["ipam"]["config"][0]["subnet"]
    assert "appdata:/data" in app["volumes"] and "./dev-secrets:/run/networkguru:ro" in app["volumes"]
    site = [v for v in app["volumes"] if v.endswith(":/site/inventory.yaml:ro")]
    assert len(site) == 1 and "./examples/inventory.example.yaml" in site[0]
    assert (ROOT / "examples" / "inventory.example.yaml").is_file()
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "docker.sock" not in text and "privileged" not in text and "/var/lib/networkguru" not in text
    assert "env_file" not in text and not re.search(r"down\s+(-\w*v|--volumes)", text)
    names = set(re.findall(r"\$\{(NG_\w+)", text))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert names == set(re.findall(r"^(NG_\w+)=", example, re.MULTILINE))   # every setting is explained


def _code(text: str) -> list[str]:
    return [re.sub(r"^(RUN |&& )", "", ln.strip()).rstrip(" \\") for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


def test_dev_dockerfile_builds_the_server_image_from_pypi():
    dev, prod = (DEPLOY / "Dockerfile.dev").read_text(encoding="utf-8"), (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    d, p = _code(dev), _code(prod)
    for start in ("FROM ", "USER ", "WORKDIR ", "EXPOSE ", "CMD ", "COPY networkguru ", "COPY deploy/requirements.lock "):
        assert [ln for ln in d if ln.startswith(start)] == [ln for ln in p if ln.startswith(start)], start
    for ln in p:                                          # the same install, account and folders
        if ln.startswith(("pip install", "rm -rf", "groupadd", "useradd", "mkdir", "chown")):
            assert ln in d, ln
    assert not any("wheels" in ln for ln in d if ln.startswith("COPY")) and not any("VERSION.txt" in ln for ln in d)
    assert "NETWORKGURU_DEPLOY=compose" in dev and not dev.startswith("# syntax") and "--mount" not in "\n".join(d)
    spec = importlib.util.spec_from_file_location("fetch_wheels_dev", ROOT / "tools" / "fetch_wheels.py")
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    assert re.findall(r"--platform (\S+)", "\n".join(d)) == fw.PLATFORMS      # exactly the release's wheels ...
    assert f"--python-version {fw.PY}" in dev and "--only-binary=:all:" in dev
    download = next(ln for ln in d if ln.startswith("pip download"))
    assert "--require-hashes" in download and "--no-deps" in download     # ... checked against the lock's hashes
    assert "-r /tmp/requirements.lock" in dev
    ignore = [ln.strip() for ln in (DEPLOY / "Dockerfile.dev.dockerignore").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    assert ignore[0] == "*" and {ln for ln in ignore if ln.startswith("!")} == {"!networkguru", "!deploy/requirements.lock"}


def test_dev_install_files_stay_out_of_git():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = lambda p: subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT).returncode == 0  # noqa: E731
    assert ignored(".env") and ignored("dev-secrets/secrets.env")
    assert not ignored(".env.example") and not ignored("compose.yaml") and not ignored("deploy/Dockerfile.dev")
    assert "NG_" in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"(TOKEN|PASSWORD|SECRET|WEBHOOK)\w*=", (ROOT / ".env.example").read_text(encoding="utf-8"))


def test_install_script():
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert not re.search(r"down\s+(-\w*v|--volumes)", text) and "--volumes" not in code and "set -x" not in code
    assert "system prune" not in code and "image prune -a" not in code
    assert code.count("read -r -s") >= 3 and "umask 077" in code and "chmod 600" in code
    assert 'printf \'%s\\n\' "$p1" | python3 -I' in code             # password through stdin, never argv
    assert "source " not in code and "eval " not in code                # secrets.env is parsed, never run
    # Ubuntu 26.04's Rust coreutils refuse a bare owner id with no account ("invalid user: '10001'"),
    # so the host account must exist before the first install -o / chown with the app's id.
    made = code.index('useradd --system --uid "$APP_UID"')
    assert 'getent passwd "$APP_UID"' in code and 'groupadd --system --gid "$APP_UID"' in code
    assert code.index("\nensure_app_account\n") > made
    first_use = min(code.index(n) for n in ('-o "$APP_UID"', 'chown "$APP_UID'))
    assert code.index("\nensure_app_account\n") < first_use
    bash = real_bash()
    if not bash:
        pytest.skip("no bash on this PC")
    r = subprocess.run([bash, "-n", str(DEPLOY / "install.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def real_bash() -> str | None:
    """A bash that runs here: Linux, or Git Bash on Windows - not the WSL launcher in system32,
    which fails when no Linux distribution is installed (build.bat's PATH finds that one first)."""
    for cand in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if cand and Path(cand).is_file() and "system32" not in cand.lower():
            return cand
    return None


def test_server_files_have_lf_endings():
    for f in DEPLOY.rglob("*"):
        if f.is_file() and "wheels" not in f.parts:
            assert b"\r" not in f.read_bytes(), f"{f.name} has CRLF line endings"


def test_host_version_matches_the_server_files():
    first = (DEPLOY / "HOST_VERSION").read_text(encoding="utf-8").split()
    assert first[0].isdigit() and first[1] == U.host_digest(DEPLOY), (
        "compose.yaml / Dockerfile / ng_updater.py / the units changed: bump the number in deploy/HOST_VERSION if "
        "installed servers need install.sh for it, and put in the new hash: python deploy/ng_updater.py host-digest deploy")


def test_updater_runs_on_ubuntu_2204_python():
    ast.parse((DEPLOY / "ng_updater.py").read_text(encoding="utf-8"), feature_version=(3, 10))
    unit = (DEPLOY / "networkguru-updater.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in unit and "TimeoutStartSec=" in unit and "python3 -I /opt/networkguru/bin/ng_updater.py run" in unit
    path = (DEPLOY / "networkguru-updater.path").read_text(encoding="utf-8")
    assert "PathExists=/var/lib/networkguru/updates/update-request.json" in path


def test_secrets_example_has_no_values():
    for line in (DEPLOY / "secrets.env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("="), line


# ---------------------------------------------------------------------------
# the host updater against a fake Docker
class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeDocker:
    """Answers docker commands; `compose up` starts whatever .env NG_VERSION says."""

    def __init__(self, base: Path):
        self.base = base
        self.calls: list[list[str]] = []
        self.running: str | None = None
        self.unhealthy: set[str] = set()
        self.build_rc = 0
        self.fallback_rc = 0
        self.base_image_present = True
        self.images = {"networkguru-app:1.0.0", "networkguru-app:0.9.0"}

    def run(self, args, timeout=600, env=None):
        self.calls.append(list(args))
        cmd = " ".join(args)
        if args[:3] == ["docker", "image", "inspect"]:
            if args[3].startswith("python:"):
                return (0 if self.base_image_present else 1), ""
            return (0 if args[3] in self.images else 1), ""
        if args[:2] == ["docker", "build"]:
            if self.build_rc == 0:
                self.images.add(args[args.index("-t") + 1])
            return self.build_rc, "ERROR: failed to resolve source metadata" if self.build_rc else "built"
        if args[:2] == ["docker", "run"] and "sh" in args:
            return self.fallback_rc, ""
        if args[:2] == ["docker", "commit"]:
            self.images.add(args[-1])
            return 0, "sha256:abc"
        if "up" in args and "compose" in args:
            env_ = dict(line.split("=", 1) for line in (self.base / ".env").read_text().splitlines()
                        if "=" in line and not line.startswith("#"))
            self.running = env_["NG_VERSION"]
            return 0, ""
        if args[:4] == ["docker", "image", "ls", "networkguru-app"]:
            return 0, "\n".join(sorted(i.split(":")[1] for i in self.images))
        if args[:3] == ["docker", "image", "rm"]:
            self.images.discard(args[3])
            return 0, ""
        if "logs" in cmd:
            return 0, "Traceback: boom"
        return 0, ""

    def health(self, url):
        if self.running is None:
            raise OSError("connection refused")
        return {"ok": self.running not in self.unhealthy, "version": self.running, "tsdb": "victoria"}


def server(tmp_path, current="1.0.0", host_version=1):
    base, updates = tmp_path / "opt", tmp_path / "updates"
    (base / "releases").mkdir(parents=True)
    (base / "site").mkdir()
    updates.mkdir()
    shutil.copy(DEPLOY / "Dockerfile", base / "Dockerfile")
    shutil.copy(DEPLOY / "compose.yaml", base / "compose.yaml")
    (base / "HOST_VERSION").write_text(f"{host_version} x\n")
    (base / ".env").write_text(f"NG_VERSION={current}\nNG_PREVIOUS=0.9.0\nNG_PORT=8080\nNG_MODE=sim\n")
    docker = FakeDocker(base)
    docker.running = current or None
    clock = Clock()
    up = U.Updater(base, updates, runner=docker, http_get=docker.health, clock=clock, sleep=clock.sleep, uid=10001)
    return up, docker, base, updates, clock


def request(updates, clock, version="1.1.0", **zip_opts):
    z = make_release_zip(updates / f"NetworkGuru_v{version}.zip", version, **zip_opts)
    (updates / (z.name + ".sha256")).write_text(sha_text(z))
    sha = sha_text(z).split()[0]
    req = {"request_id": "0123456789abcdef", "version": version, "zip": z.name, "sha256": sha,
           "requested_by": "Casey", "requested_at": clock.t - 10}
    (updates / U.REQUEST).write_text(json.dumps(req))
    return req


def result(updates):
    return json.loads((updates / U.RESULT).read_text())


def env(base):
    return dict(line.split("=", 1) for line in (base / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def leftovers(updates):
    return sorted(p.name for p in updates.iterdir() if p.name not in (U.RESULT, U.STATUS))


def test_update_installed(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    request(updates, clock)
    assert up.process() == 0
    r = result(updates)
    assert r["outcome"] == "installed" and r["request_id"] == "0123456789abcdef" and r["previous"] == "1.0.0"
    assert env(base)["NG_VERSION"] == "1.1.0" and env(base)["NG_PREVIOUS"] == "1.0.0"
    assert (base / "releases" / "1.1.0" / "networkguru" / "__init__.py").is_file()
    assert leftovers(updates) == ["update-request.done"]                    # staged zip + .sha256 removed
    build = next(c for c in docker.calls if c[:2] == ["docker", "build"])
    assert "--network" in build and build[build.index("-f") + 1] == str(base / "Dockerfile")   # never the zip's
    assert ["docker", "image", "rm", "networkguru-app:0.9.0"] in docker.calls       # keeps 2
    assert not any("python:" in " ".join(c) and c[:3] == ["docker", "image", "rm"] for c in docker.calls)
    prune = next(c for c in docker.calls if c[:3] == ["docker", "image", "prune"])
    assert "-a" not in prune and "label=networkguru.version" in prune
    assert json.loads((updates / U.STATUS).read_text())["request_id"] == "0123456789abcdef"


def test_unhealthy_update_rolls_back(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    docker.unhealthy = {"1.1.0"}
    request(updates, clock)
    up.process()
    r = result(updates)
    assert r["outcome"] == "rolled_back" and "not healthy within 60 s" in r["message"] and "boom" in r["log_tail"]
    assert env(base) == {"NG_VERSION": "1.0.0", "NG_PREVIOUS": "0.9.0", "NG_PORT": "8080", "NG_MODE": "sim"}
    assert docker.running == "1.0.0" and "networkguru-app:1.1.0" not in docker.images
    assert not (base / "releases" / "1.1.0").exists() and leftovers(updates) == ["update-request.rejected"]


def test_both_versions_unhealthy_is_said_plainly(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    docker.unhealthy = {"1.1.0", "1.0.0"}
    request(updates, clock)
    up.process()
    r = result(updates)
    assert r["outcome"] == "failed" and "did not come up healthy either" in r["message"]


def test_forced_failure_for_the_hardware_test(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    (base / "test-fail-next-update").write_text("")
    request(updates, clock)
    up.process()
    assert result(updates)["outcome"] == "rolled_back" and not (base / "test-fail-next-update").exists()


def test_build_failure_switches_nothing(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    docker.build_rc = docker.fallback_rc = 1
    request(updates, clock)
    up.process()
    r = result(updates)
    assert r["outcome"] == "failed" and "building 1.1.0 failed" in r["message"]
    assert env(base)["NG_VERSION"] == "1.0.0" and not any("up" in c and "compose" in c for c in docker.calls)


def test_builder_free_fallback(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    docker.build_rc = 1
    request(updates, clock)
    up.process()
    assert result(updates)["outcome"] == "installed"
    run = next(c for c in docker.calls if c[:2] == ["docker", "run"] and "sh" in c)
    assert run[run.index("--network") + 1] == "none" and run[-1] == U.FALLBACK_SCRIPT
    commit = next(c for c in docker.calls if c[:2] == ["docker", "commit"])
    assert "USER 10001:10001" in commit and commit[-1] == "networkguru-app:1.1.0"


def test_missing_base_image_says_run_install(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    docker.base_image_present = False
    request(updates, clock)
    up.process()
    assert "run install.sh with the new images" in result(updates)["message"]


@pytest.mark.parametrize("problem", ["sha", "older", "stale", "zipslip", "name", "oversize", "garbage"])
def test_bad_requests_are_rejected(tmp_path, monkeypatch, problem):
    up, docker, base, updates, clock = server(tmp_path)
    if problem == "older":
        req = request(updates, clock, version="0.9.5")
    elif problem == "zipslip":
        req = request(updates, clock, zipslip=True)
    elif problem == "oversize":
        monkeypatch.setattr(U, "MAX_ZIP", 100)
        req = request(updates, clock)
    else:
        req = request(updates, clock)
    if problem == "sha":
        req["sha256"] = "f" * 64
    if problem == "stale":
        req["requested_at"] = clock.t - 2 * 86400
    if problem == "name":
        req["zip"] = "../../etc/passwd"
    (updates / U.REQUEST).write_text("not json {" if problem == "garbage" else json.dumps(req))
    up.process()
    r = result(updates)
    assert r["outcome"] == "rejected", r
    assert env(base)["NG_VERSION"] == "1.0.0" and not any(c[:2] == ["docker", "build"] for c in docker.calls)
    assert not (updates / U.REQUEST).exists() and not (updates / U.PROCESSING).exists()   # the path unit stays quiet
    assert (updates / "update-request.rejected").exists()


def test_symlinked_zip_is_refused(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    req = request(updates, clock)
    real = updates / req["zip"]
    target = tmp_path / "elsewhere.zip"
    real.rename(target)
    try:
        os.symlink(target, real)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make symlinks here (Windows without developer mode)")
    up.process()
    assert result(updates)["outcome"] == "rejected" and "not a regular file" in result(updates)["message"]


def test_fifo_request_is_refused(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on Windows")
    up, docker, base, updates, clock = server(tmp_path)
    os.mkfifo(updates / U.REQUEST)
    up.process()                                            # must not hang on the FIFO
    assert result(updates)["outcome"] == "rejected"


def test_release_that_needs_install_sh(tmp_path):
    up, docker, base, updates, clock = server(tmp_path, host_version=1)
    request(updates, clock, host_version=2)
    up.process()
    r = result(updates)
    assert r["outcome"] == "needs_install" and "install.sh" in r["message"]
    assert not any(c[:2] == ["docker", "build"] for c in docker.calls) and env(base)["NG_VERSION"] == "1.0.0"


def test_reboot_in_the_middle_is_reported(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    (updates / U.PROCESSING).write_text(json.dumps({"request_id": "0123456789abcdef"}))
    up.process()
    r = result(updates)
    assert r["outcome"] == "interrupted" and r["request_id"] == "0123456789abcdef"
    assert not (updates / U.PROCESSING).exists()


def test_result_written_atomically_and_owned_by_the_app(tmp_path, monkeypatch):
    up, docker, base, updates, clock = server(tmp_path)
    owners = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: owners.append((uid, gid)), raising=False)
    up.write_shared(U.RESULT, {"x": 1})
    assert owners == [(10001, 10001)] and json.loads((updates / U.RESULT).read_text()) == {"x": 1}
    assert [p.name for p in updates.iterdir()] == [U.RESULT]                # no temp files left


def test_first_install_and_same_version_rerun(tmp_path):
    up, docker, base, updates, clock = server(tmp_path, current="")
    docker.images = set()
    src = tmp_path / "src"
    src.mkdir()
    z = make_release_zip(tmp_path / "r.zip", "1.0.0")
    import zipfile
    with zipfile.ZipFile(z) as zz:
        zz.extractall(src)
    rel = src / "NetworkGuru_v1.0.0"
    assert up.install_local(rel) == 0
    assert env(base)["NG_VERSION"] == "1.0.0" and docker.running == "1.0.0"
    assert any("check-inventory" in c for c in docker.calls)
    docker.calls.clear()
    assert up.install_local(rel) == 0                                      # re-run: no rebuild, same version
    assert not any(c[:2] == ["docker", "build"] for c in docker.calls)


def test_first_install_that_fails_is_left_running(tmp_path):
    up, docker, base, updates, clock = server(tmp_path, current="")
    docker.unhealthy = {"1.0.0"}
    z = make_release_zip(tmp_path / "r.zip", "1.0.0")
    import zipfile
    with zipfile.ZipFile(z) as zz:
        zz.extractall(tmp_path / "src")
    assert up.install_local(tmp_path / "src" / "NetworkGuru_v1.0.0") == 1
    assert env(base)["NG_VERSION"] == "1.0.0" and docker.running == "1.0.0"


def test_manual_rollback(tmp_path):
    up, docker, base, updates, clock = server(tmp_path)
    assert up.rollback() == 0
    assert env(base)["NG_VERSION"] == "0.9.0" and env(base)["NG_PREVIOUS"] == "1.0.0" and docker.running == "0.9.0"


def test_hash_password_command(capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("long-enough-password\n"))
    assert U.main(["hash-password"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("scrypt:32768:8:1:") and "long-enough" not in out
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert U.main(["hash-password"]) == 1
