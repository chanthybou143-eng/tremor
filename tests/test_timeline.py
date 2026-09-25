from __future__ import annotations

import random

import numpy as np
import pytest

from tremor.rocof import rocof_from_window
from tremor.timeline import _slope, rocof_series


def test_closed_form_slope_matches_numpy_polyfit_via_rocof_from_window():
    rng = random.Random(5)
    for n in (2, 3, 4, 7):
        ts = sorted(rng.uniform(0, 2) for _ in range(n))
        fs = [50 + rng.gauss(0, 0.03) for _ in range(n)]
        assert _slope(ts, fs) == pytest.approx(rocof_from_window(np.array(ts), np.array(fs)), abs=1e-9)
    assert _slope([1.0], [50.0]) is None
    assert _slope([1.0, 1.0], [50.0, 50.1]) is None


def test_a_gentle_ramp_recovers_its_true_slope():
    pts = [(1000.0 + i, 50.0 + 0.01 * i, "b") for i in range(8)]
    s = rocof_series(pts)
    assert [round(x, 6) for _t, x in s.points] == [0.01] * 7
    assert s.skipped_boundary == 0 and s.skipped_implausible == 0


def test_first_point_and_points_after_a_real_gap_have_no_slope():
    pts = [(0.0, 50.0, "b"), (1.0, 50.0, "b"), (10.0, 50.02, "b"), (11.0, 50.02, "b")]
    s = rocof_series(pts)
    assert [round(t) for t, _ in s.points] == [1, 11]      # t=10 has no neighbour within 1.5 s
    assert s.skipped_boundary == 0


def test_never_bridges_across_a_boot_id_change_even_when_the_gap_is_small():
    pts = [(1000.0, 50.0, "aaaa"), (1001.0, 50.0, "aaaa"), (1001.5, 49.5, "bbbb")]
    s = rocof_series(pts)
    assert [t for t, _ in s.points] == [1001.0]              # the 49.5 Hz reading contributes nothing
    assert s.skipped_boundary == 1
    assert all(abs(x) < 1e-9 for _t, x in s.points)


def test_legacy_readings_without_a_boot_id_bridge_normally():
    pts = [(1000.0, 50.0, None), (1000.5, 50.02, None)]
    assert rocof_series(pts).points[0][1] == pytest.approx(0.04)


def test_none_next_to_a_boot_id_is_not_a_boundary():
    # a legacy row beside a v2 row (during the client rollout) is not treated as a restart
    pts = [(1000.0, 50.0, None), (1001.0, 50.01, "aaaa")]
    assert rocof_series(pts).points[0][1] == pytest.approx(0.01)


def test_implausible_slope_is_skipped_and_counted():
    pts = [(1000.0, 50.0, "b"), (1001.0, 60.0, "b")]
    s = rocof_series(pts, plausibility_hz_s=5.0)
    assert s.points == [] and s.skipped_implausible == 1


def test_identical_timestamps_are_not_used_as_a_second_point():
    pts = [(1000.0, 50.0, "b"), (1000.0, 50.5, "b"), (1001.0, 50.0, "b")]
    s = rocof_series(pts)
    assert all(abs(x) < 10 for _t, x in s.points)
    assert len(s.points) >= 1
