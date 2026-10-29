"""Shared data shapes: statuses, metrics and the result every driver returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Status(IntEnum):
    """Health of a component, group or tab. Higher rank = worse (roll-up takes the max)."""
    OK = 0
    UNKNOWN = 1     # no data yet, stale data, or hidden behind a failed upstream device
    OFFLINE = 2     # not responding (a parked vehicle is Offline, not an alarm)
    WARNING = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.capitalize()


STATUS_NAMES = {s.value: s.name.lower() for s in Status}


class Info(str):
    """A dashboard note that is information, not a to-do (0.9.4): shown under the footer's "What's checked",
    not as a banner. Still a plain string everywhere else."""


@dataclass
class Metric:
    """One numeric reading. `labels` only for a bounded sub-entity (interface, peer, MPPT)."""
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        if not self.labels:
            return self.name
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.labels.items()))
        return f"{self.name}{{{inner}}}"


@dataclass
class CompResult:
    """What one poll of one component produced."""
    comp_id: str
    ts: float                                   # sample time (epoch seconds)
    reachable: bool                             # transport-level success
    metrics: list[Metric] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)   # text facts (firmware, error codes ...)
    error: str | None = None                    # short, secret-masked
    duration_ms: float = 0.0
