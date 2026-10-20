#!/usr/bin/env python3
r"""
Fetch the Docker images a site server needs, on this PC (no Docker needed here)
================================================================================
    python tools\fetch_images.py                 # -> ..\images\  (beside the release zips)
    python tools\fetch_images.py --out D:\usb\images

Downloads, for linux/amd64:
    python:3.13-slim-trixie                      base image the server builds Network Guru on
    victoriametrics/victoria-metrics:v1.154.0    the history store

straight from Docker Hub's registry API (every blob sha256-checked) and writes `docker load`
archives plus SHA256SUMS (LF line endings, `sha256sum -c` reads it). install.sh --images <dir>
checks SHA256SUMS and loads them, so a server without internet needs nothing else.
The tars are too large for git or the release zip; they change only when the images change.

fetch_image() and its helpers are copied unchanged from the All-Stop Deploy Tool's
netassist.py (only the User-Agent and temp-folder prefix differ). Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

Log = Callable[[str], None]

IMAGES = ["python:3.13-slim-trixie", "victoriametrics/victoria-metrics:v1.154.0"]
ARCH = "amd64"
DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "images"


# ---------------------------------------------------------------------------
# copied from the All-Stop Deploy Tool (netassist.py)
# ---------------------------------------------------------------------------
REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def split_ref(ref: str) -> Tuple[str, str]:
    """'emqx:5.1.3' -> ('library/emqx', '5.1.3'); 'org/img:tag' -> ('org/img', 'tag')."""
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    if name.startswith("docker.io/"):
        name = name[len("docker.io/"):]
    if "/" not in name:
        name = "library/" + name
    return name, tag


def _open(url: str, token: Optional[str] = None, accept: Optional[str] = None, timeout: float = 60):
    """GET with the bearer token; redirects (blob CDN) are followed WITHOUT the token."""
    headers = {"User-Agent": "networkguru-fetch_images"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    for _ in range(5):
        try:
            return _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                headers.pop("Authorization", None)
                continue
            raise
    raise OSError(f"too many redirects for {url}")


def _token(repo: str) -> str:
    q = urllib.parse.urlencode({"service": "registry.docker.io", "scope": f"repository:{repo}:pull"})
    with _open(f"{AUTH}?{q}", timeout=30) as r:
        return json.loads(r.read())["token"]


def _blob_to_file(repo: str, digest: str, token: str, dest: Path, log: Log, label: str) -> int:
    algo, want = digest.split(":", 1)
    h = hashlib.new(algo)
    size = 0
    with _open(f"{REGISTRY}/v2/{repo}/blobs/{digest}", token, timeout=120) as r, open(dest, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        shown = 0
        for chunk in iter(lambda: r.read(1 << 20), b""):
            fh.write(chunk)
            h.update(chunk)
            size += len(chunk)
            if total > 50 * 1048576 and size * 4 // total > shown:
                shown = size * 4 // total
                log(f"  … {label}: {shown * 25}% of {total / 1048576:.0f} MB")
    if h.hexdigest() != want:
        dest.unlink(missing_ok=True)
        raise OSError(f"checksum mismatch for {digest}")
    return size


def cache_path(cache_dir: Path, ref: str, arch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref)
    return Path(cache_dir) / f"{safe}_{arch}.tar"


def fetch_image(ref: str, arch: str, cache_dir: Path, log: Log = print) -> Tuple[Path, bool]:
    """A `docker load` archive of `ref` for linux/<arch>. Returns (path, came_from_cache).
    Raises OSError / urllib errors when it cannot be downloaded and is not cached."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_path(cache_dir, ref, arch)
    if out.is_file() and out.stat().st_size > 0 and out.with_suffix(".json").is_file():
        return out, True
    repo, tag = split_ref(ref)
    token = _token(repo)
    with _open(f"{REGISTRY}/v2/{repo}/manifests/{tag}", token, MANIFEST_TYPES) as r:
        body, mtype = r.read(), (r.headers.get("Content-Type") or "").split(";")[0]
    doc = json.loads(body)
    if mtype in INDEX_TYPES or doc.get("manifests"):
        cands = [m for m in doc.get("manifests", [])
                 if (m.get("platform") or {}).get("os") == "linux"
                 and (m.get("platform") or {}).get("architecture") == arch]
        if not cands:
            raise OSError(f"{ref} has no linux/{arch} image")
        with _open(f"{REGISTRY}/v2/{repo}/manifests/{cands[0]['digest']}", token, MANIFEST_TYPES) as r:
            body = r.read()
        doc = json.loads(body)
    config_digest = doc["config"]["digest"]
    layers = [layer["digest"] for layer in doc["layers"]]
    log(f"  {ref} (linux/{arch}): {len(layers)} layer(s), "
        f"{sum(int(layer.get('size', 0)) for layer in doc['layers']) / 1048576:.0f} MB to download")

    work = Path(tempfile.mkdtemp(prefix="ng_img_", dir=str(cache_dir)))
    try:
        cfg_hex = config_digest.split(":", 1)[1]
        _blob_to_file(repo, config_digest, token, work / f"{cfg_hex}.json", log, "config")
        names = []
        for i, digest in enumerate(layers, 1):
            hexd = digest.split(":", 1)[1]
            (work / hexd).mkdir(exist_ok=True)
            _blob_to_file(repo, digest, token, work / hexd / "layer.tar", log, f"layer {i}/{len(layers)}")
            names.append(f"{hexd}/layer.tar")
        manifest = [{"Config": f"{cfg_hex}.json", "RepoTags": [ref], "Layers": names}]
        part = out.with_suffix(".part")
        with tarfile.open(part, "w", format=tarfile.PAX_FORMAT) as tar:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime = len(data), int(time.time())
            tar.addfile(info, io.BytesIO(data))
            tar.add(work / f"{cfg_hex}.json", arcname=f"{cfg_hex}.json")
            for n in names:
                tar.add(work / n, arcname=n)
        part.replace(out)
        out.with_suffix(".json").write_text(json.dumps({"ref": ref, "arch": arch, "config": config_digest,
                                                        "layers": layers}, indent=2))
        return out, False
    finally:
        for f in sorted(work.rglob("*"), reverse=True):
            try:
                f.unlink() if f.is_file() else f.rmdir()
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch the Docker images for a Network Guru site server.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="folder for the .tar files (default: images, beside the release zips)")
    ap.add_argument("--refresh", action="store_true", help="download again even if cached (tags like 3.13-slim move)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.refresh:
        for ref in IMAGES:
            for p in (cache_path(out, ref, ARCH), cache_path(out, ref, ARCH).with_suffix(".json")):
                p.unlink(missing_ok=True)
    lines = []
    for ref in IMAGES:
        print(f"{ref} (linux/{ARCH}) ...")
        try:
            tar, cached = fetch_image(ref, ARCH, out)
        except (OSError, urllib.error.URLError, KeyError, ValueError) as e:
            print(f"ERROR: could not fetch {ref}: {e}")
            return 1
        print(f"  {'cached' if cached else 'downloaded'}: {tar.name} ({tar.stat().st_size / 1048576:.0f} MB)")
        lines.append(f"{sha256_file(tar)}  {tar.name}")
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {out / 'SHA256SUMS'}. Copy the whole folder to the server and run:")
    print("  sudo bash deploy/install.sh --images <that folder> --inventory <site inventory.yaml>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
