from __future__ import annotations

import numpy as np
import pytest

from tremor.rocof import rocof_from_window


def test_returns_none_for_fewer_than_two_points():
    assert rocof_from_window(np.array([0.0]), np.array([50.0])) is None
    assert rocof_from_window(np.array([]), np.array([])) is None


def test_recovers_known_slope():
    t = np.linspace(0.0, 0.5, 26)
    freq_hz = 50.0 + 0.8 * t  # 0.8 Hz/s ramp

    slope = rocof_from_window(t, freq_hz)

    assert slope == pytest.approx(0.8, abs=1e-9)


def test_zero_slope_for_constant_frequency():
    t = np.linspace(0.0, 0.5, 26)
    freq_hz = np.full_like(t, 50.0)

    slope = rocof_from_window(t, freq_hz)

    assert slope == pytest.approx(0.0, abs=1e-9)
