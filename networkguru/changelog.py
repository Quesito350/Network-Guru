"""
Network Guru - what changed in each version (shown when the version is clicked)
==========================================================================================
Newest first. Written for the people who use the dashboard; the technical history is in
ARCHITECTURE.md. Add an entry with every version (the version must match
networkguru.__version__ - release.py refuses to package otherwise).
"""

CHANGES = [
    ("0.9.1", "2026-10-27", [
        "Admin → Found on the network has an 'Also scan these networks' box: type the networks the scan should "
        "cover too (for example the Mobius server's network, 10.x.y.0/24), Save, then Scan now. No spreadsheet "
        "change or restart needed. Private ranges only.",
    ]),
    ("0.9.0", "2026-10-27", [
        "The dashboard can find the site's devices itself: Admin → Found on the network → Scan now, and every night "
        "at 02:00. It looks around the addresses the inventory has (and the Sites sheet's Subnets) for Mobius "
        "servers, InfluxDB and NTRIP casters (by their ports), Cerbo GX units, and radios, routers, modems and "
        "switches (by SNMP, with only the site's own logins).",
        "A device that moved - recognised by its serial number, MAC or name - is followed to its new address by "
        "itself, and Events shows 'Address changed'. Ready for the move to DHCP.",
        "A new device waits on the Admin page: Add puts it on the dashboard at once (pick a group, give it a name), "
        "Ignore hides it, and 'Download new ones for devices.xlsx' gives rows to bring it into the spreadsheet.",
        "New command: scan - prints what answers on the site's networks, without keeping anything. Tell IT before "
        "the first scan: network security tools may notice it.",
    ]),
    ("0.8.0", "2026-10-21", [
        "The status tiles now filter: click Critical, Warning, OK, Offline or Unknown to see only those cards, "
        "click again (or Show all) for everything. The choice stays in the address, so a refresh keeps it.",
        "Live forgets what has gone away: a vehicle, desk or location none of whose devices has answered for an "
        "hour drops off the Live cards and the map, with a 'Show them' switch. History, its own page, alerts and "
        "the morning report keep it. Admin → Live view sets the time and the tabs it applies to.",
        "Everything Mobius has: each vehicle, location and desk page shows every value the Mobius InfluxDB has "
        "for it (Mobius data), cards say how fresh it is, vehicles with a position in Mobius move on the map, and "
        "the Mobius Server's page lists what Mobius knows that is not in the inventory yet.",
        "Rajant and Tachyon values from their MIB files: Rajant temperature, channel busy and noise per radio, "
        "dropped packets; the Tachyon link's signal at both ends, SNR, MCS, PHY rate, radio temperature and CPU.",
        "Cell modems, Starlink and WireGuard where a site has them: Sierra RV55 (SNMP) and XR80 (through ALMS), "
        "MikroTik and OpenWrt modems with their signal and operator (Celona or a carrier); the Starlink dish's "
        "latency, drop, obstruction and alerts through the trailer router; each WireGuard tunnel's state from the "
        "Netmaker gateway. New sheet columns: Netmaker URL (Sites), Netmaker host (Groups), API address (Devices).",
        "New bring-up commands: probe-netmaker, probe-starlink and probe-snmp --type cell_modem. The Admin page "
        "lists every device login the inventory names, set or not.",
    ]),
    ("0.7.0", "2026-10-21", [
        "Device readers in live mode: the Rajants (SNMP v3), Tachyons and trailer switches (read-only SNMP) and "
        "the Cerbo GX units (Modbus TCP) now report their own values - uptime and traffic, switch ports and PoE "
        "watts, battery, solar and load. A device that answers ping but cannot be read shows a Warning with the "
        "reason (for example a wrong SNMP login).",
        "The logins go in secrets.env by name; install.sh --secrets asks for each one the inventory uses. A "
        "reader without its logins is 'not set up' (one note on the dashboard) and the device is still pinged.",
        "New bring-up commands: probe-snmp, probe-modbus and probe-influx show what each device gives. probe-influx "
        "lists the Mobius database's names (no data) for the Mobius mapping, which brings the VCU, GNSS (real "
        "vehicle positions on the map) and VAI values in the next build.",
        "Devices sheet: a 'Modbus unit' column for the Cerbo's solar charger (charger state and daily yield).",
    ]),
    ("0.6.0", "2026-10-21", [
        "New Map page (the Map tab): a pin for every site location in its live status colour, the vehicles where "
        "their GNSS last put them, and the 60 GHz links as lines. Hover for the reason, click for the location. "
        "Satellite images when the PC has internet, a plain grid without (Admin → Map chooses satellite, street "
        "map or none).",
        "Positions come from new Lat / Lon columns on the Groups sheet of devices.xlsx; importing the Rajant "
        "snapshot fills them from the radios' GPS (a typed position always wins).",
        "First real device checks: in live mode every device with an address is pinged and its service ports are "
        "checked, so the cards show real up/down and latency. The device readers (SNMP, Modbus, Mobius) follow in "
        "0.7.0; until then VCU, GNSS and VAI say 'Not checked yet'.",
        "Report a problem (header): a signed-in admin downloads one zip with the version, status, the last 7 days "
        "of events, recent log lines, the inventory and any errors the page caught - never passwords or tokens. "
        "The button shows a red dot when the page has caught an error.",
    ]),
    ("0.5.5", "2026-10-21", [
        "Every release now also updates the test-install image on GitHub by itself: build.bat starts the image "
        "build, waits for it and says when ghcr.io/quesito350/network-guru has the new version (and that anyone "
        "can pull it). A test PC gets it with 'docker compose pull' and 'docker compose up -d'.",
    ]),
    ("0.5.4", "2026-10-21", [
        "Each release is now also a Docker image on GitHub (ghcr.io/quesito350/network-guru), built and "
        "tested there. A test PC needs only Docker Desktop and two small files: 'docker compose up -d' to start, "
        "'docker compose pull' and 'docker compose up -d' to update. No Git and no build on the PC.",
        "The site's inventory.yaml goes in a 'site' folder next to compose.yaml. Without one, the test install "
        "shows the demo site.",
        "deploy/WINDOWS_TEST.md: the new steps, what to do with older installs on the PC, and help when "
        "PowerShell says 'git is not recognized'.",
    ]),
    ("0.5.3", "2026-10-20", [
        "The source is public on GitHub (Quesito350/Network-Guru), starting fresh at this version. The "
        "daily update check and the download now work without a GitHub token (one is still used if set).",
        "Site details are no longer in the program: each site's name, time zone, location, Mobius InfluxDB "
        "address and subnets are on a new Sites sheet in devices.xlsx, which build-inventory reads.",
        "The demo site and the examples use made-up addresses.",
    ]),
    ("0.5.2", "2026-10-20", [
        "New: a developer / test install straight from the git repository. On a PC with Docker Desktop: git "
        "clone, then 'docker compose up -d --build' in the folder; to update, 'git pull' and the same command "
        "again. No release zip, Ubuntu or install.sh needed, with the same lock-down and the same checked "
        "Python packages as a site server. Steps: deploy/WINDOWS_TEST.md, Way A. Site servers keep install.sh "
        "and the admin-approved updates.",
        "In that install the Admin page shows the two update commands instead of Approve and Upload, and the "
        "sign-in uses a one-time password from its log (docker compose logs app), unless you set your own in "
        "dev-secrets/secrets.env.",
        "A secrets.env saved by Notepad (with a byte-order mark) is now read correctly.",
    ]),
    ("0.5.1", "2026-10-20", [
        "Fixed: installing on Ubuntu 26.04 stopped with \"install: invalid user: '10001'\". The installer now "
        "creates the locked 'networkguru' account (id 10001, no login, no home) that the app runs as, the same "
        "as inside the container. Ubuntu 26.04 needs it because its new system tools refuse a bare user id.",
        "New guide deploy/WINDOWS_TEST.md: trying a release on a Windows PC with Docker Desktop (installed "
        "from an Ubuntu WSL window), with the few hardware-test steps that differ on Windows.",
    ]),
    ("0.5.0", "2026-10-15", [
        "Runs on a site server: install.sh sets it up in Docker on an Ubuntu server - also without internet "
        "(the images and Python packages come along). Secrets are typed on the server with hidden input and kept "
        "in secrets.env, readable only by Network Guru. Until the real device drivers arrive (0.6.0) the server "
        "shows simulated data on the real layout, clearly marked.",
        "Daily update check that asks first: at 06:00 (site time) the server looks for a newer release. Everyone "
        "sees a banner; only a signed-in admin can Approve, Remind me tomorrow or Skip this version.",
        "An approved update installs itself: the download is checked (checksum and version), the server rebuilds "
        "and restarts the dashboard, watches it for a minute and goes back to the previous version if it is not "
        "healthy. The result shows as a banner and in Events, and the page reloads by itself.",
        "Sites without internet: upload the update zip and its .sha256 on the Admin page - the same checks run. "
        "When the server cannot check, the footer and the Admin page say why.",
        "Morning report every day (06:30 by default) to Teams and e-mail (Microsoft 365 or SMTP): red and yellow "
        "first, then what changed since yesterday, with a link to the dashboard. It is sent even when everything "
        "is green, so a missing report means the server needs a look. See it on the Morning report page; an "
        "admin can send a test.",
        "New Admin page (header): sign-in, updates, the report settings, which secrets are set (never their "
        "values), and a history of updates, reports and sign-ins. Events has an 'Updates, reports & sign-ins' "
        "filter.",
    ]),
    ("0.4.1", "2026-10-15", [
        "Mendon's trailers are reached over BOTH the 60 GHz Tachyon links and the Rajant mesh, so one "
        "backhaul failing no longer makes the trailers behind it look unreachable.",
        "A 60 GHz link that is down while its radios still answer is now a Warning ('60 GHz link COW-T46 "
        "down'): redundancy is lost, the trailer is still on the mesh. The links panel says so.",
        "A trailer where nothing answers is one Critical alarm: 'No device at Trailer 1846 answers - power, "
        "or both backhauls down'; its devices show 'Unreachable via Switch'.",
        "Simulator: new '60 GHz link down' fault on a Tachyon (both radios stay up) to try this out.",
        "build-inventory --backhaul tachyon keeps the old chained wiring for a site without a mesh path.",
    ]),
    ("0.4.0", "2026-10-15", [
        "New name: Network Guru. The source lives in GitHub (Quesito350/Network-Guru); every version is a "
        "tagged GitHub Release with its zip and checksum.",
        "Real Mendon layout: trailers 1846-1850 (switch, Cerbo GX, Rajant, Tachyon pair), the COW and its "
        "60 GHz backhaul, the towers, buildings and repeater, the Rajant vehicles, the Mobius server, built "
        "from the IPAM sheet and the BC|Commander snapshot. Logins in those files are never read.",
        "New commands: import-ipam, import-rajant and import-devices fill one devices.xlsx per site set; "
        "build-inventory turns it into the dashboard's inventory. Edits in the sheet are kept on re-import.",
        "60 GHz links: each Tachyon knows its far end. A location page shows both ends side by side against "
        "the install signal (warning 6 dB below it, critical 12 dB).",
        "Backhaul-aware alarms: when a trailer's link fails, that one radio is the alarm and everything behind "
        "it shows 'Unreachable via Trailer 1846 · Tachyon 2' instead of a wall of red. The device in front of a "
        "failed one is re-checked at once, so the real failure is always found first.",
        "Ready for DHCP: devices carry their serial and MAC; an address that changes is shown (with a DHCP tag) "
        "and recorded as an event, and history carries on. Address and firmware changes appear in Events.",
        "History is kept in VictoriaMetrics on this PC (survives restarts; only the gap is filled).",
    ]),
    ("0.3.0", "2026-10-14", [
        "The dashboard pages: Vehicles, Control Room and Infrastructure tabs with a card for every "
        "vehicle, desk/server and site location. Click a card for its detail page.",
        "Vehicle pages show the path from the Mobius Server to the truck (site network, Rajant mesh or "
        "Celona, the truck's radio, router, VCU and VAI) with the health and latency of each hop.",
        "Trailer and repeater pages show the solar and battery picture from the Victron Cerbo GX: "
        "battery %, solar vs load, charger state and daily solar yield for 30 days.",
        "Live / History switch with 1 h, 24 h, 7 d, 30 d or a custom range. In History, cards show "
        "availability % and a status strip; charts cover the chosen range.",
        "Events list (top right): every change of health, newest first. Click one to jump to that "
        "device at that time.",
        "Light and dark display, following your Windows setting or the button in the header.",
    ]),
    ("0.2.0", "2026-10-14", [
        "Health rules for every device type (thresholds in rules_default.yaml, starting values to "
        "review with ops), with Offline vs Critical, parked vehicles shown grey, and devices behind a "
        "failed radio shown as 'unreachable via ...' instead of extra alarms.",
        "Simulator: a demo Mendon site with trailers, towers, vehicles on shifts and a Mobius control "
        "room, with 30 days of history built at start-up.",
    ]),
    ("0.1.0", "2026-10-14", [
        "First version: inventory file (inventory.yaml) for the site, control room, vehicles and "
        "infrastructure, with a check command that lists any missing setting by name.",
    ]),
]


def _index(version):
    return next((i for i, (v, _d, _x) in enumerate(CHANGES) if v == version), None)


def split_since(version, before):
    """(entries newer than `before` up to and including `version`, all the other entries).
    An unknown or not-older `before` means: since the previous release."""
    cur = _index(version) or 0
    b = _index(before) if before else None
    if b is None or b <= cur:
        b = cur + 1
    newer = CHANGES[cur:b]
    return newer, [e for e in CHANGES if e not in newer]


def as_json():
    return [{"version": v, "date": d, "items": items} for v, d, items in CHANGES]


def as_text() -> str:
    lines = ["Network Guru - what's new", ""]
    for version, date, items in CHANGES:
        lines.append(f"Version {version} ({date})")
        lines += [f"  - {i}" for i in items]
        lines.append("")
    return "\n".join(lines)
