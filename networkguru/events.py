"""SQLite: status changes (the event list), collector runs, availability.

Every node (component, group, tab) gets a row each time its status changes. Status
timelines and availability % are computed from those rows. Time when the collector was
not running counts as Unknown - never as OK.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .model import Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started REAL NOT NULL, last_beat REAL NOT NULL, version TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS status_changes(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, node_id TEXT NOT NULL, node_kind TEXT NOT NULL,
  old INTEGER, new INTEGER NOT NULL, reason TEXT, suppressed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sc_node_ts ON status_changes(node_id, ts);
CREATE INDEX IF NOT EXISTS ix_sc_ts ON status_changes(ts);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS info_changes(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, comp_id TEXT NOT NULL, key TEXT NOT NULL, old TEXT, new TEXT);
CREATE INDEX IF NOT EXISTS ix_ic_ts ON info_changes(ts);
CREATE INDEX IF NOT EXISTS ix_ic_comp_ts ON info_changes(comp_id, ts);
CREATE TABLE IF NOT EXISTS system_events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL, actor TEXT);
CREATE INDEX IF NOT EXISTS ix_se_ts ON system_events(ts);
"""
# Schema changes must stay ADDITIVE (new tables, nullable columns): after a failed update the
# server rolls back to the previous version, which must still open this database.

INFO_LABEL = {"address": "Address", "firmware": "Firmware", "software": "Software"}

RUN_SLACK_S = 90.0          # a run covers until last_beat + this


class EventStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.run_id: int | None = None

    def close(self) -> None:
        self.db.close()

    # -- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                            (key, value, value))

    # -- runs -------------------------------------------------------------
    def start_run(self, ts: float, version: str, mode: str) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO runs(started,last_beat,version,mode) VALUES(?,?,?,?)",
                                  (ts, ts, version, mode))
            self.run_id = cur.lastrowid
        return self.run_id

    def beat(self, ts: float) -> None:
        if self.run_id is not None:
            with self.lock:
                self.db.execute("UPDATE runs SET last_beat=? WHERE id=?", (ts, self.run_id))

    def last_beat(self) -> float | None:
        row = self.db.execute("SELECT MAX(last_beat) FROM runs").fetchone()
        return row[0] if row and row[0] is not None else None

    def gaps(self, start: float, end: float, now: float) -> list[tuple[float, float]]:
        """Periods in [start, end] when no collector run was covering."""
        rows = self.db.execute(
            "SELECT started, last_beat, id FROM runs WHERE last_beat + ? >= ? AND started <= ? ORDER BY started",
            (RUN_SLACK_S, start, end)).fetchall()
        covered: list[list[float]] = []
        for started, last, rid in rows:
            hi = now if rid == self.run_id else last + RUN_SLACK_S
            if covered and started <= covered[-1][1]:
                covered[-1][1] = max(covered[-1][1], hi)
            else:
                covered.append([started, hi])
        gaps, cur = [], start
        for lo, hi in covered:
            if lo > cur:
                gaps.append((cur, min(lo, end)))
            cur = max(cur, hi)
            if cur >= end:
                break
        if cur < end:
            gaps.append((cur, end))
        return [(a, b) for a, b in gaps if b > a]

    # -- changes ----------------------------------------------------------
    def add_changes(self, rows: list[tuple]) -> None:
        """rows: (ts, node_id, node_kind, old, new, reason, suppressed)"""
        if not rows:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT INTO status_changes(ts,node_id,node_kind,old,new,reason,suppressed) VALUES(?,?,?,?,?,?,?)",
                rows)
            self.db.execute("COMMIT")

    def add_info_changes(self, rows: list[tuple]) -> None:
        """rows: (ts, comp_id, key, old, new) - an address or firmware that changed."""
        if not rows:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO info_changes(ts,comp_id,key,old,new) VALUES(?,?,?,?,?)", rows)
            self.db.execute("COMMIT")

    def info_events(self, start: float, end: float, node_ids: list[str] | None = None,
                    limit: int = 500) -> list[dict]:
        sql = "SELECT ts,comp_id,key,old,new FROM info_changes WHERE ts>=? AND ts<=?"
        args: list = [start, end]
        if node_ids:
            sql += f" AND comp_id IN ({','.join('?' * len(node_ids))})"
            args += node_ids
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [{"ts": r[0], "node": r[1], "kind": "info", "key": r[2], "old_text": r[3], "new_text": r[4],
                 "reason": f"{INFO_LABEL.get(r[2], r[2])} {r[3]} → {r[4]}", "suppressed": False}
                for r in self.db.execute(sql, args).fetchall()]

    # -- system events (updates, morning reports, admin sign-ins) ------------
    def add_system_event(self, ts: float, kind: str, text: str, level: int = 0, actor: str | None = None) -> None:
        """kind: update | report | admin. level: a Status value (0 OK, 3 warning, 4 critical)."""
        with self.lock:
            self.db.execute("INSERT INTO system_events(ts,kind,level,text,actor) VALUES(?,?,?,?,?)",
                            (ts, kind, int(level), text, actor))

    def system_events(self, start: float, end: float, limit: int = 500, kinds: tuple[str, ...] | None = None
                      ) -> list[dict]:
        sql = "SELECT ts,kind,level,text,actor FROM system_events WHERE ts>=? AND ts<=?"
        args: list = [start, end]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [{"ts": r[0], "node": None, "kind": "system", "sys_kind": r[1], "level": r[2], "reason": r[3],
                 "actor": r[4], "suppressed": False} for r in self.db.execute(sql, args).fetchall()]

    def backup_to(self, dest: Path) -> bool:
        """Consistent copy of the database (before an update). False for an in-memory store."""
        if self.path == ":memory:":
            return False
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(str(dest))
        try:
            with self.lock:
                self.db.backup(out)
        finally:
            out.close()
        return True

    def prune(self, before_ts: float) -> None:
        with self.lock:
            self.db.execute("DELETE FROM status_changes WHERE ts < ?", (before_ts,))
            self.db.execute("DELETE FROM info_changes WHERE ts < ?", (before_ts,))
            self.db.execute("DELETE FROM system_events WHERE ts < ?", (before_ts,))
            self.db.execute("DELETE FROM runs WHERE last_beat < ?", (before_ts,))

    def _status_at(self, node_ids: list[str], ts: float) -> dict[str, int]:
        out = {}
        for nid in node_ids:
            row = self.db.execute(
                "SELECT new FROM status_changes WHERE node_id=? AND ts<=? ORDER BY ts DESC, id DESC LIMIT 1",
                (nid, ts)).fetchone()
            out[nid] = row[0] if row else int(Status.UNKNOWN)
        return out

    def intervals(self, node_ids: list[str], start: float, end: float, now: float) -> dict[str, list[list]]:
        """{node: [[t0, t1, status], ...]} covering [start, min(end, now)]."""
        end = min(end, now)
        if end <= start or not node_ids:
            return {n: [] for n in node_ids}
        initial = self._status_at(node_ids, start)
        q = ",".join("?" * len(node_ids))
        rows = self.db.execute(
            f"SELECT node_id, ts, new FROM status_changes WHERE node_id IN ({q}) AND ts>? AND ts<=? "
            f"ORDER BY ts, id", (*node_ids, start, end)).fetchall()
        changes: dict[str, list[tuple[float, int]]] = {n: [] for n in node_ids}
        for nid, ts, new in rows:
            changes[nid].append((ts, new))
        gaps = self.gaps(start, end, now)
        out = {}
        for nid in node_ids:
            segs: list[list] = []
            cur_t, cur_s = start, initial[nid]
            for ts, new in changes[nid]:
                if new == cur_s:
                    continue
                if ts > cur_t:
                    segs.append([cur_t, ts, cur_s])
                cur_t, cur_s = ts, new
            segs.append([cur_t, end, cur_s])
            out[nid] = _overlay_unknown(segs, gaps)
        return out

    def availability(self, node_ids: list[str], start: float, end: float, now: float) -> dict[str, dict]:
        """Up % = time OK or Warning / time with a known, in-service status.
        Unknown never counts; Offline (a parked vehicle, an unused desk) is not in service."""
        out = {}
        for nid, segs in self.intervals(node_ids, start, end, now).items():
            by = {s.name.lower(): 0.0 for s in Status}
            for t0, t1, st in segs:
                by[Status(st).name.lower()] += t1 - t0
            up = by["ok"] + by["warning"]
            known = up + by["critical"]
            out[nid] = {"pct": (100.0 * up / known) if known > 0 else None,
                        "seconds": {k: round(v) for k, v in by.items()}}
        return out

    def events(self, start: float, end: float, node_ids: list[str] | None = None, min_status: int = 0,
               include_suppressed: bool = False, kinds: tuple[str, ...] | None = None,
               limit: int = 500) -> list[dict]:
        sql = "SELECT ts,node_id,node_kind,old,new,reason,suppressed FROM status_changes WHERE ts>=? AND ts<=?"
        args: list = [start, end]
        if node_ids:
            sql += f" AND node_id IN ({','.join('?' * len(node_ids))})"
            args += node_ids
        if min_status:
            sql += " AND (new>=? OR old>=?)"
            args += [min_status, min_status]
        if not include_suppressed:
            sql += " AND suppressed=0"
        if kinds:
            sql += f" AND node_kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [{"ts": r[0], "node": r[1], "kind": r[2], "old": r[3], "new": r[4], "reason": r[5] or "",
                 "suppressed": bool(r[6])} for r in self.db.execute(sql, args).fetchall()]


def _overlay_unknown(segs: list[list], gaps: list[tuple[float, float]]) -> list[list]:
    if not gaps:
        return segs
    out: list[list] = []
    for t0, t1, st in segs:
        pieces = [[t0, t1, st]]
        for g0, g1 in gaps:
            nxt = []
            for a, b, s in pieces:
                if g1 <= a or g0 >= b or s == Status.UNKNOWN:
                    nxt.append([a, b, s])
                    continue
                if a < g0:
                    nxt.append([a, g0, s])
                nxt.append([max(a, g0), min(b, g1), int(Status.UNKNOWN)])
                if g1 < b:
                    nxt.append([g1, b, s])
            pieces = nxt
        out.extend(pieces)
    merged: list[list] = []
    for seg in out:
        if merged and merged[-1][2] == seg[2] and abs(merged[-1][1] - seg[0]) < 1e-6:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)
    return merged
