#!/usr/bin/env python3
"""Time the /history page's server work against the REAL data, on the machine you run it on.

    python scripts/bench_history.py --db ~/tremor_data/readings.db              # on a copy (default)
    python scripts/bench_history.py --db ~/tremor_data/readings.db --live       # read-only, live file
    python scripts/bench_history.py --synthetic --dir /tmp/x [--days 30]         # scratch data, anywhere

Run it ON PYTHONANYWHERE (Bash console) before deploying the history page. A free account has ONE
web worker: while it computes a history view an ingest POST from the Pico waits, and the Pico
gives the whole request 4 s. Under SQLite's rollback journal a long READ also blocks the ingest's
COMMIT, so besides the total time per view this reports the longest single statement.

Default (copy): first shows the database size and the account's disk use (the quota covers every
file under --quota-root, default your home directory). If the copy would take use above
--max-quota-fraction (75%) it does NOT copy and tells you to use --live. Otherwise it copies the
database with SQLite's online backup API in small steps (a shared lock only for each step, so
ingest keeps committing; a step that races a commit is redone), next to the real one (same network
filesystem), checks the copy, times every preset range cold and warm, and deletes the copy.

--live: opens the real database read-only (mode=ro: nothing can be written) and runs each range
once. Every raw read the history code makes covers at most 6 hours, so no statement holds the lock
for long; run it at a quiet moment anyway.

Verdict: OK if every cold view is under 1 s, WARN under 2 s, otherwise FAIL.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tremor.history import RawDayCache, build_overview  # noqa: E402
from tremor.ingest import parse_payload  # noqa: E402
from tremor.store import DAY_US, US, AggRow, DayState, open_store  # noqa: E402

DEADLINE_S = 4.0
PAGE_TZ = ZoneInfo("Australia/Adelaide")      # what the page asks for (local hours / days)
# single-statement store reads, timed per call; raw_histogram runs several statements on one
# connection and reports each through store.statement_timer
STORE_CALLS = ("day_states", "rollup_aggregates", "excluded_aggregate_minutes", "read_points",
               "aggregate_mean_histogram", "unit_states", "aggregates")


def mb(n: float) -> str:
    return f"{n / 1e6:.1f} MB"


def tree_size(root: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def instrument(store) -> list:
    """Wrap the store's read methods to record each statement's duration (seconds)."""
    calls: list = []
    for name in STORE_CALLS:
        real = getattr(store, name)

        def timed(*a, _real=real, _name=name, **k):
            t = time.perf_counter()
            try:
                return _real(*a, **k)
            finally:
                calls.append((time.perf_counter() - t, _name))
        setattr(store, name, timed)
    store.statement_timer = lambda name, dt: calls.append((dt, name))
    return calls


def backup_copy(src_path: str, dst_path: str) -> dict:
    src = sqlite3.connect(f"file:{os.path.abspath(src_path)}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(dst_path)
    restarts, last_remaining = 0, None

    def progress(_status, remaining, total):
        nonlocal restarts, last_remaining
        if last_remaining is not None and remaining > last_remaining:
            restarts += 1                     # the source changed under us; sqlite started over
        last_remaining = remaining

    t = time.perf_counter()
    try:
        src.backup(dst, pages=256, progress=progress, sleep=0.02)     # ~1 MB per step
        ok = dst.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        src.close()
        dst.close()
    return dict(seconds=time.perf_counter() - t, restarts=restarts, quick_check=ok)


# ---------------------------------------------------------------- synthetic data (--synthetic)
def freq_at(t: float) -> float:
    return 50.0 + 0.02 * math.sin(t / 1800.0) + 0.015 * math.sin(t / 86400.0 * 2 * math.pi) + 0.004 * math.sin(t / 37.0)


def build_synthetic(path: str, now: float, days: int, raw_hours: float, rng: random.Random) -> None:
    store = open_store(path, synchronous="NORMAL")
    raw_from = now - raw_hours * 3600
    first_day = int(now // 86400) - days
    for d in range(first_day, int(raw_from // 86400)):
        rows = []
        for m in range(d * 1440, (d + 1) * 1440):
            if rng.random() < 0.01:
                continue
            f = freq_at(m * 60.0)
            amp = 0.02 if d % 6 == 1 and (m % 1440) in range(800, 830) else 0.744
            rows.append(AggRow("unit-1", m, 60, 0, f, f - 0.012, f + 0.012, 0.006, 0.01 + 0.02 * rng.random(), amp))
        store.save_aggregates(rows)
        store.save_day_state(DayState("unit-1", d, export_done=True, agg_done=True))
    t, seq = math.floor(max(raw_from, first_day * 86400.0)), 0
    while t < now:
        readings = []
        while len(readings) < 1000 and t < now:
            if not (int(t) % 7200 < 300 and rng.random() < 0.9):
                us = int(t * US)
                readings.append({"seq": seq, "frequency_hz": freq_at(t) + rng.gauss(0, 0.004), "amplitude_v": 0.744,
                                 "gps": [us // DAY_US, (us % DAY_US) // US, us % US]})
                seq += 1
            t += 1.0
        if readings:
            store.ingest(parse_payload({"unit_id": "unit-1", "boot_id": "be0c4a11e5f3d9a2", "readings": readings}),
                         min(now, t + 30))


# ---------------------------------------------------------------- timing
def run_ranges(store, repeats: int, warm: bool) -> float:
    calls = instrument(store)
    now = time.time()
    units = store.unit_states()
    if not units:
        print("no units in this database")
        return 0.0
    worst_cold = 0.0
    for u in units:
        starts = [u.first_seen - 3600]
        oldest = store.aggregates(u.unit_id, 0, 2 ** 62, 1)
        if oldest:
            starts.append(oldest[0].minute * 60)
        data_start_us = int(min(starts) * US)
        print(f"\n{u.unit_id}: data since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(data_start_us / US + 3600))} UTC")
        print(f"{'range':10s} {'cold ms':>8s} {'warm ms':>8s} {'KB':>5s} {'longest stmt ms':>16s} {'stmts':>6s}  "
              "sources / histogram")
        for name, span in (("last 1 h", 3600), ("last 6 h", 6 * 3600), ("last 24 h", 86400),
                           ("7 days", 7 * 86400), ("30 days", 30 * 86400), ("all", None)):
            to_us = int(now * US)
            from_us = data_start_us if span is None else to_us - span * US
            cold, warm_ms, longest, n_stmt = [], [], 0.0, 0
            for _ in range(repeats):
                cache = RawDayCache()
                calls.clear()
                t = time.perf_counter()
                body = build_overview(store, u.unit_id, from_us, to_us, now, cache=cache, data_start_us=data_start_us, tz=PAGE_TZ)
                cold.append(time.perf_counter() - t)
                longest = max([longest] + [c[0] for c in calls])
                n_stmt = len(calls)
                if warm:
                    t = time.perf_counter()
                    build_overview(store, u.unit_id, from_us, to_us, now, cache=cache, data_start_us=data_start_us, tz=PAGE_TZ)
                    warm_ms.append((time.perf_counter() - t) * 1000)
            worst_cold = max(worst_cold, max(cold))
            h = body["histogram"]
            hist = ("raw" if h["raw"] else "") + ("+1min-means" if h["minute_means"] else "") or "none"
            if not h["complete"]:
                hist += f" ({h['pending_hours']} h pending: budget reached)"
            print(f"{name:10s} {statistics.median(cold) * 1000:8.0f} "
                  f"{(statistics.median(warm_ms) if warm_ms else float('nan')):8.0f} {len(json.dumps(body)) / 1024:5.0f} "
                  f"{longest * 1000:16.0f} {n_stmt:6d}  "
                  f"{'+'.join(sorted({x['source'] for x in body['sources']})) or '-'} / {hist}")
    return worst_cold


def verdict(worst_cold: float) -> int:
    print(f"\nslowest cold view: {worst_cold * 1000:.0f} ms (the Pico's whole-request deadline is {DEADLINE_S * 1000:.0f} ms)")
    if worst_cold < 1.0:
        print("VERDICT: OK")
        return 0
    if worst_cold < 2.0:
        print("VERDICT: WARN -- a history view can hold up an ingest POST for a large share of its deadline")
        return 1
    print("VERDICT: FAIL -- too slow for the single web worker")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="the real readings.db")
    ap.add_argument("--live", action="store_true", help="read the live database read-only instead of a copy")
    ap.add_argument("--copy-dir", help="where to put the copy (default: next to --db)")
    ap.add_argument("--quota-mb", type=float, default=float(os.environ.get("TREMOR_QUOTA_MB", "512")))
    ap.add_argument("--quota-root", default=os.path.expanduser("~"))
    ap.add_argument("--max-quota-fraction", type=float, default=0.75)
    ap.add_argument("--repeats", type=int, default=None, help="default 2 on a copy, 1 live")
    ap.add_argument("--synthetic", action="store_true", help="time against generated data instead")
    ap.add_argument("--dir", help="--synthetic: directory for the scratch database")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--raw-hours", type=float, default=26.0)
    ap.add_argument("--keep", action="store_true", help="leave the copy / scratch database in place")
    a = ap.parse_args()

    if a.synthetic:
        if not a.dir:
            ap.error("--synthetic needs --dir")
        d = os.path.expanduser(a.dir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "history_probe.db")
        for suffix in ("", "-journal"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        build_synthetic(path, time.time(), a.days, a.raw_hours, random.Random(1))
        print(f"synthetic database: {path} ({mb(os.path.getsize(path))})")
        store = open_store(path)
        try:
            return verdict(run_ranges(store, a.repeats or 2, warm=True))
        finally:
            if not a.keep:
                for suffix in ("", "-journal"):
                    if os.path.exists(path + suffix):
                        os.remove(path + suffix)

    if not a.db:
        ap.error("--db is required (or --synthetic)")
    db = os.path.expanduser(a.db)
    if not os.path.isfile(db):
        print(f"no database at {db}")
        return 2
    db_bytes = os.path.getsize(db) + (os.path.getsize(db + "-journal") if os.path.exists(db + "-journal") else 0)
    quota = a.quota_mb * 1024 * 1024
    print(f"database: {db}  {mb(db_bytes)}")
    print(f"measuring disk use under {a.quota_root} ...")
    used = tree_size(os.path.expanduser(a.quota_root))
    after = used + db_bytes
    print(f"disk use: {mb(used)} of {mb(quota)} ({100 * used / quota:.0f}%); "
          f"with a copy: {mb(after)} ({100 * after / quota:.0f}%), limit for copying {100 * a.max_quota_fraction:.0f}%")

    if a.live:
        print("\nLIVE mode: real database, read-only; each range once, cold only")
        store = open_store(db, read_only=True)
        return verdict(run_ranges(store, a.repeats or 1, warm=False))

    if after > a.max_quota_fraction * quota:
        print("\nNOT copying: that would take the account above the limit. Re-run with --live to read the "
              "real database read-only instead.")
        return 3
    copy_dir = os.path.expanduser(a.copy_dir) if a.copy_dir else os.path.dirname(os.path.abspath(db))
    copy = os.path.join(copy_dir, "history_bench_copy.db")
    for suffix in ("", "-journal"):
        if os.path.exists(copy + suffix):
            os.remove(copy + suffix)
    try:
        print(f"\ncopying to {copy} (online backup, ~1 MB steps) ...")
        info = backup_copy(db, copy)
        print(f"copied in {info['seconds']:.1f} s, restarts {info['restarts']}, quick_check: {info['quick_check']}")
        if info["quick_check"] != "ok":
            print("the copy failed its integrity check; not timing it")
            return 2
        store = open_store(copy)
        return verdict(run_ranges(store, a.repeats or 2, warm=True))
    finally:
        if not a.keep:
            for suffix in ("", "-journal"):
                if os.path.exists(copy + suffix):
                    os.remove(copy + suffix)
            print(f"removed {copy}")


if __name__ == "__main__":
    sys.exit(main())
