# Network Guru

A browser dashboard for one autonomous site. It shows whether the comms chain **Mobius Server ↔ Mobius Clients ↔ vehicles** is healthy. It also covers the infrastructure in between: Rajant, Celona, Starlink, Tachyon 60 GHz, Ubiquiti, the trailers' switches, and their solar and battery.

It has a **Live** view and up to **30 days of History**. The pages are organised into three tabs: Vehicles, Control Room and Infrastructure. Each tab shows an overview card for every vehicle, desk and location, and each card opens a detail page.

> **Version 0.5.0 installs on a site server, but it still shows simulated data.** The data uses the **real Mendon layout**, built from the IPAM sheet and the BC|Commander snapshot. This version adds:
> - a Docker install for an Ubuntu server, including servers with no internet
> - a daily update check that asks an admin before anything is installed
> - the morning report to Teams and e-mail
>
> Next is 0.6.0, the first real device readers: Rajant SNMP v3, Tachyon read-only SNMP, switch SNMP, Cerbo GX and Mobius InfluxDB. See ARCHITECTURE.md → *Roadmap*.

Source: **github.com/Quesito350/Network-Guru** (private). Every version is a tagged GitHub Release with its zip and `.sha256`.

## Try it on this PC

1. Double-click `run_demo.bat`.
   - The first run installs the Python packages, which needs internet.
2. It builds 30 days of simulated history for the demo site. This takes about 15 s.
3. It then opens **http://127.0.0.1:8080**.
4. Close the window to stop it.

Command-line equivalent:

```
python run.py dev                                              # demo site
python run.py dev --inventory ..\sites\mendon\inventory.yaml   # the real Mendon layout
python run.py check-inventory ..\sites\mendon\inventory.yaml
```

**Admin page on this PC:** the console prints a one-time admin password at every start. It is never stored.

**History between runs:** run `python tools\fetch_vm.py` once. It downloads VictoriaMetrics v1.154.0 into `tools\vm\` with a checksum check. `dev` then starts it by itself and keeps 30 days of history in `%LOCALAPPDATA%\NetworkGuru\`. A restart only fills the gap since the last run. Without it, history lives in memory and is rebuilt on each start.

## Test install from the published image (Docker Compose)

Every release is also published as a Docker image on GitHub: `ghcr.io/quesito350/network-guru`. A test PC with Docker Desktop (or any Docker host) needs no Git, no build, no Ubuntu and no `install.sh`: two files and a `site` folder.

```
mkdir C:\NetworkGuru; cd C:\NetworkGuru
curl.exe -L -o compose.yaml https://raw.githubusercontent.com/Quesito350/Network-Guru/main/compose.yaml
curl.exe -L -o .env https://raw.githubusercontent.com/Quesito350/Network-Guru/main/.env.example
mkdir site                          (put the site's inventory.yaml in it; without one you get the demo)
docker compose up -d
```

- **Update:** `docker compose pull`, then `docker compose up -d`.
- **Admin password:** a one-time password is printed by `docker compose logs app`. A fixed one and the other secrets can go in `dev-secrets\secrets.env`.
- **The image** is built by `.github/workflows/image.yml` from `deploy/Dockerfile.dev` on each version tag, and only pushed after a smoke test. It is the server image, with the release's Python packages, checked against `deploy/requirements.lock`.
- It has no in-app updates or rollback. The Admin page shows the update commands instead.
- **From a git checkout** (unreleased changes): `docker compose -f compose.yaml -f compose.build.yaml up -d --build`.

The full steps, with help for old installs and for "git is not recognized", are in `deploy/WINDOWS_TEST.md` → *Way A*.

## Install on a site server (Ubuntu, Docker)

You need an Ubuntu Server 22.04 or 24.04 machine (or a Proxmox VM) with Docker Engine and the compose plugin. A server without internet needs Docker installed beforehand: the Ubuntu packages `docker.io` and `docker-compose-v2`.

1. **On this PC:**
   - `build.bat` makes `..\NetworkGuru_v<version>.zip`, which includes the Linux Python packages.
   - `python tools\fetch_images.py` writes `..\images\` with the two Docker images and `SHA256SUMS`.
2. Copy the zip, the `images` folder and the site's `..\sites\<site>\inventory.yaml` to the server.
3. **On the server:**
   ```bash
   python3 -m zipfile -e NetworkGuru_v0.5.0.zip .
   cd NetworkGuru_v0.5.0
   sudo bash deploy/install.sh --images ../images --inventory ../inventory.yaml
   ```

`install.sh` does the following:
- It checks the server.
- It loads the images.
- It asks for each secret with hidden input and stores them in `/opt/networkguru/secrets/secrets.env` (mode 600). The secrets are the admin password, the Teams webhook and the mail login, plus an optional GitHub token (not needed: the repository is public).
- It builds the app image with no internet and starts it.

The dashboard then runs at `http://<server>:8080`. It starts in simulated mode, clearly marked, until the real drivers arrive (`--mode live` switches it). Re-run `install.sh` at any time: it keeps the data. `--secrets` asks for the secrets again.

**Never** remove the Docker volumes: they hold the history and the events. `deploy/HARDWARE_TEST.md` is the checklist for a first install.

**Updates:**
- Every morning at 06:00 (site time) the server checks GitHub for a newer release.
- A banner offers **Review update**. Only a signed-in admin can **Approve**, **Remind me tomorrow** or **Skip this version**.
- An approved update is checked, then built and installed by the server's own updater. If the new version is not healthy within a minute, the updater goes back to the previous one.
- **Sites without internet:** upload the zip and its `.sha256` on the **Admin** page.

**Morning report:**
- It is sent every day at 06:30 to Teams and/or e-mail, including when everything is green.
- Set it up on the Admin page. Preview it on the **Morning report** page.

## A site's devices: one spreadsheet

Site data lives **outside this folder and outside git**, in `..\sites\`. It holds IPs, names and serials of real sites.

```
python run.py import-ipam   "<site IPAM>.xlsx"             --sheet ..\sites\devices.xlsx --site mendon
python run.py import-rajant "<site snapshot>.mesh"         --sheet ..\sites\devices.xlsx --site mendon
python run.py import-devices ..\sites\extra-devices.csv    --sheet ..\sites\devices.xlsx --site mendon
python run.py build-inventory ..\sites\devices.xlsx --site mendon --out ..\sites\mendon\inventory.yaml
```

- **Import steps:**
  - **IPAM sheet:** trailers, COW, switch, Cerbo GX, Rajant, and the Tachyon pairs with their link name, master/slave end and install RSSI. The login rows at the top of that sheet are skipped and never read.
  - **BC|Commander snapshot:** reads serial, name, model, firmware, IP, MAC and DHCP/static for every BreadCrumb. It matches the IPAM rows by IP and adds vehicles and Tremonton. Mesh keys and passphrases in the snapshot are never read.
  - **devices.csv:** the V&V radio-monitor format (`name, site, type, ip, serial, model, firmware`), plus optional `group, kind, mobius_asset`.
- **`devices.xlsx`** has a **Sites** sheet, a **Groups** sheet (vehicles, trailers, COW, towers, stations) and a **Devices** sheet, with drop-down lists. Edit it freely: re-imports keep your edits and only refresh IPs, firmware, serials and MACs.
  - **Sites:** one row per site, with the name shown in the dashboard, the time zone, GPS (for the solar figures), the Mobius InfluxDB URL, the site's *Subnets* (where the snapshot import puts a Rajant on that site) and its *Infrastructure subnet* (where an unnamed Rajant is a tower). `build-inventory` reads it. Without a row, it uses defaults and says so.
  - **Vehicles:** fill in their *Mobius asset* to get VCU and GNSS checks (set *Has VAI* for VAI).
  - **Monitor = no:** marks spare or turned-off devices.
  - **No password columns.** Logins are named once per device type and typed on the server.

## Things to try

- **Mendon backhaul (redundant: Tachyon links + Rajant mesh):**
  - **Trailer goes dark:** inject an outage on Trailer 1846's **Switch**. Within about 30 s it is one Critical alarm, "No device at Trailer 1846 answers", and its devices show "Unreachable via Switch". Trailers 1849 and 1850 stay up on the mesh, with only a Warning that their 60 GHz link toward 1846 is down.
  - **Link drops:** on a Tachyon's own page choose **"60 GHz link down"**. Both ends turn Warning ("60 GHz link COW-T47 down") and the trailer stays OK on the mesh.
- **60 GHz links** (on a trailer page): both ends of each link, signal now against signal at install.
- **DHCP devices** (Tremonton radios) show a DHCP tag. When their address changes, it appears in Events as "Address changed".
- **Events** (top right): every health, address and firmware change. Click one to jump to that device at that time.

## Files

| Path | What |
|---|---|
| `networkguru\` | the dashboard (Python package + web pages in `web\static\`) |
| `networkguru\importers\` | IPAM / BC\|Commander / CSV importers, `devices.xlsx`, `build-inventory` |
| `networkguru\rules_default.yaml` | health limits (starting values for ops to review) |
| `examples\inventory.example.yaml` | the demo site |
| `tools\fetch_vm.py` | downloads VictoriaMetrics for this PC |
| `deploy\` | the server: `install.sh`, `compose.yaml`, `Dockerfile`, the host updater `ng_updater.py` and its systemd units, `requirements.lock`, the hardware test checklist |
| `tools\fetch_wheels.py` | Linux Python packages for the server image (`deploy\wheels`, in the zip only) |
| `tools\fetch_images.py` | the Docker images for an offline server (`..\images\`) |
| `tests\` | `python -m pytest` (VictoriaMetrics tests run when `tools\vm\` has the exe) |
| `build.bat` | wheels, tests, then release zip, then publish to GitHub (`build.bat nopublish` to skip) |
| `ARCHITECTURE.md`, `REQUIREMENTS.md` | design and requirements, updated every version |
