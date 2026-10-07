#!/usr/bin/env python3
"""Bench report for a unit's run, from the server's own data (read-only GETs):

    python3 scripts/bench_gap_report.py --unit unit-1 --boot <boot_id> [--from ISO --to ISO]
    python3 scripts/bench_gap_report.py --unit unit-1 --boot <A> --compare-boot <B>    # e.g. soft vs hard ADC timer

Per run: every seq present exactly once? GPS-time gaps between consecutive seq (the "missing seconds"),
whether those gaps are locked to the POST cadence, readings per 10 minutes, grid-independent noise
metrics (std of successive 1 s frequency differences, amplitude spread), single-reading glitches, and
the unit's latest telemetry from /api/health. Mean frequency is printed only as context (the grid moves
it between runs). Device-side counters per run (ADC overflow, PPS interval spread, heap): see
scripts/bench_log_summary.py.
Exit status 1 if any seq is missing in a run (the outage bench test's pass criterion).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://tremorgrid.pythonanywhere.com"
POST_CADENCE_S = 30.0


def fetch_rows(base, unit, boot=None, from_=None, to=None):
    rows, after_id = [], 0
    while True:
        q = {"unit": unit, "resolution": "raw", "limit": 10_000, "after_id": after_id}
        if from_:
            q["from"] = from_
        if to:
            q["to"] = to
        with urllib.request.urlopen(f"{base}/api/history?{urllib.parse.urlencode(q)}", timeout=60) as r:
            page = json.load(r)
        rows += page["readings"]
        if not page.get("truncated") or page.get("next_after_id") is None:
            break
        after_id = page["next_after_id"]
        if page.get("next_from_us") is not None:
            from_ = page["next_from_us"] / 1e6
    return [r for r in rows if boot is None or r["boot_id"] == boot]


def fetch_telemetry(base, unit):
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=60) as r:
            h = json.load(r)
    except Exception as exc:                                       # informational only
        return {"error": type(exc).__name__}
    return next((u.get("telemetry") for u in h.get("units", []) if u["unit_id"] == unit), None)


def analyse(rows, cadence_s=POST_CADENCE_S):
    rows = sorted((r for r in rows if r.get("seq") is not None), key=lambda r: r["seq"])
    out = {"readings": len(rows)}
    if not rows:
        return out
    seqs = [r["seq"] for r in rows]
    out["seq_first"], out["seq_last"] = seqs[0], seqs[-1]
    out["seq_duplicates"] = len(seqs) - len(set(seqs))
    missing = []
    for a, b in zip(seqs, seqs[1:]):
        if b - a > 1:
            missing.append((a + 1, b - 1))
    out["seq_missing"] = sum(b - a + 1 for a, b in missing)
    out["seq_missing_ranges"] = missing[:20]

    timed = [r for r in rows if r.get("t") is not None]
    gaps = []                                                     # (t_of_gap, dt) between CONSECUTIVE seq only
    for a, b in zip(timed, timed[1:]):
        if b["seq"] == a["seq"] + 1:
            dt = b["t"] - a["t"]
            if dt > 1.5:
                gaps.append((a["t"], dt))
    out["time_gaps"] = len(gaps)
    out["time_gap_hist"] = {k: sum(1 for _, d in gaps if lo < d <= hi)
                            for k, lo, hi in (("2s", 1.5, 2.5), ("3s", 2.5, 3.5), ("4-5s", 3.5, 5.5), (">5s", 5.5, 1e9))}
    out["missing_seconds_in_gaps"] = round(sum(round(d) - 1 for _, d in gaps), 1)
    if len(gaps) >= 2:
        t0 = gaps[0][0]
        locked = sum(1 for t, _ in gaps if abs(((t - t0) / cadence_s) - round((t - t0) / cadence_s)) * cadence_s <= 2.0)
        out["gaps_locked_to_post_cadence"] = f"{locked}/{len(gaps)}"
    if timed:
        span = timed[-1]["t"] - timed[0]["t"]
        out["span_s"] = round(span, 1)
        out["readings_per_10min"] = round(len(timed) / span * 600, 1) if span > 0 else None

    # Grid-independent: std of successive differences between CONSECUTIVE 1 s readings (seq + 1 and
    # <= 1.5 s apart), i.e. per-reading measurement noise plus the grid's own 1 s wander -- comparable
    # across runs taken at different times, unlike the mean (which the grid moves).
    pairs = [(a, b) for a, b in zip(timed, timed[1:]) if b["seq"] == a["seq"] + 1 and b["t"] - a["t"] <= 1.5]
    if len(pairs) >= 3:
        d = [b["freq_hz"] - a["freq_hz"] for a, b in pairs]
        out["freq_diff1s_std_mHz"] = round(statistics.pstdev(d) * 1000, 3)
        out["freq_diff1s_mad_mHz"] = round(statistics.median(abs(x) for x in d) * 1000, 3)
    f = [r["freq_hz"] for r in timed]
    if len(f) >= 3:
        out["grid_freq_mean"] = round(statistics.fmean(f), 5)          # grid-dependent: context only
        out["grid_freq_std"] = round(statistics.pstdev(f), 5)          # grid-dependent: context only
        glitches = 0
        for i in range(len(f)):
            win = f[max(0, i - 5):i] + f[i + 1:i + 6]
            if win and abs(f[i] - statistics.median(win)) > 0.02:
                glitches += 1
        out["freq_glitches_gt_20mHz"] = glitches
    amps = [r["amplitude_v"] for r in timed if r.get("amplitude_v") is not None]
    if len(amps) >= 2:
        out["amplitude_mean_v"] = round(statistics.fmean(amps), 4)
        out["amplitude_std_mV"] = round(statistics.pstdev(amps) * 1000, 3)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--unit", default="unit-1")
    ap.add_argument("--boot", help="boot_id of the run (from the '# BOOT_ID' line)")
    ap.add_argument("--compare-boot", help="a second boot_id, reported side by side")
    ap.add_argument("--from", dest="from_", help="Unix seconds or ISO-8601 UTC")
    ap.add_argument("--to")
    a = ap.parse_args(argv)

    runs = {a.boot or "all": analyse(fetch_rows(a.base, a.unit, a.boot, a.from_, a.to))}
    if a.compare_boot:
        runs[a.compare_boot] = analyse(fetch_rows(a.base, a.unit, a.compare_boot, a.from_, a.to))
    keys = sorted({k for r in runs.values() for k in r}, key=lambda k: list(next(iter(runs.values()))).index(k)
                  if k in next(iter(runs.values())) else 99)
    width = max(len(k) for k in keys)
    print(" " * width + "".join(f"  {name[:24]:>24}" for name in runs))
    for k in keys:
        print(k.ljust(width) + "".join(f"  {str(r.get(k, '')):>24}" for r in runs.values()))
    print("\nlatest telemetry:", json.dumps(fetch_telemetry(a.base, a.unit)))
    return 1 if any(r.get("seq_missing") for r in runs.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
