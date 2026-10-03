#!/usr/bin/env python3
"""Time the /history page's server work on the machine and filesystem you run it on.

    python scripts/bench_history.py --dir ~/tremor_data [--days 30] [--raw-hours 26] [--repeats 3]

Run this ON PYTHONANYWHERE (in a Bash console) before deploying the history page. It builds a
SCRATCH database (history_probe.db) in --dir -- same filesystem as the real database, never the
real database -- shaped like a unit after --days of running: 1-minute aggregates for every
closed day (what the retention engine leaves), plus --raw-hours of raw 1-per-second readings for
the days not aggregated yet (today and, until it settles, yesterday), with some outages and
excluded stretches mixed in. It then times history.build_overview (exactly what
/api/history/overview runs) for each preset range, cold (empty raw-day cache) and warm, and
deletes the scratch files afterwards (--keep leaves them, e.g. to point a local server at).

Why it matters: a free PythonAnywhere account has ONE web worker. While it computes a history
view, an ingest POST from the Pico waits -- and the Pico gives the whole request 4 s.

Verdict: OK if every cold request is under 1 s, WARN under 2 s, otherwise FAIL.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tremor.history import RawDayCache, build_overview  # noqa: E402
from tremor.ingest import parse_payload  # noqa: E402
from tremor.store import DAY_US, US, AggRow, DayState, open_store  # noqa: E402

UNIT = "unit-1"
BOOT = "be0c4a11e5f3d9a2"


def freq_at(t: float) -> float:
    """Slow wander plus a daily swing, ~±40 mHz: enough shape to see on a chart."""
    return 50.0 + 0.02 * math.sin(t / 1800.0) + 0.015 * math.sin(t / 86400.0 * 2 * math.pi) + 0.004 * math.sin(t / 37.0)


def build(path: str, now: float, days: int, raw_hours: float, rng: random.Random) -> dict:
    store = open_store(path, synchronous="NORMAL")
    raw_from = now - raw_hours * 3600
    agg_until_day = int(raw_from // 86400)                 # days before this one are aggregated
    first_day = int(now // 86400) - days
    # -- aggregates for closed days
    n_agg = 0
    for d in range(first_day, agg_until_day):
        rows = []
        for m in range(d * 1440, (d + 1) * 1440):
            t = m * 60.0
            if rng.random() < 0.01 or (m % 1440) in range(600, 640) and d % 5 == 2:
                continue                                    # outages
            n = 60 if rng.random() > 0.05 else rng.randint(30, 59)
            f = freq_at(t)
            fmin, fmax, amp = f - 0.012, f + 0.012, 0.744
            if d % 6 == 1 and (m % 1440) in range(800, 830):
                amp = 0.02                                   # plugpack unplugged
            if d % 9 == 4 and m % 1440 == 300:
                fmax = 80.995                                # the 2026-09-27 glitch, as an aggregate sees it
            rows.append(AggRow(UNIT, m, n, 0, f, fmin, fmax, 0.006, 0.01 + 0.02 * rng.random(), amp))
        store.save_aggregates(rows)
        n_agg += len(rows)
        store.save_day_state(DayState(UNIT, d, export_done=True, agg_done=True))
    # -- raw readings for the not-yet-aggregated tail (one ingest per 1000 readings)
    start = max(raw_from, first_day * 86400.0)
    t, seq, n_raw = math.floor(start), 0, 0
    while t < now:
        readings = []
        while len(readings) < 1000 and t < now:
            if not (int(t) % 7200 < 300 and rng.random() < 0.9):      # a 5-minute outage every 2 h
                f, amp = freq_at(t) + rng.gauss(0, 0.004), 0.744
                if int(t) % 86400 in range(36000, 36300):
                    f, amp = 50.0 + rng.uniform(-20, 20), 0.01         # unplugged: garbage frequency
                us = int(t * US)
                readings.append({"seq": seq, "frequency_hz": f, "amplitude_v": amp,
                                 "gps": [us // DAY_US, (us % DAY_US) // US, us % US]})
                seq += 1
            t += 1.0
        if readings:
            # received just after the batch's last reading, as on the real server (a receipt time
            # more than ingest.MAX_AGE_S after a reading would mark its GPS time implausible)
            store.ingest(parse_payload({"unit_id": UNIT, "boot_id": BOOT, "readings": readings}), min(now, t + 30))
            n_raw += len(readings)
    return dict(store=store, aggregate_rows=n_agg, raw_rows=n_raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True, help="directory on the SAME filesystem as the real database")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--raw-hours", type=float, default=26.0)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="leave the scratch database in place")
    a = ap.parse_args()

    d = os.path.expanduser(a.dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "history_probe.db")
    for suffix in ("", "-journal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)

    now = time.time()
    t0 = time.perf_counter()
    built = build(path, now, a.days, a.raw_hours, random.Random(1))
    store = built["store"]
    print(f"scratch database: {path}  ({os.path.getsize(path) / 1e6:.1f} MB, built in {time.perf_counter() - t0:.0f} s)")
    print(f"  {built['aggregate_rows']} aggregate minutes over {a.days} days + {built['raw_rows']} raw readings "
          f"({a.raw_hours:g} h)")

    ranges = [("last 1 h", 3600), ("last 6 h", 6 * 3600), ("last 24 h", 86400), ("7 days", 7 * 86400),
              ("30 days", 30 * 86400), ("all", None)]
    first_us = int((now - (a.days + 1) * 86400) * US)
    worst_cold = 0.0
    try:
        print(f"{'range':10s} {'cold ms':>8s} {'warm ms':>8s} {'KB':>6s}  freq/rocof/coverage points")
        for name, span in ranges:
            to_us = int(now * US)
            from_us = first_us if span is None else to_us - span * US
            cold, warm = [], []
            for _ in range(a.repeats):
                cache = RawDayCache()
                s = time.perf_counter()
                body = build_overview(store, UNIT, from_us, to_us, now, cache=cache, data_start_us=first_us)
                kb = len(json.dumps(body)) / 1024
                cold.append((time.perf_counter() - s) * 1000)
                s = time.perf_counter()
                build_overview(store, UNIT, from_us, to_us, now, cache=cache, data_start_us=first_us)
                warm.append((time.perf_counter() - s) * 1000)
            worst_cold = max(worst_cold, max(cold))
            print(f"{name:10s} {statistics.median(cold):8.0f} {statistics.median(warm):8.0f} {kb:6.0f}  "
                  f"{len(body['freq']['t'])}/{len(body['rocof']['t'])}/{len(body['coverage']['t'])}"
                  f"  ({'+'.join(sorted({x['source'] for x in body['sources']}))})")
    finally:
        store.close()
        if not a.keep:
            for suffix in ("", "-journal"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    print(f"slowest cold request: {worst_cold:.0f} ms (the Pico's whole-request deadline is 4000 ms)")
    if worst_cold < 1000:
        print("VERDICT: OK")
        return 0
    if worst_cold < 2000:
        print("VERDICT: WARN -- a history view can hold up an ingest POST for a large share of its deadline")
        return 1
    print("VERDICT: FAIL -- too slow for the single web worker")
    return 2


if __name__ == "__main__":
    sys.exit(main())
