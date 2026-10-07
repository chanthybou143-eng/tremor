#!/usr/bin/env python3
"""Device-side counters from bench logs (the `tee`d output of scripts/bench_*.py), side by side:

    python3 scripts/bench_log_summary.py ~/bench_soft.log ~/bench_hard.log

Grid-independent by construction: ADC ring overflow, skipped/degenerate chunks, chunk-capacity overflow,
POST durations / aborts, heap, resets, and the PPS interval spread -- max - min of the accepted 1 s PPS
intervals per 10 s STATUS window, which is (twice) how late the PPS interrupt got to stamp an edge.
Counters are cumulative on the device, so the LAST STATUS line of a run is its total.
"""

from __future__ import annotations

import re
import statistics
import sys

_KV = re.compile(r"(\w+)=(\S+)")


def _num(v):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return None


def summarise(lines):
    status = [dict(_KV.findall(line)) for line in lines if line.startswith("# STATUS ")]
    out = {"status_lines": len(status)}
    if not status:
        return out
    last = status[-1]
    for k in ("elapsed_s", "overflow", "skipped_chunks", "dup_timestamp_count", "chunk_capacity_overflow",
              "dropped", "peak_buffered", "post_attempts", "post_successes", "post_aborts", "slow_posts_15s",
              "longest_post_duration_s", "guard_expired", "wifi_reconnects", "wifi_escalations",
              "pps_resync", "pps_rejected"):
        if k in last:
            out[k] = _num(last[k])
    heap = [_num(s["heap_free"]) for s in status if _num(s.get("heap_free", "x")) is not None]
    if heap:
        out["heap_free_min"] = min(heap)
        out["heap_free_median"] = int(statistics.median(heap))
    spreads = [_num(s["pps_iv_max_us"]) - _num(s["pps_iv_min_us"]) for s in status
               if _num(s.get("pps_iv_min_us", "None")) is not None and _num(s.get("pps_iv_max_us", "None")) is not None]
    if spreads:
        out["pps_spread_windows"] = len(spreads)
        out["pps_spread_us_median"] = statistics.median(spreads)
        out["pps_spread_us_p95"] = sorted(spreads)[int(0.95 * (len(spreads) - 1))]
        out["pps_spread_us_max"] = max(spreads)
        out["pps_windows_spread_gt_10us"] = sum(1 for x in spreads if x > 10)
    temps = [_num(s["die_temp_c"]) for s in status if _num(s.get("die_temp_c", "None")) is not None]
    if temps:
        out["die_temp_c_last"] = temps[-1]
    out["boot_lines"] = sum(1 for line in lines if line.startswith("# BOOT "))
    out["slow_post_lines"] = sum(1 for line in lines if line.startswith("# SLOW_POST"))
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    runs = {}
    for path in argv:
        with open(path, errors="replace") as fh:
            runs[path.rsplit("/", 1)[-1]] = summarise([line.strip() for line in fh])
    keys = list(dict.fromkeys(k for r in runs.values() for k in r))
    width = max(len(k) for k in keys)
    print(" " * width + "".join(f"  {name[:22]:>22}" for name in runs))
    for k in keys:
        print(k.ljust(width) + "".join(f"  {str(r.get(k, '')):>22}" for r in runs.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
