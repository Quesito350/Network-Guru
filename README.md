# Network Guru

A browser dashboard for one autonomous site. It shows whether the comms chain **Mobius Server ↔ Mobius Clients ↔ vehicles** is healthy. It also covers the infrastructure in between: Rajant, Celona, Starlink, Tachyon 60 GHz, Ubiquiti, the trailers' switches, and their solar and battery.

It has a **Live** view and up to **30 days of History**. The pages are organised into three tabs: Vehicles, Control Room and Infrastructure. Each tab shows an overview card for every vehicle, desk and location, and each card opens a detail page.

> **Version 0.6.0** adds:
> - a **Map** of the site: a pin per location in its live status colour, the vehicles and the 60 GHz links (satellite images when online, a plain grid offline)
> - the first **real device checks**: in live mode every device with an address is pinged and its service ports are checked
> - **Report a problem**: an admin downloads one zip for whoever looks into an issue
>
> It still starts in simulated mode on the **real Mendon layout**. Next is 0.7.0, the device readers: Rajant SNMP v3, Tachyon read-only SNMP, switch SNMP, Cerbo GX and Mobius InfluxDB. See ARCHITECTURE.md → *Roadmap*.

Source: **github.com/Quesito350/Network-Guru** (public). Every version is a tagged GitHub Release with its zip and `.sha256`.

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
- **The image** is built on GitHub by `.github/workflows/image.yml` from `deploy/Dockerfile.dev` with each release: `build.bat` starts it and waits for the result. It is only pushed after a smoke test. It is the server image, with the release's Python packages, checked against `deploy/requirements.lock`.
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

The dashboard then runs at `http://<server>:8080`. It starts in simulated mode, clearly marked; `--mode live` checks the real devices with ping and their ports (0.6.0). Re-run `install.sh` at any time: it keeps the data. `--secrets` asks for the secrets again.

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

## Map, live checks and problem reports

- **Status tiles** (top of each tab): click Critical, Warning, OK, Offline or Unknown to show only those cards; click it again or **Show all** for everything. The address keeps the choice (`#/vehicles?show=critical`), so a refresh or a shared link shows the same.
- **What has gone quiet** drops off Live: a vehicle, station or location none of whose devices has answered for an hour (fresh Mobius data counts as an answer) leaves the Live cards, the tile counts and the map. Each tab says how many are hidden, with a **Show them** switch. History, the group's own page, alerts and the morning report keep it. **Admin → Live view** sets the time (0 = never) and the tabs it applies to.
- **Map** (the Map tab):
  - a pin per location in its live status colour, the vehicles where their GNSS (or the Mobius data) last put them, and the 60 GHz links as lines
  - hover for the reason, click for the location's page
  - positions: the **Lat / Lon** columns of the Groups sheet in `devices.xlsx`; `import-rajant` fills them from the radios' GPS (a typed position wins), then `build-inventory`
  - background: **Admin → Map** (satellite, street map or none). The viewer's browser loads the images straight from the provider; offline it is a plain grid, with the pins still placed by GPS
- **Live checks** (`NG_MODE=live`, or `install.sh --mode live`):
  - every device with an address is pinged (4 echoes: latency and loss), and its service ports are checked (`checks: ["tcp:18080"]` in the inventory, added by type for the Mobius server, InfluxDB and the GNSS base; Starlink through its `target`)
  - a device that answers nothing for 3 polls is Offline; the existing "Unreachable via" logic works as with the simulator
  - the device readers (below) add the devices' own values
  - the site firewall must let the server ping the device networks; when ICMP is filtered, the port checks still count
- **Report a problem** (header): a signed-in admin downloads one zip with the version, status, the last 7 days of events, recent log lines, the inventory and any errors the page caught. Secrets are never in it. A red dot on the button means the page caught an error.

## Discovery scan: find devices on the network (0.9.0)

In live mode the dashboard can look for the site's devices itself (**Admin → Found on the network**: **Scan now**, and every night at 02:00 site time).

- **Where:** the /24 around every address in the inventory, the Sites sheet's **Subnets**, and the networks in **Also scan these networks** on the Admin page (e.g. the Mobius server's network - saved at once, no rebuild). Private ranges only (10.x, 172.16-31.x, 192.168.x), at most 65,536 addresses per scan, about 300 a second.
- **How it tells what it found:**
  - **Mobius servers** by port 18080, **InfluxDB** by its `/ping` answer on 8086, **NTRIP casters** (GNSS bases) by their sourcetable on 2101, **Cerbo GX** by Modbus. No login needed. Windows PCs give their name and MAC (NetBIOS).
  - **Radios, routers, modems, switches** by SNMP: first a request with no login (any SNMP device answers it), then only those devices get the site's own logins - the ones the inventory names. Logins are never guessed.
- **What happens:**
  - A device that **moved** (proven by its serial, MAC or name) is followed to its new address by itself - "Address changed" in Events.
  - A **new** device waits on the Admin page: **Add…** puts it on the dashboard at once (a group and a name; no restart), **Ignore** hides it. The site's devices.xlsx is not changed: **Download new ones for devices.xlsx** gives rows for `import-devices`.
  - An address that now **answers as something else** is listed, never followed.
- **Tell IT before the first scan** - network security tools may notice it. Only reads; nothing on any device is changed.
- **Try it from a terminal first:** `python -m networkguru scan` (on the test install: `docker compose exec app python -m networkguru scan`) prints what answers, without keeping anything.

## Connecting the devices (live mode readers)

In live mode each device is pinged and its ports checked, and its **reader** asks the device itself for its values. The logins go in `secrets.env` by name (`install.sh --secrets` asks for every name the inventory uses; the test install: `dev-secrets\secrets.env`). They never go in the inventory.

| Devices | Reader | Needs | Login names (secrets.env) |
|---|---|---|---|
| Rajant BreadCrumbs | SNMP v3 authPriv (SHA1 / AES128): uptime, traffic, temperature, channel busy and noise per radio, dropped packets | the SNMP v3 user on the radios (the V&V change request), with this server as the source | `RAJANT_SNMP_USER`, `RAJANT_SNMP_AUTH`, `RAJANT_SNMP_PRIV` |
| Tachyon 60 GHz | SNMP v2c, read-only: the link's signal (both ends), SNR, MCS, PHY rate, radio temperature, CPU / memory | a read-only community | `TACHYON_SNMP_RO` |
| Trailer switches | SNMP v2c, read-only (ports up, PoE watts) | the RO community they already have | `SWITCH_SNMP_RO` |
| Cerbo GX | Modbus TCP (battery, solar, load; charger state and yield with the **Modbus unit** column) | Settings → Services → Modbus TCP → on | none |
| Cell modems (Type `cell_modem`; the **Model** column picks the reader) | Sierra **RV55 / RV50 / MP70**: SNMP. **MikroTik** (LtAP, Chateau …): SNMP. **OpenWrt**: SNMP + the small `deploy/openwrt/ng-cell.sh` script. Signal (RSRP, SINR, RSRQ, RSSI), network, operator, band | a read-only SNMP community on the modem | `CELL_SNMP_RO` |
| Sierra **XR80 / XR90** (no SNMP) | ALMS (AirVantage) cloud API, by the **Serial** column - only signal and network values are kept | an ALMS API client allowed to view systems (client credentials) | `ALMS_CLIENT_ID`, `ALMS_CLIENT_SECRET` |
| Starlink | the dish's own status API (latency, drop, obstruction, throughput, alerts) through the trailer router | a NAT on the router to the dish's **192.168.100.1:9201**; the Devices row's **API address** = router address:NAT port | none |
| WireGuard tunnels | the Netmaker gateway's API: online, last check-in (Pro servers also latency and uptime) | **Netmaker URL** on the Sites sheet, **Netmaker host** on each group with a tunnel; a dedicated Netmaker user's access token, with an expiry | `NETMAKER_TOKEN` |
| VCU, GNSS (vehicle positions), VAI | the Mobius InfluxDB | InfluxDB reachable on 8086 (the Mobius team's sign-off), then the mapping from `probe-influx` | none (or a user / password by name) |
| **Everything else in Mobius** | the Mobius InfluxDB, read every minute, shown on each vehicle / location / desk page as **Mobius data**; the Mobius Server's page lists what Mobius knows that is not in the inventory | the same InfluxDB access - no mapping needed | none |

- **Firewall:** from the server, UDP 161 (SNMP), TCP 502 (Modbus), TCP 8086 (InfluxDB), the Starlink NAT port, plus ping; out to the internet on 443 for the Netmaker gateway and ALMS (`na.airvantage.net`).
- **The Netmaker token** can change the VPN on Community Edition (it has no read-only role): it lives only in secrets.env, the dashboard only reads with it, and an expiry date on the token is a good idea.
- **Rajant's SNMP has no peer count or input voltage**: those charts are filled by the simulator only. The Tachyon MIB does not give units for rx power and tx rate; `probe-snmp` shows the raw numbers to compare with the Tachyon's own page.
- **A device that answers ping but not its reader** is a Warning, "Device data (snmp): Not read", with the reason on its page.
- **Readers without their logins** are "not set up" (one note on the dashboard, no warning on every card); the device is still pinged. Adding the logins needs no restart.
- **Bring-up commands** (on the test install prefix them with `docker compose exec app`):
  - `python -m networkguru probe-snmp <ip> --type rajant|tachyon|switch` - every value the reader gets (`--walk <OID>` dumps a subtree)
  - `python -m networkguru probe-snmp <ip> --type cell_modem --model RV55` (or `MikroTik`, `OpenWrt`)
  - `python -m networkguru probe-netmaker` - the Netmaker host names (for the Groups sheet) and each tunnel's status
  - `python -m networkguru probe-starlink <router address>:<NAT port>` - what the dish gives
  - `python -m networkguru probe-modbus <ip> [--mppt-unit N]`
  - `python -m networkguru probe-influx --url http://<mobius server>:8086 --out influx-layout.txt` - the database layout, names only (send this file for the Mobius mapping)

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
