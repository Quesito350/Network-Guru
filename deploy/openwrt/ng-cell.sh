#!/bin/sh
# Network Guru - an OpenWrt cell modem's signal, for the dashboard's SNMP reader (0.8.0).
#
# Prints key=value lines (rsrp, rsrq, sinr, rssi, operator, tech, registration). snmpd runs it as
# "extend ngcell" and Network Guru reads NET-SNMP-EXTEND-MIB nsExtendOutputFull."ngcell" - read-only.
# Uses ModemManager (mmcli) when the modem is managed by it, else uqmi (QMI modems).
#
# Install once per modem (as root on the modem):
#   1. copy this file to /usr/bin/ng-cell.sh and run:  chmod 755 /usr/bin/ng-cell.sh
#   2. check it:  /usr/bin/ng-cell.sh        (it should print rsrp=... lines)
#   3. add to /etc/config/snmpd:
#        config extend
#            option name ngcell
#            option prog /usr/bin/ng-cell.sh
#      and restart snmpd:  /etc/init.d/snmpd restart
# The QMI device can be changed with NG_CELL_DEV (default /dev/cdc-wdm0).
# Nothing here changes the modem; it never prints the IMEI, IMSI or SIM number.

DEV="${NG_CELL_DEV:-/dev/cdc-wdm0}"

out() {
    case "$2" in ""|"--"|null) ;; *) echo "$1=$2" ;; esac
}

num() {    # "-94.00" -> -94.00 ; anything else -> nothing
    echo "$1" | sed -n 's/^\(-\{0,1\}[0-9][0-9]*\(\.[0-9]*\)\{0,1\}\).*/\1/p'
}

if command -v mmcli >/dev/null 2>&1 && mmcli -L 2>/dev/null | grep -q "/Modem/"; then
    mmcli -m any --signal-setup=30 >/dev/null 2>&1
    sig=$(mmcli -m any --signal-get -J 2>/dev/null)
    for k in rsrp rsrq rssi; do
        out "$k" "$(num "$(echo "$sig" | jsonfilter -e "@.modem.signal.lte.$k" 2>/dev/null)")"
    done
    out sinr "$(num "$(echo "$sig" | jsonfilter -e '@.modem.signal.lte.snr' 2>/dev/null)")"
    info=$(mmcli -m any -J 2>/dev/null)
    out operator "$(echo "$info" | jsonfilter -e '@.modem["3gpp"]["operator-name"]' 2>/dev/null | tr ' ' '_')"
    out registration "$(echo "$info" | jsonfilter -e '@.modem["3gpp"]["registration-state"]' 2>/dev/null)"
    out tech "$(echo "$info" | jsonfilter -e '@.modem.generic["access-technologies"][0]' 2>/dev/null)"
elif command -v uqmi >/dev/null 2>&1 && [ -e "$DEV" ]; then
    sig=$(uqmi -s -d "$DEV" --get-signal-info 2>/dev/null)
    for k in rsrp rsrq rssi; do
        out "$k" "$(num "$(echo "$sig" | jsonfilter -e "@.$k" 2>/dev/null)")"
    done
    out sinr "$(num "$(echo "$sig" | jsonfilter -e '@.snr' 2>/dev/null)")"
    out tech "$(echo "$sig" | jsonfilter -e '@.type' 2>/dev/null)"
    srv=$(uqmi -s -d "$DEV" --get-serving-system 2>/dev/null)
    out registration "$(echo "$srv" | jsonfilter -e '@.registration' 2>/dev/null)"
    out operator "$(echo "$srv" | jsonfilter -e '@.plmn_description' 2>/dev/null | tr ' ' '_')"
else
    echo "error=no_mmcli_or_uqmi_modem"
fi
