# Network Guru on a Windows test PC (Docker Desktop)

The real deployment is a Linux server (see `HARDWARE_TEST.md`). This page is for trying Network Guru on a Windows PC that is on the site network and has **Docker Desktop**. There are two ways:

| | **Way A: the image from GitHub** | **Way B: the release with `install.sh`** |
|---|---|---|
| Good for | Trying each new version quickly | Testing the real site-server install |
| Runs from | PowerShell, in one folder with two files | An Ubuntu window (WSL) |
| Update | `docker compose pull`, then `docker compose up -d` | Admin page: Approve (or upload the zip) |
| Needs | Docker Desktop and internet (no Git, no build) | The release zip, Ubuntu in WSL |
| Not included | In-app updates, the updater, rollback | (everything) |

Run only one of them at a time on port 8080 (or give Way A another port, see A3).

## Way A: the image from GitHub (simplest)

Each release builds the image on GitHub and publishes it as `ghcr.io/quesito350/network-guru`. The PC only downloads it.

### A1. Docker Desktop

**Docker Desktop → General:** *Use the WSL 2 based engine* and *Start Docker Desktop when you sign in* are on. No Ubuntu and no Git are needed for Way A.

### A2. The folder and its two files

In PowerShell:

```powershell
mkdir C:\NetworkGuru -Force
cd C:\NetworkGuru
curl.exe -L -o compose.yaml https://raw.githubusercontent.com/Quesito350/Network-Guru/main/compose.yaml
curl.exe -L -o .env https://raw.githubusercontent.com/Quesito350/Network-Guru/main/.env.example
mkdir site -Force
```

Then copy the site's `inventory.yaml` (from `Documents\Network Guru\sites\<site>\` on the build PC) into `C:\NetworkGuru\site\`. If it's already in `C:\NetworkGuru`, move it: `move inventory.yaml site\`.

- It holds the site's real addresses: keep it off anything public. It is never in git or in the image.
- Without it, the dashboard shows the built-in demo site.

Find the PC's address with `ipconfig` (its IPv4 address on the site network).

### A3. Settings

```powershell
notepad .env
```

Set the address other PCs open (used for the link in the morning report):

```
NG_PUBLIC_URL=http://<PC-IP>:8080/
```

- `.env` holds no secrets.
- `NG_TAG` empty means the newest version. Set a version (for example `0.6.0`) to stay on it.
- `NG_MODE=live` checks the **real devices** with ping and their ports (then `docker compose up -d`). The site firewall must let this PC ping the device networks; if ping is blocked, the port checks still count. VCU, GNSS and VAI stay Unknown until the Mobius reader (0.7.0).
- If Way B is installed and running on this PC, either stop it (see *Old installs on this PC* below) or set `NG_PORT=8081`.

### A4. Start it

```powershell
docker compose up -d
```

- The first start downloads the Network Guru image and VictoriaMetrics (about a minute on a normal connection).
- After about a minute, `docker compose ps` shows `networkguru-dev-app-1` as **healthy**, plus `networkguru-dev-victoriametrics-1`.
- The containers have the same lock-down as on a site server (user 10001, read-only, no capabilities). VictoriaMetrics has no port on the PC at all.

### A5. Open it

- **On the test PC:** http://localhost:8080
- **From another PC:** `http://<PC-IP>:8080`
- If Windows asks whether to allow *Docker Desktop Backend*, allow it on domain and private networks.
- If another PC still can't connect, run this in PowerShell as administrator:

```powershell
New-NetFirewallRule -DisplayName "Network Guru 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

### A5b. Real devices (live mode)

Set `NG_MODE=live` in `.env` (then `docker compose up -d`). For the devices' own values, put their logins in `C:\NetworkGuru\dev-secrets\secrets.env` (see A6 for the file), one per line:

```
RAJANT_SNMP_USER=...
RAJANT_SNMP_AUTH=...
RAJANT_SNMP_PRIV=...
TACHYON_SNMP_RO=...
SWITCH_SNMP_RO=...
CELL_SNMP_RO=...
ALMS_CLIENT_ID=...
ALMS_CLIENT_SECRET=...
NETMAKER_TOKEN=...
```

Only the lines for devices the site has are needed (the Admin page lists every name the inventory uses, set or not): `CELL_SNMP_RO` for Sierra RV55 / MikroTik / OpenWrt modems, the two `ALMS_...` for Sierra XR80 / XR90 modems, `NETMAKER_TOKEN` for the WireGuard tunnels.

Then try one device of each kind before looking at the dashboard:

```powershell
docker compose exec app python -m networkguru probe-snmp <switch IP> --type switch
docker compose exec app python -m networkguru probe-snmp <Rajant IP> --type rajant
docker compose exec app python -m networkguru probe-modbus <Cerbo IP>
docker compose exec app python -m networkguru probe-snmp <modem IP> --type cell_modem --model RV55
docker compose exec app python -m networkguru probe-starlink <trailer router IP>:<NAT port>
docker compose exec app python -m networkguru probe-netmaker --url https://<netmaker api address>
docker compose exec app python -m networkguru probe-influx --url http://<Mobius server>:8086 --out /data/influx-layout.txt
docker compose cp app:/data/influx-layout.txt .
```

- Each probe prints the values it got, or why it could not (wrong login, no answer, port closed).
- `probe-influx` lists the Mobius database's names only - send `influx-layout.txt` so the next build can map the VCU, GNSS and VAI values. Without the mapping, each vehicle's page already shows everything Mobius has for it (**Mobius data**), and the Mobius Server's page lists the names Mobius uses that are not in the inventory.
- `probe-netmaker` lists the Netmaker host names: type each one into the **Netmaker host** column of its vehicle / location on the Groups sheet (and the API address into Sites → **Netmaker URL**), then rebuild the inventory.
- `probe-starlink` needs the router's dst-nat to the dish's **192.168.100.1:9201**; that `router IP:port` goes into the Starlink row's **API address**.
- Send the `probe-snmp` output of one Rajant and one Tachyon (it has no logins in it): the Tachyon file does not say the units of rx power and tx rate, so they are checked against the Tachyon's own page.
- On the Cerbo GX, switch Modbus TCP on (Settings → Services → Modbus TCP) and note the solar charger's unit ID for the **Modbus unit** column of the Devices sheet.
- **Find devices (0.9.0):** tell IT first, then `docker compose exec app python -m networkguru scan` lists what answers around the inventory's addresses (Mobius servers, InfluxDB, NTRIP casters, Cerbos, SNMP devices) and whether each is known, moved or new - nothing is kept. Then **Admin → Found on the network → Scan now** keeps the list: Add or Ignore each new one. A network with nothing from the inventory in it (e.g. the Mobius server's) goes in **Also scan these networks** there (Save, then Scan now) - no rebuild needed. Docker Desktop reaches the site network through the PC, so the PC must be on it (or on its VPN).

### A6. Admin sign-in

```powershell
docker compose logs app
```

The log shows "Admin page: one-time password for this start". It is never stored, and there is a new one each time the app starts (after an update or a restart).

**A fixed password, or the Teams / e-mail secrets (optional):**

```powershell
mkdir dev-secrets -Force
curl.exe -L -o dev-secrets\secrets.env https://raw.githubusercontent.com/Quesito350/Network-Guru/main/deploy/secrets.env.example
docker compose run --rm app python -m networkguru hash-password
notepad dev-secrets\secrets.env
```

- `hash-password` asks for the password twice (hidden) and prints its hash. Paste the hash after `NETWORKGURU_ADMIN_PASSWORD_HASH=`. Never put the password itself in the file.
- Fill in only what you want to test, for example the Teams webhook URL for the morning report. A GitHub token is not needed.
- The app reads the file again within seconds; no restart is needed.
- `dev-secrets\` is a plain-text file on this PC. Use it only on a test PC, and delete it when the testing is done.

### A7. Update

```powershell
cd C:\NetworkGuru
docker compose pull
docker compose up -d
```

- `pull` downloads the newest image; `up -d` restarts the app on it.
- The history and the events stay: they are in the Docker volumes.
- The Admin page shows these commands instead of Approve and Upload. A newer release still shows its banner as a reminder.
- Once in a while `compose.yaml` itself changes (the release notes say so). Then download it again with the `curl.exe` line from A2 before `pull`.

### A8. Stop, start, change settings

| To | Run |
|---|---|
| Stop | `docker compose stop` |
| Start again | `docker compose start` |
| See the logs | `docker compose logs -f app` |
| After changing `site\inventory.yaml` | `docker compose restart app` |
| After editing `.env` | `docker compose up -d` |

**Never run `docker compose down -v`:** the volumes hold the history and the events. Plain `docker compose down` keeps them.

### A9. Testing: `HARDWARE_TEST.md` with Way A

| Checklist section | With Way A |
|---|---|
| 1. Install | Not used. A2-A6 replace it |
| 2. What runs | The names are `networkguru-dev-app-1` and `networkguru-dev-victoriametrics-1`. VictoriaMetrics has no published port. The network is `networkguru-dev_ng` (`172.29.251.0/24`) |
| 3. Persistence | Restart Windows instead of `sudo reboot`. For the port and mode steps, change `NG_PORT` / `NG_MODE` in `.env` and run `docker compose up -d` |
| 4. Admin page | Same, with the password from A6 |
| 5. Updates, 6. Offline site | Need Way B: Way A refuses Approve and Upload on purpose |
| 7. Morning report | Same, after putting the secrets in `dev-secrets\secrets.env` |

**Done when:** `docker compose ps` shows the app healthy, the dashboard opens from a second PC at `http://<PC-IP>:8080` with the site's layout, the Admin sign-in works, and `docker compose pull` + `docker compose up -d` brings in the next version.

### Old installs on this PC

You don't need to delete anything for Way A: it uses its own names (`networkguru-dev-…`), its own data volumes and its own network.

- **An old git clone** (`C:\Network-Guru`, from 0.5.2 or 0.5.3) is no longer needed for Way A. You can delete it: `Remove-Item -Recurse -Force C:\Network-Guru`. Developers who still want a clone: see the next section.
- **An old `install.sh` install (Way B)** also wants port 8080. Check with `docker ps --format "{{.Names}}"`: `networkguru-app-1` (no "dev" in the name) is the old one. Stop it with `docker compose -p networkguru stop`, or give Way A `NG_PORT=8081`.
- **Optional clean-up** if you won't test Way B again: `docker compose -p networkguru down -v`. This deletes the old test install's own (simulated) data, not Way A's.
- **The old release zip** in `C:\NetworkGuru` is harmless: delete it or leave it.

### Developers: building the image from a git checkout

Only needed to try changes that are not released yet.

```powershell
cd C:\
git clone https://github.com/Quesito350/Network-Guru.git
cd Network-Guru
copy .env.example .env
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

The site's inventory goes in `site\` inside the checkout (git-ignored). To update: `git pull`, then the same `docker compose … --build` command.

**"git is not recognized"** means Git isn't installed, or PowerShell can't find it:

1. Close every PowerShell window and open a new one: a window opened before Git was installed can't see it. Try `git --version`.
2. Not installed? Run `winget install --id Git.Git -e --source winget`, or use the installer from https://git-scm.com/download/win with the default choices (ask IT if it needs admin rights).
3. Installed, but still not recognized? Find it:
   ```powershell
   Get-ChildItem "C:\Program Files\Git\cmd\git.exe", "C:\Program Files (x86)\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe" -ErrorAction SilentlyContinue | Select-Object FullName
   ```
   Then add that folder (without `git.exe`) to your PATH and open a new window:
   ```powershell
   [Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";C:\Program Files\Git\cmd", "User")
   ```
4. Or use the full path for now: `& "C:\Program Files\Git\cmd\git.exe" clone https://github.com/Quesito350/Network-Guru.git`.

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
- The dashboard opens from a second PC at `http://<PC-IP>:8080`, with the Mendon layout ("Simulated data" in sim mode).
- **Admin** sign-in works.

### Stop or remove

- **Stop:** in Ubuntu, `sudo docker compose --project-directory /opt/networkguru -f /opt/networkguru/compose.yaml stop`.
- **Never run `down -v`:** the volumes hold the history and the events.
