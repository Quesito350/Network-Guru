# Network Guru 0.5.0: hardware test checklist (site server)

The Docker install, the systemd updater, real image builds, rollback, and real Teams and e-mail delivery cannot run on the development PC, which has no Docker and no WSL distribution. The unit tests cover their logic with fakes. This checklist covers them on real hardware.

**Trying it on a Windows PC with Docker Desktop instead?** Follow `WINDOWS_TEST.md`. It has two ways: Way A pulls the published image with `docker compose` (no Git needed), and Way B installs the same release from an Ubuntu WSL window. Each way has a table for the steps that differ.

**Test machine:** an Ubuntu Server 24.04 or 26.04 VM on Proxmox: 2 vCPU, 4 GB RAM, 30 GB disk. Run it on 26.04 at least once, because a site server runs 26.04. Do a first pass **with internet**, then repeat the starred (★) steps **with the network unplugged** (Proxmox: disconnect the NIC, or block the gateway).

Write down the result of each step (✓ / ✗ and what you saw).

## 0. On the PC

1. `build.bat` publishes `NetworkGuru_v0.5.0.zip` and its `.sha256` to `Documents\Network Guru\`. The zip contains `deploy\wheels\` (22 wheels).
2. Run `python tools\fetch_images.py`. This writes `Documents\Network Guru\images\` with 2 tars and `SHA256SUMS`.
3. Copy these to the server, for example with `scp` or a USB stick:
   - the zip
   - the `images` folder
   - `sites\mendon\inventory.yaml`

## 1. Install ★

1. Unpack and start the installer:
   ```bash
   python3 -m zipfile -e NetworkGuru_v0.5.0.zip .
   cd NetworkGuru_v0.5.0
   sudo bash deploy/install.sh --images ../images --inventory ../inventory.yaml
   ```
2. **Offline:** if Docker is missing, it stops with the apt package names. **Online:** it offers to install Docker.
3. Images load after `SHA256SUMS` passes.
4. **Corrupt-image check:** change one byte of a tar and run step 1 again. It must stop with "damaged (checksum)". Then restore the tar.
5. **Secrets prompts:**
   - Nothing you type is echoed.
   - A pasted value prints "received N characters".
   - The admin password must be typed twice and be at least 10 characters.
6. Afterwards, `history | tail` shows no secret.
7. While the install runs, `ps aux | grep -i password` in a second shell shows nothing.
8. Check the secrets file and folder: `sudo ls -la /opt/networkguru/secrets/`.
   - The folder is `drwx------ networkguru networkguru`.
   - `secrets.env` is `-rw------- networkguru networkguru`.
   - `getent passwd 10001` shows `networkguru:x:10001:10001::/nonexistent:/usr/sbin/nologin`. The installer creates this locked host account; Ubuntu 26.04 needs it (see 0.5.1).
9. The build works **offline**. Note which path was used:
   - `docker build`, or
   - "trying the builder-free fallback" in the output.
10. `check-inventory` prints the Mendon summary, with no UTC time-zone warning.
11. The installer ends with "is running and healthy", the dashboard URL and the admin URL.

## 2. What runs and how it is locked down

1. `sudo ss -tlnp`:
   - `:8080` is on `0.0.0.0`.
   - `8428` is on `127.0.0.1` **only**.
2. `docker inspect networkguru-app-1 --format '{{.Config.User}} {{.HostConfig.CapDrop}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Sysctls}}'` prints `10001:10001 [ALL] true map[net.ipv4.ping_group_range:0 2147483647]`.
3. `docker inspect networkguru-victoriametrics-1 --format '{{.HostConfig.CapDrop}}'` prints `[ALL]`. VictoriaMetrics still writes: after 10 minutes, History shows data.
4. `docker network inspect networkguru_ng` shows subnet `172.29.250.0/24`.
5. Neither container mounts the Docker socket: `docker inspect … | grep -c docker.sock` prints `0`.
6. **Ping as a non-root user inside the container** (live mode's ping checks need it):
   ```bash
   docker exec networkguru-app-1 python -c "import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP); print('ok')"
   ```
   It prints `ok`.
7. The dashboard opens from another PC at `http://<server>:8080`. It shows the "Simulated data" banner and the Mendon layout.

## 3. Persistence

1. Run `sudo reboot`. Afterwards both containers are up (`docker ps`), and History still shows the hours before the reboot.
2. Re-run `sudo bash deploy/install.sh` with the same release. It must:
   - keep the secrets
   - say it is running and healthy
   - keep the history and events
3. Re-run it with `--port 8090`. The dashboard moves to `:8090`. Then go back to 8080.
4. Re-run it with `--mode live`. Every device with an address shows its real ping / port status (devices that are switched off go Offline after 3 polls); With the device logins in `secrets.env` (`install.sh --secrets` asks for them), the Rajants, Tachyons and switches show their SNMP values and the Cerbos their battery / solar values; `probe-snmp` / `probe-modbus` from `docker exec networkguru-app-1 python -m networkguru ...` show what each device gives. VCU, GNSS and VAI show "Not mapped yet" until the Mobius mapping. If devices that are up show "no reply to ping", the network filters ICMP - their port checks still count. Then switch back with `--mode sim`.
5. Run `sudo bash deploy/install.sh --secrets` and press Enter on every prompt. Every value is kept.

## 4. Admin page

1. **Admin** in the header shows the sign-in form.
2. A wrong password 3 times leads to "try again in 10 s".
3. A failed sign-in appears under Events → "Updates, reports & sign-ins".
4. Sign in with your name. The page shows "Signed in as …".
5. The Secrets section lists names with set / not set only, never values.

## 5. Update check and approval

These steps need a release newer than the installed one. Use whichever is available:
- **(a)** the next real release (for example 0.5.1)
- **(b)** a test build: on the PC, bump to 0.5.1 locally **without committing**, run `build.bat nopublish`, then revert

1. **Online (no GitHub token needed):**
   - Admin → **Check now** finds the newer version.
   - The banner "Network Guru X is available · Review update" shows for everyone, including a browser that is not signed in.
2. **Not signed in:** Review shows "Sign in as admin". Calling the API directly answers 401:
   ```bash
   curl -X POST -H 'X-Requested-With: networkguru' http://<server>:8080/api/admin/update/approve
   ```
3. **Remind me tomorrow:**
   - The banner disappears.
   - It is back after the next day's check time (06:00 site time).
   - To test without waiting a day, set the check time to 2 minutes from now on the Admin page.
4. **Skip this version:** the banner stays away for this version.
5. **Approve:**
   - The page shows Downloading → Checking → "Handed to the server's updater" → Installing.
   - `journalctl -u networkguru-updater -f` shows the build, the switch and "healthy".
   - The page reloads itself on the new version, the green "Updated to X" banner shows, and there is an event.
6. Check the result on the server:
   - `cat /opt/networkguru/.env` shows `NG_PREVIOUS=<old>` and `NG_VERSION=<new>`.
   - `docker image ls networkguru-app` shows 2 tags.
   - The base image is still listed (`docker image ls python`).
7. **Rollback test:**
   - Run `sudo touch /opt/networkguru/test-fail-next-update`, then approve the next update (or upload it).
   - The updater reports "forced to fail" and goes back. The dashboard shows the red "Update failed: rolled back to …" banner and a Critical event.
   - `.env` again has the old `NG_VERSION`.
8. Manual rollback: `sudo python3 /opt/networkguru/bin/ng_updater.py rollback` swaps back.
9. **Stale request:** make the request file by hand with `requested_at` set 2 days ago. The updater rejects it, and `update-request.rejected` is left. The path unit is still `active` (`systemctl is-active networkguru-updater.path`).
10. **Reboot during an update:** reboot while "Installing" shows. After the reboot the dashboard shows "interrupted", and the old version runs.

## 6. Offline site ★

1. With no internet, Admin shows "Can't check for updates (no internet …)", and the footer shows "Updates: can't check (offline)". This is the same with or without a GitHub token.
2. With internet and no token, **Check now** works (the repository is public).
3. **Upload:**
   - Choose the zip and its `.sha256`, then **Upload and install**. The progress bar fills and the same steps as Approve follow.
   - A `.sha256` from another version is refused with "checksum mismatch", and nothing is staged in `/var/lib/networkguru/updates`.
   - An older zip is refused with "not newer".
4. The offline build works, and the fallback is used if BuildKit refuses. Note which path was used.

## 7. Morning report

1. Admin → Morning report:
   - Set the time to 2 minutes from now and switch on Teams and/or e-mail.
   - For Microsoft 365, enter the Graph tenant, app ID and sender, and put the client secret in with `--secrets`.
2. **Preview:** the report shows red, then yellow, then changes since yesterday. "SIMULATED" is in the subject.
3. **Send a test now:**
   - The Teams channel gets the card with the **Open the dashboard** button. The button opens the right address.
   - The e-mail arrives, both as HTML and as plain text, with "[TEST]" in the subject.
4. **At the set time:**
   - The report arrives once.
   - Restarting the app (`docker restart networkguru-app-1`) does not send it a second time.
   - An event "Morning report sent: teams ✓ accepted; email ✓ sent" is recorded.
5. **All green:**
   - Inject no faults and wait for a quiet period, or use live mode.
   - The report is still sent ("all clear", or "no data from any device" in live 0.5.0).
6. **Wrong Teams URL:** put a wrong URL in with `--secrets`. The report retries 3 times, 5 minutes apart. Then an event "Morning report NOT fully sent" appears, with the URL masked.
7. **No channel set up:** a daily warning event says "built but NOT sent".

## 8. Clean-up

1. Put the real secrets back with `--secrets`.
2. Remove `test-fail-next-update` if it is still there.
3. Write down the results, and open a ticket for anything marked ✗.
