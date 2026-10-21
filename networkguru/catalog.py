"""What every component type reports, how its numbers are labelled, and which charts it gets.

Sim drivers and real drivers both produce the metric names below, so the rules, the
cards and the charts work the same either way. Adding a device type = one TypeSpec here
(+ a driver); the UI needs no change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: str = ""                 # ms, %, °C, V, A, W, kWh, dBm, dB, bps, s, m, Mbps, or "" (count)
    dec: int = 0                   # decimals shown
    enum: dict[int, str] | None = None   # value -> text (state metrics)


METRICS: dict[str, MetricSpec] = {
    # reachability (our own probes)
    "ping_rtt_ms": MetricSpec("Ping RTT", "ms", 0),
    "ping_loss_pct": MetricSpec("Packet loss", "%", 0),
    "tcp_ms": MetricSpec("TCP connect", "ms", 0),
    "port_up": MetricSpec("Port", "", 0, {1: "Open", 0: "Closed"}),
    "uptime_s": MetricSpec("Uptime", "s", 0),
    "mobius_radio_rtt_ms": MetricSpec("Mobius ping", "ms", 0),
    # Rajant
    "rajant_temp_c": MetricSpec("Temperature", "°C", 0),
    "rajant_input_v": MetricSpec("Input voltage", "V", 1),
    "rajant_peers": MetricSpec("Mesh peers", "", 0),
    "if_rx_bps": MetricSpec("Rx", "bps", 1),
    "if_tx_bps": MetricSpec("Tx", "bps", 1),
    # vehicle routers
    "router_cpu_pct": MetricSpec("CPU", "%", 0),
    "router_mem_used_pct": MetricSpec("Memory used", "%", 0),
    "router_temp_c": MetricSpec("Temperature", "°C", 0),
    "router_supply_v": MetricSpec("Supply voltage", "V", 1),
    "wan_rx_bps": MetricSpec("WAN Rx", "bps", 1),
    "wan_tx_bps": MetricSpec("WAN Tx", "bps", 1),
    "wg_handshake_age_s": MetricSpec("WireGuard handshake age", "s", 0),
    # cellular modem on a vehicle
    "cell_rsrp_dbm": MetricSpec("RSRP", "dBm", 0),
    "cell_sinr_db": MetricSpec("SINR", "dB", 0),
    # ASI VCU (from Mobius)
    "vcu_communicating": MetricSpec("Mobius link", "", 0, {1: "Communicating", 0: "Silent"}),
    "vcu_comms_latency_ms": MetricSpec("Comms latency", "ms", 0),
    "vcu_comms_drop_pct": MetricSpec("Comms drop", "%", 1),
    "vcu_cpu_temp_c": MetricSpec("CPU temp", "°C", 0),
    "vcu_temp_c": MetricSpec("Board temp", "°C", 0),
    "vcu_supply_v": MetricSpec("Supply voltage", "V", 1),
    "vcu_cpu_pct": MetricSpec("CPU", "%", 0),
    # GNSS (from Mobius)
    "gnss_mode": MetricSpec("GNSS fix", "", 0,
                            {4: "RTK fixed", 5: "RTK float", 2: "DGPS", 1: "Autonomous", 0: "No fix"}),
    "gnss_sats": MetricSpec("Satellites", "", 0),
    "gnss_hdop": MetricSpec("HDOP", "", 1),
    "gnss_corr_age_s": MetricSpec("Correction age", "s", 1),
    "gnss_rms_m": MetricSpec("Position RMS", "m", 2),
    # ASI VAI (from Mobius)
    "vai_connected": MetricSpec("VAI link", "", 0, {1: "Connected", 0: "Disconnected"}),
    "vai_heartbeat_age_s": MetricSpec("Heartbeat age", "s", 1),
    # Victron Cerbo GX
    "power_soc_pct": MetricSpec("Battery", "%", 0),
    "power_batt_v": MetricSpec("Battery voltage", "V", 1),
    "power_batt_a": MetricSpec("Battery current", "A", 1),
    "power_batt_w": MetricSpec("Battery power", "W", 0),
    "power_pv_w": MetricSpec("Solar", "W", 0),
    "power_load_w": MetricSpec("Load", "W", 0),
    "power_mppt_state": MetricSpec("Charger", "", 0,
                                   {0: "Off", 2: "Fault", 3: "Bulk", 4: "Absorption", 5: "Float",
                                    6: "Storage", 7: "Equalize"}),
    "power_yield_today_kwh": MetricSpec("Solar yield today", "kWh", 2),
    # Starlink
    "starlink_state": MetricSpec("Dish state", "", 0, {1: "Connected", 0: "Searching"}),
    "starlink_pop_latency_ms": MetricSpec("PoP latency", "ms", 0),
    "starlink_pop_drop_pct": MetricSpec("PoP drop", "%", 1),
    "starlink_obstructed_pct": MetricSpec("Obstructed", "%", 1),
    "starlink_down_bps": MetricSpec("Download", "bps", 1),
    "starlink_up_bps": MetricSpec("Upload", "bps", 1),
    "starlink_alerts": MetricSpec("Active alerts", "", 0),
    "starlink_power_w": MetricSpec("Dish power", "W", 0),
    # Tachyon 60 GHz
    "tachyon_link_up": MetricSpec("60 GHz link", "", 0, {1: "Up", 0: "Down"}),
    "tachyon_rssi_dbm": MetricSpec("RSSI", "dBm", 0),
    "tachyon_remote_rssi_dbm": MetricSpec("Remote RSSI", "dBm", 0),
    "tachyon_snr_db": MetricSpec("SNR", "dB", 0),
    "tachyon_tx_mcs": MetricSpec("Tx MCS", "", 0),
    "tachyon_phy_rate_mbps": MetricSpec("PHY rate", "Mbps", 0),
    "tachyon_radio_temp_c": MetricSpec("Radio temp", "°C", 0),
    # Ubiquiti airMAX
    "airmax_signal_dbm": MetricSpec("Signal", "dBm", 0),
    "airmax_noise_dbm": MetricSpec("Noise floor", "dBm", 0),
    "airmax_quality_pct": MetricSpec("airMAX quality", "%", 0),
    "airmax_capacity_pct": MetricSpec("airMAX capacity", "%", 0),
    # Ubiquiti UniFi
    "unifi_cpu_pct": MetricSpec("CPU", "%", 0),
    "unifi_mem_pct": MetricSpec("Memory", "%", 0),
    "unifi_uplink_rx_bps": MetricSpec("Uplink Rx", "bps", 1),
    "unifi_uplink_tx_bps": MetricSpec("Uplink Tx", "bps", 1),
    # managed PoE switch on a trailer
    "switch_ports_up": MetricSpec("Ports up", "", 0),
    "switch_poe_w": MetricSpec("PoE power", "W", 0),
    # Celona
    "celona_ues": MetricSpec("Connected devices", "", 0),
    "celona_aps_up": MetricSpec("APs up", "", 0),
    # Mobius server / InfluxDB / NTRIP / clients
    "mobius_up": MetricSpec("Mobius service", "", 0, {1: "Up", 0: "Down"}),
    "mobius_clients": MetricSpec("Connected clients", "", 0),
    "mobius_assets": MetricSpec("Communicating assets", "", 0),
    "mobius_clock_skew_s": MetricSpec("Clock skew", "s", 2),
    "influx_write_age_s": MetricSpec("Newest data age", "s", 0),
    "ntrip_connected": MetricSpec("RTK corrections", "", 0, {1: "Connected", 0: "Disconnected"}),
    "client_connected": MetricSpec("Mobius session", "", 0, {1: "Connected", 0: "Not connected"}),
    "host_cpu_pct": MetricSpec("CPU", "%", 0),
    "host_mem_used_pct": MetricSpec("Memory used", "%", 0),
    "host_disk_free_pct": MetricSpec("Disk free", "%", 0),
    # written by the pipeline for every node
    "status": MetricSpec("Status", "", 0),
}


@dataclass(frozen=True)
class SeriesSpec:
    metric: str
    label: str = ""


@dataclass(frozen=True)
class ChartSpec:
    key: str
    title: str
    series: tuple[SeriesSpec, ...]
    kind: str = "line"             # line | area | state | daily
    primary: bool = False          # shown on group pages (vehicle / location / control room)
    ymin: float | None = None
    ymax: float | None = None


def C(key, title, *series, kind="line", primary=False, ymin=None, ymax=None) -> ChartSpec:
    return ChartSpec(key, title, tuple(SeriesSpec(m, lbl) for m, lbl in series), kind, primary, ymin, ymax)


@dataclass(frozen=True)
class TypeSpec:
    key: str
    label: str                     # "Rajant BreadCrumb"
    short: str                     # 2-4 letters for device dots
    interval: float                # seconds between polls
    headline: tuple[str, ...]      # metrics shown on cards / tables
    charts: tuple[ChartSpec, ...]
    source: str = "device"         # device = polled directly; mobius = read from Mobius InfluxDB
    ping: bool = True              # gets the implicit ping probe when it has a host
    load_w: float = 0.0            # typical draw on a solar trailer (simulator)


RTT = C("rtt", "Round-trip time", ("ping_rtt_ms", "Ping"), primary=True, ymin=0)
LOSS = C("loss", "Packet loss", ("ping_loss_pct", "Loss"), ymin=0, ymax=100)

TYPES: dict[str, TypeSpec] = {t.key: t for t in [
    TypeSpec("rajant", "Rajant BreadCrumb", "BC", 10,
             ("ping_rtt_ms", "rajant_peers", "rajant_temp_c"),
             (C("rtt", "Round-trip time", ("ping_rtt_ms", "Dashboard ping"), ("mobius_radio_rtt_ms", "Mobius ping"),
                primary=True, ymin=0),
              LOSS,
              C("peers", "Mesh peers", ("rajant_peers", "Peers"), ymin=0),
              C("temp", "Temperature", ("rajant_temp_c", "Temperature")),
              C("volt", "Input voltage", ("rajant_input_v", "Input voltage")),
              C("traffic", "Traffic", ("if_rx_bps", "Rx"), ("if_tx_bps", "Tx"), ymin=0)),
             load_w=18),
    TypeSpec("mikrotik", "MikroTik router", "MT", 30,
             ("ping_rtt_ms", "router_cpu_pct", "router_temp_c"),
             (RTT,
              C("cpu", "CPU and memory", ("router_cpu_pct", "CPU"), ("router_mem_used_pct", "Memory"), ymin=0, ymax=100),
              C("traffic", "WAN traffic", ("wan_rx_bps", "Rx"), ("wan_tx_bps", "Tx"), ymin=0),
              C("temp", "Temperature", ("router_temp_c", "Temperature")),
              C("volt", "Supply voltage", ("router_supply_v", "Supply voltage")),
              C("wg", "WireGuard handshake age", ("wg_handshake_age_s", "Handshake age"), ymin=0),
              LOSS)),
    TypeSpec("westermo", "Westermo Viper", "WV", 30,
             ("ping_rtt_ms", "router_cpu_pct", "router_temp_c"),
             (RTT,
              C("cpu", "CPU and memory", ("router_cpu_pct", "CPU"), ("router_mem_used_pct", "Memory"), ymin=0, ymax=100),
              C("traffic", "WAN traffic", ("wan_rx_bps", "Rx"), ("wan_tx_bps", "Tx"), ymin=0),
              C("temp", "Temperature", ("router_temp_c", "Temperature")),
              LOSS)),
    TypeSpec("cell_modem", "Cellular modem", "LTE", 30,
             ("ping_rtt_ms", "cell_rsrp_dbm", "cell_sinr_db"),
             (RTT,
              C("rsrp", "Signal (RSRP)", ("cell_rsrp_dbm", "RSRP"), primary=True),
              C("sinr", "SINR", ("cell_sinr_db", "SINR")),
              LOSS)),
    TypeSpec("vcu", "ASI VCU", "VCU", 10,
             ("vcu_comms_latency_ms", "vcu_comms_drop_pct", "vcu_cpu_temp_c"),
             (C("latency", "Mobius comms latency", ("vcu_comms_latency_ms", "Latency"), primary=True, ymin=0),
              C("drop", "Mobius comms drop", ("vcu_comms_drop_pct", "Drop"), primary=True, ymin=0),
              C("link", "Mobius link", ("vcu_communicating", "Link"), kind="state"),
              C("temp", "Temperatures", ("vcu_cpu_temp_c", "CPU"), ("vcu_temp_c", "Board")),
              C("volt", "Supply voltage", ("vcu_supply_v", "Supply voltage")),
              C("cpu", "CPU load", ("vcu_cpu_pct", "CPU"), ymin=0, ymax=100)),
             source="mobius", ping=False),
    TypeSpec("gnss", "GNSS (VCU)", "GPS", 10,
             ("gnss_mode", "gnss_corr_age_s", "gnss_sats"),
             (C("mode", "GNSS fix", ("gnss_mode", "Fix"), kind="state", primary=True),
              C("corr", "RTK correction age", ("gnss_corr_age_s", "Correction age"), primary=True, ymin=0),
              C("sats", "Satellites", ("gnss_sats", "Satellites"), ymin=0),
              C("hdop", "HDOP", ("gnss_hdop", "HDOP"), ymin=0),
              C("rms", "Position RMS", ("gnss_rms_m", "RMS"), ymin=0)),
             source="mobius", ping=False),
    TypeSpec("vai", "ASI VAI", "VAI", 10,
             ("vai_connected", "vai_heartbeat_age_s"),
             (C("link", "VAI link", ("vai_connected", "Link"), kind="state", primary=True),
              C("hb", "Heartbeat age", ("vai_heartbeat_age_s", "Heartbeat age"), ymin=0)),
             source="mobius", ping=False),
    TypeSpec("victron_cerbo", "Victron Cerbo GX", "PWR", 30,
             ("power_soc_pct", "power_pv_w", "power_load_w"),
             (C("soc", "Battery state of charge", ("power_soc_pct", "SOC"), primary=True, ymin=0, ymax=100),
              C("pv", "Solar vs load", ("power_pv_w", "Solar"), ("power_load_w", "Load"), kind="area",
                primary=True, ymin=0),
              C("battv", "Battery voltage", ("power_batt_v", "Voltage")),
              C("batta", "Battery current", ("power_batt_a", "Current")),
              C("mppt", "Charger state", ("power_mppt_state", "Charger"), kind="state", primary=True),
              C("yield", "Daily solar yield", ("power_yield_today_kwh", "Yield"), kind="daily", primary=True)),
             load_w=3),
    TypeSpec("starlink", "Starlink", "SL", 15,
             ("starlink_pop_latency_ms", "starlink_pop_drop_pct", "starlink_obstructed_pct"),
             (C("lat", "PoP latency", ("starlink_pop_latency_ms", "Latency"), primary=True, ymin=0),
              C("drop", "PoP drop rate", ("starlink_pop_drop_pct", "Drop"), primary=True, ymin=0),
              C("obs", "Obstructed", ("starlink_obstructed_pct", "Obstructed"), ymin=0),
              C("tput", "Throughput", ("starlink_down_bps", "Download"), ("starlink_up_bps", "Upload"), ymin=0),
              C("state", "Dish state", ("starlink_state", "State"), kind="state"),
              C("power", "Dish power", ("starlink_power_w", "Power"), ymin=0)),
             ping=False, load_w=60),
    TypeSpec("tachyon", "Tachyon 60 GHz", "60G", 30,
             ("tachyon_rssi_dbm", "tachyon_snr_db", "tachyon_tx_mcs"),
             (C("rssi", "Signal", ("tachyon_rssi_dbm", "Local"), ("tachyon_remote_rssi_dbm", "Remote"), primary=True),
              C("link", "60 GHz link", ("tachyon_link_up", "Link"), kind="state"),
              C("snr", "SNR", ("tachyon_snr_db", "SNR")),
              C("mcs", "Tx MCS", ("tachyon_tx_mcs", "MCS"), ymin=0),
              C("rate", "PHY rate", ("tachyon_phy_rate_mbps", "PHY rate"), ymin=0),
              C("temp", "Radio temperature", ("tachyon_radio_temp_c", "Temperature")),
              RTT),
             load_w=10),
    TypeSpec("airmax", "Ubiquiti airMAX", "AM", 30,
             ("airmax_signal_dbm", "airmax_quality_pct", "airmax_capacity_pct"),
             (C("signal", "Signal and noise", ("airmax_signal_dbm", "Signal"), ("airmax_noise_dbm", "Noise floor"),
                primary=True),
              C("quality", "airMAX quality and capacity", ("airmax_quality_pct", "Quality"),
                ("airmax_capacity_pct", "Capacity"), ymin=0, ymax=100),
              RTT),
             load_w=8),
    TypeSpec("unifi", "Ubiquiti UniFi", "UF", 60,
             ("ping_rtt_ms", "unifi_cpu_pct", "unifi_mem_pct"),
             (RTT,
              C("cpu", "CPU and memory", ("unifi_cpu_pct", "CPU"), ("unifi_mem_pct", "Memory"), ymin=0, ymax=100),
              C("uplink", "Uplink traffic", ("unifi_uplink_rx_bps", "Rx"), ("unifi_uplink_tx_bps", "Tx"), ymin=0)),
             load_w=15),
    TypeSpec("switch", "Managed PoE switch", "SW", 30,
             ("ping_rtt_ms", "switch_ports_up", "switch_poe_w"),
             (RTT,
              C("poe", "PoE power to devices", ("switch_poe_w", "PoE"), primary=True, ymin=0),
              C("ports", "Ports up", ("switch_ports_up", "Ports up"), ymin=0),
              LOSS),
             load_w=8),
    TypeSpec("celona_ap", "Celona AP", "CEL", 30,
             ("ping_rtt_ms", "celona_ues"),
             (RTT,
              C("ues", "Connected devices", ("celona_ues", "Devices"), primary=True, ymin=0),
              LOSS),
             load_w=25),
    TypeSpec("celona_edge", "Celona Edge", "EDGE", 30,
             ("ping_rtt_ms", "celona_aps_up"),
             (RTT,
              C("aps", "APs up", ("celona_aps_up", "APs up"), primary=True, ymin=0),
              LOSS)),
    TypeSpec("mobius_server", "Mobius Server", "MOB", 10,
             ("mobius_clients", "mobius_assets", "host_cpu_pct"),
             (C("clients", "Connected clients", ("mobius_clients", "Clients"), primary=True, ymin=0),
              C("assets", "Communicating assets", ("mobius_assets", "Assets"), primary=True, ymin=0),
              C("host", "Server CPU and memory", ("host_cpu_pct", "CPU"), ("host_mem_used_pct", "Memory"),
                primary=True, ymin=0, ymax=100),
              C("svc", "Mobius service (port 18080)", ("mobius_up", "Service"), kind="state"),
              C("skew", "Clock skew", ("mobius_clock_skew_s", "Skew")),
              C("tcp", "Port 18080 connect time", ("tcp_ms", "Connect"), ymin=0))),
    TypeSpec("influxdb", "Mobius InfluxDB", "DB", 30,
             ("tcp_ms", "influx_write_age_s"),
             (C("age", "Newest data age", ("influx_write_age_s", "Age"), primary=True, ymin=0),
              C("tcp", "Port 8086 connect time", ("tcp_ms", "Connect"), ymin=0),
              RTT)),
    TypeSpec("mobius_ntrip", "NTRIP client (RTK)", "RTK", 10,
             ("ntrip_connected",),
             (C("ntrip", "RTK corrections", ("ntrip_connected", "Corrections"), kind="state", primary=True),),
             source="mobius", ping=False),
    TypeSpec("mobius_client", "Mobius Client", "PC", 30,
             ("client_connected", "host_cpu_pct", "host_mem_used_pct"),
             (C("session", "Mobius session", ("client_connected", "Session"), kind="state", primary=True),
              C("host", "CPU and memory", ("host_cpu_pct", "CPU"), ("host_mem_used_pct", "Memory"),
                primary=True, ymin=0, ymax=100),
              C("disk", "Disk free", ("host_disk_free_pct", "Disk free"), ymin=0, ymax=100),
              RTT)),
    TypeSpec("gnss_base", "GNSS base station", "BASE", 30,
             ("ping_rtt_ms", "tcp_ms"),
             (RTT,
              C("tcp", "NTRIP caster connect time", ("tcp_ms", "Connect"), primary=True, ymin=0),
              LOSS),
             load_w=6),
    TypeSpec("host", "Network device", "NET", 10,
             ("ping_rtt_ms", "ping_loss_pct"),
             (RTT, LOSS)),
]}

# Path view: which component types count as each hop of a vehicle's chain.
VEHICLE_RADIO_TYPES = ("rajant", "cell_modem")
VEHICLE_ROUTER_TYPES = ("mikrotik", "westermo")
ACCESS_TYPES = {"rajant": ("rajant",), "celona": ("celona_ap", "celona_edge")}


def metric_spec(name: str) -> MetricSpec:
    return METRICS.get(name) or MetricSpec(name)


def type_spec(key: str) -> TypeSpec:
    return TYPES.get(key) or TYPES["host"]


def catalog_json() -> dict:
    """The catalog as plain JSON for the browser."""
    return {
        "metrics": {k: {"label": m.label, "unit": m.unit, "dec": m.dec,
                        "enum": {str(a): b for a, b in (m.enum or {}).items()} or None}
                    for k, m in METRICS.items()},
        "types": {k: {"label": t.label, "short": t.short, "headline": list(t.headline),
                      "source": t.source,
                      "charts": [{"key": c.key, "title": c.title, "kind": c.kind, "primary": c.primary,
                                  "ymin": c.ymin, "ymax": c.ymax,
                                  "series": [{"metric": s.metric, "label": s.label} for s in c.series]}
                                 for c in t.charts]}
                  for k, t in TYPES.items()},
    }
