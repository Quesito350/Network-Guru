"""Command line.

  python -m networkguru dev                      simulator on this PC (http://127.0.0.1:8080)
  python -m networkguru serve --inventory X      the dashboard server (Docker uses this)
  python -m networkguru check-inventory X        validate an inventory, list missing secret NAMES
  python -m networkguru hash-password            admin password (stdin) -> the hash for secrets.env
  python -m networkguru probe-snmp IP --type T    what the SNMP reader gets from a device (logins from secrets.env)
  python -m networkguru probe-modbus IP           what the Modbus reader gets from a Cerbo GX
  python -m networkguru probe-influx --url URL    the Mobius InfluxDB layout (names only) for the Mobius mapping
  python -m networkguru version
"""

from __future__ import annotations

import argparse
import atexit
import ipaddress
import logging
import os
import secrets as _rand
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import TOOL_NAME, __version__
from .catalog import TYPES
from .config import Settings, default_data_dir, secret
from .inventory import InventoryError, load_inventory

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "inventory.example.yaml"
VM_DIR = ROOT / "tools" / "vm"


def _find_vm_exe() -> Path | None:
    if not VM_DIR.is_dir():
        return None
    return next(iter(sorted(VM_DIR.glob("victoria-metrics*.exe"))), None)


def vm_command(exe: Path, store: Path, port: int) -> list[str]:
    """VictoriaMetrics flags. latencyOffset 5 s (default 30 s) so live charts show the newest points."""
    return [str(exe), f"-storageDataPath={store}", "-retentionPeriod=31d", f"-httpListenAddr=127.0.0.1:{port}",
            "-search.latencyOffset=5s", "-loggerLevel=ERROR"]


def _stop_vm(proc: subprocess.Popen, url: str) -> None:
    """Flush recent points to disk first - terminate() on Windows is a hard kill."""
    try:
        urllib.request.urlopen(f"{url}/internal/force_flush", timeout=3)
    except OSError:
        pass
    proc.terminate()


def _start_local_vm(data_dir: Path, port: int = 8428) -> str | None:
    """Start the VictoriaMetrics Windows exe as a child process (dev only)."""
    exe = _find_vm_exe()
    if exe is None:
        return None
    url = f"http://127.0.0.1:{port}"
    try:
        urllib.request.urlopen(f"{url}/health", timeout=1)
        return url                                      # already running
    except OSError:
        pass
    store = data_dir / "vm"
    store.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(vm_command(exe, store, port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags)
    atexit.register(_stop_vm, proc, url)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=1)
            return url
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    return None


def _sheet_cmd(args, fn) -> int:
    from .importers.sheet import Sheet
    sheet = Sheet.load(args.sheet)
    fn(sheet)
    sheet.save(args.sheet)
    for line in sheet.log or ["(no changes)"]:
        print("  " + line)
    print(f"Saved {args.sheet}: {len(sheet.groups)} groups, {len(sheet.devices)} devices")
    return 0


def cmd_import_ipam(args) -> int:
    from .importers.build import import_ipam
    from .importers.ipam import read_ipam
    rows = read_ipam(args.file)
    print(f"{args.file}: {len(rows)} address rows")
    return _sheet_cmd(args, lambda s: import_ipam(s, rows, args.site))


def cmd_import_rajant(args) -> int:
    from .importers.build import import_rajant
    from .importers.rajant_snapshot import read_snapshot
    nodes = read_snapshot(args.file)
    print(f"{args.file}: {len(nodes)} BreadCrumbs (names, serials, IPs, firmware - nothing else is read)")
    return _sheet_cmd(args, lambda s: import_rajant(s, nodes, args.site))


def cmd_import_devices(args) -> int:
    from .importers.build import import_csv
    from .importers.sheet import read_devices_csv
    rows = read_devices_csv(args.file)
    print(f"{args.file}: {len(rows)} rows")
    return _sheet_cmd(args, lambda s: import_csv(s, rows, args.site))


def cmd_build(args) -> int:
    from .importers.build import build_inventory, inventory_yaml
    from .importers.sheet import Sheet
    from .inventory import parse_inventory
    sheet = Sheet.load(args.sheet)
    data, report = build_inventory(sheet, args.site, name=args.name, backhaul=args.backhaul)
    for line in report:
        print("  " + line)
    try:
        inv = parse_inventory(data, args.out)
    except InventoryError as e:
        print(f"The sheet gives an inventory with problems - nothing written:\n{e}")
        return 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(inventory_yaml(data, Path(args.sheet).name), encoding="utf-8")
    for w in inv.warnings:
        print("  warning: " + w)
    print(f"Wrote {args.out}")
    return 0


def cmd_check(args) -> int:
    try:
        inv = load_inventory(args.inventory)
    except InventoryError as e:
        print(f"Inventory has problems:\n{e}")
        return 1
    print(f"{inv.site.name} ({inv.site.id}), time zone {inv.site.timezone}")
    for tab, label in (("vehicles", "Vehicles"), ("control", "Control room"), ("infrastructure", "Infrastructure")):
        gs = inv.tab_groups(tab)
        print(f"  {label}: {len(gs)} - {sum(len(g.components) for g in gs)} components")
    counts: dict[str, int] = {}
    for c in inv.comps.values():
        counts[c.type] = counts.get(c.type, 0) + 1
    print("  Types: " + ", ".join(f"{TYPES[k].label} x{v}" for k, v in sorted(counts.items())))
    missing = inv.missing_secrets()
    if missing:
        print("  Secrets not set (add them to secrets.env): " + ", ".join(missing))
    for w in inv.warnings + server_warnings(inv):
        print("  warning: " + w)
    print("OK")
    return 0


def server_warnings(inv) -> list[str]:
    """Things that work on a PC but bite on the site server."""
    out = []
    if inv.site.timezone in ("UTC", "Etc/UTC"):
        out.append("site.timezone is UTC - the 06:00 update check and 06:30 morning report would run on UTC. "
                   "Set it (Mendon: America/Denver).")
    nets = [ipaddress.ip_network("172.17.0.0/16")]
    try:
        nets.append(ipaddress.ip_network(os.environ.get("NG_SUBNET") or "172.29.250.0/24"))
    except ValueError:
        pass
    for c in inv.comps.values():
        host = (c.host or "").split(":")[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        clash = next((n for n in nets if ip in n), None)
        if clash:
            out.append(f"{c.id} ({host}) is inside {clash}, which Docker uses on the server - the dashboard "
                       "could not reach it. Change NG_SUBNET (install.sh --subnet, or .env for the compose "
                       "install), or the device address.")
    return out


def _serve(settings: Settings, open_browser: bool) -> int:
    import uvicorn

    from .web.app import create_app
    try:
        app = create_app(settings)
    except InventoryError as e:
        print(f"Inventory has problems:\n{e}")
        return 1
    url = f"http://{'127.0.0.1' if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}"
    print(f"{TOOL_NAME} {__version__} - {settings.mode} mode - {url}", flush=True)
    if open_browser:
        import threading
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", ws_ping_interval=20)
    return 0


def cmd_hash_password(args) -> int:
    """Reads the password from stdin (install.sh pipes it; nothing on the command line)."""
    from .auth import MIN_LEN, hash_password
    if sys.stdin.isatty():
        import getpass
        pw = getpass.getpass("Admin password: ")
        if pw != getpass.getpass("Again: "):
            print("The two passwords differ.", file=sys.stderr)
            return 1
    else:
        pw = sys.stdin.readline().rstrip("\r\n")
    if len(pw) < MIN_LEN:
        print(f"The admin password needs at least {MIN_LEN} characters.", file=sys.stderr)
        return 1
    print(hash_password(pw))
    return 0


def _probe_comp(args, typ: str):
    """A component for a probe: from the inventory when it has this address, else the type's standard logins."""
    from .inventory import Component, load_inventory
    inv_path = args.inventory or os.environ.get("NETWORKGURU_PROBE_INVENTORY") or "/site/inventory.yaml"
    if Path(inv_path).is_file():
        try:
            for c in load_inventory(inv_path).comps.values():
                if (c.host or "").split(":")[0] == args.host and c.type == typ:
                    return c
        except InventoryError:
            pass
    return Component(id="probe", type=typ, host=args.host)


def _print_read(metrics, info) -> None:
    from .catalog import metric_spec
    for m in metrics:
        spec = metric_spec(m.name)
        label = spec.label + (f" ({', '.join(m.labels.values())})" if m.labels else "")
        text = spec.enum.get(int(m.value), m.value) if spec.enum else round(m.value, max(spec.dec, 1))
        print(f"  {m.key:34} {label}: {text} {spec.unit}".rstrip())
    for k, v in info.items():
        print(f"  info {k}: {v}")


def cmd_probe_snmp(args) -> int:
    import asyncio

    from .drivers.readers import ReadError, SnmpReader
    from .drivers.snmp import SnmpError
    r = SnmpReader(_probe_comp(args, args.type))
    missing = r.missing()
    if missing:
        print(f"Not set up: add {', '.join(missing)} to secrets.env (the test install: dev-secrets/secrets.env).")
        return 1
    print(f"{args.type} at {args.host} - {r.label}")

    async def go():
        if args.walk:
            c = r._client_for()
            try:
                rows = await c.walk(args.walk, max_rows=args.max)
            finally:
                c.close()
            for oid, v in rows:
                print(f"  {oid} = {v.decode('utf-8', 'replace') if isinstance(v, bytes) else v}")
            print(f"{len(rows)} values under {args.walk}")
            return
        m, i = await r.read(0.0)
        await asyncio.sleep(args.gap)                  # a second read, for the traffic rates
        m, i = await r.read(args.gap)
        _print_read(m, i)
    try:
        asyncio.run(go())
    except (ReadError, SnmpError) as e:
        print(f"Could not read it: {e}")
        return 1
    return 0


def cmd_probe_modbus(args) -> int:
    import asyncio

    from .drivers.modbus import ModbusClient, ModbusError
    from .drivers.victron import read

    async def go():
        async with ModbusClient(args.host, args.port) as m:
            return await read(m, args.mppt_unit)
    try:
        metrics, info = asyncio.run(go())
    except ModbusError as e:
        print(f"Could not read it: {e}")
        return 1
    print(f"Cerbo GX at {args.host}:{args.port} - Modbus TCP")
    _print_read(metrics, info)
    return 0


def cmd_probe_influx(args) -> int:
    import asyncio

    from .config import secret
    from .drivers.influx import InfluxClient, InfluxError, probe
    url = args.url
    if not url:
        inv_path = args.inventory or "/site/inventory.yaml"
        if Path(inv_path).is_file():
            url = load_inventory(inv_path).data.mobius.influx_url
    if not url:
        print("Give the address: --url http://<mobius server>:8086")
        return 1

    async def go():
        async with InfluxClient(url, None, secret(args.user_ref) if args.user_ref else None,
                                secret(args.password_ref) if args.password_ref else None) as c:
            return await probe(c, args.db)
    try:
        text = asyncio.run(go())
    except InfluxError as e:
        print(f"Could not read it: {e}")
        return 1
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Saved to {args.out} - send this file (it holds names, never data values).")
    return 0


def cmd_dev(args) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    vm_url = None
    if args.vm == "auto":
        vm_url = _start_local_vm(data_dir)
        print("History store: " + (f"VictoriaMetrics {vm_url}" if vm_url
                                   else "in memory (put the VictoriaMetrics exe in tools\\vm\\ to keep it)"))
    elif args.vm != "none":
        vm_url = args.vm
    dev_pw = None
    if not os.environ.get("NETWORKGURU_ADMIN_PASSWORD_HASH"):
        dev_pw = _rand.token_urlsafe(9)
        print(f"Admin page: one-time password for this run: {dev_pw}  (never stored; a new one each start)", flush=True)
    s = Settings(inventory=Path(args.inventory), rules=Path(args.rules) if args.rules else None,
                 data_dir=data_dir, mode="sim", vm_url=vm_url, host=args.host, port=args.port,
                 history_days=args.days, fresh=args.fresh, dev_admin_password=dev_pw)
    return _serve(s, open_browser=not args.no_browser)


def cmd_serve(args) -> int:
    env = os.environ
    mode = "sim" if args.sim else (env.get("NETWORKGURU_MODE") or "live").strip().lower()
    if mode not in ("sim", "live"):
        print(f"NETWORKGURU_MODE must be sim or live, not {mode!r}")
        return 1
    rules = args.rules or env.get("NETWORKGURU_RULES")
    if rules and not Path(rules).is_file():
        rules = None                     # the site's rules.yaml is optional
    deploy = env.get("NETWORKGURU_DEPLOY") or "dev"
    inventory = Path(args.inventory)
    if deploy == "compose" and not inventory.is_file():
        # the test install shows the demo until the site folder has an inventory; a site server never does
        print(f"No inventory at {inventory} - showing the demo site. Put the site's inventory.yaml in the site "
              "folder next to compose.yaml, then: docker compose restart app", flush=True)
        inventory = EXAMPLE
    dev_pw = None
    if deploy == "compose" and not secret("NETWORKGURU_ADMIN_PASSWORD_HASH"):
        # the test install has no install.sh to set one: same as run.py dev
        dev_pw = _rand.token_urlsafe(9)
        print(f"Admin page: one-time password for this start: {dev_pw}  (never stored; a new one each start - "
              "or set your own in dev-secrets/secrets.env)", flush=True)
    s = Settings(inventory=inventory, rules=Path(rules) if rules else None,
                 data_dir=Path(args.data_dir) if args.data_dir else default_data_dir(),
                 mode=mode, vm_url=args.vm_url or env.get("NETWORKGURU_VM_URL") or None,
                 host=args.host, port=args.port, history_days=args.days,
                 deploy=deploy, public_url=env.get("NETWORKGURU_PUBLIC_URL") or "", dev_admin_password=dev_pw)
    if s.deploy in ("docker", "compose"):
        inv = load_inventory(s.inventory)
        for w in server_warnings(inv):
            logging.getLogger("networkguru").warning(w)
    return _serve(s, open_browser=False)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="networkguru", description=TOOL_NAME)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dev", help="run the simulator on this PC")
    d.add_argument("--inventory", default=str(EXAMPLE))
    d.add_argument("--rules")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--vm", default="auto", help="auto | none | URL of VictoriaMetrics")
    d.add_argument("--data-dir")
    d.add_argument("--days", type=int, default=30, help="days of simulated history")
    d.add_argument("--fresh", action="store_true", help="rebuild the simulated history")
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(fn=cmd_dev)

    s = sub.add_parser("serve", help="run the dashboard server")
    s.add_argument("--inventory", required=True)
    s.add_argument("--rules")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--vm-url")
    s.add_argument("--data-dir")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--sim", action="store_true", help="simulated devices (demo)")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("check-inventory", help="validate an inventory file")
    c.add_argument("inventory", nargs="?", default=str(EXAMPLE))
    c.set_defaults(fn=cmd_check)

    for cmd, fn, what in (("import-ipam", cmd_import_ipam, "an IPAM .xlsx (IP | Location | Device | ...)"),
                          ("import-rajant", cmd_import_rajant, "a BC|Commander snapshot (.mesh)"),
                          ("import-devices", cmd_import_devices, "a devices.csv (name, site, type, ip, ...)")):
        p = sub.add_parser(cmd, help=f"add {what} to a site's devices.xlsx")
        p.add_argument("file")
        p.add_argument("--sheet", required=True, help="the site's devices.xlsx (created if missing)")
        p.add_argument("--site", default="mendon")
        p.set_defaults(fn=fn)

    b = sub.add_parser("build-inventory", help="devices.xlsx -> inventory.yaml for one site")
    b.add_argument("sheet")
    b.add_argument("--site", default="mendon")
    b.add_argument("--out", required=True)
    b.add_argument("--name", help="site name shown in the dashboard")
    b.add_argument("--backhaul", choices=("redundant", "tachyon"), default="redundant",
                   help="redundant: trailers reached over Tachyon AND the Rajant mesh (default); "
                        "tachyon: the 60 GHz links are the only path")
    b.set_defaults(fn=cmd_build)

    ps = sub.add_parser("probe-snmp", help="what the SNMP reader gets from one device")
    ps.add_argument("host")
    ps.add_argument("--type", required=True, choices=("rajant", "tachyon", "switch"))
    ps.add_argument("--inventory", help="for this device's logins (default: /site/inventory.yaml, else the standard names)")
    ps.add_argument("--walk", help="dump everything under this OID instead (e.g. a vendor subtree)")
    ps.add_argument("--max", type=int, default=500, help="most values for --walk")
    ps.add_argument("--gap", type=float, default=3.0, help="seconds between the two reads (traffic rates)")
    ps.set_defaults(fn=cmd_probe_snmp)
    pm = sub.add_parser("probe-modbus", help="what the Modbus reader gets from a Cerbo GX")
    pm.add_argument("host")
    pm.add_argument("--port", type=int, default=502)
    pm.add_argument("--mppt-unit", type=int, help="the solar charger's Modbus unit ID (Cerbo: Settings > Services > Modbus TCP)")
    pm.set_defaults(fn=cmd_probe_modbus)
    pi = sub.add_parser("probe-influx", help="the Mobius InfluxDB layout (names only, no data values)")
    pi.add_argument("--url", help="http://<mobius server>:8086 (default: the inventory's mobius.influx_url)")
    pi.add_argument("--db", help="only this database")
    pi.add_argument("--inventory")
    pi.add_argument("--user-ref", help="secrets.env name of an InfluxDB user, if it needs one")
    pi.add_argument("--password-ref", help="secrets.env name of its password")
    pi.add_argument("--out", help="also save the layout to this file")
    pi.set_defaults(fn=cmd_probe_influx)

    hp = sub.add_parser("hash-password", help="admin password from stdin -> scrypt hash for secrets.env")
    hp.set_defaults(fn=cmd_hash_password)

    v = sub.add_parser("version")
    v.set_defaults(fn=lambda a: print(f"{TOOL_NAME} {__version__}") or 0)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
