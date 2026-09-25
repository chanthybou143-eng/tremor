"""Persistent reading storage for the web server.

``ReadingStore`` is the small interface the web app and ``retention.py`` talk
to; ``SqliteReadingStore`` is the only implementation today (stdlib sqlite3,
default rollback journal -- PythonAnywhere's disk is a network filesystem, so
no WAL). Swapping the backend (e.g. MySQL) means implementing this ABC; nothing
outside this module knows it is SQLite.

Every reading is stored under the device's own GPS UTC time
(``gps_utc_us``, exact integer microseconds since the Unix epoch). Receipt time
is kept only as ``received_at`` metadata. A reading with no usable GPS time is
stored with ``gps_utc_us`` NULL and a flag -- never given receipt time.

Deduplication (INSERT OR IGNORE against partial unique indexes):
  * v2 payloads: (unit_id, boot_id, seq)
  * legacy v1 payloads: (unit_id, gps_utc_us)
  * unlocked legacy readings cannot be deduplicated (no key); they are stored
    and flagged, deliberately not matched by content.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .ingest import (
    FLAG_TIME_IMPLAUSIBLE,
    FLAG_UNLOCKED,
    US,
    ParsedBatch,
    resolve_time,
)

SCHEMA_VERSION = 1
DAY_US = 86400 * US
MINUTE_US = 60 * US


class StoreError(Exception):
    """The backend is unavailable or failed (locked, disk full, I/O error). The
    HTTP layer maps this to 503 so the device keeps its readings and retries --
    safe, because ingest is idempotent."""


@dataclass(frozen=True)
class IngestResult:
    accepted: int
    inserted: int
    duplicates: int
    unlocked: int
    implausible: int


@dataclass(frozen=True)
class Row:
    id: int
    unit_id: str
    boot_id: Optional[str]
    seq: Optional[int]
    gps_utc_us: Optional[int]
    gps_raw: Optional[float]
    time_src: int
    freq_hz: float
    amplitude_v: Optional[float]
    gps_locked: int
    flags: int
    received_at: float


@dataclass(frozen=True)
class UnitState:
    unit_id: str
    first_seen: float
    last_received_at: float
    last_gps_us: Optional[int]
    last_gps_locked: bool
    last_boot_id: Optional[str]
    readings_total: int
    unlocked_total: int
    duplicates_total: int
    implausible_total: int


@dataclass(frozen=True)
class AggRow:
    unit_id: str
    minute: int                      # whole minutes since the Unix epoch (UTC)
    n: int                           # locked readings in the minute
    n_unlocked: int                  # unlocked readings received in the minute
    freq_mean: Optional[float]
    freq_min: Optional[float]
    freq_max: Optional[float]
    freq_std: Optional[float]
    rocof_max_abs: Optional[float]
    amp_mean: Optional[float]


@dataclass
class DayState:
    unit_id: str
    day: int                         # UTC days since 1970-01-01
    export_path: Optional[str] = None
    export_cursor: Optional[str] = None      # JSON, see read_day_page
    export_rows: int = 0
    export_sha256: Optional[str] = None
    export_done: bool = False
    agg_next_hour: int = 0
    agg_done: bool = False
    verified_at: Optional[float] = None
    pruned_rows: int = 0
    pruned_done: bool = False
    attention: Optional[str] = None          # set when verification fails -> never pruned


@dataclass(frozen=True)
class HistoryPage:
    rows: List[Row]
    unlocked: List[Row]
    truncated: bool
    next_from_us: Optional[int]
    next_after_id: Optional[int]


class ReadingStore(ABC):
    # --- ingest / live view -------------------------------------------------
    @abstractmethod
    def ingest(self, batch: ParsedBatch, received_at: float) -> IngestResult: ...

    @abstractmethod
    def unit_states(self) -> List[UnitState]: ...

    @abstractmethod
    def window(self, unit_id: str, span_s: float) -> List[Row]:
        """Locked, plausible readings within ``span_s`` of the unit's newest
        GPS time, ordered by GPS time."""

    @abstractmethod
    def history(self, unit_id: str, from_us: int, to_us: int, limit: int,
                after_id: int = 0, include_unlocked: bool = False,
                unlocked_to_us: Optional[int] = None) -> HistoryPage:
        """GPS-timed rows in [from, to]. Unlocked rows have no GPS time, so when
        requested they are listed separately, bounded by RECEIPT time in
        [from, unlocked_to_us or to] -- a filter, never a time assignment."""

    @abstractmethod
    def aggregates(self, unit_id: str, from_minute: int, to_minute: int, limit: int) -> List[AggRow]: ...

    @abstractmethod
    def health(self) -> dict: ...

    # --- retention primitives (see retention.py) ----------------------------
    @abstractmethod
    def oldest_day(self, unit_id: str) -> Optional[int]: ...

    @abstractmethod
    def day_states(self, unit_id: str) -> Dict[int, DayState]: ...

    @abstractmethod
    def save_day_state(self, st: DayState) -> None: ...

    @abstractmethod
    def read_day_page(self, unit_id: str, day: int, cursor: Optional[dict], limit: int
                      ) -> Tuple[List[Row], Optional[dict]]: ...

    @abstractmethod
    def read_locked_range(self, unit_id: str, start_us: int, end_us: int) -> List[Row]: ...

    @abstractmethod
    def unlocked_minute_counts(self, unit_id: str, start_s: float, end_s: float) -> Dict[int, int]: ...

    @abstractmethod
    def day_row_counts(self, unit_id: str, day: int) -> Tuple[int, int]:
        """(locked, unlocked) raw rows currently stored for a UTC day."""

    @abstractmethod
    def save_aggregates(self, rows: List[AggRow]) -> None: ...

    @abstractmethod
    def aggregate_totals(self, unit_id: str, day: int) -> Tuple[int, int]: ...

    @abstractmethod
    def add_event(self, unit_id: str, start_us: int, end_us: int, reason: str,
                  peak_abs_rocof: Optional[float], freq_min: Optional[float],
                  freq_max: Optional[float]) -> None: ...

    @abstractmethod
    def events(self, unit_id: Optional[str] = None) -> List[dict]: ...

    @abstractmethod
    def delete_day_rows(self, unit_id: str, day: int, limit: int) -> int:
        """Delete up to ``limit`` raw rows of the day that are not inside a
        kept event interval; returns how many were deleted."""

    @abstractmethod
    def prune_ingests(self, older_than_received_at: float) -> int: ...

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY,
  unit_id TEXT NOT NULL,
  boot_id TEXT,
  seq INTEGER,
  gps_utc_us INTEGER,
  gps_raw REAL,
  time_src INTEGER NOT NULL,
  freq_hz REAL NOT NULL,
  amplitude_v REAL,
  gps_locked INTEGER NOT NULL,
  flags INTEGER NOT NULL DEFAULT 0,
  received_at REAL NOT NULL,
  ingest_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_boot_seq ON readings(unit_id, boot_id, seq) WHERE boot_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_legacy_gps ON readings(unit_id, gps_utc_us)
  WHERE boot_id IS NULL AND gps_utc_us IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_unit_gps ON readings(unit_id, gps_utc_us);
CREATE INDEX IF NOT EXISTS ix_unlocked ON readings(unit_id, received_at) WHERE gps_utc_us IS NULL;

CREATE TABLE IF NOT EXISTS units (
  unit_id TEXT PRIMARY KEY,
  first_seen REAL NOT NULL,
  last_received_at REAL NOT NULL,
  last_gps_us INTEGER,
  last_gps_locked INTEGER NOT NULL DEFAULT 0,
  last_boot_id TEXT,
  readings_total INTEGER NOT NULL DEFAULT 0,
  unlocked_total INTEGER NOT NULL DEFAULT 0,
  duplicates_total INTEGER NOT NULL DEFAULT 0,
  implausible_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingests (
  id INTEGER PRIMARY KEY,
  unit_id TEXT NOT NULL,
  received_at REAL NOT NULL,
  boot_id TEXT,
  mode INTEGER NOT NULL,
  n_sent INTEGER NOT NULL,
  n_inserted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ingests_received ON ingests(received_at);

CREATE TABLE IF NOT EXISTS retention_days (
  unit_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  export_path TEXT,
  export_cursor TEXT,
  export_rows INTEGER NOT NULL DEFAULT 0,
  export_sha256 TEXT,
  export_done INTEGER NOT NULL DEFAULT 0,
  agg_next_hour INTEGER NOT NULL DEFAULT 0,
  agg_done INTEGER NOT NULL DEFAULT 0,
  verified_at REAL,
  pruned_rows INTEGER NOT NULL DEFAULT 0,
  pruned_done INTEGER NOT NULL DEFAULT 0,
  attention TEXT,
  PRIMARY KEY (unit_id, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS readings_1min (
  unit_id TEXT NOT NULL,
  minute INTEGER NOT NULL,
  n INTEGER NOT NULL,
  n_unlocked INTEGER NOT NULL,
  freq_mean REAL, freq_min REAL, freq_max REAL, freq_std REAL,
  rocof_max_abs REAL, amp_mean REAL,
  PRIMARY KEY (unit_id, minute)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  unit_id TEXT NOT NULL,
  start_us INTEGER NOT NULL,
  end_us INTEGER NOT NULL,
  reason TEXT NOT NULL,
  peak_abs_rocof REAL, freq_min REAL, freq_max REAL
);
CREATE INDEX IF NOT EXISTS ix_events_unit ON events(unit_id, start_us, end_us);
"""

_ROW_COLS = ("id, unit_id, boot_id, seq, gps_utc_us, gps_raw, time_src, freq_hz, amplitude_v, "
             "gps_locked, flags, received_at")


def _row(t) -> Row:
    return Row(*t)


class SqliteReadingStore(ReadingStore):
    def __init__(self, path: str, synchronous: str = "FULL", busy_timeout_s: float = 30.0,
                 read_timeout_s: float = 5.0):
        if synchronous.upper() not in ("FULL", "NORMAL", "EXTRA", "OFF"):
            raise ValueError("synchronous must be FULL, NORMAL, EXTRA or OFF")
        self.path = path
        self._sync = synchronous.upper()
        self._timeout = busy_timeout_s
        self._read_timeout = read_timeout_s
        self._count_cache: Tuple[float, int] = (0.0, 0)
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        try:
            with self._conn() as db:
                cur_ver = db.execute("PRAGMA user_version").fetchone()[0]
                if cur_ver > SCHEMA_VERSION:
                    raise StoreError(f"database schema v{cur_ver} is newer than this code (v{SCHEMA_VERSION})")
                db.executescript(_SCHEMA)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open {path}: {exc}") from exc

    # -- connection handling: one short-lived connection per call ------------
    @contextmanager
    def _conn(self, timeout: Optional[float] = None) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=self._timeout if timeout is None else timeout,
                             isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode = DELETE")     # explicitly NOT WAL: NFS-backed disk
            db.execute(f"PRAGMA synchronous = {self._sync}")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._conn() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                else:
                    db.execute("COMMIT")
                    self._count_cache = (0.0, 0)       # row count in health() is stale after any write
        except sqlite3.Error as exc:
            raise StoreError(str(exc)) from exc

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._conn(self._read_timeout) as db:
                yield db
        except sqlite3.Error as exc:
            raise StoreError(str(exc)) from exc

    def close(self) -> None:
        pass  # connections are per call; nothing is held open

    # -- ingest --------------------------------------------------------------
    def ingest(self, batch: ParsedBatch, received_at: float) -> IngestResult:
        resolved = [resolve_time(r, received_at) for r in batch.readings]
        mode = batch.mode
        with self._write() as db:
            ingest_id = db.execute(
                "INSERT INTO ingests(unit_id, received_at, boot_id, mode, n_sent) VALUES (?,?,?,?,?)",
                (batch.unit_id, received_at, batch.boot_id, mode, len(batch.readings))).lastrowid
            inserted = unlocked = implausible = 0
            last_us: Optional[int] = None
            last_locked = None
            for r, t in zip(batch.readings, resolved):
                cur = db.execute(
                    "INSERT OR IGNORE INTO readings(unit_id, boot_id, seq, gps_utc_us, gps_raw, time_src, "
                    "freq_hz, amplitude_v, gps_locked, flags, received_at, ingest_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch.unit_id, batch.boot_id, r.seq, t.gps_utc_us, t.gps_raw, t.time_src,
                     r.freq_hz, r.amplitude_v, t.gps_locked, t.flags, received_at, ingest_id))
                if cur.rowcount == 1:
                    inserted += 1
                    if t.flags & FLAG_UNLOCKED:
                        unlocked += 1
                    if t.flags & FLAG_TIME_IMPLAUSIBLE:
                        implausible += 1
                    if t.gps_utc_us is not None and (last_us is None or t.gps_utc_us > last_us):
                        last_us = t.gps_utc_us
                    last_locked = 1 if t.gps_locked and not (t.flags & FLAG_TIME_IMPLAUSIBLE) else 0
            n = len(batch.readings)
            dups = n - inserted
            db.execute("UPDATE ingests SET n_inserted=? WHERE id=?", (inserted, ingest_id))
            db.execute(
                "INSERT INTO units(unit_id, first_seen, last_received_at, last_gps_us, last_gps_locked, "
                "last_boot_id, readings_total, unlocked_total, duplicates_total, implausible_total) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(unit_id) DO UPDATE SET "
                "last_received_at=excluded.last_received_at, "
                "last_gps_us=CASE WHEN excluded.last_gps_us IS NOT NULL AND "
                "  (units.last_gps_us IS NULL OR excluded.last_gps_us > units.last_gps_us) "
                "  THEN excluded.last_gps_us ELSE units.last_gps_us END, "
                "last_gps_locked=CASE WHEN ? IS NULL THEN units.last_gps_locked ELSE ? END, "
                "last_boot_id=COALESCE(excluded.last_boot_id, units.last_boot_id), "
                "readings_total=units.readings_total+excluded.readings_total, "
                "unlocked_total=units.unlocked_total+excluded.unlocked_total, "
                "duplicates_total=units.duplicates_total+excluded.duplicates_total, "
                "implausible_total=units.implausible_total+excluded.implausible_total",
                (batch.unit_id, received_at, received_at, last_us, 1 if last_locked else 0,
                 batch.boot_id, inserted, unlocked, dups, implausible, last_locked, last_locked))
        return IngestResult(n, inserted, dups, unlocked, implausible)

    # -- live view -----------------------------------------------------------
    def unit_states(self) -> List[UnitState]:
        with self._read() as db:
            rows = db.execute(
                "SELECT unit_id, first_seen, last_received_at, last_gps_us, last_gps_locked, last_boot_id, "
                "readings_total, unlocked_total, duplicates_total, implausible_total FROM units "
                "ORDER BY first_seen, unit_id").fetchall()
        return [UnitState(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8], r[9]) for r in rows]

    def window(self, unit_id: str, span_s: float) -> List[Row]:
        with self._read() as db:
            top = db.execute(
                "SELECT gps_utc_us FROM readings WHERE unit_id=? AND gps_utc_us IS NOT NULL "
                "ORDER BY gps_utc_us DESC LIMIT 1", (unit_id,)).fetchone()
            if top is None:
                return []
            rows = db.execute(
                f"SELECT {_ROW_COLS} FROM readings WHERE unit_id=? AND gps_utc_us > ? "
                "ORDER BY gps_utc_us, id", (unit_id, top[0] - int(span_s * US))).fetchall()
        return [_row(r) for r in rows]

    def history(self, unit_id, from_us, to_us, limit, after_id=0, include_unlocked=False,
                unlocked_to_us=None) -> HistoryPage:
        with self._read() as db:
            rows = db.execute(
                f"SELECT {_ROW_COLS} FROM readings WHERE unit_id=? AND gps_utc_us IS NOT NULL "
                "AND gps_utc_us <= ? AND (gps_utc_us > ? OR (gps_utc_us = ? AND id > ?)) "
                "ORDER BY gps_utc_us, id LIMIT ?",
                (unit_id, to_us, from_us, from_us, after_id, limit + 1)).fetchall()
            unlocked: List[Row] = []
            if include_unlocked:
                unlocked = [_row(r) for r in db.execute(
                    f"SELECT {_ROW_COLS} FROM readings WHERE unit_id=? AND gps_utc_us IS NULL "
                    "AND received_at >= ? AND received_at <= ? ORDER BY received_at, id LIMIT ?",
                    (unit_id, from_us / US, (to_us if unlocked_to_us is None else unlocked_to_us) / US,
                     limit)).fetchall()]
        truncated = len(rows) > limit
        page_rows = [_row(r) for r in rows[:limit]]
        last = page_rows[-1] if (truncated and page_rows) else None
        return HistoryPage(page_rows, unlocked, truncated,
                           last.gps_utc_us if last else None, last.id if last else None)

    def aggregates(self, unit_id, from_minute, to_minute, limit) -> List[AggRow]:
        with self._read() as db:
            rows = db.execute(
                "SELECT unit_id, minute, n, n_unlocked, freq_mean, freq_min, freq_max, freq_std, "
                "rocof_max_abs, amp_mean FROM readings_1min WHERE unit_id=? AND minute>=? AND minute<=? "
                "ORDER BY minute LIMIT ?", (unit_id, from_minute, to_minute, limit)).fetchall()
        return [AggRow(*r) for r in rows]

    def health(self) -> dict:
        def size(p):
            try:
                return os.path.getsize(p)
            except OSError:
                return 0
        now = time.time()
        with self._read() as db:
            if now - self._count_cache[0] > 60:
                self._count_cache = (now, db.execute("SELECT count(*) FROM readings").fetchone()[0])
            attention = db.execute(
                "SELECT unit_id, day, attention FROM retention_days WHERE attention IS NOT NULL").fetchall()
            n_events = db.execute("SELECT count(*) FROM events").fetchone()[0]
            n_agg = db.execute("SELECT count(*) FROM readings_1min").fetchone()[0]
            unpruned_exported = db.execute(
                "SELECT count(*) FROM retention_days WHERE export_done=1 AND agg_done=1 AND pruned_done=0").fetchone()[0]
        return {
            "backend": "sqlite", "schema_version": SCHEMA_VERSION, "synchronous": self._sync,
            "db_bytes": size(self.path), "journal_bytes": size(self.path + "-journal"),
            "raw_rows": self._count_cache[1], "aggregate_rows": n_agg, "event_intervals": n_events,
            "days_exported_awaiting_prune": unpruned_exported,
            "days_needing_attention": [{"unit_id": a, "day": b, "reason": c} for a, b, c in attention],
        }

    # -- retention primitives ------------------------------------------------
    def oldest_day(self, unit_id: str) -> Optional[int]:
        with self._read() as db:
            a = db.execute("SELECT gps_utc_us FROM readings WHERE unit_id=? AND gps_utc_us IS NOT NULL "
                           "ORDER BY gps_utc_us LIMIT 1", (unit_id,)).fetchone()
            b = db.execute("SELECT received_at FROM readings WHERE unit_id=? AND gps_utc_us IS NULL "
                           "ORDER BY received_at LIMIT 1", (unit_id,)).fetchone()
        days = []
        if a:
            days.append(a[0] // DAY_US)
        if b:
            days.append(int(b[0] // 86400))
        return min(days) if days else None

    _DS_COLS = ("unit_id, day, export_path, export_cursor, export_rows, export_sha256, export_done, "
                "agg_next_hour, agg_done, verified_at, pruned_rows, pruned_done, attention")

    @staticmethod
    def _ds(r) -> DayState:
        return DayState(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], bool(r[8]), r[9], r[10],
                        bool(r[11]), r[12])

    def day_states(self, unit_id: str) -> Dict[int, DayState]:
        with self._read() as db:
            rows = db.execute(f"SELECT {self._DS_COLS} FROM retention_days WHERE unit_id=?", (unit_id,)).fetchall()
        return {r[1]: self._ds(r) for r in rows}

    def save_day_state(self, st: DayState) -> None:
        with self._write() as db:
            db.execute(
                f"INSERT OR REPLACE INTO retention_days({self._DS_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (st.unit_id, st.day, st.export_path, st.export_cursor, st.export_rows, st.export_sha256,
                 int(st.export_done), st.agg_next_hour, int(st.agg_done), st.verified_at, st.pruned_rows,
                 int(st.pruned_done), st.attention))

    def read_day_page(self, unit_id, day, cursor, limit):
        lo, hi = day * DAY_US, (day + 1) * DAY_US
        with self._read() as db:
            if cursor is None or cursor.get("p") == 0:
                a, b = (lo - 1, 0) if cursor is None else (cursor["a"], cursor["b"])
                rows = [_row(r) for r in db.execute(
                    f"SELECT {_ROW_COLS} FROM readings WHERE unit_id=? AND gps_utc_us >= ? AND gps_utc_us < ? "
                    "AND (gps_utc_us > ? OR (gps_utc_us = ? AND id > ?)) ORDER BY gps_utc_us, id LIMIT ?",
                    (unit_id, lo, hi, a, a, b, limit)).fetchall()]
                if len(rows) == limit:
                    return rows, {"p": 0, "a": rows[-1].gps_utc_us, "b": rows[-1].id}
                nxt = {"p": 1, "a": day * 86400.0 - 1.0, "b": 0}
                if not rows:
                    return self._unlocked_page(db, unit_id, day, nxt, limit)
                return rows, nxt
            return self._unlocked_page(db, unit_id, day, cursor, limit)

    def _unlocked_page(self, db, unit_id, day, cursor, limit):
        rows = [_row(r) for r in db.execute(
            f"SELECT {_ROW_COLS} FROM readings WHERE unit_id=? AND gps_utc_us IS NULL AND received_at >= ? "
            "AND received_at < ? AND (received_at > ? OR (received_at = ? AND id > ?)) "
            "ORDER BY received_at, id LIMIT ?",
            (unit_id, day * 86400.0, (day + 1) * 86400.0, cursor["a"], cursor["a"], cursor["b"], limit)).fetchall()]
        if len(rows) == limit:
            return rows, {"p": 1, "a": rows[-1].received_at, "b": rows[-1].id}
        return rows, None

    def read_locked_range(self, unit_id, start_us, end_us) -> List[Row]:
        with self._read() as db:
            rows = db.execute(
                f"SELECT {_ROW_COLS} FROM readings WHERE unit_id=? AND gps_utc_us >= ? AND gps_utc_us < ? "
                "ORDER BY gps_utc_us, id", (unit_id, start_us, end_us)).fetchall()
        return [_row(r) for r in rows]

    def unlocked_minute_counts(self, unit_id, start_s, end_s) -> Dict[int, int]:
        with self._read() as db:
            rows = db.execute(
                "SELECT CAST(received_at / 60 AS INTEGER) m, count(*) FROM readings WHERE unit_id=? "
                "AND gps_utc_us IS NULL AND received_at >= ? AND received_at < ? GROUP BY m",
                (unit_id, start_s, end_s)).fetchall()
        return {m: c for m, c in rows}

    def day_row_counts(self, unit_id, day) -> Tuple[int, int]:
        with self._read() as db:
            a = db.execute("SELECT count(*) FROM readings WHERE unit_id=? AND gps_utc_us >= ? AND gps_utc_us < ?",
                           (unit_id, day * DAY_US, (day + 1) * DAY_US)).fetchone()[0]
            b = db.execute("SELECT count(*) FROM readings WHERE unit_id=? AND gps_utc_us IS NULL "
                           "AND received_at >= ? AND received_at < ?",
                           (unit_id, day * 86400.0, (day + 1) * 86400.0)).fetchone()[0]
        return a, b

    def save_aggregates(self, rows: List[AggRow]) -> None:
        if not rows:
            return
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO readings_1min(unit_id, minute, n, n_unlocked, freq_mean, freq_min, "
                "freq_max, freq_std, rocof_max_abs, amp_mean) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r.unit_id, r.minute, r.n, r.n_unlocked, r.freq_mean, r.freq_min, r.freq_max, r.freq_std,
                  r.rocof_max_abs, r.amp_mean) for r in rows])

    def aggregate_totals(self, unit_id, day) -> Tuple[int, int]:
        with self._read() as db:
            r = db.execute("SELECT COALESCE(sum(n),0), COALESCE(sum(n_unlocked),0) FROM readings_1min "
                           "WHERE unit_id=? AND minute >= ? AND minute < ?",
                           (unit_id, day * 1440, (day + 1) * 1440)).fetchone()
        return r[0], r[1]

    def add_event(self, unit_id, start_us, end_us, reason, peak_abs_rocof, freq_min, freq_max) -> None:
        with self._write() as db:
            ov = db.execute("SELECT id, start_us, end_us, reason, peak_abs_rocof, freq_min, freq_max FROM events "
                            "WHERE unit_id=? AND start_us <= ? AND end_us >= ?", (unit_id, end_us, start_us)).fetchall()
            reasons = set(reason.split("+"))
            for (eid, s, e, rs, pk, fmn, fmx) in ov:
                start_us, end_us = min(start_us, s), max(end_us, e)
                reasons |= set(rs.split("+"))
                if pk is not None:
                    peak_abs_rocof = pk if peak_abs_rocof is None else max(peak_abs_rocof, pk)
                if fmn is not None:
                    freq_min = fmn if freq_min is None else min(freq_min, fmn)
                if fmx is not None:
                    freq_max = fmx if freq_max is None else max(freq_max, fmx)
                db.execute("DELETE FROM events WHERE id=?", (eid,))
            db.execute("INSERT INTO events(unit_id, start_us, end_us, reason, peak_abs_rocof, freq_min, freq_max) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (unit_id, start_us, end_us, "+".join(sorted(reasons)), peak_abs_rocof, freq_min, freq_max))

    def events(self, unit_id=None) -> List[dict]:
        q = "SELECT unit_id, start_us, end_us, reason, peak_abs_rocof, freq_min, freq_max FROM events"
        args: tuple = ()
        if unit_id is not None:
            q += " WHERE unit_id=?"
            args = (unit_id,)
        with self._read() as db:
            rows = db.execute(q + " ORDER BY unit_id, start_us", args).fetchall()
        return [dict(unit_id=r[0], start_us=r[1], end_us=r[2], reason=r[3], peak_abs_rocof=r[4],
                     freq_min=r[5], freq_max=r[6]) for r in rows]

    def delete_day_rows(self, unit_id, day, limit) -> int:
        lo, hi = day * DAY_US, (day + 1) * DAY_US
        deleted = 0
        with self._write() as db:
            cur = db.execute(
                "DELETE FROM readings WHERE id IN (SELECT r.id FROM readings r WHERE r.unit_id=? "
                "AND r.gps_utc_us >= ? AND r.gps_utc_us < ? AND NOT EXISTS (SELECT 1 FROM events e "
                "WHERE e.unit_id = r.unit_id AND e.start_us <= r.gps_utc_us AND e.end_us >= r.gps_utc_us) "
                "ORDER BY r.gps_utc_us LIMIT ?)", (unit_id, lo, hi, limit))
            deleted += cur.rowcount
            if deleted < limit:
                cur = db.execute(
                    "DELETE FROM readings WHERE id IN (SELECT r.id FROM readings r WHERE r.unit_id=? "
                    "AND r.gps_utc_us IS NULL AND r.received_at >= ? AND r.received_at < ? AND NOT EXISTS "
                    "(SELECT 1 FROM events e WHERE e.unit_id = r.unit_id AND e.start_us <= CAST(r.received_at*1000000 AS INTEGER) "
                    "AND e.end_us >= CAST(r.received_at*1000000 AS INTEGER)) ORDER BY r.received_at LIMIT ?)",
                    (unit_id, day * 86400.0, (day + 1) * 86400.0, limit - deleted))
                deleted += cur.rowcount
        return deleted

    def prune_ingests(self, older_than_received_at: float) -> int:
        with self._write() as db:
            return db.execute("DELETE FROM ingests WHERE received_at < ? AND id IN "
                              "(SELECT id FROM ingests WHERE received_at < ? LIMIT 5000)",
                              (older_than_received_at, older_than_received_at)).rowcount


def open_store(path: str, **kw) -> ReadingStore:
    """Factory used by the web app -- the one place that names the backend."""
    return SqliteReadingStore(path, **kw)
