"""Where each device is right now.

Static devices: the address in the inventory. DHCP devices: whatever a lookup source last
saw for their serial / MAC / hostname - the inventory `host` is only a starting hint. The
real lookup sources (UniFi client list, MikroTik leases/ARP, the Rajant mesh ARP table
over SNMP, passive MNDP, DNS) arrive with the real drivers; each must confirm the device's
identity (serial / identity / sysName) before its address is trusted, so a reused IP never
mixes two devices' data. Metrics are stored by component id, never by IP, so history
carries on across address changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AddressEntry:
    address: str
    source: str                 # inventory | last known | sim | unifi | mikrotik | rajant-arp | mndp | dns
    seen: float | None = None


class AddressBook:
    def __init__(self, inv):
        self.inv = inv
        self.entries: dict[str, AddressEntry] = {}
        self._by_serial = {c.serial: c.id for c in inv.comps.values() if c.serial}
        self._by_mac = {c.mac: c.id for c in inv.comps.values() if c.mac}
        self._by_host = {c.hostname.lower(): c.id for c in inv.comps.values() if c.hostname}
        for c in inv.comps.values():
            if c.host:
                self.entries[c.id] = AddressEntry(c.host, "inventory" if c.address == "static" else "last known")

    def get(self, comp_id: str) -> str | None:
        e = self.entries.get(comp_id)
        return e.address if e else None

    def set(self, comp_id: str, address: str, source: str, ts: float) -> str | None:
        """Record where a device is; returns the previous address when it moved."""
        old = self.entries.get(comp_id)
        self.entries[comp_id] = AddressEntry(address, source, ts)
        return old.address if old and old.address != address else None

    def find(self, serial: str | None = None, mac: str | None = None, hostname: str | None = None) -> str | None:
        """Component id for a lookup-source record, by the strongest identity first."""
        if serial and serial in self._by_serial:
            return self._by_serial[serial]
        if mac:
            from .inventory import normalise_mac
            try:
                m = normalise_mac(mac)
            except ValueError:
                m = None
            if m and m in self._by_mac:
                return self._by_mac[m]
        if hostname and hostname.lower() in self._by_host:
            return self._by_host[hostname.lower()]
        return None
