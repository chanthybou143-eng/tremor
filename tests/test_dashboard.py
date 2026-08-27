from __future__ import annotations

import numpy as np
import pytest

from tremor.dashboard import (
    ROCOF_STARTUP_DISCARD_S,
    ROCOF_Y_DEFAULT_HZ_S,
    _DashboardState,
    _readout_frequency,
    _rocof_ylim,
)
from tremor.frequency import FreqSample


def test_rocof_discards_startup_transient():
    state = _DashboardState()
    dt = 0.02
    t = 0.0
    while t < 1.5:
        state.add_sample(FreqSample(t=t, freq_hz=50.0 + 0.1 * t))
        t += dt

    freq_data, rocof_data = state.snapshot()

    assert len(freq_data) > 0
    assert len(rocof_data) > 0
    # None of the surviving RoCoF points come from before the discard
    # window, even though the underlying frequency buffer does.
    assert all(t >= ROCOF_STARTUP_DISCARD_S for t, _ in rocof_data)
    assert freq_data[0][0] < ROCOF_STARTUP_DISCARD_S


def test_rocof_recovers_known_slope_after_startup():
    state = _DashboardState()
    dt = 0.02
    t = 0.0
    slope_hz_s = 0.8
    while t < 2.0:
        state.add_sample(FreqSample(t=t, freq_hz=50.0 + slope_hz_s * t))
        t += dt

    _, rocof_data = state.snapshot()
    assert np.mean([v for _, v in rocof_data]) == pytest.approx(slope_hz_s, abs=0.05)


def test_rocof_ylim_clamps_to_default_when_data_is_small():
    lo, hi = _rocof_ylim(np.array([-0.1, 0.05, 0.2]))
    assert (lo, hi) == (-ROCOF_Y_DEFAULT_HZ_S, ROCOF_Y_DEFAULT_HZ_S)


def test_rocof_ylim_expands_when_data_exceeds_default():
    lo, hi = _rocof_ylim(np.array([-3.5, 0.0, 1.0]))
    assert (lo, hi) == (-3.5, 3.5)


def test_readout_frequency_is_median_of_recent_window():
    freq_ts = np.array([-5.0, -0.9, -0.5, -0.1, 0.0])
    freq_vals = np.array([1000.0, 50.1, 49.9, 50.2, 50.0])

    # The far-outlier at t=-5.0 is outside the 1s window and must not
    # affect the median. Median of the remaining [49.9, 50.0, 50.1, 50.2]
    # is the mean of the two middle values.
    result = _readout_frequency(freq_ts, freq_vals, window_s=1.0)

    assert result == pytest.approx(50.05)
