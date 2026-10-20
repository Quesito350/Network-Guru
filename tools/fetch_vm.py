#!/usr/bin/env python3
r"""
Fetch the VictoriaMetrics Windows exe for running Network Guru on this PC (dev only)
===================================================================================
    python tools\fetch_vm.py              # pinned version below
    python tools\fetch_vm.py --version 1.154.0

Downloads the single-node Windows zip and its checksums file from the official
VictoriaMetrics GitHub release into a new empty temp folder, checks the SHA-256, and
copies only victoria-metrics-windows-amd64-prod.exe into tools\vm\. `run.py dev` then
starts it by itself. tools\ is never part of the release zip or the git repo.

v1.153.0, v1.148.5 and v1.136.19 are refused: their Windows (pure-Go) builds have a zstd
bug that can corrupt stored data (VictoriaMetrics issue #11683).
"""

import argparse
import hashlib
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PINNED = "1.154.0"
BAD = {"1.153.0", "1.148.5", "1.136.19"}
REPO = "https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download"
EXE = "victoria-metrics-windows-amd64-prod.exe"
DEST = Path(__file__).resolve().parent / "vm"


def fetch(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "NetworkGuru-fetch_vm"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--version", default=PINNED)
    v = ap.parse_args().version.lstrip("v")
    if v in BAD:
        sys.exit(f"ERROR: v{v} has the Windows zstd data-corruption bug (#11683) - use {PINNED}.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        sys.exit("ERROR: version must look like 1.154.0")
    name = f"victoria-metrics-windows-amd64-v{v}.zip"
    work = Path(tempfile.mkdtemp(prefix="ng_vm_"))
    try:
        print(f"Downloading {name} ...")
        fetch(f"{REPO}/v{v}/{name}", work / name)
        fetch(f"{REPO}/v{v}/victoria-metrics-windows-amd64-v{v}_checksums.txt", work / "checksums.txt")
        digest = hashlib.sha256((work / name).read_bytes()).hexdigest()
        sums = (work / "checksums.txt").read_text(encoding="utf-8", errors="replace")
        if digest not in sums:
            sys.exit(f"ERROR: SHA-256 of {name} ({digest}) is not in the published checksums - not installed.")
        print(f"  SHA-256 OK ({digest[:16]}...)")
        with zipfile.ZipFile(work / name) as z:
            member = next((m for m in z.namelist() if Path(m).name == EXE), None)
            if member is None:
                sys.exit(f"ERROR: {EXE} not found in the zip: {z.namelist()}")
            DEST.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, open(DEST / EXE, "wb") as out:
                shutil.copyfileobj(src, out)
            exe_sum = hashlib.sha256((DEST / EXE).read_bytes()).hexdigest()
        (DEST / "VERSION.txt").write_text(f"VictoriaMetrics v{v}\n{EXE} sha256 {exe_sum}\n", encoding="utf-8")
        print(f"  Installed {DEST / EXE} ({(DEST / EXE).stat().st_size / 1048576:.1f} MB)")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
