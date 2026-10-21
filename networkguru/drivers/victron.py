"""Victron Cerbo GX over Modbus TCP -> our power_* metrics.

Register numbers, types and scales are from Victron's "Modbus-TCP register list" (rev 3.81,
victronenergy.com - Whitepapers). A value is the register divided by its scale.

    unit 100, com.victronenergy.system   (no set-up needed beyond switching Modbus TCP on)
      817-819  AC consumption L1-L3       uint16  W
      840      battery voltage            uint16  /10  V
      841      battery current            int16   /10  A
      842      battery power              int16        W
      843      battery state of charge    uint16       %
      850      PV DC-coupled power        uint16       W
      860      DC system power            int16        W
    the solar charger's own unit ID (`mppt_unit`; Cerbo: Settings > Services > Modbus TCP >
    Available services), com.victronenergy.solarcharger
      775      charge state               uint16       0 Off, 2 Fault, 3 Bulk, 4 Absorption, 5 Float, ...
      784      yield today                uint16  /10  kWh

Switch it on in the Cerbo: Settings > Services > Modbus TCP > Enable.
"""

from __future__ import annotations

from ..model import Metric
from .modbus import ModbusClient, ModbusError, signed

SYSTEM_UNIT = 100


async def read(m: ModbusClient, mppt_unit: int | None = None) -> tuple[list[Metric], dict]:
    ac = await m.read_holding(SYSTEM_UNIT, 817, 3)
    batt = await m.read_holding(SYSTEM_UNIT, 840, 4)
    pv = await m.read_holding(SYSTEM_UNIT, 850, 1)
    dc = await m.read_holding(SYSTEM_UNIT, 860, 1)
    metrics = [
        Metric("power_batt_v", batt[0] / 10.0),
        Metric("power_batt_a", signed(batt[1]) / 10.0),
        Metric("power_batt_w", float(signed(batt[2]))),
        Metric("power_soc_pct", float(batt[3])),
        Metric("power_pv_w", float(pv[0])),
        Metric("power_load_w", float(max(0, signed(dc[0])) + sum(ac))),
    ]
    info: dict = {}
    if mppt_unit:
        try:
            state = await m.read_holding(int(mppt_unit), 775, 1)
            yld = await m.read_holding(int(mppt_unit), 784, 1)
        except ModbusError as e:
            info["mppt_error"] = f"solar charger (unit {mppt_unit}): {e}"
        else:
            metrics += [Metric("power_mppt_state", float(state[0])), Metric("power_yield_today_kwh", yld[0] / 10.0)]
    return metrics, info
