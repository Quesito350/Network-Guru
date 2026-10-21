#!/usr/bin/env bash
# =============================================================================================
# Network Guru - install or upgrade on a site Ubuntu server (Docker)
# =============================================================================================
# Unpack the release and run this from its folder (Ubuntu may have no unzip; a zip made on
# Windows carries no execute bit, hence "bash ..."):
#
#   python3 -m zipfile -e NetworkGuru_v0.5.0.zip .
#   cd NetworkGuru_v0.5.0
#   sudo bash deploy/install.sh --images ../images --inventory ../inventory.yaml
#
# Options
#   --images DIR       folder from tools/fetch_images.py (SHA256SUMS + .tar) - needed offline
#   --inventory FILE   the site's inventory.yaml (Documents\Network Guru\sites\<site>\ on the PC)
#   --rules FILE       optional site rules.yaml (threshold overrides)
#   --mode sim|live    sim = simulated data on the real layout (default); live = ping and port checks
#   --port N           dashboard port (default 8080)
#   --subnet CIDR      Docker network for the two containers (default 172.29.250.0/24)
#   --url URL          dashboard address used in the morning report links
#   --secrets          ask for the secrets again (Enter keeps a value)
#   --rebuild          build the app image again even if it exists
#   --force            allow installing an older version than the one running
#
# What it does: checks the server, creates /opt/networkguru and /var/lib/networkguru/updates,
# installs compose.yaml, the Dockerfile, the host updater and its systemd units, loads the
# images, asks for the secrets with hidden input (secrets.env, mode 600), builds the app
# image offline and starts it - with the same health check and roll-back as an update.
# It never removes the Docker volumes (history and events live there). Re-running it is safe.
# =============================================================================================
set -euo pipefail
umask 022

BASE=/opt/networkguru
UPDATES=/var/lib/networkguru/updates
APP_UID=10001
LOCK=/run/networkguru-updater.lock
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose --project-directory "$BASE" -f "$BASE/compose.yaml")

IMAGES_DIR="" INVENTORY="" RULES="" MODE="" PORT="" SUBNET="" PUBLIC_URL=""
ASK_SECRETS=0 REBUILD="" FORCE=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
ask_yes() { local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }
usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --images)    IMAGES_DIR="${2:?}"; shift 2 ;;
    --inventory) INVENTORY="${2:?}"; shift 2 ;;
    --rules)     RULES="${2:?}"; shift 2 ;;
    --mode)      MODE="${2:?}"; shift 2 ;;
    --port)      PORT="${2:?}"; shift 2 ;;
    --subnet)    SUBNET="${2:?}"; shift 2 ;;
    --url)       PUBLIC_URL="${2:?}"; shift 2 ;;
    --secrets)   ASK_SECRETS=1; shift ;;
    --rebuild)   REBUILD="--rebuild"; shift ;;
    --force)     FORCE=1; shift ;;
    -h|--help)   usage 0 ;;
    *)           warn "unknown option $1"; usage 1 ;;
  esac
done

online() {
  python3 -I - <<'PY' >/dev/null 2>&1
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen("https://registry-1.docker.io/v2/", timeout=5)
except urllib.error.HTTPError:
    sys.exit(0)            # 401 = the registry answered: we are online
except Exception:
    sys.exit(1)
PY
}

# ---------------------------------------------------------------------------------------------
step "1/7 Checking this server"
[ "$(id -u)" -eq 0 ] || die "run it with sudo: sudo bash deploy/install.sh ..."
[ "$(uname -s)" = Linux ] || die "this installs on a Linux (Ubuntu) server"
[ "$(uname -m)" = x86_64 ] || die "only x86_64 servers are supported (the images are linux/amd64)"
command -v systemctl >/dev/null || die "systemd is needed (Ubuntu Server has it)"
command -v python3 >/dev/null || die "python3 is needed: sudo apt install python3"
for tool in getent groupadd useradd; do
  command -v "$tool" >/dev/null || die "$tool is needed (Ubuntu's passwd and libc-bin packages have it)"
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "python3 3.10 or newer is needed"
[ -f "$SRC/networkguru/__init__.py" ] && [ -f "$SRC/deploy/compose.yaml" ] || die "run this from an unpacked release folder"
ls "$SRC"/deploy/wheels/*.whl >/dev/null 2>&1 || die "deploy/wheels is missing - use the release zip from GitHub, not a git checkout"
NEW_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/networkguru/__init__.py")"
say "Release: Network Guru $NEW_VERSION ($SRC)"

if ! docker compose version >/dev/null 2>&1; then
  if online && ask_yes "Docker (with the compose plugin) is not installed. Install docker.io + docker-compose-v2 from Ubuntu now?"; then
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y docker.io docker-compose-v2
    systemctl enable --now docker
  else
    die "Docker Engine and the compose plugin are needed. Offline servers: install them first (Ubuntu packages docker.io and docker-compose-v2)."
  fi
fi
docker info >/dev/null 2>&1 || die "Docker is installed but not running: sudo systemctl start docker"
if docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -q userns; then
  warn "Docker runs with userns-remap: the app's user id 10001 will not own its files - turn it off or expect permission errors."
fi

# Existing install: its settings are the defaults.
declare -A ENV=()
if [ -f "$BASE/.env" ]; then
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    ENV["${line%%=*}"]="${line#*=}"
  done < "$BASE/.env"
fi
CUR_VERSION="${ENV[NG_VERSION]:-}"
MODE="${MODE:-${ENV[NG_MODE]:-sim}}"
PORT="${PORT:-${ENV[NG_PORT]:-8080}}"
SUBNET="${SUBNET:-${ENV[NG_SUBNET]:-172.29.250.0/24}}"
[[ "$MODE" =~ ^(sim|live)$ ]] || die "--mode is sim or live"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port is a number 1-65535"
if [ -z "$PUBLIC_URL" ]; then
  PUBLIC_URL="${ENV[NG_PUBLIC_URL]:-}"
  [ -n "$PUBLIC_URL" ] || PUBLIC_URL="http://$(hostname -I | awk '{print $1}'):$PORT/"
fi
if [ -n "$CUR_VERSION" ]; then
  say "Installed now: $CUR_VERSION"
  if python3 -c 'import re,sys; t=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)); sys.exit(0 if t(sys.argv[1]) < t(sys.argv[2]) else 1)' "$NEW_VERSION" "$CUR_VERSION"; then
    [ "$FORCE" -eq 1 ] || die "$NEW_VERSION is older than the installed $CUR_VERSION - add --force to go back (or: sudo python3 $BASE/bin/ng_updater.py rollback)"
  fi
fi

# The port must be free, unless our own container holds it.
if command -v ss >/dev/null && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
  if ! docker ps --filter "label=com.docker.compose.project=networkguru" --format '{{.Ports}}' | grep -q ":$PORT->"; then
    die "port $PORT is already used by another program - choose one with --port"
  fi
fi

# The containers' subnet must not be a network the site uses.
python3 -I - "$SUBNET" <<'PY' || warn "the subnet $SUBNET overlaps a network this server routes to - choose another with --subnet"
import ipaddress, subprocess, sys
mine = ipaddress.ip_network(sys.argv[1], strict=False)
out = subprocess.run(["ip", "-4", "route", "show"], capture_output=True, text=True).stdout
for line in out.splitlines():
    parts = line.split()
    if not parts or parts[0] == "default" or "docker0" in parts or any(p.startswith("br-") for p in parts):
        continue
    try:
        if ipaddress.ip_network(parts[0], strict=False).overlaps(mine):
            sys.exit(1)
    except ValueError:
        pass
PY

# One installer or updater at a time (the host updater takes the same lock).
exec 9>"$LOCK"
command -v flock >/dev/null && { flock -n 9 || die "an update is being installed right now - try again in a few minutes"; }
export NG_LOCK_HELD=1

# ---------------------------------------------------------------------------------------------
step "2/7 Folders and the server's own files"
# The app runs as id 10001 (the 'networkguru' account inside the image). The host needs an
# account with that id too: Ubuntu 25.10+/26.04 ship the Rust coreutils, whose install -o and
# chown refuse a bare number nobody owns ("install: invalid user: '10001'"). Same locked
# account as in the image: no login, no home folder.
ensure_app_account() {
  local name
  if ! getent group "$APP_UID" >/dev/null; then
    getent group networkguru >/dev/null && die "a group 'networkguru' already exists with another id - remove or rename it, the app needs group id $APP_UID"
    groupadd --system --gid "$APP_UID" networkguru
  fi
  if ! getent passwd "$APP_UID" >/dev/null; then
    getent passwd networkguru >/dev/null && die "a user 'networkguru' already exists with another id - remove or rename it, the app needs user id $APP_UID"
    useradd --system --uid "$APP_UID" --gid "$APP_UID" --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin networkguru
  fi
  name="$(getent passwd "$APP_UID" | cut -d: -f1)"
  say "  app account: $name ($APP_UID)"
}
ensure_app_account
install -d -m 755 -o root -g root "$BASE" "$BASE/bin" "$BASE/site" "$BASE/releases"
install -d -m 700 -o "$APP_UID" -g "$APP_UID" "$BASE/secrets"
install -d -m 755 -o root -g root /var/lib/networkguru
install -d -m 750 -o "$APP_UID" -g "$APP_UID" "$UPDATES"
STAMP="$(date +%Y%m%d-%H%M%S)"
for f in compose.yaml Dockerfile HOST_VERSION; do
  if [ -f "$BASE/$f" ] && ! cmp -s "$SRC/deploy/$f" "$BASE/$f"; then
    cp -p "$BASE/$f" "$BASE/$f.bak-$STAMP"
    say "  $f changed - the old one is kept as $f.bak-$STAMP"
  fi
  install -m 644 -o root -g root "$SRC/deploy/$f" "$BASE/$f"
done
install -m 755 -o root -g root "$SRC/deploy/ng_updater.py" "$BASE/bin/ng_updater.py"
install -m 644 -o root -g root "$SRC/deploy/networkguru-updater.path" /etc/systemd/system/networkguru-updater.path
install -m 644 -o root -g root "$SRC/deploy/networkguru-updater.service" /etc/systemd/system/networkguru-updater.service
systemctl daemon-reload
systemctl enable --now networkguru-updater.path
say "  host updater: networkguru-updater.path is $(systemctl is-active networkguru-updater.path)"

# ---------------------------------------------------------------------------------------------
step "3/7 Docker images"
BASE_IMAGE="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]\{1,\}\).*/\1/p' "$BASE/Dockerfile" | head -n1)"
VM_IMAGE="$(sed -n 's/^[[:space:]]*image:[[:space:]]*\(victoriametrics\/[^[:space:]#]*\).*/\1/p' "$BASE/compose.yaml" | head -n1)"
if [ -n "$IMAGES_DIR" ]; then
  [ -f "$IMAGES_DIR/SHA256SUMS" ] || die "$IMAGES_DIR has no SHA256SUMS (made by tools/fetch_images.py)"
  (cd "$IMAGES_DIR" && sha256sum -c --quiet SHA256SUMS) || die "an image file in $IMAGES_DIR is damaged (checksum)"
  for tar in "$IMAGES_DIR"/*.tar; do
    say "  loading $(basename "$tar") ..."
    docker load -i "$tar" >/dev/null
  done
fi
for img in "$BASE_IMAGE" "$VM_IMAGE"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    if online; then
      say "  pulling $img ..."
      docker pull "$img" >/dev/null || die "could not pull $img"
    else
      die "$img is not on this server and there is no internet - copy the images folder (tools/fetch_images.py) and add --images DIR"
    fi
  fi
  say "  $img present"
done

# ---------------------------------------------------------------------------------------------
step "4/7 Site inventory"
if [ -n "$INVENTORY" ]; then
  [ -f "$INVENTORY" ] || die "$INVENTORY not found"
  install -m 644 -o root -g root "$INVENTORY" "$BASE/site/inventory.yaml"
fi
[ -f "$BASE/site/inventory.yaml" ] || die "no site inventory yet - add --inventory <the site's inventory.yaml>"
if [ -n "$RULES" ]; then
  install -m 644 -o root -g root "$RULES" "$BASE/site/rules.yaml"
fi
say "  $BASE/site/inventory.yaml (checked after the build)"

# ---------------------------------------------------------------------------------------------
step "5/7 Secrets (typed here, never shown, stored in $BASE/secrets/secrets.env - mode 600)"
SECRETS="$BASE/secrets/secrets.env"
declare -A VAL=()
ORDER=(NETWORKGURU_GITHUB_TOKEN NETWORKGURU_ADMIN_PASSWORD_HASH NETWORKGURU_TEAMS_WEBHOOK_URL
       NETWORKGURU_GRAPH_CLIENT_SECRET NETWORKGURU_SMTP_USER NETWORKGURU_SMTP_PASSWORD)
if [ -f "$SECRETS" ]; then
  while IFS= read -r line; do                 # plain KEY=VALUE, never sourced or eval'd
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    VAL["$key"]="${line#*=}"
    case " ${ORDER[*]} " in *" $key "*) ;; *) ORDER+=("$key") ;; esac
  done < "$SECRETS"
fi

ask_secret() {   # name, what it is
  local name="$1" what="$2" v=""
  say ""
  say "$what"
  if [ -n "${VAL[$name]:-}" ]; then
    say "  ($name is set. Enter keeps it, a single - clears it.)"
  else
    say "  ($name is not set. Enter leaves it empty.)"
  fi
  IFS= read -r -s -p "  value (hidden): " v
  printf '\n'
  if [ "$v" = "-" ]; then VAL["$name"]=""; say "  cleared"
  elif [ -n "$v" ]; then VAL["$name"]="$v"; say "  received ${#v} characters"
  fi
}

ask_admin_password() {
  local p1="" p2=""
  while :; do
    IFS= read -r -s -p "  admin password (at least 10 characters, hidden): " p1; printf '\n'
    IFS= read -r -s -p "  the same again: " p2; printf '\n'
    if [ "$p1" != "$p2" ]; then say "  the two differ - again"; continue; fi
    if [ "${#p1}" -lt 10 ]; then say "  too short - at least 10 characters"; continue; fi
    break
  done
  # Through stdin only (printf is a shell builtin: the password is never on a command line).
  VAL[NETWORKGURU_ADMIN_PASSWORD_HASH]="$(printf '%s\n' "$p1" | python3 -I "$BASE/bin/ng_updater.py" hash-password)"
  p1="" p2=""
  say "  admin password set (stored as a scrypt hash)"
}

if [ ! -f "$SECRETS" ] || [ "$ASK_SECRETS" -eq 1 ]; then
  ask_secret NETWORKGURU_GITHUB_TOKEN "GitHub token (optional - the repository is public, so Enter skips it). Only if you want one: fine-grained, only Quesito350/Network-Guru, Contents read-only."
  say ""
  say "Admin password (the dashboard's Admin page: approve updates, report settings)."
  if [ -z "${VAL[NETWORKGURU_ADMIN_PASSWORD_HASH]:-}" ] || ask_yes "  Change the admin password?"; then
    ask_admin_password
  fi
  ask_secret NETWORKGURU_TEAMS_WEBHOOK_URL "Morning report to Teams: the URL of the Teams Workflows flow 'When a Teams webhook request is received'."
  ask_secret NETWORKGURU_GRAPH_CLIENT_SECRET "Morning report e-mail through Microsoft 365: the Entra app's client secret (Mail.Send). Tenant, app ID and sender go on the Admin page."
  ask_secret NETWORKGURU_SMTP_USER "...or through an SMTP server: the SMTP user (host and port go on the Admin page)."
  ask_secret NETWORKGURU_SMTP_PASSWORD "SMTP password."
  # Device logins the site's inventory names (`*_ref: NAME`, e.g. the Rajant SNMP v3 user) - the
  # device readers read them from here. Found with a plain text scan (the host needs no PyYAML).
  DEVICE_REFS=()
  if [ -f "$BASE/site/inventory.yaml" ]; then
    mapfile -t DEVICE_REFS < <(python3 -I - "$BASE/site/inventory.yaml" <<'PY'
import re, sys
text = "".join(ln for ln in open(sys.argv[1], encoding="utf-8") if not ln.lstrip().startswith("#"))
print("\n".join(sorted(set(re.findall(r"\b\w+_ref:\s*['\"]?([A-Z][A-Z0-9_]*)", text)))))
PY
)
  fi
  for name in "${DEVICE_REFS[@]}"; do
    [ -n "$name" ] || continue
    case " ${ORDER[*]} " in *" $name "*) ;; *) ORDER+=("$name") ;; esac
    ask_secret "$name" "Device login the inventory names: $name (SNMP user / password / community, ...)."
  done
  TMP="$(umask 077; mktemp "$BASE/secrets/.secrets.XXXXXX")"
  {
    printf '# Network Guru secrets - written by install.sh %s. KEY=VALUE, read by Network Guru itself.\n' "$STAMP"
    for key in "${ORDER[@]}"; do printf '%s=%s\n' "$key" "${VAL[$key]:-}"; done
  } > "$TMP"
  chown "$APP_UID:$APP_UID" "$TMP"
  chmod 600 "$TMP"
  mv -f "$TMP" "$SECRETS"
  say ""
  say "  saved $SECRETS ($(stat -c '%a %U' "$SECRETS"))"
else
  say "  $SECRETS kept (change with --secrets)"
fi
unset VAL

# ---------------------------------------------------------------------------------------------
step "6/7 Settings, build and start"
{
  printf '# Written by install.sh / ng_updater.py - no secrets here (they are in secrets/secrets.env)\n'
  printf 'NG_VERSION=%s\n' "${ENV[NG_VERSION]:-}"
  printf 'NG_PREVIOUS=%s\n' "${ENV[NG_PREVIOUS]:-}"
  printf 'NG_MODE=%s\n' "$MODE"
  printf 'NG_PORT=%s\n' "$PORT"
  printf 'NG_SUBNET=%s\n' "$SUBNET"
  printf 'NG_PUBLIC_URL=%s\n' "$PUBLIC_URL"
} > "$BASE/.env.new"
chmod 644 "$BASE/.env.new"
mv -f "$BASE/.env.new" "$BASE/.env"
say "  mode $MODE, port $PORT, subnet $SUBNET, link $PUBLIC_URL"
NG_SUBNET="$SUBNET" python3 -I "$BASE/bin/ng_updater.py" install-local --src "$SRC" $REBUILD \
  || die "the new version did not start healthy - see above (docker compose logs app). The previous version, if any, is running again."

# ---------------------------------------------------------------------------------------------
step "7/7 Done"
VERSION_NOW="$(sed -n 's/^NG_VERSION=//p' "$BASE/.env")"
say "Network Guru $VERSION_NOW is running ($MODE mode)."
say ""
say "  Dashboard:      $PUBLIC_URL"
say "  Admin page:     ${PUBLIC_URL%/}/#/admin  (approve updates, morning report settings)"
say "  Logs:           sudo ${COMPOSE[*]} logs -f app"
say "  Stop / start:   sudo ${COMPOSE[*]} stop   |   ... up -d"
say "  Roll back:      sudo python3 $BASE/bin/ng_updater.py rollback"
say "  Secrets:        sudo bash deploy/install.sh --secrets"
say ""
say "Never remove the Docker volumes (no 'down' with the volumes option): they hold the history and the events."
