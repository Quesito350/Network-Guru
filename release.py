#!/usr/bin/env python3
r"""
Release packaging for Network Guru
==================================
Run as the last step of build.bat (or by hand):

    python release.py              # zip the source release, keep the newest 5
    python release.py --publish    # ... then commit, tag v<version>, push, GitHub Release
    python release.py --keep 3

What it does
  1. Reads __version__ from networkguru\__init__.py and refuses to package if
     changelog.py has no entry for it.
  2. Refuses to package anything that looks like a secret (secrets.env, *.env other than
     *.example, a password/community/token/api_key value in YAML, private keys, GitHub tokens,
     Teams / Power Automate webhook URLs, an admin password hash) - in every text file,
     install.sh, the Dockerfile and the systemd units included. Refuses to package when the
     Linux wheels in deploy\wheels do not match deploy\requirements.lock (tools\fetch_wheels.py).
  3. Zips the source release into ..\NetworkGuru_v<version>.zip - ONE LEVEL UP
     from this source folder - with a VERSION.txt listing every file and its sha256,
     plus NetworkGuru_v<version>.zip.sha256 beside it (the updater checks it).
  4. Keeps the newest --keep zips there and moves older ones into ..\archive\.
     Versions compare numerically (0.10.0 > 0.9.0).
  5. --publish: runs the same secret check over every file git would commit, then
     commits, tags v<version>, pushes to Quesito350/Network-Guru and creates the GitHub
     Release with the zip + .sha256 (same flow as the Router Config Tool; uses the PC's
     `gh` sign-in - no token is stored anywhere).
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASES_DIR = ROOT.parent
ARCHIVE_DIR = RELEASES_DIR / "archive"
NAME_PREFIX = "NetworkGuru_v"
TOOL_TITLE = "Network Guru"
GITHUB_REPO = "Quesito350/Network-Guru"
IMAGE = "ghcr.io/quesito350/network-guru"           # the test-install image (.github/workflows/image.yml)
IMAGE_WORKFLOW = "image.yml"
IMAGE_WAIT_S = 20 * 60
DEFAULT_KEEP = 5

INCLUDE = ["networkguru", "examples", "tests", "deploy", "tools/fetch_vm.py", "tools/fetch_wheels.py",
           "tools/fetch_images.py", "run.py", "run_demo.bat", "build.bat", "release.py", "requirements.txt",
           "requirements-dev.txt", "pytest.ini", "README.md", "ARCHITECTURE.md", "REQUIREMENTS.md", ".gitattributes",
           "compose.yaml", "compose.build.yaml", ".env.example", ".github"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", "vm", ".git"}
SECRET_KEY = re.compile(r"^\s*-?\s*\{?.*?\b(password|passwd|community|token|api[_-]?key|secret)\b\s*:\s*([^\s,}#]+)",
                        re.IGNORECASE | re.MULTILINE)
PRIVATE_KEY = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|^\s*PrivateKey\s*=", re.MULTILINE)
GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})\b")
WEBHOOK_URL = re.compile(r"https://[^\s\"'<>]*(?:logic\.azure\.com|environment\.api\.powerplatform\.com|"
                         r"webhook\.office\.com)[^\s\"'<>]*", re.IGNORECASE)
PASSWORD_HASH = re.compile(r"scrypt:\d+:\d+:\d+:[A-Za-z0-9+/=]{16,}:[A-Za-z0-9+/=]{40,}")
BINARY = {".whl", ".png", ".ico", ".zip", ".exe", ".tar", ".gz", ".db", ".pyc", ".xlsx", ".mesh", ".kmz"}


def read_version() -> str:
    src = (ROOT / "networkguru" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.MULTILINE)
    if not m:
        sys.exit("ERROR: could not find __version__ in networkguru\\__init__.py")
    return m.group(1).strip()


def check_changelog(version: str) -> None:
    sys.path.insert(0, str(ROOT))
    from networkguru.changelog import CHANGES
    if not CHANGES or CHANGES[0][0] != version:
        sys.exit(f"ERROR: changelog.py has no entry for {version} at the top - add one first.")


def files_to_ship() -> list[Path]:
    out = []
    for name in INCLUDE:
        p = ROOT / name
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(f for f in sorted(p.rglob("*")) if f.is_file() and not (set(f.relative_to(ROOT).parts) & SKIP_PARTS))
    return out


def secret_problems(files: list[Path]) -> list[str]:
    problems = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        name = f.name.lower()
        if name in ("secrets.env", ".env") or (name.endswith(".env") and not name.endswith(".example")) \
                or (name.startswith(".env.") and not name.endswith(".example")):
            problems.append(f"{rel}: secrets file")
            continue
        if f.suffix.lower() in BINARY:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if PRIVATE_KEY.search(text):
            problems.append(f"{rel}: contains a private key")
        if GITHUB_TOKEN.search(text):
            problems.append(f"{rel}: contains a GitHub token")
        if any("sig=" in m.group(0).lower() for m in WEBHOOK_URL.finditer(text)):
            problems.append(f"{rel}: contains a Teams / Power Automate webhook URL")
        if PASSWORD_HASH.search(text):
            problems.append(f"{rel}: contains an admin password hash")
        if f.suffix.lower() in (".yaml", ".yml"):
            for m in SECRET_KEY.finditer(text):
                value = m.group(2).strip("'\"")
                if value.startswith("${{"):              # a GitHub Actions expression, e.g. ${{ secrets.GITHUB_TOKEN }}
                    continue
                if value and value.lower() not in ("null", "none", "~", '""', "''"):
                    problems.append(f"{rel}: '{m.group(1)}' has a value - use a *_ref name instead")
    return problems


def wheel_problems() -> list[str]:
    sys.path.insert(0, str(ROOT / "tools"))
    import fetch_wheels
    return fetch_wheels.problems()


def version_key(path: Path):
    ver = path.stem[len(NAME_PREFIX):] if path.stem.startswith(NAME_PREFIX) else path.stem
    return tuple(int(p) for p in re.findall(r"\d+", ver)), ver


def package(version: str) -> Path:
    files = files_to_ship()
    problems = secret_problems(files)
    if problems:
        sys.exit("ERROR: refusing to package - possible secrets:\n  " + "\n  ".join(problems))
    wheels = wheel_problems()
    if wheels:
        sys.exit("ERROR: refusing to package - the server could not build the image offline:\n  "
                 + "\n  ".join(wheels) + "\n  Run: python tools\\fetch_wheels.py")
    folder = f"{NAME_PREFIX}{version}"
    zip_path = RELEASES_DIR / f"{folder}.zip"
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [TOOL_TITLE, f"Version : {version}", f"Built   : {stamp}", "Files (sha256):"]
    for f in files:
        lines.append(f"  {hashlib.sha256(f.read_bytes()).hexdigest()[:16]}  {f.relative_to(ROOT).as_posix()}")
    if zip_path.exists():
        print(f"  Note: {zip_path.name} already exists - replacing it.")
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f"{folder}/{f.relative_to(ROOT).as_posix()}")
        zf.writestr(f"{folder}/VERSION.txt", "\n".join(lines) + "\n")
    sha = write_sha256(zip_path)
    print(f"  Created {zip_path}  ({zip_path.stat().st_size / 1024:.0f} KB, {len(files)} files) + {sha.name}")
    return zip_path


def write_sha256(zip_path: Path) -> Path:
    sha = zip_path.with_name(zip_path.name + ".sha256")
    sha.write_text(f"{hashlib.sha256(zip_path.read_bytes()).hexdigest()}  {zip_path.name}\n", encoding="utf-8",
                   newline="\n")                 # LF, so sha256sum -c on the server reads it
    return sha


def prune(keep: int) -> None:
    zips = sorted(RELEASES_DIR.glob(f"{NAME_PREFIX}*.zip"), key=version_key, reverse=True)
    old = zips[keep:]
    if not old:
        print(f"  {len(zips)} release(s) in {RELEASES_DIR.name}\\ - nothing to archive (keeping {keep}).")
        return
    ARCHIVE_DIR.mkdir(exist_ok=True)
    for z in old:
        for f in (z, z.with_name(z.name + ".sha256")):
            if not f.exists():
                continue
            target = ARCHIVE_DIR / f.name
            if target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
        print(f"  Archived {z.name}  ->  {RELEASES_DIR.name}\\archive\\")


# ---------------------------------------------------------------------------
# Publishing to GitHub (--publish)
# ---------------------------------------------------------------------------
def _run(cmd, capture=True):
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=capture, text=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        print("    " + out.replace("\n", "\n    "))
    return r.returncode, out


def _gh() -> "str | None":
    found = shutil.which("gh")
    if found:
        return found
    for cand in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GitHub CLI" / "gh.exe"):
        if cand.is_file():
            return str(cand)
    return None


def release_notes(version: str) -> str:
    sys.path.insert(0, str(ROOT))
    from networkguru.changelog import CHANGES
    entry = next((e for e in CHANGES if e[0] == version), None)
    if not entry:
        return f"{TOOL_TITLE} {version}"
    return f"## {TOOL_TITLE} {version} ({entry[1]})\n\n" + "".join(f"- {i}\n" for i in entry[2])


def publish(version: str) -> None:
    """Commit + tag + push the source, then create (or complete) GitHub Release v<version>."""
    z = RELEASES_DIR / f"{NAME_PREFIX}{version}.zip"
    if not z.is_file():
        sys.exit(f"ERROR: {z.name} not found - package first.")
    sha = z.with_name(z.name + ".sha256")
    if not sha.is_file():
        write_sha256(z)
    tag = f"v{version}"
    print(f"Publishing {tag} to GitHub ({GITHUB_REPO}) ...")
    rc, _ = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        sys.exit("ERROR: this folder is not a git repository.")
    rc, listing = _run(["git", "ls-files", "-co", "--exclude-standard"])
    tracked = [ROOT / p for p in listing.splitlines() if p.strip()]
    problems = secret_problems([p for p in tracked if p.is_file()])
    if problems:
        sys.exit("ERROR: refusing to commit - possible secrets:\n  " + "\n  ".join(problems))
    _run(["git", "add", "-A"])
    rc, _ = _run(["git", "diff", "--cached", "--quiet"])
    if rc != 0:
        _run(["git", "commit", "-m", f"{TOOL_TITLE} {version}"])
    rc, _ = _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"])
    if rc != 0:
        _run(["git", "tag", "-a", tag, "-m", f"{TOOL_TITLE} {version}"])
    rc, _ = _run(["git", "push", "origin", "HEAD:main", "--tags"])
    if rc != 0:
        sys.exit("ERROR: git push failed (signed in? run 'gh auth login' and 'gh auth setup-git' once).")
    gh = _gh()
    if not gh:
        sys.exit("ERROR: GitHub CLI (gh) not installed: winget install GitHub.cli")
    notes = Path(tempfile.gettempdir()) / f"networkguru_notes_{version}.md"
    notes.write_text(release_notes(version), encoding="utf-8")
    rc, _ = _run([gh, "release", "view", tag, "--repo", GITHUB_REPO])
    if rc == 0:
        rc, _ = _run([gh, "release", "upload", tag, str(z), str(sha), "--clobber", "--repo", GITHUB_REPO])
    else:
        rc, _ = _run([gh, "release", "create", tag, str(z), str(sha), "--repo", GITHUB_REPO,
                      "--title", f"{TOOL_TITLE} {version}", "--notes-file", str(notes)])
    if rc != 0:
        sys.exit("ERROR: GitHub Release failed - see above.")
    print(f"  Published https://github.com/{GITHUB_REPO}/releases/tag/{tag}")
    print("Building the test-install image on GitHub ...")
    if not build_image(gh, version):
        sys.exit(f"ERROR: the release zip is published, but the test-install image {IMAGE}:{version} is NOT - see above.")


def _gh_quiet(cmd: list[str]) -> tuple[int, str]:
    """A gh call that is polled, so it is not echoed every time."""
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def build_image(gh: str, version: str, sleep=time.sleep, clock=time.time, poll_s: float = 15.0,
                wait_s: float = IMAGE_WAIT_S) -> bool:
    """Start .github/workflows/image.yml on the release tag and follow it to the end. In 0.5.4 a tag push
    did not start it, so release.py starts it itself. True when the image was smoke-tested and pushed."""
    tag = f"v{version}"
    again = f"gh workflow run {IMAGE_WORKFLOW} --repo {GITHUB_REPO} --ref {tag}"
    started = clock()
    rc, out = _run([gh, "workflow", "run", IMAGE_WORKFLOW, "--repo", GITHUB_REPO, "--ref", tag])
    if rc != 0:
        print(f"  The image build did not start. Start it by hand:  {again}")
        return False
    m = re.search(r"/actions/runs/(\d+)", out)
    run_id = m.group(1) if m else _find_run(gh, tag, started, sleep, clock)
    if not run_id:
        print(f"  The image build was started, but its run was not found: https://github.com/{GITHUB_REPO}/actions")
        return False
    url = f"https://github.com/{GITHUB_REPO}/actions/runs/{run_id}"
    print(f"  Run: {url}")
    last_note = clock()
    while True:
        rc, out = _gh_quiet([gh, "run", "view", run_id, "--repo", GITHUB_REPO, "--json", "status,conclusion"])
        state = json.loads(out) if rc == 0 and out.startswith("{") else {}
        if state.get("status") == "completed":
            if state.get("conclusion") == "success":
                print(f"  Image published: {IMAGE}:{version} (also :latest)")
                print("  " + image_public_note(version))
                return True
            print(f"  The image build ended '{state.get('conclusion')}' - the image is NOT published. Log: {url}")
            print(f"  After a fix, run it again:  {again}")
            return False
        if clock() - started > wait_s:
            print(f"  The image build did not finish within {wait_s / 60:.0f} min - the image is NOT confirmed. Log: {url}")
            return False
        if clock() - last_note >= 60:
            print(f"    still building ({(clock() - started) / 60:.0f} min) ...", flush=True)
            last_note = clock()
        sleep(poll_s)


def _find_run(gh: str, tag: str, started: float, sleep, clock) -> str | None:
    """The run id of the workflow_dispatch run for `tag` started since `started` (older gh prints no URL)."""
    for _ in range(12):
        rc, out = _gh_quiet([gh, "run", "list", "--repo", GITHUB_REPO, "--workflow", IMAGE_WORKFLOW, "--event",
                             "workflow_dispatch", "--limit", "10", "--json", "databaseId,headBranch,createdAt"])
        if rc == 0 and out.startswith("["):
            for r in json.loads(out):
                at = _dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).timestamp()
                if r.get("headBranch") == tag and at >= started - 60:
                    return str(r["databaseId"])
        sleep(5)
    return None


def image_public_note(version: str) -> str:
    """Can anyone pull it? An anonymous pull token from ghcr.io, then the version's manifest."""
    name = IMAGE.split("/", 1)[1]
    try:
        with urllib.request.urlopen(f"https://ghcr.io/token?scope=repository:{name}:pull", timeout=15) as r:
            token = json.loads(r.read().decode("utf-8")).get("token", "")
        req = urllib.request.Request(f"https://ghcr.io/v2/{name}/manifests/{version}", method="HEAD", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"})
        with urllib.request.urlopen(req, timeout=15):
            return "Anyone can pull it (no login needed)."
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            owner = GITHUB_REPO.split("/")[0]
            return (f"WARNING: it can't be pulled without a login ({e.code}). Make the package public: "
                    f"https://github.com/users/{owner}/packages/container/{name.split('/')[1]}/settings")
        return f"Could not check whether it is public ({e.code})."
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"Could not check whether it is public ({e})."


def main() -> None:
    ap = argparse.ArgumentParser(description="Zip the dashboard by version and archive old releases.")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--publish", action="store_true", help="commit, tag, push and create the GitHub Release")
    args = ap.parse_args()
    if args.keep < 1:
        sys.exit("ERROR: --keep must be at least 1")
    version = read_version()
    check_changelog(version)
    print(f"Packaging version {version} ...")
    package(version)
    print("Archiving older releases ...")
    prune(args.keep)
    if args.publish:
        publish(version)


if __name__ == "__main__":
    main()
