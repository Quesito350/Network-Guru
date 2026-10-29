"""Real device drivers (live mode).

    reach.py      ping and TCP port checks for every device with an address (0.6.0)
    snmp.py       SNMP v2c / v3 client; snmp_profiles.py: what each type is asked for (vendor values 0.8.0)
    cellular.py   cell modems by model (Sierra ALEOS, MikroTik, OpenWrt over SNMP); alms.py: Sierra XR80 / XR90
    starlink.py   the Starlink dish's status API through the router's NAT
    netmaker.py   WireGuard tunnels from the Netmaker gateway
    modbus.py     Modbus TCP client; victron.py: the Cerbo GX registers
    influx.py     InfluxDB client (InfluxQL: 1.x, and 3.x through its 1.x API) + probe-influx;
                  mobius.py / mobius_map.py: the mapped Mobius reader (the map: devices.xlsx "Mobius map");
                  mobius_explore.py: everything else Mobius has (shown on the pages, never a status)
    readers.py    the per-device readers; device.py: one poller per device (ping + ports + reader)
"""

from __future__ import annotations

from collections import Counter

from ..catalog import TYPES, type_spec
from ..model import Info

NOT_MAPPED = ("Not mapped yet - add its values to the Mobius map sheet of devices.xlsx (probe-influx lists "
              "the Mobius tables), then build the inventory again")
NO_INFLUX = "Not checked - no Mobius InfluxDB for this site (Sites sheet: Mobius InfluxDB URL)"
NO_PING = "Not checked - ping is not permitted on this host, and it has no port to check"
NO_NETMAKER = "Not checked - no Netmaker gateway for this site (Sites sheet: Netmaker URL)"


def live_pollers(inv, can_ping: bool, only: set[str] | None = None) -> tuple[list, dict[str, str], list[str]]:
    """(pollers, {comp id: why it is not checked}, dashboard notes) for live mode. `only`: just these
    components (devices added while running)."""
    from .cellular import UNKNOWN_MODEL
    from .device import DevicePoller
    from .mobius import ALWAYS, MobiusPoller
    from .mobius_map import mobius_map
    from .netmaker import NetmakerPoller, api_for
    from .reach import _ipv4
    from .readers import cell_kind, reader_for

    pollers, unchecked, mobius, tunnels = [], {}, [], []
    mapped = mobius_map(inv)
    url = inv.data.mobius.influx_url
    netmaker = api_for(inv.data.netmaker)
    reading: Counter = Counter()
    not_set_up: dict[str, set[str]] = {}
    unknown_cells = 0
    for c in inv.comps.values():
        if only is not None and c.id not in only:
            continue
        if type_spec(c.type).source == "mobius" or c.source == "mobius":
            if url and c.type in mapped:
                mobius.append(c)
            else:
                unchecked[c.id] = NOT_MAPPED if url else NO_INFLUX
            continue
        if c.type == "wireguard":
            if netmaker:
                tunnels.append(c)
            else:
                unchecked[c.id] = NO_NETMAKER
            continue
        reader = reader_for(c, inv)
        if c.type == "cell_modem" and not cell_kind(c):
            unknown_cells += 1
        p = DevicePoller(c, can_ping, reader)
        if reader and not (p.ping_host or p.ports) and reader.missing():
            # nothing to ping and no login yet: Unknown, not a row of Offline cards
            unchecked[c.id] = f"Not set up - add {', '.join(reader.missing())} to secrets.env, then restart"
            not_set_up.setdefault(f"{TYPES[c.type].label} {reader.label}", set()).update(reader.missing())
            continue
        if p.has_work:
            pollers.append(p)
            if reader:
                what = f"{TYPES[c.type].label} {reader.label}"
                missing = reader.missing()
                if missing:
                    not_set_up.setdefault(what, set()).update(missing)
                else:
                    reading[what] += 1
        elif not can_ping and _ipv4(c.host):
            unchecked[c.id] = NO_PING
    # the Mobius reader also runs for the map alone (every vehicle's position and link, 0.9.3)
    fleet = bool(url) and only is None and any((t, k) in ALWAYS for t, values in mapped.items() for k in values)
    if mobius or fleet:
        pollers.append(MobiusPoller(inv, mobius))
    if tunnels:
        if netmaker.missing():
            for c in tunnels:
                unchecked[c.id] = f"Not set up - add {netmaker.token_ref} to secrets.env, then restart"
            not_set_up.setdefault("Netmaker", set()).add(netmaker.token_ref)
        else:
            pollers.append(NetmakerPoller(inv, tunnels, netmaker))
    # the summary is information (the footer's "What's checked"); the notes after it are to-dos (banners)
    notes = [Info("Live mode: ping and port checks for every device with an address. Device readers: "
                  + (", ".join(f"{k} ×{v}" for k, v in sorted(reading.items())) or "none set up yet")
                  + "; Mobius: " + (f"{len(mobius)} components read" if mobius else "not mapped yet (Mobius map sheet)"
                                    if url else "no InfluxDB set")
                  + (f"; Netmaker: {len(tunnels)} WireGuard tunnels" if tunnels and not netmaker.missing() else "") + ".")]
    if not_set_up:
        notes.append("Device readers not set up (add these to secrets.env): "
                     + "; ".join(f"{k} ({', '.join(sorted(v))})" for k, v in sorted(not_set_up.items())) + ".")
    if unknown_cells:
        notes.append(f"{UNKNOWN_MODEL} ({unknown_cells} modem{'s' if unknown_cells > 1 else ''} get ping only).")
    return pollers, unchecked, notes
