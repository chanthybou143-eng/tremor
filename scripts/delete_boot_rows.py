#!/usr/bin/env python3
"""Guarded one-off: delete ALL raw readings of ONE boot_id of ONE unit from the SQLite store.

Written for boot_id 398474c3bef237a1 (unit-1, 289 rows), whose timestamps are ~1 s early (anchor-lockout
bug, fixed in 4361f64). Run it ON PYTHONANYWHERE in a Bash console. It is a dry run unless told otherwise:

    # 1. dry run (changes nothing, prints what it found)
    python scripts/delete_boot_rows.py --db ~/tremor_data/readings.db --unit unit-1 \\
        --boot-id 398474c3bef237a1 --expect-rows 289

    # 2. the real thing: same command plus the boot id typed a second time
    python scripts/delete_boot_rows.py --db ~/tremor_data/readings.db --unit unit-1 \\
        --boot-id 398474c3bef237a1 --expect-rows 289 --confirm-boot-id 398474c3bef237a1

It refuses (exit 2, nothing changed) unless ALL of these hold:
  * boot id is exactly 16 lowercase hex digits (no wildcards, no empty / NULL / legacy rows);
  * the number of matching rows equals --expect-rows exactly (re-checked INSIDE the delete transaction);
  * the database passes PRAGMA quick_check;
  * no retention day touching those rows has been exported, aggregated or pruned (a copy of the bad rows
    would already exist in an export or a 1-minute aggregate, and deleting the raw rows would not remove it);
  * no event interval overlaps the rows' time span.
Before deleting it writes the rows to <db dir>/quarantine/<unit>_<boot>_<utc>.jsonl (mode 600), checks the
line count, and only then deletes them in one BEGIN IMMEDIATE transaction (rolled back on any mismatch).
Only the `readings` table is touched. The cumulative counters in `units` (readings_total ...) are left as
they are: they count what was received, not what is stored.

Deleting is idempotent in effect: a second run finds 0 rows and refuses (expected 289, found 0).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DAY_US = 86_400 * 1_000_000
BOOT_RE = re.compile(r"^[0-9a-f]{16}$")
COLS = ("id, unit_id, boot_id, seq, gps_utc_us, gps_raw, time_src, freq_hz, amplitude_v, "
        "gps_locked, flags, received_at, ingest_id")


class Refused(Exception):
    """A guard failed. Nothing was changed."""


def _connect(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise Refused(f"database not found: {path}")
    db = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    db.execute("PRAGMA journal_mode = DELETE")          # same as the app: NOT WAL (network filesystem)
    db.execute("PRAGMA synchronous = FULL")
    return db


def _utc(us):
    return dt.datetime.fromtimestamp(us / 1e6, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if us is not None else "-"


def inspect(db, unit, boot, expect):
    """Run every guard; return a summary dict. Raises Refused."""
    if not BOOT_RE.match(boot or ""):
        raise Refused("--boot-id must be exactly 16 lowercase hex digits")
    if not unit or not isinstance(unit, str):
        raise Refused("--unit is required")
    if not isinstance(expect, int) or expect < 1:
        raise Refused("--expect-rows must be a positive integer")
    q = db.execute("PRAGMA quick_check").fetchall()
    if q != [("ok",)]:
        raise Refused(f"PRAGMA quick_check failed: {q[:3]}")
    n, lo, hi, smin, smax, fmean = db.execute(
        "SELECT COUNT(*), MIN(gps_utc_us), MAX(gps_utc_us), MIN(seq), MAX(seq), AVG(freq_hz) "
        "FROM readings WHERE unit_id = ? AND boot_id = ?", (unit, boot)).fetchone()
    if n != expect:
        raise Refused(f"expected {expect} rows for {unit} / {boot}, found {n}")
    days = set()
    for (g,) in db.execute("SELECT gps_utc_us FROM readings WHERE unit_id = ? AND boot_id = ? AND gps_utc_us IS NOT NULL",
                           (unit, boot)):
        days.add(g // DAY_US)
    for (r,) in db.execute("SELECT received_at FROM readings WHERE unit_id = ? AND boot_id = ? AND gps_utc_us IS NULL",
                           (unit, boot)):
        days.add(int(r // 86400))
    bad = []
    for day in sorted(days):
        row = db.execute("SELECT export_done, agg_next_hour, agg_done, pruned_rows, pruned_done, export_rows "
                         "FROM retention_days WHERE unit_id = ? AND day = ?", (unit, day)).fetchone()
        if row and (row[0] or row[1] or row[2] or row[3] or row[4] or row[5]):
            bad.append(day)
    if bad:
        raise Refused(f"retention has already exported/aggregated/pruned day(s) {bad}: those copies would keep "
                      "the bad rows; do not delete by hand, ask for a separate clean-up plan")
    if lo is not None:
        ev = db.execute("SELECT COUNT(*) FROM events WHERE unit_id = ? AND start_us <= ? AND end_us >= ?",
                        (unit, hi, lo)).fetchone()[0]
        if ev:
            raise Refused(f"{ev} event interval(s) overlap these rows; refusing")
    return dict(rows=n, first_gps=_utc(lo), last_gps=_utc(hi), seq_min=smin, seq_max=smax,
                freq_mean=round(fmean, 4) if fmean is not None else None, days=sorted(days))


def quarantine(db, unit, boot, expect, db_path):
    """Write the rows to a JSONL file next to the database; return its path. Verifies the line count."""
    qdir = Path(db_path).resolve().parent / "quarantine"
    qdir.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = qdir / f"{unit}_{boot}_{stamp}.jsonl"
    names = [c.strip() for c in COLS.split(",")]
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    count = 0
    with os.fdopen(fd, "w") as f:
        for row in db.execute(f"SELECT {COLS} FROM readings WHERE unit_id = ? AND boot_id = ? ORDER BY id", (unit, boot)):
            f.write(json.dumps(dict(zip(names, row))) + "\n")
            count += 1
        f.flush()
        os.fsync(f.fileno())
    if count != expect:
        out.unlink()
        raise Refused(f"quarantine wrote {count} rows, expected {expect}; nothing deleted")
    with open(out) as f:
        if sum(1 for _ in f) != expect:
            out.unlink()
            raise Refused("quarantine file line count mismatch; nothing deleted")
    return out


def delete(db, unit, boot, expect):
    db.execute("BEGIN IMMEDIATE")
    try:
        n = db.execute("SELECT COUNT(*) FROM readings WHERE unit_id = ? AND boot_id = ?", (unit, boot)).fetchone()[0]
        if n != expect:
            raise Refused(f"row count changed to {n} before the delete; rolled back")
        cur = db.execute("DELETE FROM readings WHERE unit_id = ? AND boot_id = ?", (unit, boot))
        if cur.rowcount != expect:
            raise Refused(f"delete touched {cur.rowcount} rows, expected {expect}; rolled back")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return cur.rowcount


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("TREMOR_DB_PATH"), help="path to readings.db (default: $TREMOR_DB_PATH)")
    ap.add_argument("--unit", required=True)
    ap.add_argument("--boot-id", required=True)
    ap.add_argument("--expect-rows", type=int, required=True, help="exact number of rows that must match")
    ap.add_argument("--confirm-boot-id", default=None, help="the boot id again: required to actually delete")
    a = ap.parse_args(argv)
    try:
        if not a.db:
            raise Refused("--db (or TREMOR_DB_PATH) is required")
        db = _connect(os.path.expanduser(a.db))
        try:
            info = inspect(db, a.unit, a.boot_id, a.expect_rows)
            print(f"found {info['rows']} rows for {a.unit} / {a.boot_id}: gps {info['first_gps']} .. {info['last_gps']} UTC, "
                  f"seq {info['seq_min']}..{info['seq_max']}, mean {info['freq_mean']} Hz, day(s) {info['days']}")
            if a.confirm_boot_id is None:
                print("DRY RUN: nothing changed. Re-run with --confirm-boot-id " + a.boot_id + " to delete.")
                return 0
            if a.confirm_boot_id != a.boot_id:
                raise Refused("--confirm-boot-id does not match --boot-id")
            out = quarantine(db, a.unit, a.boot_id, a.expect_rows, os.path.expanduser(a.db))
            print(f"rows saved to {out}")
            n = delete(db, a.unit, a.boot_id, a.expect_rows)
            print(f"DELETED {n} rows. The `units` counters (readings_total etc.) were left unchanged on purpose.")
            return 0
        finally:
            db.close()
    except Refused as exc:
        print(f"REFUSED: {exc}\nNothing was changed.", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"REFUSED (database error): {exc}\nNothing was changed unless the transaction had committed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
