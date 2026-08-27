from __future__ import annotations

import numpy as np
import pytest

from tremor.frequency import estimate_frequency_zero_crossing, find_rising_crossings
from tremor.signal import generate_mains_signal

# Realistic Pico ADC sample rates (MicroPython, not offline-DSP rates), each
# paired with a tolerance that reflects how much crossing-interpolation
# accuracy that rate can actually deliver.
SAMPLE_RATES_HZ = {4000.0: 0.02, 10000.0: 0.005}
OFFSETS_HZ = [-0.5, -0.1, 0.0, 0.1, 0.2, 0.5]


def test_find_rising_crossings_hand_built():
    t = np.array([0.0, 0.25, 0.5, 0.75])
    samples = np.array([-1.0, 1.0, -1.0, 1.0])

    crossings = find_rising_crossings(t, samples)

    assert np.allclose(crossings, [0.125, 0.625])


def test_estimate_frequency_hand_built():
    t = np.array([0.0, 0.25, 0.5, 0.75])
    samples = np.array([-1.0, 1.0, -1.0, 1.0])

    result = estimate_frequency_zero_crossing(t, samples)

    assert np.allclose(result.t, [0.375])
    assert np.allclose(result.freq_hz, [2.0])


def test_too_few_crossings_raises():
    t = np.array([0.0, 0.25])
    samples = np.array([-1.0, 1.0])

    with pytest.raises(ValueError):
        estimate_frequency_zero_crossing(t, samples)


@pytest.mark.parametrize("sample_rate_hz,tol_hz", SAMPLE_RATES_HZ.items())
@pytest.mark.parametrize("offset_hz", OFFSETS_HZ)
def test_recovers_constant_offset(sample_rate_hz, tol_hz, offset_hz):
    sig = generate_mains_signal(
        duration_s=2.0,
        sample_rate_hz=sample_rate_hz,
        freq_offset_hz=offset_hz,
    )

    result = estimate_frequency_zero_crossing(sig.t, sig.samples)

    assert np.mean(result.freq_hz) == pytest.approx(50.0 + offset_hz, abs=tol_hz)


@pytest.mark.parametrize("sample_rate_hz,tol_hz", SAMPLE_RATES_HZ.items())
def test_estimate_count_matches_cycle_count(sample_rate_hz, tol_hz):
    duration_s = 2.0
    sig = generate_mains_signal(duration_s=duration_s, sample_rate_hz=sample_rate_hz)

    result = estimate_frequency_zero_crossing(sig.t, sig.samples)

    # The buffer starts exactly on a rising crossing (t=0) and ends just
    # before the sample that would close the final cycle, so a couple of
    # cycles are lost to that boundary effect -- not a bug in the estimator.
    expected_cycles = duration_s * 50.0
    assert result.freq_hz.size == pytest.approx(expected_cycles, abs=2)


# Unfiltered zero-crossing detection is sensitive to noise near the
# crossing itself: an occasional noisy sample can register a spurious extra
# crossing a fraction of a sample away from the real one, producing a huge
# (wrong) instantaneous frequency for that one cycle. That's exactly why
# tremor.filters exists (see test_filters.py for the filtered, tighter-
# tolerance version of this check) -- here, on the raw signal, the median
# across cycles is the meaningful statistic, not the mean, since the mean
# is dominated by rare outliers.
NOISE_TOL_HZ = {4000.0: 0.05, 10000.0: 0.1}


@pytest.mark.parametrize("sample_rate_hz,tol_hz", SAMPLE_RATES_HZ.items())
@pytest.mark.parametrize("offset_hz", OFFSETS_HZ)
def test_recovers_offset_with_noise(sample_rate_hz, tol_hz, offset_hz):
    rng = np.random.default_rng(1234)
    sig = generate_mains_signal(
        duration_s=2.0,
        sample_rate_hz=sample_rate_hz,
        freq_offset_hz=offset_hz,
        noise_std=0.02,
        rng=rng,
    )

    result = estimate_frequency_zero_crossing(sig.t, sig.samples)

    assert np.median(result.freq_hz) == pytest.approx(
        50.0 + offset_hz, abs=NOISE_TOL_HZ[sample_rate_hz]
    )
