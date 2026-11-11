"""0.10.0 tower coverage: Coverage m / Antenna bearing / Beam width on the Groups sheet go through the build
into the inventory and the browser's tree (the map's Coverage layer); bad values are report lines and left out.
Made-up places only."""

from networkguru.importers.build import build_inventory
from networkguru.importers.sheet import GROUP_COLS, Sheet
from networkguru.inventory import parse_inventory


def _sheet(tmp_path):
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "America/Denver", "Lat": "40.0", "Lon": "-111.0",
                        "Mobius InfluxDB URL": "", "Mobius vehicle tag": "", "Netmaker URL": "", "Subnets": "",
                        "Infrastructure subnet": "", "Notes": ""})
    groups = {
        "Omni Tower": {"Coverage m": "1500"},
        "Sector Tower": {"Coverage m": "2200", "Antenna bearing": "135", "Beam width": "90"},
        "Half Sector": {"Coverage m": "900", "Antenna bearing": "40"},           # a width is missing: an omni
        "Bad Range": {"Coverage m": "far"},
        "Width Only": {"Beam width": "60"},
        "Plain Trailer": {},
    }
    for i, (name, cols) in enumerate(groups.items()):
        sheet.upsert_group("lab", name, "tower" if "Trailer" not in name else "trailer",
                           Lat=f"40.0{i}", Lon=f"-111.0{i}", **cols)
        sheet.upsert_device({"Site": "lab", "Group": name, "Type": "rajant", "Name": "BC", "IP": f"10.200.9.{i + 10}"})
    sheet.upsert_group("lab", "Truck", "vehicle", **{"Coverage m": "500"})               # vehicles never get one
    sheet.upsert_device({"Site": "lab", "Group": "Truck", "Type": "rajant", "Name": "ME4", "IP": "10.200.9.90"})
    path = tmp_path / "devices.xlsx"
    sheet.save(path)
    return Sheet.load(path)


def test_columns_are_on_the_sheet():
    assert GROUP_COLS.index("Coverage m") == GROUP_COLS.index("Lon") + 1
    assert {"Antenna bearing", "Beam width"} <= set(GROUP_COLS)


def test_coverage_goes_through_the_build(tmp_path):
    data, report = build_inventory(_sheet(tmp_path), "lab")
    groups = {g["name"]: g for tab in ("vehicles", "control_room", "infrastructure") for g in data[tab]}
    assert groups["Omni Tower"]["coverage_m"] == 1500.0 and "bearing" not in groups["Omni Tower"]
    s = groups["Sector Tower"]
    assert (s["coverage_m"], s["bearing"], s["beamwidth"]) == (2200.0, 135.0, 90.0)
    assert groups["Half Sector"]["coverage_m"] == 900.0 and "bearing" not in groups["Half Sector"]
    assert "coverage_m" not in groups["Bad Range"] and "coverage_m" not in groups["Width Only"]
    assert "coverage_m" not in groups["Plain Trailer"] and "coverage_m" not in groups["Truck"]
    text = "\n".join(report)
    assert "Bad Range: Coverage m 'far' is not 1-100000 - left out" in text
    assert "Half Sector: a sector needs both Antenna bearing and Beam width - drawn as an omni" in text
    assert "Width Only: Antenna bearing / Beam width without Coverage m - left out" in text

    tree = parse_inventory(data).public_tree()
    by_name = {g["name"]: g for t in tree["tabs"] for g in t["groups"]}
    assert (by_name["Sector Tower"]["coverage_m"], by_name["Sector Tower"]["bearing"], by_name["Sector Tower"]["beamwidth"]) \
        == (2200.0, 135.0, 90.0)
    assert by_name["Plain Trailer"]["coverage_m"] is None
