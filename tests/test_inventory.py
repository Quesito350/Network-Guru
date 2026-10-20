import datetime as dt

import pytest

from networkguru.inventory import InventoryError, Schedule, parse_inventory


def test_example_inventory_loads(example_inv):
    assert len(example_inv.tab_groups("vehicles")) == 7
    assert len(example_inv.tab_groups("control")) == 5
    assert len(example_inv.tab_groups("infrastructure")) == 9
    assert example_inv.comps["ac2845-vcu"].source == "mobius"        # VCU defaults to Mobius data


def test_default_dependencies(mini_inv):
    c = mini_inv.comps
    assert c["tr1-radio"].depends_on is None
    assert c["tr1-router"].depends_on == "tr1-radio"
    assert c["tr1-vcu"].depends_on == "tr1-router"
    assert c["t1-gx"].depends_on == "t1-bc"            # trailer devices hang off the trailer radio
    assert c["t1-bc"].depends_on is None
    assert c["srv-ntrip"].depends_on == "srv-mob"


def test_duplicate_and_unknown_type_reported(mini_raw):
    mini_raw["vehicles"][0]["components"].append({"id": "tr1-radio", "type": "rajant"})
    mini_raw["vehicles"][0]["components"].append({"id": "tr1-x", "type": "toaster"})
    with pytest.raises(InventoryError) as e:
        parse_inventory(mini_raw)
    assert "duplicate id 'tr1-radio'" in str(e.value)
    assert "unknown type 'toaster'" in str(e.value)


def test_bad_id_and_missing_site_field(mini_raw):
    mini_raw["vehicles"][0]["id"] = "Truck 1"
    with pytest.raises(InventoryError):
        parse_inventory(mini_raw)
    del mini_raw["site"]["name"]
    with pytest.raises(InventoryError) as e:
        parse_inventory(mini_raw)
    assert "site.name" in str(e.value)


def test_missing_secrets_lists_names_only(mini_inv):
    env = {"TACHYON_RO_USER": "dash"}
    assert mini_inv.missing_secrets(env) == ["TACHYON_RO_PASS"]
    assert mini_inv.missing_secrets({"TACHYON_RO_USER": "a", "TACHYON_RO_PASS": "b"}) == []


def test_public_tree_has_no_secret_refs(mini_inv):
    text = str(mini_inv.public_tree())
    assert "TACHYON_RO_PASS" not in text and "_ref" not in text


def test_offline_severity_defaults(mini_inv):
    assert mini_inv.offline_severity("truck1") == "offline"
    assert mini_inv.offline_severity("t1") == "critical"
    assert mini_inv.offline_severity("srv") == "critical"


def test_schedule_parse_and_active():
    s = Schedule.parse("Mon-Fri 06:00-18:00")
    assert s.active(dt.datetime(2026, 10, 14, 7, 0))          # Wednesday
    assert not s.active(dt.datetime(2026, 10, 14, 18, 0))
    assert not s.active(dt.datetime(2026, 10, 17, 9, 0))      # Saturday
    assert Schedule.parse("always").active(dt.datetime(2026, 10, 18, 3, 0))
    wrap = Schedule.parse("Fri-Mon 08:00-12:00")
    assert wrap.active(dt.datetime(2026, 10, 18, 9, 0)) and not wrap.active(dt.datetime(2026, 10, 14, 9, 0))
    with pytest.raises(ValueError):
        Schedule.parse("weekdays 6-18")
