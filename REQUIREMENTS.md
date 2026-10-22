# Network Guru: requirements

"Since" = the version that first met the requirement. "Planned" = not met yet.

| # | Requirement | Since |
|---|---|---|
| R1 | Show the health of the devices on the path between the Mobius Server, the Mobius Clients and the vehicles | 0.3.0 (simulated) |
| R2 | Show the health of the infrastructure: Rajant, Celona private cellular, Starlink, Tachyon mmWave, Ubiquiti (airMAX/UISP and UniFi) | 0.3.0 (simulated) |
| R3 | Vehicle components: MikroTik, Westermo Viper, ASI VCU, ASI VAI (plus the vehicle's radio and the VCU's GNSS) | 0.3.0 (simulated) |
| R4 | Control-room components: Mobius Server (incl. its InfluxDB and NTRIP client) and Mobius Clients | 0.3.0 (simulated) |
| R5 | A Live view that updates by itself | 0.3.0 |
| R6 | A History view covering up to the past 30 days (1 h / 24 h / 7 d / 30 d / custom) | 0.3.0 |
| R7 | Tabs: Vehicles, Control Room, Infrastructure | 0.3.0 |
| R8 | Each tab has an overview of every component; clicking one drills down to its detail page; every device has its own page | 0.3.0 |
| R9 | Battery and solar panel stats for the infrastructure (Victron Cerbo GX): state of charge, voltage, current, solar W vs load W, charger state, daily solar yield | 0.3.0 (simulated) |
| R10 | Runs 24/7 in Docker on a site server; anyone on the site network opens it in a browser | 0.5.0 (hardware test pending: deploy/HARDWARE_TEST.md) |
| R11 | Works at sites with no internet (no external web assets; offline install path) | 0.3.0 for the pages; offline install and update upload 0.5.0 |
| R12 | Read-only: no control actions on any device; read-only device accounts | 0.3.0 |
| R13 | No credentials in files we write or ship: inventory uses `*_ref` names, values only in the server's `secrets.env` (typed with hidden input, mode 600), masked in logs and API output, release refuses to package secrets (GitHub tokens, webhook URLs and password hashes too since 0.5.0); no real site addresses, locations or file names in the public repository (a test allows only made-up private ranges) | 0.3.0; repository 0.5.3 |
| R14 | Statuses never rely on colour alone (icon + word); light and dark display | 0.3.0 |
| R15 | Parked vehicles / idle desks are grey Offline, not alarms, unless an `expected_online` schedule says they should be up | 0.3.0 |
| R16 | Devices behind a failed upstream device show "unreachable via …" instead of extra alarms | 0.3.0 |
| R17 | Every health change is recorded with its reason and listed in Events; a click opens that device at that time | 0.3.0 |
| R18 | Thresholds live in one editable file with site and per-device overrides; defaults follow Mobius's own bands where they exist | 0.3.0 (values to review with ops) |
| R19 | A simulator so the dashboard can be shown and tested without hardware | 0.3.0 |
| R20 | Real data: Mobius InfluxDB, ping/TCP, Rajant SNMP v3, Tachyon read-only SNMP, switch SNMP, MikroTik REST, Victron Modbus | Planned 0.6.0 |
| R21 | Real data: Celona Orchestrator, Starlink, Westermo, airMAX, UniFi, Windows hosts | Planned 0.7.0 |
| R22 | Same conventions as the other tools: version bump per change, What's new on the version link, ARCHITECTURE.md + REQUIREMENTS.md per version, release zip one level up with 5 kept | 0.3.0 |
| R23 | Named **Network Guru**; source in GitHub `Quesito350/Network-Guru`; every version a tagged GitHub Release with zip + `.sha256` | 0.4.0 |
| R24 | Checks GitHub for a new version daily and **asks the admin** before updating (approve / remind tomorrow / skip); offline sites can upload the update zip; no GitHub token needed (public repository, 0.5.3) | 0.5.0 (host updater hardware test pending) |
| R25 | Real site layout from the site's own files: IPAM sheet, BC\|Commander snapshot, devices.csv → one `devices.xlsx` → inventory; manual edits kept on re-import; logins in those files never read | 0.4.0 |
| R26 | Ready for DHCP / a dynamic network: devices identified by serial / MAC / hostname, IP treated as a looked-up fact, address changes recorded, history unbroken | 0.4.0 (lookup sources 0.7.0) |
| R27 | 60 GHz backhaul aware: Tachyon pairs shown end-to-end against their install signal. At Mendon the Tachyon links and the Rajant mesh are **redundant** paths: a link down is a Warning (redundancy lost) and never hides other trailers; a dark trailer is one alarm | 0.4.0, redundancy 0.4.1 |
| R28 | Morning report to Teams and email, sent every day even when all green; red/yellow first, then changes since yesterday | 0.5.0 (delivery to the real Teams channel / mailbox to be confirmed on site) |
| R29 | History kept across restarts (VictoriaMetrics), verified end-to-end | 0.4.0 |
| R30 | Only a signed-in admin changes anything (approve / postpone / skip updates, report settings, test sends); every such action and failed sign-in is recorded with who did it | 0.5.0 |
| R31 | A failed update never leaves the site without a dashboard: health-checked for 60 s, previous version restored automatically, the result shown on the dashboard | 0.5.0 (hardware test pending) |
| R32 | A test install runs from the published image (`ghcr.io/quesito350/network-guru`, built and smoke-tested on GitHub for each release) with only `compose.yaml` + `.env` (`docker compose pull` + `up -d` to update), or from a git checkout with `compose.build.yaml`, with the same container lock-down and the same hash-checked packages as a site server, and never mixes with an install.sh install | 0.5.2; image 0.5.4 (test PC run pending: deploy/WINDOWS_TEST.md Way A) |
| R33 | A map of the site: every location as a pin in its live status, vehicles where their GNSS last put them, the 60 GHz links; satellite images when online, a plain map by GPS offline; positions from the spreadsheet (Rajant GPS fills them) | 0.6.0 |
| R34 | An admin can download a problem report (status, events, logs, inventory, page errors) for whoever looks into an issue; never any secret | 0.6.0 |
| R35 | Live mode shows real reachability (ping latency/loss, service ports) for every device with an address, without logins; the device readers follow | 0.6.0 (test PC / site run pending) |
| R36 | Live mode reads the devices' own values: Rajant SNMP v3 (authPriv), Tachyon and switch read-only SNMP, Cerbo GX Modbus TCP, the Mobius InfluxDB (VCU, GNSS positions, VAI); logins only by name from secrets.env; a device that answers but cannot be read is a Warning with the reason | 0.7.0, Rajant / Tachyon vendor values 0.8.0 (Mobius mapping: after the Mendon probe; site run pending) |
| R37 | Cell modems (private Celona and public carrier: Sierra RV55 / XR80, MikroTik, OpenWrt), Starlink (through the router's NAT) and the WireGuard tunnels (cloud Netmaker gateway) are shown where they are in use, with their signal / link values; nothing for a site without them; no IMEI / IMSI / SIM numbers or keys read or kept | 0.8.0 (site run pending) |
| R38 | Everything the Mobius InfluxDB has for a vehicle, location or client desk is visible on its page without a mapping, and what Mobius knows that is not in the inventory is listed | 0.8.0 |
| R39 | The status tiles (Critical, Warning, OK, Offline, Unknown) filter the cards; Live leaves out what has gone quiet (no answer for an hour, admin-set) with a switch to show it, while History, alerts and the report keep it | 0.8.0 |
