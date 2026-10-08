#!/usr/bin/env python3
"""Soft- vs hard-ADC-timer A/B report from two bench logs (bench_soft.log, bench_hard.log):

    python3 scripts/bench_ab_report.py ~/bench_soft.log ~/bench_hard.log

For each run it reads the boot_id from the log, fetches that boot's readings from the server (read-only
GETs, scripts/bench_gap_report.py) and combines them with the device-side counters in the log
(scripts/bench_log_summary.py) and every POST parsed from the log's POST_STAGE / POST_FAIL lines.

The uplink differs between runs, so the gap metrics are also NORMALISED by the POSTs that caused them:
missing seconds per POST attempt and per second spent inside POSTs (timeout_post, dns -> done/fail),
next to the raw per-10-minute figures, with each run's POST count, failures, aborts and median POST
duration so you can see whether the conditions were comparable. Mean frequency is not compared (the
grid moves it between runs). PPS spread is reported for both modes, without a pass/fail.
"""

from __future__ import annotations

import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_gap_report import DEFAULT_BASE, analyse, fetch_rows  # noqa: E402
from bench_log_summary import summarise  # noqa: E402

_KV = re.compile(r"(\w+)=(\S+)")


def parse_posts(lines):
    """One dict per POST attempt: start/end t_ms, duration_ms, outcome ok|http|fail|abort|unknown."""
    posts, cur = [], None
    for line in lines:
        if line.startswith("# POST_STAGE "):
            kv = dict(_KV.findall(line))
            st, t = kv.get("stage"), int(kv.get("t_ms", "0"))
            if st == "dns":
                if cur is not None:                       # previous attempt: "ok" if it reached done, else unknown
                    cur["outcome"] = cur["outcome"] or "unknown"
                    posts.append(cur)
                cur = {"start": t, "end": None, "duration_ms": None, "outcome": None, "last_stage": "dns"}
            elif cur is not None and st == "done":
                cur.update(end=t, duration_ms=t - cur["start"], outcome="ok")
            elif cur is not None:
                cur["last_stage"] = st
        elif line.startswith("# POST_FAIL ") and cur is not None:
            kv = dict(_KV.findall(line))
            reason = kv.get("reason", "")
            if reason == "http_status":
                cur["outcome"] = "http"                   # duration already set by its "done" stage
            else:
                cur["outcome"] = "abort" if reason.startswith("aborted_slow_link") else "fail"
                if kv.get("elapsed_s") not in (None, "None"):
                    cur["duration_ms"] = int(round(float(kv["elapsed_s"]) * 1000))
            posts.append(cur)
            cur = None
    if cur is not None:
        cur["outcome"] = cur["outcome"] or "unknown"
        posts.append(cur)
    return posts


def post_stats(posts):
    d = [p["duration_ms"] for p in posts if p["duration_ms"] is not None]
    return {
        "posts": len(posts),
        "posts_ok": sum(p["outcome"] == "ok" for p in posts),
        "posts_failed": sum(p["outcome"] in ("fail", "http") for p in posts),
        "posts_aborted": sum(p["outcome"] == "abort" for p in posts),
        "posts_duration_unknown": len(posts) - len(d),
        "post_ms_median": int(statistics.median(d)) if d else None,
        "post_ms_p90": sorted(d)[int(0.9 * (len(d) - 1))] if d else None,
        "post_ms_max": max(d) if d else None,
        "post_seconds_total": round(sum(d) / 1000, 1),
    }


def boot_id(lines):
    for line in lines:
        if line.startswith("# BOOT_ID "):
            return dict(_KV.findall(line)).get("boot_id")
    return None


def run_report(lines, rows):
    g = analyse(rows)
    s = summarise(lines)
    p = post_stats(parse_posts(lines))
    out = dict(g)
    out.update({k: v for k, v in s.items()})
    out.update(p)
    span = g.get("span_s") or 0
    miss = g.get("missing_seconds_in_gaps", 0) or 0
    out["missing_s_per_10min"] = round(miss / span * 600, 2) if span else None
    out["missing_s_per_post"] = round(miss / p["posts"], 3) if p["posts"] else None
    out["missing_s_per_post_second"] = round(miss / p["post_seconds_total"], 3) if p["post_seconds_total"] else None
    out["posts_per_10min"] = round(p["posts"] / span * 600, 2) if span else None
    return out


def verdicts(soft, hard):
    """(metric, pass?) for the A/B criteria in deploy/DEPLOY_FW_RESILIENCE.md section 2.1."""
    def rel(k, lim):
        a, b = soft.get(k), hard.get(k)
        return None if a in (None, 0) or b is None else b <= a * lim
    v = [
        ("hard: missing_s_per_10min <= 0.5", None if hard.get("missing_s_per_10min") is None
         else hard["missing_s_per_10min"] <= 0.5),
        ("hard: missing_s_per_post <= 10% of soft", None if not soft.get("missing_s_per_post")
         else (hard.get("missing_s_per_post") or 0) <= 0.1 * soft["missing_s_per_post"]),
        ("hard: readings_per_10min >= 595", None if hard.get("readings_per_10min") is None
         else hard["readings_per_10min"] >= 595),
        ("freq_diff1s_std_mHz: hard <= soft +15%", rel("freq_diff1s_std_mHz", 1.15)),
        ("freq_glitches_gt_20mHz: hard <= soft", None if soft.get("freq_glitches_gt_20mHz") is None
         else hard.get("freq_glitches_gt_20mHz", 0) <= soft["freq_glitches_gt_20mHz"]),
        ("amplitude_mean_v: within 1%", None if not soft.get("amplitude_mean_v") or hard.get("amplitude_mean_v") is None
         else abs(hard["amplitude_mean_v"] / soft["amplitude_mean_v"] - 1) <= 0.01),
        ("amplitude_std_mV: hard <= soft +15%", rel("amplitude_std_mV", 1.15)),
        # ADC ring overflow is NOT judged: the soft timer simply takes no samples while a POST stalls the
        # VM (uncounted), the hard timer keeps sampling and counts what the 4 s ring cannot hold -- the
        # real loss of both is in missing seconds above. Reported as information.
        ("chunk_capacity_overflow + dup_timestamp_count: hard <= soft",
         (hard.get("chunk_capacity_overflow") or 0) + (hard.get("dup_timestamp_count") or 0)
         <= (soft.get("chunk_capacity_overflow") or 0) + (soft.get("dup_timestamp_count") or 0)),
        ("seq_missing: 0 in both", (soft.get("seq_missing") or 0) == 0 and (hard.get("seq_missing") or 0) == 0),
        ("dropped: 0 in both", (soft.get("dropped") or 0) == 0 and (hard.get("dropped") or 0) == 0),
        ("one boot per log (no reset)", soft.get("boot_lines") == 1 and hard.get("boot_lines") == 1),
    ]
    return v


ROWS = ["status_lines", "elapsed_s", "readings", "seq_missing", "seq_duplicates", "span_s", "readings_per_10min",
        "skipped_chunks", "dup_timestamp_count", "chunk_capacity_overflow", "overflow", "dropped",
        "time_gaps", "time_gap_hist", "gaps_locked_to_post_cadence", "missing_seconds_in_gaps",
        "missing_s_per_10min", "missing_s_per_post", "missing_s_per_post_second",
        "posts", "posts_per_10min", "posts_ok", "posts_failed", "posts_aborted", "posts_duration_unknown",
        "post_ms_median", "post_ms_p90", "post_ms_max", "post_seconds_total", "slow_post_lines",
        "freq_diff1s_std_mHz", "freq_diff1s_mad_mHz", "freq_glitches_gt_20mHz", "amplitude_mean_v", "amplitude_std_mV",
        "pps_spread_windows", "pps_spread_us_median", "pps_spread_us_p95", "pps_spread_us_max",
        "pps_windows_spread_gt_10us", "pps_resync", "heap_free_min", "heap_free_median", "die_temp_c_last",
        "boot_lines", "grid_freq_mean"]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    now = int(time.time())
    reports = {}
    for name, path in zip(("soft", "hard"), argv):
        lines = [line.strip() for line in open(path, errors="replace")]
        b = boot_id(lines)
        rows = fetch_rows(DEFAULT_BASE, "unit-1", b, now - 4 * 3600, now) if b else []
        reports[name] = run_report(lines, rows)
        reports[name]["boot_id"] = b
    w = max(len(k) for k in ROWS + ["boot_id"])
    print("".ljust(w) + f"  {'soft':>26}  {'hard':>26}")
    for k in ["boot_id"] + ROWS:
        print(k.ljust(w) + "".join(f"  {str(reports[n].get(k, '')):>26}" for n in ("soft", "hard")))
    print()
    for label, ok in verdicts(reports["soft"], reports["hard"]):
        print(("PASS " if ok else "FAIL " if ok is False else "n/a  ") + label)
    print("info PPS spread (both modes): no pass/fail -- decide from the numbers above")
    print("info overflow: counted only by the hard timer's way of losing samples -- compare missing seconds instead")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
