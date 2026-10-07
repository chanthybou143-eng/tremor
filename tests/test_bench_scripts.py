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
