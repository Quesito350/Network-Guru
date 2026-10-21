"""Threshold rules: numbers -> Status + human-readable reasons.

Pure logic, no I/O apart from loading YAML. Memory of "how long has this been bad" and
"which level are we in" lives in RuleMemory so hysteresis and `for:` work.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .catalog import metric_spec
from .model import Metric, Status

DEFAULT_RULES_PATH = Path(__file__).with_name("rules_default.yaml")
LEVEL = {"ok": Status.OK, "warning": Status.WARNING, "warn": Status.WARNING,
         "critical": Status.CRITICAL, "crit": Status.CRITICAL, "offline": Status.OFFLINE,
         "unknown": Status.UNKNOWN}


def parse_duration(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", str(v).lower())
    if not m:
        raise ValueError(f"bad duration {v!r} (use 30s, 2m, 1h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def fmt_value(name: str, v: float) -> str:
    """'312 ms', '12.4 Mbps', 'RTK float' - used in reasons and events."""
    spec = metric_spec(name)
    if spec.enum is not None:
        return spec.enum.get(int(round(v)), str(v))
    unit = spec.unit
    if unit == "bps":
        for div, u in ((1e9, "Gbps"), (1e6, "Mbps"), (1e3, "kbps")):
            if abs(v) >= div:
                return f"{v / div:.1f} {u}"
        return f"{v:.0f} bps"
    if unit == "s" and v >= 120:
        return f"{v / 60:.0f} min" if v < 7200 else f"{v / 3600:.1f} h"
    num = f"{v:.{spec.dec}f}"
    if not unit:
        return num
    return f"{num}{unit}" if unit in ("%", "°C") else f"{num} {unit}"


def merge_rules(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k == "metrics" and isinstance(v, dict):
            out.setdefault("metrics", {})
            for mk, mv in v.items():
                out["metrics"][mk] = {**out["metrics"].get(mk, {}), **(mv or {})}
        else:
            out[k] = v
    return out


def load_rules(site_rules: str | Path | None = None) -> dict:
    base = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    if site_rules and Path(site_rules).is_file():
        base = merge_rules(base, yaml.safe_load(Path(site_rules).read_text(encoding="utf-8")) or {})
    validate_rules(base)
    return base


def validate_rules(rules: dict) -> None:
    for name, r in (rules.get("metrics") or {}).items():
        unknown = set(r) - {"warn", "crit", "low_warn", "low_crit", "enum", "zero", "for"}
        if unknown:
            raise ValueError(f"rules for {name}: unknown key(s) {sorted(unknown)}")
        parse_duration(r.get("for"))
        if "zero" in r and str(r["zero"]).lower() not in LEVEL:
            raise ValueError(f"rules for {name}: zero must be offline/critical/warning")
        for lvl in (r.get("enum") or {}).values():
            if str(lvl).lower() not in LEVEL:
                raise ValueError(f"rules for {name}: enum level {lvl!r} unknown")


@dataclass
class _MetricMemory:
    level: Status = Status.OK
    breach_since: float | None = None


@dataclass
class RuleMemory:
    """Per-component state the rules need between polls."""
    metrics: dict[str, _MetricMemory] = field(default_factory=dict)


@dataclass
class Evaluation:
    status: Status
    reasons: list[str]
    offline: bool = False          # a `zero: offline` rule fired - the device itself is absent


class RuleSet:
    def __init__(self, rules: dict):
        self.rules = rules
        self.hyst = float(rules.get("hysteresis", 0.05))
        self.offline_after = int(rules.get("offline_after_polls", 3))
        self.stale_after = float(rules.get("stale_after_intervals", 4))
        self._metric_rules: dict[str, dict] = rules.get("metrics") or {}

    def metric_rule(self, name: str, override: dict | None = None) -> dict | None:
        base = self._metric_rules.get(name)
        if override and name in override:
            return {**(base or {}), **override[name]}
        return base

    def thresholds_json(self) -> dict:
        """For drawing threshold lines on charts."""
        out = {}
        for name, r in self._metric_rules.items():
            t = {k: r[k] for k in ("warn", "crit", "low_warn", "low_crit") if k in r}
            if t:
                out[name] = t
        return out

    def enum_rules_json(self) -> dict:
        """State metrics -> {code: ok|warning|critical|offline, "*": ...} for colouring timelines."""
        out = {}
        for name, r in self._metric_rules.items():
            if "enum" in r:
                out[name] = {str(k): str(v).lower() for k, v in r["enum"].items()}
            elif "zero" in r:
                out[name] = {"0": str(r["zero"]).lower(), "*": "ok"}
        return out

    # ------------------------------------------------------------------
    def evaluate(self, metrics: list[Metric], ts: float, mem: RuleMemory,
                 overrides: dict | None = None) -> Evaluation:
        worst = Status.OK
        reasons: list[tuple[Status, str]] = []
        for m in metrics:
            rule = self.metric_rule(m.name, overrides)
            if not rule:
                continue
            level, why = self._level(m, rule, mem.metrics.setdefault(m.key, _MetricMemory()), ts)
            if level == Status.OFFLINE:
                return Evaluation(Status.OFFLINE, [why], offline=True)
            if level > Status.OK:
                reasons.append((level, why))
                worst = max(worst, level)
        reasons.sort(key=lambda x: -x[0])
        return Evaluation(worst, [r for _, r in reasons])

    def _level(self, m: Metric, rule: dict, mm: _MetricMemory, ts: float) -> tuple[Status, str]:
        spec = metric_spec(m.name)
        label = spec.label
        if "port" in m.labels:                     # "Port 2101: Closed"
            label = f"{label} {m.labels['port']}"
        v = m.value
        # state metrics
        if "zero" in rule and v == 0:
            lvl = LEVEL[str(rule["zero"]).lower()]
            return self._sustain(lvl, mm, ts, rule, f"{label}: {fmt_value(m.name, v)}")
        if "enum" in rule:
            table = {str(k): v2 for k, v2 in rule["enum"].items()}
            code = str(int(round(v)))
            lvl = LEVEL[str(table.get(code, table.get("*", "ok"))).lower()]
            return self._sustain(lvl, mm, ts, rule, f"{label}: {fmt_value(m.name, v)}")
        # thresholds with hysteresis
        lvl, why = Status.OK, ""
        for key, status, high in (("crit", Status.CRITICAL, True), ("low_crit", Status.CRITICAL, False),
                                  ("warn", Status.WARNING, True), ("low_warn", Status.WARNING, False)):
            if key not in rule:
                continue
            thr = float(rule[key])
            margin = abs(thr) * self.hyst if mm.level >= status else 0.0
            hit = v >= thr - margin if high else v <= thr + margin
            if hit:
                op = "≥" if high else "≤"
                lvl, why = status, f"{label} {fmt_value(m.name, v)} {op} {fmt_value(m.name, thr)}"
                break
        return self._sustain(lvl, mm, ts, rule, why)

    @staticmethod
    def _sustain(lvl: Status, mm: _MetricMemory, ts: float, rule: dict, why: str) -> tuple[Status, str]:
        hold = parse_duration(rule.get("for"))
        if lvl == Status.OK:
            mm.level, mm.breach_since = Status.OK, None
            return Status.OK, ""
        if mm.breach_since is None:
            mm.breach_since = ts
        if ts - mm.breach_since < hold:
            return mm.level, why if mm.level > Status.OK else ""
        mm.level = lvl
        return lvl, why
