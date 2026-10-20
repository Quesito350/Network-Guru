"""Shared test setup. Run from the source folder:  python -m pytest -q"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from networkguru.inventory import load_inventory, parse_inventory  # noqa: E402

EXAMPLE = ROOT / "examples" / "inventory.example.yaml"

MINI = {
    "site": {"id": "testsite", "name": "Test site", "timezone": "America/Denver", "lat": 40.76, "lon": -111.89},
    "control_room": [
        {"id": "srv", "name": "Mobius Server", "kind": "server", "components": [
            {"id": "srv-mob", "type": "mobius_server", "host": "10.0.0.10"},
            {"id": "srv-ntrip", "type": "mobius_ntrip"}]},
    ],
    "infrastructure": [
        {"id": "core", "name": "Core", "kind": "core", "devices": [{"id": "core-sw", "type": "unifi", "host": "10.0.0.2"}]},
        {"id": "t1", "name": "Trailer 1", "kind": "trailer", "devices": [
            {"id": "t1-bc", "type": "rajant", "host": "10.0.1.7"},
            {"id": "t1-gx", "type": "victron_cerbo", "host": "10.0.1.8"},
            {"id": "t1-60g", "type": "tachyon", "host": "10.0.1.9", "user_ref": "TACHYON_RO_USER",
             "password_ref": "TACHYON_RO_PASS"}]},
    ],
    "vehicles": [
        {"id": "truck1", "name": "Truck 1", "access": "rajant", "components": [
            {"id": "tr1-radio", "type": "rajant", "host": "10.0.2.61"},
            {"id": "tr1-router", "type": "mikrotik", "host": "10.0.2.161"},
            {"id": "tr1-vcu", "type": "vcu"},
            {"id": "tr1-gnss", "type": "gnss"}]},
    ],
}


@pytest.fixture
def mini_raw():
    import copy
    return copy.deepcopy(MINI)


@pytest.fixture
def mini_inv(mini_raw):
    return parse_inventory(mini_raw)


@pytest.fixture(scope="session")
def example_inv():
    return load_inventory(EXAMPLE)


def make_release_zip(path: Path, version: str, *, inner_version: str | None = None, zipslip: bool = False,
                     symlink: bool = False, missing: tuple[str, ...] = (), host_version: int = 1,
                     extra_bytes: int = 0) -> Path:
    """A minimal NetworkGuru_v<version>.zip shaped like release.py builds it."""
    import zipfile
    top = f"NetworkGuru_v{version}/"
    inner = inner_version or version
    files = {
        "networkguru/__init__.py": f'__version__ = "{inner}"\nTOOL_NAME = "Network Guru"\n',
        "VERSION.txt": f"Network Guru\nVersion : {inner}\n",
        "requirements.txt": "fastapi==0.143.1\n",
        "deploy/Dockerfile": "FROM python:3.13-slim-trixie\n",
        "deploy/HOST_VERSION": f"{host_version} abc\n",
        "deploy/requirements.lock": "fastapi==0.143.1 --hash=sha256:" + "0" * 64 + "\n",
    }
    if extra_bytes:
        files["deploy/wheels/big.whl"] = "x" * extra_bytes
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            if name not in missing:
                z.writestr(top + name, text)
        if zipslip:
            z.writestr(top + "../../evil.txt", "x")
        if symlink:
            info = zipfile.ZipInfo(top + "link")
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
    return path


def sha_text(path: Path) -> str:
    import hashlib
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\r\n"     # CRLF, as Windows writes
