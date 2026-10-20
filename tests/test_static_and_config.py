import re

from networkguru import __version__
from networkguru.changelog import CHANGES
from networkguru.config import mask, register_secret, secret

from .conftest import ROOT

STATIC = ROOT / "networkguru" / "web" / "static"


def test_static_files_work_offline():
    """Sites can be offline: nothing in the pages may load from the internet."""
    bad = []
    for f in STATIC.rglob("*"):
        if f.suffix in (".js", ".css", ".html"):
            for m in re.finditer(r"https?://[^\s\"'`)]+", f.read_text(encoding="utf-8")):
                if not m.group(0).startswith("http://www.w3.org/2000/svg"):
                    bad.append(f"{f.name}: {m.group(0)}")
    assert not bad, bad


def test_index_references_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:src|href)="/static/([^"]+)"', html):
        assert (STATIC / ref).is_file(), ref


def test_js_imports_resolve():
    for f in STATIC.rglob("*.js"):
        for ref in re.findall(r'from "(\.[^"]+)"', f.read_text(encoding="utf-8")):
            assert (f.parent / ref).resolve().is_file(), f"{f.name} imports {ref}"


def test_changelog_matches_version():
    assert CHANGES[0][0] == __version__


def test_release_refuses_secrets(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("release", ROOT / "release.py")
    rel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rel)
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    good = tmp_path / "inv.yaml"
    good.write_text("devices:\n  - {id: a, type: mikrotik, user_ref: MT_RO_USER, password_ref: MT_RO_PASS}\n"
                    "mobius: {token_ref: null}\n", encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("devices:\n  - {id: a, type: mikrotik, password: hunter2}\nsnmp:\n  community: public\n", encoding="utf-8")
    env = tmp_path / "secrets.env"
    env.write_text("X=1\n", encoding="utf-8")
    wg = tmp_path / "wg.conf"
    wg.write_text("[Interface]\nPrivateKey = abc\n", encoding="utf-8")
    assert rel.secret_problems([good]) == []
    problems = rel.secret_problems([bad, env, wg])
    assert any("bad.yaml: 'password'" in p for p in problems)
    assert any("bad.yaml: 'community'" in p for p in problems)
    assert any("secrets.env" in p for p in problems) and any("wg.conf" in p for p in problems)


def test_example_inventory_has_no_secrets():
    import importlib.util
    spec = importlib.util.spec_from_file_location("release2", ROOT / "release.py")
    rel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rel)
    assert rel.secret_problems([ROOT / "examples" / "inventory.example.yaml", ROOT / "networkguru" / "rules_default.yaml"]) == []


def test_secret_masking(monkeypatch):
    monkeypatch.setenv("SH_TEST_PASS", "hunter2-very-secret")
    assert secret("SH_TEST_PASS") == "hunter2-very-secret"
    assert mask("login failed for hunter2-very-secret at 10.0.0.1") == "login failed for ***** at 10.0.0.1"
    assert mask("GET /x?token=abc123&y=1") == "GET /x?token=*****"
    register_secret("ab")                       # too short to mask safely - ignored
    assert mask("about") == "about"
    assert secret("SH_TEST_NOT_SET") is None


def _release(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "release.py")
    rel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rel)
    return rel


def test_secrets_file_is_parsed_and_reloaded(tmp_path, monkeypatch):
    import os
    from networkguru.config import load_secrets_file
    f = tmp_path / "secrets.env"
    f.write_bytes(b"# comment\r\nNETWORKGURU_SMTP_PASSWORD=pa$$w'rd=x\r\nlower=ignored\r\nQUOTED='q-value-1'\r\nEMPTY=\r\n")
    vals = load_secrets_file(f)
    assert vals == {"NETWORKGURU_SMTP_PASSWORD": "pa$$w'rd=x", "QUOTED": "q-value-1"}
    monkeypatch.setenv("NETWORKGURU_SECRETS", str(f))
    monkeypatch.delenv("QUOTED", raising=False)
    assert secret("QUOTED") == "q-value-1"
    assert mask("sent pa$$w'rd=x to the server") == "sent ***** to the server"
    f.write_text("QUOTED=changed-value\n", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    assert secret("QUOTED") == "changed-value"                      # install.sh --secrets needs no restart
    f.write_bytes(b"\xef\xbb\xbfQUOTED=from-notepad\r\n")              # saved by Notepad with a byte-order mark
    assert load_secrets_file(f) == {"QUOTED": "from-notepad"}


def test_missing_secrets_sees_the_secrets_file(tmp_path, monkeypatch, mini_inv):
    f = tmp_path / "secrets.env"
    f.write_text("TACHYON_RO_USER=ro\nTACHYON_RO_PASS=ro-pass\n", encoding="utf-8")
    monkeypatch.setenv("NETWORKGURU_SECRETS", str(f))
    assert "TACHYON_RO_PASS" not in mini_inv.missing_secrets()
    assert "TACHYON_RO_PASS" in mini_inv.missing_secrets(env={})


def test_webhook_keys_are_masked():
    url = "https://prod-1.westus." + "logic.azure.com/workflows/x/invoke?api-version=1&sig=" + "Z" * 43
    assert "Z" * 43 not in mask(f"POST {url} failed")


def test_release_refuses_tokens_webhooks_and_hashes(tmp_path, monkeypatch):
    rel = _release("release3")
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    files = {
        "install.sh": "TOKEN=" + "ghp_" + "a1B2" * 9 + "\n",
        "Dockerfile": "ENV X=" + "github_pat_" + "x" * 82 + "\n",
        "notes.md": "url https://prod-1.westus." + "logic.azure.com:443/workflows/x/invoke?sig=" + "k" * 43 + "\n",
        "units.service": "Environment=H=" + "scrypt:32768:8:1:" + "A" * 24 + ":" + "B" * 44 + "\n",
        ".env": "NG_VERSION=1\n",
        "ok.sh": "echo 'read -r -s token'\n",
        "secrets.env.example": "NETWORKGURU_GITHUB_TOKEN=\n",
    }
    paths = []
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    problems = rel.secret_problems(paths)
    joined = "\n".join(problems)
    for needle in ("install.sh: contains a GitHub token", "Dockerfile: contains a GitHub token",
                   "notes.md: contains a Teams", "units.service: contains an admin password hash", ".env: secrets file"):
        assert needle in joined, joined
    assert "ok.sh" not in joined and "secrets.env.example" not in joined


def test_release_ships_the_server_files_without_secrets():
    rel = _release("release4")
    shipped = {f.relative_to(ROOT).as_posix() for f in rel.files_to_ship()}
    for need in ("deploy/install.sh", "deploy/compose.yaml", "deploy/Dockerfile", "deploy/ng_updater.py",
                 "deploy/requirements.lock", "deploy/HOST_VERSION", "tools/fetch_wheels.py", "tools/fetch_images.py",
                 "compose.yaml", ".env.example", "deploy/Dockerfile.dev", "deploy/Dockerfile.dev.dockerignore"):
        assert need in shipped
    assert rel.secret_problems(rel.files_to_ship()) == []


# Made-up ranges only: the demo site, the tests, Docker's subnets, Starlink's fixed dish address and
# the documentation ranges. Real site addresses live in the sites folder next to the source, never here.
ALLOWED_PRIVATE = ["10.200.0.0/16", "10.0.0.0/16", "10.8.0.0/15", "172.17.0.0/16", "172.29.250.0/23",
                   "192.168.100.1/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"]


def test_no_real_site_addresses():
    """The repository is public: every private IPv4 address in it must be in a made-up range."""
    import ipaddress
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
        files = [ROOT / n for n in out.splitlines() if n.strip()]
    except (OSError, subprocess.CalledProcessError):                  # an unpacked release: no .git
        files = _release("release5").files_to_ship()
    rel = _release("release6")
    allowed = [ipaddress.ip_network(n) for n in ALLOWED_PRIVATE]
    found = []
    for f in files:
        if not f.is_file() or f.suffix.lower() in rel.BINARY:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text):
            try:
                ip = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            if ip.is_loopback or ip.is_unspecified or ip.is_reserved or not ip.is_private:
                continue
            if not any(ip in n for n in allowed):
                found.append(f"{f.relative_to(ROOT).as_posix()}: {ip}")
    assert len(files) > 50 and not found, found


def test_wheels_match_the_lock():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_wheels_t", ROOT / "tools" / "fetch_wheels.py")
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    if not list((ROOT / "deploy" / "wheels").glob("*.whl")):
        import pytest
        pytest.skip("deploy/wheels not fetched (build.bat fetches them before the tests)")
    assert fw.problems() == []
    assert set(fw.pins()) <= set(fw.read_lock())
