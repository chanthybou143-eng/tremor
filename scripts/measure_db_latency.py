#!/usr/bin/env python3
"""Measure SQLite ingest latency on the machine and filesystem you run it on.

    python scripts/measure_db_latency.py --dir ~/tremor_data [--batches 200] [--rows 60]

Run this ON PYTHONANYWHERE (in a Bash console) before switching Unit 1 over: its disk is a
network filesystem, so commit times there can be very different from a laptop's. It writes a
SCRATCH database (latency_probe.db) in --dir -- same filesystem as the real database, but never
the real database -- times one transaction per batch of --rows readings exactly the way
/api/ingest does (SqliteReadingStore.ingest: BEGIN IMMEDIATE ... COMMIT), and deletes the scratch
file afterwards.

Why it matters: the Pico gives an ingest request only 4 s to answer (its read_response stage
deadline). A slow commit turns into failed POSTs, which the device retries -- safe, thanks to
dedupe, but it would waste the very reliability this work adds.

Verdict: OK if the 99th percentile is under 1 s (25% of the deadline), WARN under 2.5 s,
otherwise FAIL (consider TREMOR_SQLITE_SYNCHRONOUS=NORMAL, or a different backend).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tremor.ingest import parse_payload  # noqa: E402
from tremor.store import open_store  # noqa: E402

DEADLINE_S = 4.0


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q / 100 * len(xs)))]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True, help="directory on the SAME filesystem as the real database")
    ap.add_argument("--batches", type=int, default=200)
    ap.add_argument("--rows", type=int, default=60, help="readings per batch (the device sends up to 60)")
    ap.add_argument("--synchronous", default=os.environ.get("TREMOR_SQLITE_SYNCHRONOUS", "FULL"))
    a = ap.parse_args()

    d = os.path.expanduser(a.dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "latency_probe.db")
    for suffix in ("", "-journal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)

    store = open_store(path, synchronous=a.synchronous)
    t0 = time.time()
    write_ms, read_ms = [], []
    try:
        for b in range(a.batches):
            base = int((t0 - a.batches * 30 + b * 30) * 1_000_000)
            payload = {"unit_id": "probe", "boot_id": "0000000000000000", "readings": [
                {"seq": b * a.rows + i, "frequency_hz": 50.0 + 0.001 * (i % 7), "amplitude_v": 0.744,
                 "gps": [base // 86_400_000_000, (base % 86_400_000_000) // 1_000_000 + i % 30, i]}
                for i in range(a.rows)]}
            batch = parse_payload(payload)
            s = time.perf_counter()
            store.ingest(batch, t0)
            write_ms.append((time.perf_counter() - s) * 1000)
            s = time.perf_counter()
            store.window("probe", 60.0)
            store.unit_states()
            read_ms.append((time.perf_counter() - s) * 1000)
    finally:
        store.close()
        for suffix in ("", "-journal"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)

    p99 = pct(write_ms, 99)
    print(f"directory: {d}   synchronous={a.synchronous}   {a.batches} batches x {a.rows} readings")
    print(f"ingest transaction (ms):  median {statistics.median(write_ms):7.1f}   p90 {pct(write_ms, 90):7.1f}   "
          f"p99 {p99:7.1f}   max {max(write_ms):7.1f}")
    print(f"/api/units reads (ms):    median {statistics.median(read_ms):7.1f}   p99 {pct(read_ms, 99):7.1f}   max {max(read_ms):7.1f}")
    print(f"device deadline for the whole request: {DEADLINE_S * 1000:.0f} ms")
    if p99 < 1000:
        print("VERDICT: OK")
        return 0
    if p99 < 2500:
        print("VERDICT: WARN -- commits are eating a large share of the device's 4 s deadline")
        return 1
    print("VERDICT: FAIL -- too slow; try TREMOR_SQLITE_SYNCHRONOUS=NORMAL or another backend")
    return 2


if __name__ == "__main__":
    sys.exit(main())
