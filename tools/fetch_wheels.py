#!/usr/bin/env python3
r"""
Linux wheels for the server image (run on this PC; build.bat does it)
=====================================================================
    python tools\fetch_wheels.py            download into deploy\wheels\ + write deploy\requirements.lock
    python tools\fetch_wheels.py --check    the wheels match the lock (exit 1 if not)

The site server builds the app image offline (`pip install --no-index --require-hashes`), so
every package - with its dependencies - ships in the release zip as a linux / CPython 3.13
wheel. deploy\requirements.lock pins each one with its sha256 and is kept in git;
deploy\wheels\ is not (it is only in the release zip). The check also reads every wheel's own
dependency list for linux/cp313 and fails if one is missing, so a package that needs something
only on Linux cannot slip through because this PC runs Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQS = ROOT / "requirements.txt"
LOCK = ROOT / "deploy" / "requirements.lock"
WHEELS = ROOT / "deploy" / "wheels"
PLATFORMS = ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"]
PY = "3.13"
MARKER_ENV = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
              "python_version": PY, "python_full_version": PY + ".0", "implementation_name": "cpython",
              "platform_python_implementation": "CPython", "extra": ""}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_id(path: Path) -> tuple[str, str]:
    parts = path.name[:-4].split("-")
    return norm(parts[0]), parts[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_lock(path: Path = LOCK) -> dict[str, tuple[str, str]]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)\s+--hash=sha256:([0-9a-f]{64})\s*$", line.strip())
        if m:
            out[norm(m.group(1))] = (m.group(2), m.group(3))
    return out


def pins(path: Path = REQS) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)", line.strip())
        if m:
            out[norm(m.group(1))] = m.group(2)
    return out


def _requirement_cls():
    try:
        from pip._vendor.packaging.requirements import Requirement   # pip's own copy
    except ImportError:                                               # pragma: no cover
        from packaging.requirements import Requirement
    return Requirement


def problems(lock_path: Path = LOCK, wheels_dir: Path = WHEELS, reqs: Path = REQS) -> list[str]:
    """Empty when the wheels, the lock and requirements.txt agree and nothing is missing."""
    if not lock_path.is_file():
        return [f"{lock_path.name} is missing - run python tools\\fetch_wheels.py"]
    lock = read_lock(lock_path)
    out = []
    wheels = {wheel_id(w): w for w in sorted(wheels_dir.glob("*.whl"))} if wheels_dir.is_dir() else {}
    if not wheels:
        return [f"no wheels in {wheels_dir} - run python tools\\fetch_wheels.py"]
    for name, ver in pins(reqs).items():
        if lock.get(name, (None,))[0] != ver:
            out.append(f"requirements.txt pins {name}=={ver} but the lock has {lock.get(name, ('nothing',))[0]}")
    by_name = {n: (v, p) for (n, v), p in wheels.items()}
    for name, (ver, digest) in lock.items():
        w = by_name.get(name)
        if not w or w[0] != ver:
            out.append(f"no wheel for {name}=={ver}")
        elif sha256(w[1]) != digest:
            out.append(f"{w[1].name}: sha256 differs from the lock")
    for name in by_name:
        if name not in lock:
            out.append(f"{by_name[name][1].name} is not in the lock")
    Requirement = _requirement_cls()
    for (_name, _ver), path in wheels.items():
        with zipfile.ZipFile(path) as z:
            meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
            text = z.read(meta).decode("utf-8", "replace") if meta else ""
        for line in text.splitlines():
            if not line.startswith("Requires-Dist:"):
                continue
            req = Requirement(line.split(":", 1)[1].strip())
            if req.marker is not None and not req.marker.evaluate(MARKER_ENV):
                continue
            have = lock.get(norm(req.name))
            if have is None:
                out.append(f"{path.name} needs {req.name} on Linux, which is not in the lock")
            elif req.specifier and not req.specifier.contains(have[0], prereleases=True):
                out.append(f"{path.name} needs {req} but the lock has {have[0]}")
    return out


def fetch() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ng_wheels_"))
    try:
        cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir",
               "--disable-pip-version-check", "--python-version", PY, "--implementation", "cp", "--abi", "cp313",
               *[a for p in PLATFORMS for a in ("--platform", p)], "-r", str(REQS), "-d", str(tmp)]
        print("Downloading Linux wheels (pip download) ...")
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print("ERROR: pip download failed - check the internet connection.")
            return 1
        got = sorted(tmp.glob("*.whl"))
        lines = ["# Linux / CPython 3.13 packages for the server image, with their sha256.",
                 "# Written by tools/fetch_wheels.py - do not edit; the wheels are in deploy/wheels (release zip only)."]
        for w in got:
            name, ver = wheel_id(w)
            lines.append(f"{name}=={ver} --hash=sha256:{sha256(w)}")
        WHEELS.mkdir(parents=True, exist_ok=True)
        for old in WHEELS.glob("*.whl"):
            old.unlink()
        for w in got:
            shutil.copy2(w, WHEELS / w.name)
        LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"  {len(got)} wheels in deploy\\wheels, {LOCK.name} written.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    found = problems()
    for p in found:
        print("  problem: " + p)
    return 1 if found else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Linux wheels for the Network Guru server image")
    ap.add_argument("--check", action="store_true", help="only check deploy/wheels against the lock")
    args = ap.parse_args()
    if args.check:
        found = problems()
        for p in found:
            print("problem: " + p)
        print("Wheels OK." if not found else "Wheels do not match - run: python tools\\fetch_wheels.py")
        return 1 if found else 0
    return fetch()


if __name__ == "__main__":
    sys.exit(main())
