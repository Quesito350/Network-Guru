"""Real device drivers (live mode).

    reach.py      ping and TCP port checks for every device with an address (0.6.0)
    snmp.py       SNMP v2c / v3 client; snmp_profiles.py: what each type is asked for
    modbus.py     Modbus TCP client; victron.py: the Cerbo GX registers
    influx.py     InfluxDB 1.x client + probe-influx; mobius.py / mobius_map.py: the Mobius reader
    readers.py    the per-device readers; device.py: one poller per device (ping + ports + reader)
"""

from __future__ import annotations

from collections import Counter

from ..catalog import TYPES, type_spec

NOT_MAPPED = "Not mapped yet - run probe-influx at the site and send its output (Mobius values)"
NO_INFLUX = "Not checked - no Mobius InfluxDB for this site (Sites sheet: Mobius InfluxDB URL)"
NO_PING = "Not checked - ping is not permitted on this host, and it has no port to check"


def live_pollers(inv, can_ping: bool) -> tuple[list, dict[str, str], list[str]]:
    """(pollers, {comp id: why it is not checked}, dashboard notes) for live mode."""
    from .device import DevicePoller
    from .mobius import MobiusPoller
    from .mobius_map import mobius_map
    from .reach import _ipv4
    from .readers import reader_for

    pollers, unchecked, mobius = [], {}, []
    mapped = mobius_map(inv)
    url = inv.data.mobius.influx_url
    reading: Counter = Counter()
    not_set_up: dict[str, set[str]] = {}
    for c in inv.comps.values():
        if type_spec(c.type).source == "mobius" or c.source == "mobius":
            if url and c.type in mapped:
                mobius.append(c)
            else:
                unchecked[c.id] = NOT_MAPPED if url else NO_INFLUX
            continue
        reader = reader_for(c, inv)
        p = DevicePoller(c, can_ping, reader)
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
    if mobius:
        pollers.append(MobiusPoller(inv, mobius))
    notes = ["Live mode: ping and port checks for every device with an address. Device readers: "
             + (", ".join(f"{k} ×{v}" for k, v in sorted(reading.items())) or "none set up yet")
             + "; Mobius: " + (f"{len(mobius)} components read" if mobius else "not mapped yet (run probe-influx)" if url
                               else "no InfluxDB set") + "."]
    if not_set_up:
        notes.append("Device readers not set up (add these to secrets.env): "
                     + "; ".join(f"{k} ({', '.join(sorted(v))})" for k, v in sorted(not_set_up.items())) + ".")
    return pollers, unchecked, notes
