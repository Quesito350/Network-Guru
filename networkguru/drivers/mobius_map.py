"""Where each Mobius value lives in the Mobius InfluxDB (0.9.2: one table per value).

    {component type: {"<our metric, or lat / lon>": {"measurement": "<table>",
                                                     "field": "<field>"          (default Value),
                                                     "values": {"<text>": n}     (text -> number; "*" = any other),
                                                     "asset_tag": "<tag>"}}}     (default mobius.asset_tag)

Mobius keeps one table per signal (field `Value`, the vehicle in a tag). The map comes from the site:
the "Mobius map" sheet of devices.xlsx, written into inventory.yaml (`mobius: map:`) by build-inventory -
the repo carries no Mobius table names, so DEFAULT_MAP is empty. The 0.7.0 shape
({type: {"measurement", "fields": {ours: field}}}) is still read.

Besides the vehicle types (vcu, gnss, vai, mobius_ntrip):
    influxdb       influx_write_age_s: a table Mobius writes all the time - its newest point is the data age
    mobius_server  mobius_assets: the link table - vehicles whose newest value is true
                   mobius_clients: a table + field whose distinct values in the last 5 minutes are the clients
"""

from __future__ import annotations

DEFAULT_MAP: dict[str, dict] = {}
LINK = "vcu_communicating"          # a vehicle whose newest link value is false is not communicating
EMPTY = "(empty)"                   # how the sheet writes an empty text value


def _numbers(values) -> dict[str, float]:
    out = {}
    for k, v in (values.items() if isinstance(values, dict) else []):
        try:
            out["" if str(k) == EMPTY else str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _value(spec) -> dict | None:
    if isinstance(spec, str) and spec.strip():
        spec = {"measurement": spec}
    if not isinstance(spec, dict) or not spec.get("measurement"):
        return None
    out = {"measurement": str(spec["measurement"]), "field": str(spec.get("field") or "Value"),
           "values": _numbers(spec.get("values"))}
    if spec.get("asset_tag"):
        out["asset_tag"] = str(spec["asset_tag"])
    return out


def normalise(type_map) -> dict[str, dict]:
    """One type's map, per value."""
    if not isinstance(type_map, dict):
        return {}
    if type_map.get("measurement") and isinstance(type_map.get("fields"), dict):        # the 0.7.0 shape
        base = {"measurement": type_map["measurement"], "asset_tag": type_map.get("asset_tag")}
        return {str(ours): _value({**base, "field": f}) for ours, f in type_map["fields"].items() if f}
    out = {}
    for ours, spec in type_map.items():
        v = _value(spec)
        if v:
            out[str(ours)] = v
    return out


def mobius_map(inv) -> dict[str, dict[str, dict]]:
    extra = getattr(inv.data.mobius, "model_extra", None) or {}
    over = extra.get("map") or {}
    out = {k: normalise(v) for k, v in DEFAULT_MAP.items()}
    for typ, spec in (over.items() if isinstance(over, dict) else []):
        n = normalise(spec)
        if n:
            out[str(typ)] = n
    return {k: v for k, v in out.items() if v}


def as_number(spec: dict, v) -> float | None:
    """A Mobius value as a number: numbers as they are, true / false as 1 / 0, text through `values`."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    vals = spec.get("values") or {}
    if v in vals:
        return vals[v]
    if v.strip() in vals:
        return vals[v.strip()]
    if "*" in vals:
        return vals["*"]
    try:
        return float(v)
    except ValueError:
        return None


def parse_values(text: str) -> dict[str, float]:
    """The sheet's "Text values" cell: `Fixed=4, Float=5, (empty)=0, *=0`."""
    out = {}
    for part in str(text or "").replace(";", ",").split(","):
        k, sep, v = part.rpartition("=")
        if not sep:
            continue
        try:
            out[k.strip()] = float(v.strip())
        except ValueError:
            continue
    return out
