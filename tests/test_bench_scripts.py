"""The bench tooling: the gap report's analysis, and the two device-side runner scripts staying in step."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bench_gap_report import analyse  # noqa: E402


def rows(times, seq0=0, freq=lambda i: 50.0):
    return [{"seq": seq0 + i, "t": t, "freq_hz": freq(i), "amplitude_v": 0.74, "boot_id": "b"} for i, t in enumerate(times)]


def test_post_locked_holes_are_found_and_counted():
    # one reading per second, but the second at every 30 s POST is never measured (like the soft-timer
    # data), and at t = 900 s three more are lost -- seq stays contiguous throughout
    times = [float(t) for t in range(1800) if not ((t % 30 == 0 and t > 0) or 901 <= t <= 903)]
    a = analyse(rows(times))
    assert a["seq_missing"] == 0 and a["seq_duplicates"] == 0
    # 59 holes (at 30, 60, ..., 1770 s); the one at 900 s is 5 s wide
    assert a["time_gaps"] == 59 and a["time_gap_hist"]["2s"] == 58 and a["time_gap_hist"]["4-5s"] == 1
    assert a["missing_seconds_in_gaps"] == 58 + 4
    assert a["gaps_locked_to_post_cadence"] == "59/59"


def test_missing_seq_is_reported_with_its_ranges():
    r = [x for x in rows([float(i) for i in range(100)]) if not (10 <= x["seq"] <= 14 or x["seq"] == 51)]
    a = analyse(r)
    assert a["seq_missing"] == 6 and a["seq_missing_ranges"] == [(10, 14), (51, 51)]


def test_a_clean_run_has_no_gaps_and_600_readings_per_10_minutes():
    a = analyse(rows([float(i) for i in range(3600)]))
    assert a["time_gaps"] == 0 and a["readings_per_10min"] == 600.2 and a["freq_glitches_gt_20mHz"] == 0
    assert a["freq_diff1s_std_mHz"] == 0.0 and a["amplitude_std_mV"] == 0.0


def test_single_reading_glitches_are_counted():
    a = analyse(rows([float(i) for i in range(200)], freq=lambda i: 50.3 if i in (40, 120) else 50.0))
    assert a["freq_glitches_gt_20mHz"] == 2


def test_the_normal_bench_script_is_the_outage_script_without_the_outage():
    out = (ROOT / "scripts" / "bench_outage.py").read_text().splitlines()
    norm = (ROOT / "scripts" / "bench_normal.py").read_text().splitlines()
    diff = [(a, b) for a, b in zip(out, norm) if a != b]
    assert len(out) == len(norm) and len(diff) == 1
    assert diff[0][0].startswith("OUTAGE_S = 600") and diff[0][1].startswith("OUTAGE_S = 0")


def test_the_bench_runner_never_prints_the_config_and_patches_before_importing_the_client():
    src = (ROOT / "scripts" / "bench_outage.py").read_text()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
            assert "wifi_config" not in {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}
            assert "_exc)" not in ast.unparse(n) or "type(_exc).__name__" in ast.unparse(n)
    assert src.index("http_client.timeout_post = _bench_post") < src.index("import wifi_unit_client")
    assert 'sys.path[:] = ["/"]' in src and "sys.path[:] = _saved_path" in src


def test_log_summary_takes_last_counters_and_the_pps_spread():
    from bench_log_summary import summarise
    lines = ["# BOOT reset_cause=WDT_RESET",
             "# STATUS elapsed_s=10.0 overflow=0 heap_free=320000 pps_iv_min_us=999996 pps_iv_max_us=999998 die_temp_c=31.2",
             "# STATUS elapsed_s=20.0 overflow=3 heap_free=310000 pps_iv_min_us=999980 pps_iv_max_us=1000015 die_temp_c=None",
             "# STATUS elapsed_s=30.0 overflow=3 heap_free=315000 pps_iv_min_us=None pps_iv_max_us=None",
             "# SLOW_POST duration_ms=16000 stage=send aborted=True t_ms=1"]
    s = summarise(lines)
    assert s["overflow"] == 3 and s["heap_free_min"] == 310000 and s["boot_lines"] == 1 and s["slow_post_lines"] == 1
    assert s["pps_spread_windows"] == 2 and s["pps_spread_us_max"] == 35 and s["pps_windows_spread_gt_10us"] == 1
    assert s["die_temp_c_last"] == 31.2


# --- A/B report ---------------------------------------------------------------------------------------------

LOG = """# BENCH start outage_start_s=180 outage_s=0 t_ms=1
# BOOT reset_cause=WDT_RESET
# BOOT_ID boot_id=0123456789abcdef
# POST_STAGE stage=dns t_ms=1000 cache
# POST_STAGE stage=connect t_ms=1001
# POST_STAGE stage=done t_ms=3500
# STATUS elapsed_s=10.0 overflow=0 heap_free=300000 post_attempts=1
# POST_STAGE stage=dns t_ms=31000 cache
# POST_STAGE stage=tls_handshake t_ms=31200
# WDT_GUARD_ABORT stage=tls_handshake stalled_ms=9000 post_elapsed_ms=10000 t_ms=41000
# POST_FAIL reason=aborted_slow_link: stage=tls_handshake elapsed_s=10.005 stage_duration_s=9.8 deadline_s=10.0
# POST_STAGE stage=dns t_ms=91000 cache
# POST_STAGE stage=done t_ms=93000
# SLOW_POST duration_ms=16000 stage=done aborted=False t_ms=93001
# POST_FAIL reason=http_status stage=n/a status=503 heap_free_at_try_start=1 heap_free_now=1
# POST_STAGE stage=dns t_ms=150000 lookup
# POST_FAIL reason=exception stage=body type=MemoryError msg=x heap_free_at_try_start=1 heap_free_now=1
# POST_STAGE stage=dns t_ms=210000 cache
# POST_STAGE stage=done t_ms=212000
# STATUS elapsed_s=220.0 overflow=0 heap_free=290000 post_attempts=5""".splitlines()


def test_posts_are_parsed_with_outcome_and_duration():
    from bench_ab_report import boot_id, parse_posts, post_stats
    posts = parse_posts(LOG)
    assert [p["outcome"] for p in posts] == ["ok", "abort", "http", "fail", "ok"]
    assert [p["duration_ms"] for p in posts] == [2500, 10005, 2000, None, 2000]
    st = post_stats(posts)
    assert (st["posts"], st["posts_ok"], st["posts_failed"], st["posts_aborted"], st["posts_duration_unknown"]) == (5, 2, 2, 1, 1)
    assert st["post_ms_median"] == 2250 and st["post_seconds_total"] == 16.5
    assert boot_id(LOG) == "0123456789abcdef"


def test_gaps_are_normalised_per_post_and_per_post_second():
    from bench_ab_report import run_report, verdicts
    times = [float(t) for t in range(600) if t not in (30, 60, 90, 120, 150)]       # 5 missing seconds
    r = run_report(LOG, rows(times))
    assert r["missing_seconds_in_gaps"] == 5 and r["posts"] == 5
    assert r["missing_s_per_post"] == 1.0 and r["missing_s_per_post_second"] == round(5 / 16.5, 3)
    assert r["missing_s_per_10min"] == round(5 / r["span_s"] * 600, 2)
    clean = run_report(LOG, rows([float(t) for t in range(600)]))
    v = dict(verdicts(r, clean))
    assert v["hard: missing_s_per_10min <= 0.5"] and v["hard: missing_s_per_post <= 10% of soft"]
    assert v["one boot per log (no reset)"] and v["seq_missing: 0 in both"]
