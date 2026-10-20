"""GitHub Releases client for the daily update check.

Ported from the Router Config Tool's updater.py (latest_release, is_newer, pick_asset,
fetch_text, download, verify_sha256 and its redirect handling) with the tool-specific
lookups turned into parameters. Two changes: a relative redirect is resolved with urljoin,
and the .sha256 asset is required - a release without one is never installed.

Standard library only (urllib). Calls block; the update manager runs them in a thread.
The repository is public (0.5.3), so no token is needed: GitHub allows 60 calls an hour without
one; a check is one call and an install two more. A read-only token (secrets.env NETWORKGURU_GITHUB_TOKEN) is
still sent when it is set. Asset downloads answer with a redirect to a signed CDN URL; redirects
are followed by hand WITHOUT the token.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__

REPO = "Quesito350/Network-Guru"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = f"NetworkGuru/{__version__}"
ASSET_RE = re.compile(r"^NetworkGuru_v(\d+\.\d+\.\d+)\.zip$")
CHUNK = 256 * 1024

ProgressCb = Callable[[int, int], None]     # (bytes_done, bytes_total)


class UpdateError(Exception):
    """Anything that stops an update check or download; the message is shown to the admin."""


def api_url() -> str:
    return os.environ.get("NETWORKGURU_UPDATE_API") or API_LATEST


def version_tuple(v: str | None) -> tuple[int, ...]:
    """'0.5.1' -> (0, 5, 1); unparsable -> (0,). For numeric comparisons."""
    return tuple(int(p) for p in re.findall(r"\d+", v or "")) or (0,)


def is_newer(version: str, current: str = __version__) -> bool:
    return version_tuple(version) > version_tuple(current)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _open(url: str, accept: str, timeout: float, tok: str | None = None, hops: int = 5):
    """GET with our headers. Redirects are followed by hand WITHOUT the Authorization
    header: GitHub sends asset downloads to a signed objects.githubusercontent.com URL,
    which rejects a request that also carries a token."""
    for _ in range(hops):
        headers = {"User-Agent": USER_AGENT, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url, headers=headers)
        try:
            return _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                tok = None
                continue
            raise
    raise UpdateError("too many redirects")


def latest_release(token: str | None, timeout: float = 10.0) -> dict:
    """The newest GitHub Release as a dict (tag_name, assets[name,url,size], body)."""
    try:
        with _open(api_url(), "application/vnd.github+json", timeout, token) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("no release found (404) - none is published yet, or the repository is "
                              "not public and the token cannot see it") from None
        if e.code in (401, 403):
            raise UpdateError(f"GitHub refused the request ({e.code}) - rate limit (60 an hour "
                              "without a token) or a wrong / expired token") from None
        raise UpdateError(f"GitHub answered {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"no internet: could not reach GitHub ({getattr(e, 'reason', e)})") from None
    if not isinstance(data, dict) or "tag_name" not in data:
        raise UpdateError("unexpected answer from GitHub")
    return data


def latest_version(release: dict) -> str:
    return str(release.get("tag_name", "")).lstrip("vV")


def pick_asset(release: dict) -> tuple[dict, dict]:
    """(zip asset, its .sha256 asset) for the release's version."""
    version = latest_version(release)
    want = f"NetworkGuru_v{version}.zip"
    assets = {a.get("name"): a for a in release.get("assets", []) if isinstance(a, dict)}
    zip_asset = assets.get(want)
    if zip_asset is None or not ASSET_RE.match(want):
        raise UpdateError(f"release {version} has no {want}")
    sha_asset = assets.get(want + ".sha256")
    if sha_asset is None:
        raise UpdateError(f"release {version} has no {want}.sha256 - not installed without a checksum")
    return zip_asset, sha_asset


def fetch_text(asset: dict, token: str | None, timeout: float = 15.0) -> str:
    try:
        with _open(asset["url"], "application/octet-stream", timeout, token) as r:
            return r.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"could not download {asset.get('name')} ({getattr(e, 'reason', e)})") from None


def download(asset: dict, dest: Path, token: str | None, progress: ProgressCb | None = None,
             timeout: float = 30.0, max_bytes: int = 300 * 1024 * 1024) -> Path:
    """Stream the asset to `dest` (via a .part file); size checked against the size GitHub
    lists for the asset, and never more than max_bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    expected = int(asset.get("size") or 0)
    if expected > max_bytes:
        raise UpdateError(f"{asset.get('name')} is {expected / 1048576:.0f} MB - larger than allowed")
    done = 0
    try:
        with _open(asset["url"], "application/octet-stream", timeout, token) as r:
            total = expected or int(r.headers.get("Content-Length") or 0)
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > max_bytes:
                        raise UpdateError(f"{asset.get('name')} is larger than allowed")
                    f.write(chunk)
                    if progress:
                        progress(done, total)
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"download failed ({getattr(e, 'reason', e)})") from None
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    if expected and done != expected:
        part.unlink(missing_ok=True)
        raise UpdateError(f"download incomplete ({done} of {expected} bytes)")
    part.replace(dest)
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def expected_sha256(text: str) -> str:
    """The checksum from a .sha256 file ("<hex>  <name>", LF or CRLF)."""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    if not m:
        raise UpdateError("the .sha256 file has no checksum")
    return m.group(1).lower()


def verify_sha256(path: Path, expected_text: str) -> str:
    """`expected_text` is the .sha256 file content ("<hex>  <name>")."""
    want = expected_sha256(expected_text)
    actual = sha256_file(path)
    if actual.lower() != want:
        raise UpdateError("checksum mismatch - the file is not the published one")
    return actual
