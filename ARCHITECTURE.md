# Network Guru: architecture

Current version: **0.7.0** (2026-10-21). Source: github.com/Quesito350/Network-Guru (public since 0.5.3). Bump `networkguru/__init__.py` `__version__` and add a `networkguru/changelog.py` entry with every change. Update this file and REQUIREMENTS.md every version.

## What it is

The dashboard is one Python 3.13 service. It polls the site's devices, works out a health status for every component, and serves a read-only web dashboard. It runs 24/7 in Docker on a site Ubuntu server or Proxmox VM, installed by `deploy/install.sh` (0.5.0). Anyone on the site network opens it in a browser. A signed-in admin can approve updates and set up the morning report. For development it also runs on Windows with simulated devices.

```
devices / Mobius InfluxDB ──► drivers (pollers) ──► Scheduler ──► Pipeline ──► LiveState ──► WebSocket ──► browser
                                                                    │   │
                                                                    │   └──► VictoriaMetrics (numbers, 31 days)
                                                                    └──────► SQLite (status changes, runs)
browser ──► REST /api/... (history charts, availability, path, events) ──► History ──► VictoriaMetrics + SQLite
```

## Decisions (and what was rejected)

- **Custom web app, not Grafana.**
  - Grafana can't do the vehicle path view or the drill-down Casey asked for without plugins, and it adds provisioning work.
  - The numbers are in VictoriaMetrics, which speaks PromQL, so Grafana can be pointed at them later.
- **VictoriaMetrics** single-node for the numbers.
  - `-retentionPeriod=31d` guarantees 30 visible days.
  - It needs little RAM and disk, and accepts Influx line protocol.
  - Rejected:
    - Prometheus: pull-only, and no backfill for the simulator.
    - InfluxDB 3: the query file limit restricts long ranges, and it is easy to confuse with Mobius's own Influx.
    - SQLite for the numbers: around 100M rows per 30 days.
- **SQLite** holds status changes and collector runs.
  - Availability and status strips are exact intervals computed from it.
  - Time when no collector was running counts as Unknown.
- **One process.** The asyncio collector and uvicorn share one loop and the live state stays in memory. Blocking libraries will run in threads (`asyncio.to_thread`).
- **The browser never sends queries.** It asks for a named chart of a component, and the server builds the query from the catalog.
- **No build step, no third-party JavaScript.**
  - Charts are hand-written SVG following the data-viz rules: thin marks, one axis, crosshair tooltip, a table view on every chart, and status colours always paired with an icon and a word.
  - Nothing loads from the internet, because sites are often offline. A test enforces this, and the CSP is `default-src 'self'`.
- **Read-only everywhere.** The dashboard has no control actions. Devices get read-only accounts, and secrets are referenced by name. The admin's only actions concern the tool itself: approving updates, report settings and test sends.
- **The container never controls Docker.** There is no Docker socket in the app container. An approved update is a file dropped in a shared folder; a root systemd unit on the host checks it again, builds the image and switches. The host takes `compose.yaml`, the Dockerfile and its own units only from `/opt/networkguru` (install.sh), never from an update zip, so an update cannot change mounts or capabilities.
  - Rejected: the Docker socket in the container (root on the host); Watchtower-style auto-updates (no admin approval, needs a registry).
- **Nothing installs without an admin.** The daily check only offers a newer version. Approve, Remind me tomorrow and Skip this version need a signed-in admin (Casey's request).

## Source layout

| Module | Responsibility |
|---|---|
| `networkguru/__init__.py` | `__version__`, `TOOL_NAME` |
| `__main__.py`, `run.py` | CLI: `dev` (simulator; starts `tools\vm\victoria-metrics*.exe` if present, `-search.latencyOffset=5s`, force-flush on exit), `serve`, `check-inventory`, `import-ipam`, `import-rajant`, `import-devices`, `build-inventory`, `version` |
| `resolve.py` | `AddressBook`: where each device is now (static / last-known / looked up), `find()` by serial → MAC → hostname |
| `importers/protowire.py` | tiny protobuf wire reader/writer (no protobuf library) |
| `importers/rajant_snapshot.py` | BC\|Commander `.mesh` → `RajantNode` (whitelisted tag paths only, see the module docstring) |
| `importers/ipam.py` | IPAM sheet rows (reading starts at the `IP \| Location \| …` header; rows above are skipped) |
| `importers/sheet.py` | `devices.xlsx` (Groups / Devices / Read me, drop-down lists), upserts that keep manual edits |
| `importers/build.py` | import → sheet, and sheet → inventory dict (ids, Mobius vehicle components, backhaul wiring, per-type `*_ref` defaults) |
| `tools/fetch_vm.py` | dev only: VictoriaMetrics v1.154.0 Windows exe with SHA-256 check (refuses the zstd-bug versions) |
| `config.py` | `Settings` (+ `deploy`: `dev` this PC, `docker` install.sh server, `compose` git checkout; `public_url`); `secret(name)` reads env vars, then the server's `secrets.env` (`NETWORKGURU_SECRETS`, re-read when it changes), and remembers values; `mask()` removes them (and webhook `sig=` keys) from text |
| `state.py` | small JSON files in `<data>/state/` (atomic writes) for settings, update and report state, so they survive restarts even when SQLite is in memory |
| `site_settings.py` | the admin page's settings (report time, channels, recipients, Graph / SMTP host, dashboard URL, update check time) with validation; nothing secret |
| `auth.py` | admin password (scrypt hash in `secrets.env`), in-memory sessions, sign-in throttling |
| `daily.py` | once-a-day jobs at a site-local HH:MM: DST-safe, catch-up after downtime, "done" decided by the owner's saved state |
| `github.py` | GitHub Releases client ported from the Router Config Tool's `updater.py` (redirects followed without the token, `.sha256` required) |
| `updates.py` | `UpdateManager`: daily check, banner decisions, approve / remind / skip, download or upload + `verify_release_zip`, the request file for the host, results back from it |
| `report.py`, `notify.py` | the morning report (build, text / HTML / Teams card) and `ReportService` (Teams webhook, Graph, SMTP; retries; per-day, per-channel state) |
| `web/admin.py` | `/api/admin/*`: sign-in, update actions, upload, settings, test report |
| `deploy/` | `install.sh`, `compose.yaml`, `Dockerfile`, `ng_updater.py` (host updater, root, stdlib), the systemd units, `requirements.lock`, `HOST_VERSION`, `HARDWARE_TEST.md`, `WINDOWS_TEST.md`; `Dockerfile.dev` (+ `.dockerignore`) for the developer install |
| `compose.yaml`, `.env.example` (top level) | the developer / test install from a git checkout (`docker compose up -d --build`, 0.5.2) |
| `tools/fetch_wheels.py`, `tools/fetch_images.py` | the Linux wheels for the image (`deploy/wheels`, zip only) and the Docker images for an offline server (`fetch_image` from the All-Stop Deploy Tool) |
| `catalog.py` | **one place per device type**: metric names and units, card headline metrics, chart specs, poll interval, typical solar load |
| `inventory.py` | pydantic models for `inventory.yaml`; groups (vehicle / location / station) holding components; default `depends_on`; schedules; `missing_secrets()` (names only) |
| `model.py` | `Status` (OK 0 < Unknown 1 < Offline 2 < Warning 3 < Critical 4), `Metric`, `CompResult` |
| `rules.py` + `rules_default.yaml` | thresholds (`warn/crit`, `low_warn/low_crit`, `enum`, `zero`, `for`), 5 % hysteresis, reasons text |
| `pipeline.py` | per batch: raw status → escalation / suppression / staleness / caps → roll-up component → group → tab → SQLite changes → metric points |
| `scheduler.py` | poll loop: due pollers each second, one pipeline batch per tick, sweep every 10 s, run heartbeat every 30 s; `SimPoller` |
| `tsdb.py` | `VictoriaTSDB` (buffered line-protocol writes, `query_range` / `aggregate`) and `MemoryTSDB` (same semantics) |
| `events.py` | SQLite `runs`, `status_changes`, `meta`; `intervals()`, `availability()`, `events()`, collector-gap overlay |
| `history.py` | chart queries from catalog `ChartSpec`s (step = range/600 rounded, min = poll interval; peak band; daily bars in site time), card sparklines, the vehicle path and its history (availability + p95 per link) |
| `runtime.py` | wires everything: chooses the store, starts backfill + scheduler (sim) or the collector (live), housekeeping |
| `sim/noise.py`, `sim/world.py`, `sim/backfill.py` | deterministic simulator and the 30-day fast-forward through the real pipeline |
| `web/app.py`, `web/api.py`, `web/ws.py` | FastAPI app, REST endpoints, WebSocket (snapshot, then 1 s deltas) |
| `web/static/` | `index.html`, `css/app.css`, `js/app.js` (routing, header, drawer, modals), `js/views/*` (incl. `map.js`, `mobius.js`), `js/widgets/*` (chart, path, small, `slippy.js` map), `js/problem.js` (Report a problem, caught page errors), `js/quiet.js` (Live's gone-quiet rule) |
| `drivers/reach.py` | live mode: ping (unprivileged ICMP socket on Linux, `IcmpSendEcho` on Windows) and TCP port checks |
| `drivers/snmp.py`, `snmp_profiles.py` | SNMP v2c / v3 (USM: SHA1 auth, AES-128 privacy) client; what each type is asked for, incl. the Rajant / Tachyon vendor values |
| `drivers/cellular.py`, `alms.py` | cell modems: the reader by model; Sierra ALEOS / MikroTik LTE / OpenWrt (snmpd extend) over SNMP; Sierra XR80 / XR90 from ALMS |
| `drivers/starlink.py` | the dish's gRPC-Web status call through the router's NAT, decoded with `importers/protowire.py` |
| `drivers/netmaker.py` | WireGuard tunnels from the Netmaker gateway's REST API (GET only); the gateway card |
| `drivers/mobius_explore.py` | everything else in the Mobius InfluxDB, sorted onto the groups (never a status) |
| `drivers/modbus.py`, `victron.py` | Modbus TCP client; the Cerbo GX registers (Victron's register list rev 3.81) |
| `drivers/influx.py`, `mobius.py`, `mobius_map.py` | InfluxDB 1.x client + `probe-influx`; the Mobius reader and its mapping |
| `drivers/readers.py`, `device.py`, `drivers/__init__.py` | the per-device readers; one poller per device (ping + ports + reader); `live_pollers` |
| `maptiles.py`, `problem_report.py`, `logbuffer.py` | map backgrounds and their CSP origin; the problem-report zip; the last 2000 log lines in memory |
| `discovery.py` | the discovery scan: ranges and limits, probes (ping, TCP, InfluxDB `/ping`, NTRIP, Modbus, NetBIOS, SNMP), matching |
| `found.py` | the scan on the server: review list, following moved devices, Add (hot-add), the files in `/data/state` |

## Status rules (pipeline)

1. **Raw status per component.**
   - Metrics go through the rules: the worst level wins, and a `zero: offline` rule overrides everything (the device is absent).
   - A failed poll keeps the previous status. After `offline_after_polls` (3) failures in a row the status is **Offline**.
2. **Effective status**, applied in this order:
   1. Stale (no poll for 4 intervals) → **Unknown**.
   2. The component is down and its `depends_on` upstream is down **or its last check failed** → **Unknown, "Unreachable via X"**, where X is the *first* failed device up the chain (e.g. "Trailer 1846 · Tachyon 2"). It is marked suppressed, so it is left out of alarms and the default event list. A group whose devices are all hidden this way takes that reason too.
      - "Last check failed" matters because devices are polled at different rates (a Rajant every 10 s, a Tachyon every 30 s). Without it, a downstream device could be declared Offline before the failed device in front of it.
      - **Parent re-check:** in the scheduler, a failed poll makes the device's `depends_on` due at once (at most every 5 s). The real failure is then confirmed in the same few seconds.
   3. Offline in a group whose `offline_severity` is `critical` (infrastructure, server), or inside its `expected_online` schedule → **Critical "Offline - …"**.
   4. `critical: false` caps the status at Warning.
3. **Roll-up:**
   - group = worst component;
   - tab = worst group, ignoring Offline groups (a parked truck doesn't colour the tab);
   - an Offline group's reason is "Offline - last seen HH:MM".
4. **Default dependencies:**
   - vehicle: router → radio; VCU/GNSS/VAI → router (or radio);
   - location: every device → the `uplink: true` device or the first Rajant;
   - server: NTRIP → Mobius service.
   - `depends_on` may point into another group, and cycles are rejected.
5. **Backhaul wiring** (by `build-inventory --backhaul`, recorded as `defaults.backhaul`):
   - **`redundant`** (default; **Mendon**, confirmed by Casey on 2026-10-15). Management traffic reaches each trailer over **both** the 60 GHz Tachyon links and the Rajant mesh, so no trailer depends on another.
     - Everything on a trailer depends on its **switch**, which powers the radios over PoE. The switch has no upstream: either backhaul reaches it.
     - A location with a switch gets this wiring by default.
     - Effects:
       - A dark trailer is one Critical alarm on its switch, with the group reason "No device at Trailer 1846 answers - power, or both backhauls down". Its other devices show "Unreachable via Switch".
       - A 60 GHz link that is down while its radios answer is a **Warning** on both ends ("60 GHz link COW-T46 down"; `tachyon_link_up: {zero: warning}`): redundancy lost, trailer still on the mesh.
       - A trailer whose Tachyon parent is dark only gets that link Warning. It stays monitored over its Rajant.
       - A trailer Rajant that is down stays **Critical**: it is a real device failure, and vehicles lose that mesh node.
   - **`tachyon`** (for sites where the 60 GHz links are the only path):
     - the trailer's **slave** Tachyon is its uplink and depends on the **master** at the far end of the same `link`;
     - switch → uplink; everything else → switch;
     - downstream trailers show "Unreachable via Trailer 1846 · Tachyon 2". Set `tachyon_link_up` back to `zero: critical` in the site's `rules.yaml` for such a site.
   - Mendon's 60 GHz tree is COW → T46 → T49 → T50 and COW → T47 → T48.
6. **Install signal:** a Tachyon with `install_rssi` gets per-device RSSI limits of install − 6 dB (warning) and install − 12 dB (critical).
7. **Availability** = (OK + Warning) ÷ (OK + Warning + Critical). Unknown and Offline (not in service) are excluded.

## Addressing: static now, DHCP later

IT already runs DHCP (Ubiquiti) without reservations. Tremonton's Rajants are on DHCP. The goal is a dynamic network.

- **Identity:** each component can carry `serial`, `mac` (normalised) and `hostname`. Duplicates and bad MACs are errors.
- **`address: dhcp`** marks a device whose IP can change. `host` is then only the last-known address. A DHCP device must have a serial, MAC or hostname, or it is an error.
- **History** is stored by component id, never by IP, so it carries on across address changes.
- **Events:** drivers (and the simulator) report the address they reached in `info.address`. A change becomes an `info_changes` event: "Address 10.200.4.81 → 10.200.4.97". Firmware changes are recorded the same way.
- **The discovery scan (0.9.0, below)** re-finds devices by serial / MAC / name and follows them.
- **Lookups (later):** `resolve.AddressBook` could also be fed by the UniFi client list (IT's DHCP), MikroTik DHCP leases / ARP, the Rajant mesh ARP table over SNMP (IP-MIB `ipNetToPhysicalTable`), passive MNDP (router identity = vehicle name) and DNS. Rule: confirm the device's identity (serial / identity / sysName) before trusting a looked-up address.
- **Simulator:** DHCP devices move to a new lease every few days and always after an injected outage.

## Site data

Real site files never go in git or a release zip; `.gitignore` and `release.py` both block them.

- **Location:** `..\sites\devices.xlsx` (all sites, `Site` column), `..\sites\<site>\inventory.yaml`, and `..\sites\extra-devices.csv` (Mobius server, InfluxDB, NTRIP, GNSS base for Mendon).
- **The Sites sheet** (0.5.3) holds what used to be in the code: each site's name, time zone, GPS, Mobius InfluxDB URL, `Subnets` (a Rajant whose address is in another site's subnets is put on that site) and `Infrastructure subnet` (an unnamed Rajant there is a tower). No real address or location is in the source any more; a test only allows made-up private ranges.
- **Sources:**
  - the site's IPAM workbook
  - the newest BC|Commander snapshot (`.mesh`)
- **What is never read:**
  - the IPAM sheet's login rows
  - the snapshot's security, AP, SNMP, SSH, SSL, modem and imkey fields, and the JVM environment
- **Tests** use synthetic files with planted fake secrets, and check that those never come out.

## Source control and releases

- Git lives in this source folder, with `origin` = `Quesito350/Network-Guru`, branch `main`.
- **Public since 0.5.3**, as a fresh repository whose history starts at 0.5.3. Versions 0.4.0-0.5.2 (their history and release zips held real site addresses) stay in the private `Quesito350/Network-Guru-archive`. Nothing from a real site goes in: site data lives in `..\sites`, and `test_no_real_site_addresses` only allows made-up private ranges.
- One commit plus one annotated tag `v<version>` per version. No feature branches.
- `build.bat` runs the tests and `release.py --publish`, which:
  - zips the source
  - writes `.sha256`
  - runs the secret check over every file git would commit
  - commits, tags and pushes
  - runs `gh release create` with the zip + `.sha256` and the changelog entry as notes
- Use `build.bat nopublish` to build without publishing.
- Since 0.5.0:
  - `build.bat` first makes sure `deploy/wheels` matches `deploy/requirements.lock` (`tools/fetch_wheels.py`). The zip carries the wheels (about 5 MB); git does not.
  - The `.sha256` is written with LF line endings, for `sha256sum -c` on the server.
  - `.gitattributes` keeps the server files LF.
- It uses the PC's `gh` sign-in. No token is stored in the repo or the zip.

## Site server (Docker, 0.5.0)

```
/opt/networkguru/            root-owned; written by install.sh (and .env by the host updater)
  compose.yaml  Dockerfile  HOST_VERSION   the server's own copies - never taken from an update zip
  .env                       NG_VERSION, NG_PREVIOUS (rollback), NG_MODE, NG_PORT, NG_SUBNET, NG_PUBLIC_URL; no secrets
  secrets/secrets.env        10001:10001, mode 600 in a 700 folder; KEY=VALUE read by the app itself
  site/inventory.yaml        (+ optional rules.yaml)
  bin/ng_updater.py          host updater (root, standard library, Python 3.10+)
  releases/<ver>/            unpacked releases; current and previous kept
/var/lib/networkguru/updates/  the ONLY folder shared with the container (owned 10001, flat)
/etc/systemd/system/networkguru-updater.{path,service}
```

- **compose project `networkguru`:**
  - `app`: `networkguru-app:${NG_VERSION}` (built on the server, `pull_policy: never`) on `:8080`. It runs as user 10001 with `cap_drop: [ALL]`, `no-new-privileges`, a read-only root filesystem and `ping_group_range` (unprivileged ICMP for 0.6.0). Volumes: `appdata:/data` (SQLite, state, backups) and the updates folder. `./site` and `./secrets` are mounted read-only.
  - `victoriametrics` v1.154.0: `-retentionPeriod=31d`, volume `vmdata`. It is published on `127.0.0.1:8428` only and runs with `cap_drop: [ALL]`.
  - Network `ng` uses subnet `172.29.250.0/24` (`NG_SUBNET`). `check-inventory` warns about a device inside it or inside 172.17/16.
  - Both services use `restart: unless-stopped` and json-file log rotation. Both start together after a reboot: on the server the app never falls back to in-memory history, and VictoriaMetrics writes are buffered until it answers.
- **Offline build:**
  - The base image `python:3.13-slim-trixie` and VictoriaMetrics come from `tools/fetch_images.py`: registry API on the PC, `docker load` tars with `SHA256SUMS`.
  - The Python packages come from `deploy/wheels` (in the zip; `tools/fetch_wheels.py`). They are installed with `pip --no-index --require-hashes -r deploy/requirements.lock`; the lock is in git.
  - `docker build --network none` uses the host Dockerfile. If the builder refuses (offline BuildKit), the same steps run in a plain container and are saved with `docker commit`.
- **`install.sh`:**
  1. Preflight: root, x86_64, Docker + compose v2, python3 ≥ 3.10, systemd, free port, subnet overlap, userns-remap.
  2. Makes sure a host account with the app's id 10001 exists: a locked `networkguru` system account with no login and no home, the same as in the image. Ubuntu 25.10 and 26.04 ship the Rust coreutils, whose `install -o` and `chown` refuse a bare id nobody owns ("invalid user: '10001'"; added in 0.5.1). Then it writes the folders and the server files; old ones are kept as `.bak-<date>`.
  3. Enables `networkguru-updater.path`.
  4. Loads and checks the images.
  5. Asks for the secrets with `IFS= read -rs`. The admin password goes through stdin to `ng_updater.py hash-password`. The file is written under `umask 077`, chowned, then moved into place.
  6. Runs `ng_updater.py install-local`, which builds the image, runs `check-inventory`, switches, health-checks and rolls back.

  Re-running it keeps every setting and the data. It never removes volumes and refuses a downgrade without `--force`.
- **`HOST_VERSION`:** a number plus a hash of the server-side files. A test fails if those files change without a new hash. Bump the number when installed servers need `install.sh` for the change; an update with a higher number is refused with "needs install.sh".
- **Database rule:** schema changes must be ADDITIVE (new tables, nullable columns). After a failed update the previous version runs again on the same database. A copy (`/data/backups/networkguru-before-<ver>.db`) is taken before each update.

## Test install (the published image + docker compose; 0.5.2, image since 0.5.4)

For test PCs (Docker Desktop): `compose.yaml` + `.env` + a `site` folder, `docker compose up -d`, and `docker compose pull` + `up -d` to update. No Git or build is needed. Site servers keep `install.sh`.

- **The image** `ghcr.io/quesito350/network-guru:<version>` / `:<major.minor>` / `:latest` (public, linked to the repository) is made by `.github/workflows/image.yml`. **release.py starts it** on the new tag right after publishing the release (`gh workflow run image.yml --ref v<version>`), follows the run and ends with an ERROR if the image did not get published; it then checks anonymously that anyone can pull it. (In 0.5.4 the tag push did not start a tag-triggered workflow, and this repository shows no push events, so the workflow has only `workflow_dispatch`.)
  - It builds `deploy/Dockerfile.dev` for linux/amd64.
  - **Smoke test:** the image runs locked down like compose (user 10001, read-only, `cap_drop ALL`, no-new-privileges, the ping sysctl, `NETWORKGURU_DEPLOY=compose`, no site folder, so the demo) and must answer `/api/health` within 3 minutes. Only then is it pushed with the workflow's own `GITHUB_TOKEN` (permissions `contents: read`, `packages: write`); no secret is stored.
  - Every action is pinned to a commit. The one-time password is filtered out of the public log.
  - `release.py`'s secret check treats `${{ … }}` expressions as placeholders.
- **`compose.build.yaml`** is an override for a git checkout: it builds the same Dockerfile locally (`-f compose.yaml -f compose.build.yaml up -d --build`).

- **`compose.yaml` at the top of the repository**, project `networkguru-dev`, so it never mixes with an install.sh install (`networkguru`):
  - The app service has the same lock-down as `deploy/compose.yaml`: user 10001, read-only root, tmpfs /tmp, `cap_drop: [ALL]`, no-new-privileges, the ping sysctl, the healthcheck and the log limits. A test compares the two files.
  - `NETWORKGURU_DEPLOY=compose`. `./site` is mounted read-only at `/site` like on a server (`inventory.yaml`, optional `rules.yaml`); without an inventory there, `serve` shows the built-in example, which the image carries (a site server still stops with an error). The optional secrets are in `./dev-secrets/secrets.env` (git-ignored), mounted read-only at `/run/networkguru`.
  - VictoriaMetrics is the same image and retention, with **no published port**. The subnet is `172.29.251.0/24`, one above the servers', so both kinds can run on one Docker host.
  - Settings come from `.env` next to it (git-ignored; `.env.example` lists every `NG_` variable, and a test checks that).
- **`deploy/Dockerfile.dev`** is the server Dockerfile without `deploy/wheels` and `VERSION.txt`, which are not in git:
  - `pip download --only-binary=:all: --no-deps --require-hashes` with the same `--platform` list as `tools/fetch_wheels.py`, against `deploy/requirements.lock`, then the same `pip install --no-index --require-hashes`. The packages are the release's, byte for byte. It needs internet while building.
  - `deploy/Dockerfile.dev.dockerignore` lets in only `networkguru/`, the lock and the example inventory, never `.git`, `.env`, `site/` or `dev-secrets/`.
- **The app in `compose` mode:**
  - It counts as a container (`Runtime.docker`): events on /data, it waits for VictoriaMetrics, and only an admin may inject faults.
  - With no `NETWORKGURU_ADMIN_PASSWORD_HASH`, `serve` prints a one-time admin password to the log at each start, the same as `run.py dev`.
  - `UpdateManager` refuses Approve and Upload with the update commands (`docker compose pull`, then `up -d`). The daily check still runs, so a new release still shows its banner as a reminder. The Admin page and the review dialog show the commands instead of Approve and Upload.
- `HOST_VERSION` does not cover these files: they never reach a server.

## Updates: daily check, the admin decides

```
06:00 site time   app: GitHub releases/latest (Quesito350/Network-Guru, public; a token from secrets.env only if set)
                  newer? -> banner for everyone: "Network Guru X is available · Review update"
admin Approve     app: download zip + .sha256 (redirects without the token) -> size, sha256, verify_release_zip
                  (paths, no links, entry/size caps, __version__ = VERSION.txt = tag, newer, Dockerfile present)
                  -> /data/updates/NetworkGuru_vX.zip + update-request.json {request_id, version, zip, sha256, ...}
host              networkguru-updater.path -> ng_updater.py run (root, flock):
                  rename request -> .processing; read with O_NOFOLLOW|O_NONBLOCK, regular file, <= 4 KB, < 24 h old,
                  newer than .env NG_VERSION; copy the zip into a root-only folder, sha256 again, the same zip checks;
                  HOST_VERSION; build; .env NG_PREVIOUS=cur NG_VERSION=new; compose up -d --no-deps app;
                  /api/health ok + version 3x in a row within 60 s -> installed (prune to 2 tags; never the base image)
                  else NG_VERSION back, up again, failed image removed -> rolled_back
                  update-status.json / update-result.json written with mkstemp + fchown(fd) + replace
app               result (matched by request_id, each consumed once) -> system event + banner; page reloads on a new version
```

- **Remind me tomorrow** hides the banner until the next day's check time. **Skip this version** hides it until a newer version appears.
- **No token:** the check runs without one (public repository; 60 calls an hour). **No internet:** "Can't check for updates (no internet: …)". Both show on the Admin page and in the footer, never as a banner.
- **Offline sites:** on the Admin page, upload the zip with its `.sha256` (raw body, 300 MB cap). It goes through the same checks and the same request file.
- **On a PC** (`deploy=dev`), Approve stops at "downloaded and checked".
- **Watchdogs:**
  - If the host has not picked up a request after 2 min, an event says so.
  - A download killed by a restart is reset.
  - A `.processing` file left by a reboot becomes an "interrupted" result.
  - The path unit cannot loop: the request always ends as `.done` or `.rejected`.
- **Hardware test:** `/opt/networkguru/test-fail-next-update` forces one health-check failure, to try the rollback. `ng_updater.py rollback` swaps back by hand.

## Admin sign-in

- One admin password, stored as `scrypt:32768:8:1:<salt>:<hash>` in `secrets.env` (install.sh). The app and `ng_updater.py` write the same format, and a test checks they agree.
- **Sessions:** `POST /api/admin/login {password, name}` creates a random token kept in memory (8 h idle, 24 h maximum). The `ng_admin` cookie is HttpOnly, SameSite=Strict and Path=/. The name goes into events ("Approved by Casey").
- **Request checks:** every admin change needs the header `X-Requested-With: networkguru`, and, when the browser sends `Origin`, the same host. The CSP keeps `form-action 'none'`, so sign-in uses fetch.
- **Throttling:** after 3 wrong passwords from one address it waits 10 s, then 60 s, then 5 min. Failed sign-ins are system events; the password is never stored.
- **No hash set:** admin actions are off. `run.py dev` prints a one-time password and never stores it.
- **Plain HTTP** on the site LAN for 0.5.0 (Casey, 2026-10-15). HTTPS is on the 1.0.0 list.

## Morning report

- **Schedule:** daily at `report_time` (default 06:30, after the 06:00 check) through `daily.DailyJob`.
  - It waits for real numbers: the end of the backfill in sim mode, the first collector minute in live mode.
  - **Catch-up:** if the server was down at the set time, it is sent within 6 h, marked late.
  - What was sent is saved per channel and site-local date immediately after each send, so a restart never sends twice.
- **Content** (`report.build_report`):
  - Subject: `[SIMULATED]` in sim mode, then counts per status.
  - **Needs attention now:** Critical, then Warning, using the overview's rule (suppressed devices are counted, not listed).
  - **Changes since the same time yesterday:** transitions to or from Warning/Critical, grouped per device; a group only when none of its devices changed; at most 30.
  - Address and firmware changes.
  - 24 h availability per tab, the number of parked devices, the update status and the dashboard link.
  - "all clear" only when every in-service device is OK; "no data from any device" when everything is Unknown (live 0.5.0).
  - **Always sent.**
- **Formats:**
  - text
  - HTML (inline styles; every status is a symbol plus a word)
  - Teams Adaptive Card 1.4 (`attachments[0].contentType = application/vnd.microsoft.card.adaptive`, Open the dashboard action, under 28 KB)
- **Channels:**
  - Teams Workflows webhook: a 202 means "accepted"; the flow posts afterwards.
  - E-mail through Microsoft Graph (client credentials, `/users/{sender}/sendMail`) or SMTP (STARTTLS/SSL with certificate checks; no sign-in over an unencrypted line).
  - 3 tries, 5 min apart. Results are system events with secrets masked.
  - If no channel is set up, a daily warning event says the report was built but not sent.
- **Preview:** `#/report` shows the e-mail HTML in a shadow root, plus the text version. `/report/preview.html` serves the same page with its own CSP (`default-src 'none'`). An admin can send a test (`[TEST]` in the subject).

## Metric storage

- Influx line protocol goes to VictoriaMetrics `POST /write` with measurement `ng`, so VictoriaMetrics names the series `ng_<metric>`.
- Labels are `site`, `comp` and `type`, plus a bounded sub-label where needed.
- Hierarchy is **not** in labels. It comes from the inventory, so moving a device between trailers keeps its history.
- A `status` series is written for every node each batch, so Grafana can use it later.
- Queries use `<agg>_over_time(ng_x{comp="…"}[step])` with `nocache=1`.
- `MemoryTSDB` returns the same points: the value at t = agg over (t − step, t].

## Web API

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | version, site, mode, notes, backfill %, catalog, thresholds, enum rules, `started`, `hide_quiet` {min, tabs}, `mobius_explore` |
| `GET /api/admin/discovery` | the scan's state and review list (admin) |
| `POST /api/admin/discovery/scan`, `/add`, `/ignore`; `GET …/export.csv` | Scan now (409 while running or outside live mode), Add a found device, Ignore / show again, rows for `import-devices` (admin) |
| `GET /api/mobius/summary` | per group the newest Mobius data time and map positions (the cards, the gone-quiet rule) |
| `GET /api/mobius`, `/api/mobius/{group}` | everything the Mobius explorer has: matched groups, "not in the inventory", site-wide values; one group's newest values |
| `GET /api/inventory` | tabs → groups → components, with no `*_ref` and no sim knobs |
| `GET /api/live`, `/api/live/{id}` | snapshot of every node |
| `WS /ws/live` | snapshot, then deltas every 1 s, heartbeat every 15 s |
| `GET /api/history/charts/{comp}?from&to&primary&keys` | catalog charts |
| `GET /api/history/sparklines?ids&from&to` | card sparklines (about 72 points) |
| `GET /api/history/status?ids&from&to` | status intervals |
| `GET /api/availability?ids&from&to` | availability % and seconds per status |
| `GET /api/path/{vehicle}[?from&to]` | path hops, and in History each hop's availability + p95 latency |
| `GET /api/events?from&to&node&min_status&suppressed&kinds&info&system&limit` | status changes, newest first; `info=true` merges in address / firmware changes, `system=true` updates / reports / sign-ins (`kinds=system`: only those) |
| `POST /api/sim/fault` | simulator only: `{target, kind: outage\|degrade\|link, minutes}`; admin only on a server |
| `GET /api/health` | self check: `ok` (false when a runtime task died), `version`, `tsdb`, collector state - the host updater's health check |
| `GET /api/update/status` | the update banner(s), last check, newest release, job and last result (no secrets) |
| `GET /api/report/preview`, `GET /report/preview.html` | the morning report as it would be sent now |
| `GET /api/admin/session` | configured / signed in / name |
| `POST /api/admin/login`, `/logout` | sign-in (cookie `ng_admin`) |
| `GET /api/admin/status` | updates, report state, settings, which secrets are set (names only) |
| `POST /api/admin/update/{check,approve,remind,skip,dismiss}` | admin update actions |
| `POST /api/admin/update/upload?name=` | offline update: raw zip body, `X-Update-Sha256` header |
| `PUT /api/admin/settings` | report and check times, channels, recipients, Graph / SMTP host, dashboard URL, map background, `hide_quiet_min` / `hide_quiet_tabs`, `discovery_time` |
| `POST /api/admin/report/test` | send a test report now |

Every `/api/admin/*` change needs the admin cookie and `X-Requested-With: networkguru`.

Ranges are capped at 32 days.

## Simulator

- **Determinism:** every value is a function of (seed, time), using hash-based value noise and hourly episode slots. Live data continues exactly where the backfill ended.
- **Trailer batteries:** integrated every 5 min from an anchor 40 days back, so battery state is consistent too. The model covers:
  - sun height from the site latitude, plus shared weather spells
  - a 24 V LiFePO4 voltage curve
  - MPPT Off / Bulk / Absorption / Float, with rare Fault episodes
  - low-voltage disconnect at 5 %, which takes the whole trailer off the air until 20 %
- **Vehicles:** weekday shifts with daily variation and random parked days, plus `always_on`. Routes loop past the towers, so latency rises with distance. Bad-link spells and outages scale with `flaky`.
- **Correlated faults:** NTRIP outages push correction age up and degrade RTK on every vehicle. A Mobius restart silences all Mobius-sourced data.
- **Backfill:** 5-min steps, with 60 s steps for the last 6 h. It runs through the real pipeline (with `fast=True`, so one failed poll counts as Offline). 30 days for 63 components takes about 12 s.

## Testing

- Run `python -m pytest`. `build.bat` runs it before packaging. There are 160 tests; 2 are skipped on Windows (a symlinked zip and a FIFO request; they run on Linux).
  - 0.5.0 added:
    - `test_updates`: a fake GitHub (`http.server`).
      - newer / same / older release
      - no token: the check still runs and sends no Authorization header
      - no internet and 401 both give "can't check"
      - a release without a `.sha256` is not offered
      - the token never reaches the CDN, including through a relative redirect
      - bad checksum, wrong version inside, zip-slip, symlink, missing Dockerfile and size mismatch are each refused, with nothing staged
      - remind / skip
      - host results: one event each; an old `request_id` is ignored
      - the pickup watchdog
      - upload runs the same checks
    - `test_admin_auth`:
      - scrypt round-trip, and the host updater's hash is accepted
      - lockout steps
      - cookie flags
      - a missing header or another site's `Origin` gets 403
      - a non-admin gets 401 on every admin action
      - on a server, the sim fault needs an admin
      - settings validation
      - secrets status shows names only
      - no hash: admin off
    - `test_report`:
      - red before yellow, hidden devices counted
      - all-green and all-Unknown wording
      - changes since yesterday
      - HTML and the Teams card shape
      - Teams through `httpx.MockTransport`
      - SMTP through a fake class (TLS context, login, headers)
      - Graph through MockTransport
      - 3 tries, then a masked event
      - no double send after a restart
      - catch-up / too late / waits for data
      - DST days
      - no channel set up
      - the preview's own CSP and `no-cache` on `/static`
    - `test_deploy`:
      - static checks of `compose.yaml`, the Dockerfile and `install.sh`, including `bash -n`
      - LF line endings
      - the `HOST_VERSION` hash
      - Python 3.10 syntax
      - `ng_updater.py` against a fake Docker:
        - install
        - rollback; both versions unhealthy
        - forced failure
        - failed build; the builder-free fallback; missing base image
        - 7 kinds of bad request
        - needs install.sh
        - interrupted
        - `fchown` of the result
        - first install; same-version re-run
        - manual rollback
        - `hash-password`
    - `test_static_and_config`:
      - the `secrets.env` parser and its reload
      - `missing_secrets` reads the file
      - webhook masking
      - the release check refuses GitHub tokens, webhook URLs, password hashes and a bare `.env`
      - the release ships the deploy files
      - wheels vs lock
  - **Not testable on the PC** (no Docker / WSL): `install.sh`, systemd, real builds, rollback on a server, real Teams and e-mail. `deploy/HARDWARE_TEST.md` is the checklist.
  - 0.4.1 added: redundant wiring stays local; a dead trailer leaves the next one up with only a link Warning; the simulator's link fault keeps both radios up.
  - 0.4.0 added:
  - `test_vm_e2e` against the real VictoriaMetrics exe: same answers as `MemoryTSDB`, restart keeps history and fills only the gap, `--fresh` rebuilds
  - `test_site_data`: IPAM and snapshot imports with planted secrets, backhaul wiring and suppression, `devices.csv`, identity rules, DHCP address-change events, the scheduler's parent re-check

  The earlier tests cover:
  - inventory validation and dependencies
  - rules (thresholds, hysteresis, `for`, enum/zero, overrides)
  - events (intervals, availability, collector gaps)
  - tsdb (bucket semantics, line-protocol escaping, VictoriaMetrics queries through `httpx.MockTransport`)
  - pipeline (roll-up, Offline after 3, parked-vehicle suppression, `expected_online`, staleness)
  - simulator (determinism, catalog coverage, faults, 1-day backfill)
  - API end-to-end with a 1-day sim (every endpoint, WebSocket, fault → Critical)
  - offline guarantee, secret masking, changelog = version, release refuses secrets
- **Browser check:** `.claude\launch.json` has two entries:
  - `networkguru-mendon` (port 8080, the real Mendon layout)
  - `networkguru-sim` (port 8081, the demo)

  Checked: every tab; a trailer with its power panel and 60 GHz links; a Tachyon outage on T46 that hides T49 and T50; History 7 d / 30 d; the events drawer.

  0.5.0 checked against a fake GitHub on this PC:
  - the banner
  - Review signed out, then signed in
  - a wrong password
  - Approve, then staged
  - bad uploads refused
  - a failed banner and Dismiss
  - settings validation and the test send
  - the report preview
  - System events
  - light and dark themes, phone width

## Live checks, map and problem report (0.6.0)

- **`drivers/reach.py`** (standard library): one `ReachPoller` per component that has a `host` (pinged when its type is pingable) or ports (`checks: ["tcp:PORT"]`, a `target: host:port`).
  - Ping: 4 echoes 0.2 s apart. Linux uses an unprivileged ICMP datagram socket (the `ping_group_range` sysctl both compose files set, user 10001, no capabilities); Windows uses `IcmpSendEcho` from iphlpapi in a thread. The image workflow's smoke test pings inside the locked-down container.
  - Ports: `tcp_ms` (connect time) and `port_up` (rule `zero: warning`); a Mobius server's port is `mobius_up` (`zero: critical`). A refused port counts as the device answering.
  - Reachable when any ping reply or port answers; when every ping is lost but a port answers, the ping numbers are left out (ICMP filtered) instead of showing 100 % loss.
  - Components with nothing to check get a reason through `Pipeline.unchecked` ("Not checked yet - needs the Mobius reader (0.7.0)"). Without ICMP permission there is one note and only port checks.
  - `build_inventory` adds `checks` by type (`TYPE_CHECKS`: Mobius server 18080, InfluxDB 8086, GNSS base 2101).
- **Map:** groups carry `lat`/`lon` (Groups sheet Lat / Lon, filled by `import_rajant` from the snapshot GPS for non-vehicles, only when empty; validated by `build_inventory`; sent by `public_tree`).
  - Vehicles: their GNSS component's `info.lat/lon` (info, not metrics, so a quiet vehicle keeps its last position). The simulator drives them round the located pins (`SimWorld.veh_pos`); the Mobius reader will supply real ones.
  - `js/widgets/slippy.js`: Web Mercator, 256-px tiles, drag/wheel/keys, scale bar, plain-grid fallback after the first tiles fail. `js/views/map.js`: status pins by kind, link lines between `partner` components, a side list of what has no position.
  - Background: `map_background` setting (satellite / street / none, `maptiles.py`). The CSP `img-src` gets only that provider's origin (`web/app.py csp()`); tiles are loaded by the browser, never the server.
- **Report a problem:** `POST /api/admin/problem-report` (admin + same origin) returns a zip built in memory by `problem_report.build`: report.txt, status.json, events.json (7 days), log.txt (`logbuffer`), browser-errors.json (from `js/problem.js`), updates.json, morning-report.json, inventory.yaml. Every text goes through `config.mask()`; secrets.env, hashes and session tokens are never read into it. Downloading is an admin event.

## Device readers (0.7.0)

- **One poller per device** (`drivers/device.py DevicePoller`): ping + ports (`ReachPoller`) and the type's reader in one `CompResult` - the pipeline takes a component's metrics from one result. The reader runs at most every 30 s (`READ_EVERY_S`); between reads its last values go out again.
  - A read that fails on a device that answers is `read_ok{via=snmp|modbus|influx}` = 0 (rule `zero: warning`, reason "Device data (snmp): Not read") with `info.read_error`. A device that answers its reader but not ping (ICMP filtered) is reachable.
  - Logins: the component's `*_ref` names (from `defaults.types`, or `importers/build.py TYPE_DEFAULTS` for a hand-written inventory), read through `config.secret()` on every read - adding them needs no restart. Missing ones: "not set up", no read, one dashboard note (`live_pollers`).
- **SNMP** (`drivers/snmp.py`, standard library + `cryptography` for AES): BER, GET / GETBULK walks over UDP with retries; v2c; v3 USM per RFC 3414 / 3826 (engine discovery, password-to-key localisation - tested with the RFC 3414 A.3.2 vector - HMAC-SHA1-96, AES-128-CFB, one time-window resync), the usmStats reports as readable errors. `snmp_profiles.py`: sysUpTime / sysName / sysDescr; IF-MIB 64-bit (else 32-bit) octet rates over up, non-loopback interfaces with counter-wrap handling; switch ports up (Ethernet types, oper up) and POWER-ETHERNET-MIB consumption. `VENDOR` functions for Rajant / Tachyon and the cell modems (0.8.0, below).
- **Modbus** (`drivers/modbus.py`, standard library): function 3 with exception codes. `victron.py`: unit 100 (com.victronenergy.system) 817-819, 840-843, 850, 860; the solar charger's unit (`mppt_unit`, Devices sheet "Modbus unit") 775 / 784 - checked against Victron's register list rev 3.81.
- **Mobius** (`drivers/influx.py`, `mobius.py`): InfluxDB 1.x over httpx; `probe-influx` writes the layout (names, types, asset tag values, newest times - no data values). `MobiusPoller` (one for all mapped components) asks `SELECT <fields> ... WHERE time > now() - 2m GROUP BY <asset tag> ORDER BY time DESC LIMIT 1` per mapped measurement, matches the tag to the vehicle's `mobius_asset`, makes numbers metrics and lat / lon info (the map); data older than 60 s = no data. `mobius_map.DEFAULT_MAP` is empty until the Mendon probe output; `mobius.map` in the inventory overrides it. Unmapped types: "Not mapped yet".
- **Bring-up:** `probe-snmp`, `probe-modbus`, `probe-influx` (logins from secrets.env only). `install.sh --secrets` asks for every `*_ref` name in the site's inventory (stdlib text scan, comments ignored).

## Readers 2 and the Live view (0.8.0)

- **Rajant vendor values** (enterprise 34861, OIDs from Rajant's MIB and Zabbix template; the agent answers on the bare OIDs, so the reader walks the subtree and accepts both forms): temperature (hundredths of a degree), per radio frequency / noise / channel busy (busy time / active time between reads), InstaMesh dropped packets per second, serial and free memory as info. Cards show the busiest radio and the noisiest; no rules on them yet. No peer count or input voltage in this MIB (simulator only).
- **Tachyon vendor values** (57344.1): the peer table walked by its string index (the weakest peer: rx power, SNR, MCS, tx rate, distance, link uptime), peer count → `tachyon_link_up`, the radio table (temperature, channel), system (model, serial, firmware, CPU / memory). `tachyon_remote_rssi_dbm` is the other end's own reading, shared through `readers.LINK_RSSI` by link name. Units the MIB leaves open are taken in their plausible range (tenths when beyond it).
- **Cell modems** (`cellular.kind(model)`): Sierra ALEOS scalars under 20542.9.1.1 (from Sierra's published MIB - to confirm with `probe-snmp --walk` at a site); the MikroTik LTE table 14988.1.1.16.1.1 (index found with a 2-step walk so the IMEI / IMSI / SIM columns 11-13 are never fetched); OpenWrt through NET-SNMP-EXTEND-MIB `nsExtendOutputFull."ngcell"` (`deploy/openwrt/ng-cell.sh`: mmcli or uqmi, key=value lines); XR80 / XR90 from ALMS (`alms.py`: client-credentials token, `GET /api/v1/systems` every 5 min shared by all modems, matched by serial, a whitelist of data keys; RSRP / RSRQ tenths converted). Unknown model: ping only and one note. `build_inventory` gives each modem the logins of its reader (`CELL_DEFAULTS`).
- **Starlink** (`starlink.py`): `POST /SpaceX.API.Device.Device/Handle` (gRPC-Web, `00 00000003 e23e00` = `get_status`) to the `target`; no Origin / Referer (the dish answers empty to a foreign one); response field 2004 decoded with `protowire` (proto3: a missing number is 0, a missing alert off). Alerts that are notices (heating, roaming, power save, install pending, map reset) are listed but not counted.
- **WireGuard** (`netmaker.py`): `/api/hosts` + `/api/nodes` (+ `/api/metrics` on Pro; a 403 / 404 switches it off for an hour) with `Authorization: Bearer`, cached 20 s and shared by `NetmakerPoller` (every `wireguard` component, matched by `netmaker_host`) and the gateway's `NetmakerReader`. Online = connected, not offline, checked in within 10 min. Keys are never kept. Sheet: Sites "Netmaker URL" (→ `netmaker:` block + a "Netmaker gateway" group), Groups "Netmaker host" (→ a `wireguard` component).
- **Mobius explorer** (`mobius_explore.py`, a runtime task, not a poller): the layout every 30 min with a "who" tag per measurement (asset_tag, else the tag whose values match inventory names, else an asset-like tag); every 60 s `SELECT * ... WHERE time > now() - 10m GROUP BY * ORDER BY time DESC LIMIT 3` per measurement; newest value per field and series; unmatched "who" values listed; lat / lon fields → vehicle map positions. Secret-like field names dropped, text cut to 200, the problem report gets counts only.
- **Live view** (`js/quiet.js`): a group is quiet when no component was seen (and no Mobius data came) for `hide_quiet_min` - counted from the dashboard's start when it never answered, and never for a group nothing checks. Quiet groups leave the Live overview, its tiles and issues and the map (a per-tab switch shows them faded); History, pages, alerts and the report are unchanged. Status tiles are buttons that filter the cards (`?show=` in the address).

## The discovery scan (0.9.0)

- **Ranges** (`discovery.ranges`): the /24 of every private inventory address + `discovery.ranges` (Sites sheet Subnets, written by `build_inventory`); private only, `MAX_ADDRESSES` 65,536.
- **Scanner:** at most `RATE` (300) addresses started a second, 200 in flight, 1 s timeouts, 30 min cap. Who answers: ping (when ICMP is allowed) + TCP 18080 / 8086 on every address (Windows drops ping). Then, only for those: TCP 2101 / 502 / 22 / 80 / 443; InfluxDB `/ping` header; NTRIP sourcetable; Modbus unit 100 register 800 (Victron "Serial (System)" = the GX's MAC); NetBIOS NBSTAT (UDP 137: name + MAC); SNMP: `snmp.engine_probe` (a v3 discovery request, no login) first, then the inventory's own logins (`site_logins`: v3 users first, then the communities) only to agents that answered; sysObjectID enterprise → type; vendor serial (Rajant 34861.1.1, Tachyon 57344.1.3.2.0, MikroTik mtxrSerialNumber 14988.1.1.7.3.0); a MikroTik whose LTE table answers is also a cell modem. Reverse DNS for recognised hosts without a name.
- **Matching** (`match`): identity (serial > MAC > name, incl. identities learned from earlier scans) → known or **moved**; else the address: same type → known (its missing identity is learned), an SNMP device of another type → **changed**; else **new**. Unrecognised hosts are only counted ("other hosts").
- **Server** (`found.Discovery`, live mode): Scan now + a nightly `DailyJob` (`discovery_time`, default 02:00). Moved → `comp.host` and the poller's `retarget()`; `addresses.json` keeps it (dropped when the inventory catches up). Pollers report `info.address`, so the move is an "Address changed" event. **Add** writes `added.json` and hot-adds: `load_site_inventory` (file + added + overlay), `Runtime.swap_inventory` → `Pipeline.swap_inventory` (new nodes), `live_pollers(only=…)`, `Scheduler.add()`. At start an added device the sheet now has (serial / MAC / address) is left out with a note. Results are admin-only; scans are system events.
- **CLI:** `python -m networkguru scan [--range CIDR] [--only]` - one scan, printed, nothing kept.

## Roadmap

| Ver | What | Verify where |
|---|---|---|
| 0.5.0 ✓ | Docker install + **daily update check with admin approval** + **morning report** (details below). Built 2026-10-15; the hardware test (`deploy/HARDWARE_TEST.md`) is still to do | site VM |
| 0.6.0 ✓ | ping/TCP live checks, the site map, Report a problem (details in *Live checks, map and problem report*) | test PC / Mendon LAN |
| 0.7.0 ✓ | the device readers: SNMP (standard MIBs), Modbus (Cerbo GX), `probe-influx` + the mapping-driven Mobius reader (details in *Device readers*) | test PC / Mendon LAN |
| 0.8.0 ✓ | Rajant / Tachyon vendor values (MIB files), cell modems (Sierra RV55 / XR80, MikroTik, OpenWrt), Starlink, WireGuard from Netmaker, the Mobius explorer, status tiles as filters, hiding what has gone quiet (details in *Readers 2 and the Live view*) | Mendon LAN |
| 0.8.x | the Mobius mapping from `probe-influx` at Mendon (history and rules for VCU / GNSS / VAI); unit checks of the Tachyon and Sierra values against the devices | Mendon LAN |
| 0.9.0 ✓ | the discovery scan: find Mobius servers and devices, follow moved ones, Add from a review list (details in *The discovery scan*) | Mendon LAN |
| 0.10.0 | readers 3: MikroTik REST (GET only), Celona Orchestrator, airMAX SNMP, UniFi API, Westermo SNMP, windows_exporter; address lookups for DHCP (UniFi clients, MikroTik leases/ARP, Rajant ARP over SNMP, MNDP, DNS) | each device type |
| 1.0.0 | thresholds reviewed with ops, optional view password, HTTPS (admin sign-in is plain HTTP on the LAN in 0.5.0), second-site offline install | site |
| later | Rajant BCAPI (live "via tower X"), Helm direct (`WAN:21443`), 30-day backfill from Mobius Influx, vehicle tracks on the map (from history), control-room kiosk mode | |

**0.5.0 in more detail** (as planned; built as described in *Site server*, *Updates*, *Admin sign-in* and *Morning report* above):
- **Docker install:**
  - compose project `networkguru`: app on :8080, VictoriaMetrics on 127.0.0.1 only, subnet 172.29.250.0/24, `ping_group_range`, cap_drop ALL
  - vendored Linux wheels; image tars via the All-Stop Deploy Tool's `netassist.fetch_image`
  - `install.sh`: secrets typed with hidden input, an admin password stored hashed, never `down -v`, rollback tag
- **Daily update check with admin approval:**
  - GitHub releases/latest is checked (a read-only token from `secrets.env` was required until 0.5.3; now optional).
  - A banner reads "update available"; only a signed-in admin can Approve, Remind tomorrow or Skip.
  - The app downloads the zip and checks its `.sha256`. A host-side systemd updater (no Docker socket in the container) rebuilds the image, health-checks it, and rolls back if the check fails.
  - Offline sites upload the zip on the admin page.
- **Morning report** to Teams (Workflows webhook) and email (SMTP, Graph optional):
  - always sent, even when all green, so a missing report means the tool is broken
  - red and yellow first, then what changed since yesterday

**Device readers, as planned** (built in 0.7.0, see *Device readers*): Rajant SNMP v3 authPriv (SHA1/AES128, one user for all radios; v1/2c is locked to "public"); Tachyon read-only SNMP (TACHYON-MIB), not the REST login, which can change settings; trailer switch SNMP (port state, PoE watts); Cerbo GX Modbus TCP (read-only); `probe-influx` + the Mobius InfluxDB reader. MikroTik REST moved to 0.8.0.

**Ideas taken from the V&V team's "Radio network health monitor" plan:** the morning report and its rules, SNMP v3 for Rajant, read-only SNMP for Tachyon, and treating a down Celona Orchestrator as one problem (a data-source component that its APs depend on). Its "3 tries, 1 minute apart" retry rule is already the Offline rule (3 missed polls). Its SNMP change request for the 8 Rajants should list the Network Guru server as the source IP.

### Needed from Casey / other teams before the real drivers

1. **Mobius InfluxDB:** reachable from the dashboard server. v1 listens on 127.0.0.1 with no auth, so this means a binding change plus a Windows Firewall rule allowing only the dashboard IP on 8086. The Mobius team must sign off.
2. **Vehicle routers:** an input-accept rule for the dashboard IP and a read-only REST user in the Router Config Tool templates (a separate Router Config Tool version). Vehicle WAN IPs need DHCP reservations. At Celona or uplink-NAT sites, vehicles are Mobius-only.
3. **Starlink:** every dish is 192.168.100.1. Give each trailer a dst-nat to the dish's gRPC-Web port (`<trailer-router>:<port> → 192.168.100.1:9201`), and put `<trailer-router>:<port>` in the Devices row's API address.
3a. **WireGuard:** the Netmaker gateway's API URL and an access token of a dedicated Netmaker user (with an expiry; Community Edition has no read-only role), and each tunnel's host name on the Groups sheet.
3b. **Cell modems:** a read-only SNMP community on the RV55s, MikroTik and OpenWrt modems (and the ng-cell script on OpenWrt); an ALMS API client (client credentials, view systems) for the XR80s.
4. **Rajant:** SNMP v3 on the 8 infrastructure radios (the V&V change request), with the Network Guru server as the source. Per-peer data needs Rajant's BCAPI SDK.
5. **Tachyon and trailer switches:** a read-only SNMP community on each (the switches already have an RO community configured).
6. **No GitHub token is needed** since the repository went public (0.5.3). A site may still set a read-only one (fine-grained: only Network-Guru, Contents read-only).
   - Needed for 0.5.0.
6a. **Morning report channels** (0.5.0):
   - **Teams:** a Workflows flow "When a Teams webhook request is received" that posts to the ops channel. Its URL goes into `secrets.env`.
   - **E-mail through Microsoft 365:** an Entra app registration with the **Mail.Send** application permission, limited to one sender mailbox (an application access policy). IT provides the tenant ID, the app ID and a client secret, plus the sender mailbox. SMTP AUTH is often switched off in Microsoft 365, so Graph is the expected path; SMTP stays for relays.
7. **Cerbo GX:** enable Modbus TCP on each unit and note the unit IDs.
8. **windows_exporter** on the Mobius PCs, if IT allows it.

## Version history

- **0.9.0** (2026-10-27): the discovery scan (`discovery.py`, `found.py`): Mobius servers / InfluxDB / NTRIP casters by port, Cerbo GX by Modbus, Windows PCs by NetBIOS, SNMP devices with the site's own logins only after a no-login probe; moved devices followed (`retarget`, `addresses.json`), new ones on Admin → Found on the network with Add (hot-add, `added.json`), Ignore and a CSV for `import-devices`; nightly at `discovery_time`; `scan` command; Sites sheet Subnets → `discovery.ranges`.
- **0.8.0** (2026-10-21): Rajant / Tachyon vendor values; cell modem readers (Sierra ALEOS SNMP, ALMS for XR80 / XR90, MikroTik LTE, OpenWrt + `ng-cell.sh`); Starlink gRPC-Web status; WireGuard tunnels from Netmaker (+ gateway group, sheet columns Netmaker URL / host / API address); the Mobius explorer and the Mobius data sections; status tiles filter the cards; Live hides what has gone quiet (Admin → Live view); `probe-netmaker`, `probe-starlink`, `probe-snmp --type cell_modem`; the admin page lists the inventory's device logins (set or not).
- **0.7.0** (2026-10-21): device readers - SNMP v2c / v3 (Rajant, Tachyon, switches; standard MIBs), Modbus TCP (Cerbo GX), the Mobius InfluxDB reader driven by a mapping + `probe-influx`; one `DevicePoller` per device with `read_ok`; probe commands; `install.sh --secrets` asks for the inventory's login names; new dependency `cryptography` (AES for SNMP v3).
- **0.6.0** (2026-10-21): live mode pings every device and checks its service ports (`drivers/reach.py`); the Map page (`slippy.js`, `map.js`, Lat / Lon on the Groups sheet, `map_background` + CSP); Report a problem (admin zip, page error capture, log buffer). Device readers moved to 0.7.0.
- **0.5.5** (2026-10-21): release.py starts the image workflow itself and waits for it (`build_image`), so each release also publishes the test-install image; the workflow is `workflow_dispatch` only.
- **0.5.4** (2026-10-21): the test-install image on GitHub's registry (`ghcr.io/quesito350/network-guru`), built and smoke-tested by a workflow on each tag. The root `compose.yaml` pulls it (a `site` folder replaces `NG_INVENTORY`, demo without one); `compose.build.yaml` builds from a checkout. `WINDOWS_TEST.md` Way A uses the image, with help for old installs and "git is not recognized".
- **0.5.3** (2026-10-20): public repository (fresh history; 0.4.0-0.5.2 in the private archive). Site details out of the code: `SITE_DEFAULTS` and the two subnet rules became the Sites sheet of devices.xlsx. Made-up addresses (10.200/16), serials, MACs and a neutral location (Salt Lake City) in the example, tests and docs, plus a test that allows only made-up private ranges. The update check and download work without a GitHub token.
- **0.5.2** (2026-10-20): developer / test install from git: top-level `compose.yaml` + `deploy/Dockerfile.dev` (the release's packages from PyPI, hash-checked), `NETWORKGURU_DEPLOY=compose` (one-time admin password in the log, Approve/Upload refused with the git commands). `WINDOWS_TEST.md` Way A. `secrets.env` with a byte-order mark is read.
- **0.5.1** (2026-10-20): `install.sh` creates the host account for id 10001, so it works on Ubuntu 26.04 (Rust coreutils). Added `deploy/WINDOWS_TEST.md` for Docker Desktop test PCs.
- **0.5.0** (2026-10-15):
  - Docker install for a site Ubuntu server, offline-capable: vendored wheels with a hash lock, image tars, hidden-input secrets, a hashed admin password, a rollback tag.
  - Daily update check (06:00 site time) with admin approval. A host-side systemd updater builds, switches, health-checks and rolls back. Offline zip upload.
  - Admin sign-in and the Admin page.
  - Morning report (06:30) to Teams / Graph / SMTP, always sent, with a preview page.
  - System events (updates, reports, sign-ins).
  - `secrets.env` read by the app.
  - Docker mode never falls back to in-memory history.
  - The release check refuses tokens, webhook URLs and hashes, and needs the wheels.

- **0.4.1** (2026-10-15):
  - Redundant backhaul (Tachyon + Rajant mesh) is the default wiring; a link down is a Warning.
  - Location-level "No device answers" reason.
  - Simulator "60 GHz link down" fault.
  - `--backhaul tachyon` keeps the chain.

- **0.4.0** (2026-10-15):
  - Renamed to Network Guru; git + GitHub Releases.
  - VictoriaMetrics on this PC (`tools\fetch_vm.py`), plus its end-to-end test.
  - Real Mendon layout via `import-ipam` / `import-rajant` / `import-devices` / `build-inventory`.
  - `switch` type, Tachyon link pairs with install RSSI, and backhaul dependencies across groups.
  - The root cause is named in "Unreachable via" reasons; the last failed check counts as down; the scheduler re-checks the parent.
  - DHCP-ready identity (serial / MAC / hostname / address), `AddressBook`, address and firmware change events.
  - A 60 GHz links panel; a DHCP tag in tables.

- **0.3.0** (2026-10-14):
  - Web UI: three tabs, overview cards, detail pages for vehicles (path chain), locations (power panel) and stations, plus a component page.
  - Live/History with 1 h, 24 h, 7 d, 30 d and custom ranges.
  - Events drawer, What's new, help, light/dark, simulator fault box.
- **0.2.0** (2026-10-14): rules, pipeline, events, stores, simulator + backfill, REST + WebSocket.
- **0.1.0** (2026-10-14): inventory, catalog, `check-inventory`.
