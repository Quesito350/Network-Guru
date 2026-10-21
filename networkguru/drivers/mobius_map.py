"""Where each Mobius value lives in the Mobius InfluxDB.

    {component type: {"measurement": "<name>",
                      "asset_tag": "<tag holding the vehicle>"   (optional: else mobius.asset_tag),
                      "fields": {"<our metric, or lat / lon>": "<influx field>", ...}}}

Empty until `probe-influx` has shown the real names at a site; an inventory's `mobius: map:` block
overrides it per type. A mapped GNSS component's lat / lon fields feed the map.
"""

from __future__ import annotations

DEFAULT_MAP: dict[str, dict] = {}


def mobius_map(inv) -> dict[str, dict]:
    extra = getattr(inv.data.mobius, "model_extra", None) or {}
    over = extra.get("map") or {}
    out = {k: dict(v) for k, v in DEFAULT_MAP.items()}
    for typ, spec in over.items():
        if isinstance(spec, dict) and spec.get("measurement") and isinstance(spec.get("fields"), dict):
            out[typ] = spec
    return out
