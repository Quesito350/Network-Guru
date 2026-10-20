# Network Guru on a Windows test PC (Docker Desktop)

The real deployment is a Linux server (see `HARDWARE_TEST.md`). This page is for trying Network Guru on a Windows PC that is on the site network and has **Docker Desktop**. There are two ways:

| | **Way A: from git with `docker compose`** | **Way B: the release with `install.sh`** |
|---|---|---|
| Good for | Trying each new version quickly | Testing the real site-server install |
| Runs from | PowerShell, in a git checkout | An Ubuntu window (WSL) |
| Update | `git pull`, then `docker compose up -d --build` | Admin page: Approve (or upload the zip) |
| Needs | Git, internet while building | The release zip, Ubuntu in WSL |
| Not included | In-app updates, the updater, rollback | (everything) |

Run only one of them at a time on port 8080 (or give Way A another port, see A4).

## Way A: from git with `docker compose` (simplest)

### A1. Docker Desktop and Git

- **Docker Desktop → General:** *Use the WSL 2 based engine* and *Start Docker Desktop when you sign in* are on. No Ubuntu is needed for Way A.
- **Git:** in PowerShell run `winget install --id Git.Git -e`, then open a new PowerShell window.

### A2. Get the code

```powershell
cd C:\
git clone https://github.com/Quesito350/Network-Guru.git
cd Network-Guru
```

The repository is public, so no GitHub sign-in is needed.

### A3. The site's inventory

Copy the site's `inventory.yaml` (from `Documents\Network Guru\sites\<site>\` on the build PC) to `C:\NetworkGuru\inventory.yaml`. It is not in git, on purpose.

Find the PC's address with `ipconfig` (its IPv4 address on the site network).

### A4. Settings

```powershell
copy .env.example .env
notepad .env
```

Set at least these two (use forward slashes in the path):

```
NG_INVENTORY=C:/NetworkGuru/inventory.yaml
NG_PUBLIC_URL=http://<PC-IP>:8080/
```

- `.env` is git-ignored and holds no secrets.
- Without `NG_INVENTORY`, the dashboard shows the built-in demo site.
- If Way B is installed and running on this PC, either stop it (`docker compose -p networkguru stop`) or set `NG_PORT=8081`.

### A5. Start it

```powershell
docker compose up -d --build
```

- The first build downloads the Python base image and the 22 Python packages (a few minutes). The packages are exactly the ones in the release, checked against the hashes in `deploy/requirements.lock`.
- After about a minute, `docker compose ps` shows `networkguru-dev-app-1` as **healthy**, plus `networkguru-dev-victoriametrics-1`.
- The containers have the same lock-down as on a site server (user 10001, read-only, no capabilities). VictoriaMetrics has no port on the PC at all.

### A6. Open it

- **On the test PC:** http://localhost:8080
- **From another PC:** `http://<PC-IP>:8080`
- If Windows asks whether to allow *Docker Desktop Backend*, allow it on domain and private networks.
- If another PC still can't connect, run this in PowerShell as administrator:

```powershell
New-NetFirewallRule -DisplayName "Network Guru 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

### A7. Admin sign-in

```powershell
docker compose logs app
```

The log shows "Admin page: one-time password for this start". It is never stored, and there is a new one each time the app starts (after an update or a restart).

**A fixed password, the GitHub token or the Teams / e-mail secrets (optional):**

```powershell
mkdir dev-secrets
copy deploy\secrets.env.example dev-secrets\secrets.env
docker compose run --rm app python -m networkguru hash-password
notepad dev-secrets\secrets.env
```

- `hash-password` asks for the password twice (hidden) and prints its hash. Paste the hash after `NETWORKGURU_ADMIN_PASSWORD_HASH=`. Never put the password itself in the file.
- Fill in only what you want to test, for example the Teams webhook URL for the morning report. A GitHub token is not needed.
- The app reads the file again within seconds; no restart is needed.
- `dev-secrets\` is git-ignored, but it is a plain-text file on this PC. Use it only on a test PC, and delete it when the testing is done.

### A8. Update

```powershell
cd C:\Network-Guru
git pull
docker compose up -d --build
```

- Only what changed is rebuilt. The app restarts on the new version.
- The history and the events stay: they are in the Docker volumes.
- The Admin page shows these commands instead of Approve and Upload. A newer release still shows its banner as a reminder.

### A9. Stop, start, change settings

| To | Run |
|---|---|
| Stop | `docker compose stop` |
| Start again | `docker compose start` |
| See the logs | `docker compose logs -f app` |
| After editing the inventory | `docker compose restart app` |
| After editing `.env` | `docker compose up -d` |

**Never run `docker compose down -v`:** the volumes hold the history and the events. Plain `docker compose down` keeps them.

### A10. Testing: `HARDWARE_TEST.md` with Way A

| Checklist section | With Way A |
|---|---|
| 1. Install | Not used. A2-A7 replace it |
| 2. What runs | The names are `networkguru-dev-app-1` and `networkguru-dev-victoriametrics-1`. VictoriaMetrics has no published port. The network is `networkguru-dev_ng` (`172.29.251.0/24`) |
| 3. Persistence | Restart Windows instead of `sudo reboot`. For the port and mode steps, change `NG_PORT` / `NG_MODE` in `.env` and run `docker compose up -d` |
| 4. Admin page | Same, with the password from A7 |
| 5. Updates, 6. Offline site | Need Way B: Way A refuses Approve and Upload on purpose |
| 7. Morning report | Same, after putting the secrets in `dev-secrets\secrets.env` |

**Done when:** `docker compose ps` shows the app healthy, the dashboard opens from a second PC at `http://<PC-IP>:8080` with the site's layout, the Admin sign-in works, and `git pull` + `docker compose up -d --build` brings in the next version.

## Way B: the release with `install.sh` (the real server install)

This tests what runs on a site server, including the admin-approved updates and rollback.

### How it fits together

- The installer, `deploy/install.sh`, needs Linux with systemd, because the update service is a systemd unit. So it runs inside an **Ubuntu window (WSL)** and uses Docker Desktop's Docker through *WSL integration*.
- **Ports:**
  - Docker Desktop publishes the dashboard port (8080) on Windows itself, so other PCs reach it at the Windows PC's address. WSL mirrored networking is not needed, and is best left off with Docker Desktop.
  - VictoriaMetrics (8428) stays on `127.0.0.1` only.
- **Link address:** the installer's default dashboard link (used in the morning report) would be WSL's internal 172.x address. Always give the PC's real address with `--url`.
- **Updater:** it lives in Ubuntu and only runs while Ubuntu is running. The containers themselves run whenever Docker Desktop runs.
- **Licence:** Docker Desktop is free only for companies under 250 people and under $10M revenue. Above that it needs a paid subscription.

### B1. Docker Desktop settings

- **General:** *Use the WSL 2 based engine* is on.
- **General:** *Start Docker Desktop when you sign in* is on, so the containers come back after a restart.
- **Resources → WSL integration:** turn it on for **Ubuntu-24.04** (after B2), then *Apply & restart*.
- If `C:\Users\<you>\.wslconfig` has `networkingMode=mirrored`, remove that line.

### B2. Ubuntu in WSL

In PowerShell as administrator (skip this if Ubuntu-24.04 is already there):

```powershell
wsl --install -d Ubuntu-24.04
```

Open **Ubuntu** from the Start menu and create a Linux user. Then turn on systemd:

```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```

In PowerShell run `wsl --shutdown`, then reopen Ubuntu. Run `docker version`: it must show **Server: Docker Desktop**.

### B3. Files

Put these in `C:\NetworkGuru` on the test PC:

- `NetworkGuru_v<version>.zip`, from the GitHub release or `Documents\Network Guru\` on the build PC
- the site's `inventory.yaml`, from `Documents\Network Guru\sites\<site>\`

Find the PC's address with `ipconfig` (its IPv4 address on the site network).

### B4. Install

In the Ubuntu window, with the version and address filled in:

```bash
mkdir -p ~/ng && cp /mnt/c/NetworkGuru/NetworkGuru_v<version>.zip /mnt/c/NetworkGuru/inventory.yaml ~/ng/ && cd ~/ng && python3 -m zipfile -e NetworkGuru_v<version>.zip . && cd NetworkGuru_v<version>
sudo bash deploy/install.sh --inventory ../inventory.yaml --url http://<PC-IP>:8080/
```

- Docker Desktop's Docker is found, so there is no Docker install question.
- The two images are pulled from Docker Hub.
- **GitHub token:** press Enter to skip it (the repository is public, so the update check needs none).
- **Admin password:** at least 10 characters, typed twice.
- **Teams / e-mail:** Enter to skip, unless you want to test the morning report.
- It ends with "running and healthy".

### B5. Open it

- **On the test PC:** http://localhost:8080
- **From another PC:** `http://<PC-IP>:8080`
- If Windows asks whether to allow *Docker Desktop Backend*, allow it on domain and private networks.
- If another PC still can't connect, run this in PowerShell as administrator:

```powershell
New-NetFirewallRule -DisplayName "Network Guru 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

### B6. Testing: `HARDWARE_TEST.md` with these changes

| Checklist says | On Windows |
|---|---|
| `sudo reboot` | Restart Windows and sign in (Docker Desktop starts the containers), then open Ubuntu |
| `sudo ss -tlnp` | `docker ps` (Ports column), or `netstat -ano \| findstr :8080` in PowerShell; 8428 must be `127.0.0.1` only |
| `journalctl -u networkguru-updater -f` | Same, in the Ubuntu window |
| Update, rollback and "reboot during an update" steps | Keep the Ubuntu window open (minimised is fine), because the updater runs inside Ubuntu |
| Offline (★) steps | Turn the PC's network off; Docker Desktop keeps the loaded images |

Checks that are not meaningful on Windows: the `/var/lib/docker` free-space check (Docker Desktop keeps its data in its own disk image) and the subnet-overlap warning (WSL routes differ from a real server's).

### Done when

- `docker ps` shows `networkguru-app-1` (healthy) and `networkguru-victoriametrics-1`.
- The dashboard opens from a second PC at `http://<PC-IP>:8080`, with the Mendon layout ("Simulated data" until the 0.6.0 drivers).
- **Admin** sign-in works.

### Stop or remove

- **Stop:** in Ubuntu, `sudo docker compose --project-directory /opt/networkguru -f /opt/networkguru/compose.yaml stop`.
- **Never run `down -v`:** the volumes hold the history and the events.
