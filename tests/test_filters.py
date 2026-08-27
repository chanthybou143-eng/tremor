from __future__ import annotations

import numpy as np
import pytest

from tremor.filters import lowpass_filtfilt
from tremor.frequency import estimate_frequency_zero_crossing
from tremor.signal import generate_mains_signal


@pytest.mark.parametrize("sample_rate_hz,tol_hz", [(4000.0, 0.02), (10000.0, 0.005)])
def test_lowpass_attenuates_harmonic(sample_rate_hz, tol_hz):
    duration_s = 2.0
    sig = generate_mains_signal(
        duration_s=duration_s,
        sample_rate_hz=sample_rate_hz,
        harmonics={3: 0.2, 5: 0.1},
    )

    filtered = lowpass_filtfilt(sig.t, sig.samples, cutoff_hz=75.0)

    n = filtered.size
    spectrum = np.abs(np.fft.rfft(filtered))
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)

    fundamental_bin = np.argmin(np.abs(freqs - 50.0))
    third_harmonic_bin = np.argmin(np.abs(freqs - 150.0))

    ratio = spectrum[third_harmonic_bin] / spectrum[fundamental_bin]
    assert ratio < 0.02


@pytest.mark.parametrize("sample_rate_hz,tol_hz", [(4000.0, 0.02), (10000.0, 0.005)])
def test_filter_preserves_zero_crossing_timing(sample_rate_hz, tol_hz):
    sig = generate_mains_signal(duration_s=2.0, sample_rate_hz=sample_rate_hz)

    filtered = lowpass_filtfilt(sig.t, sig.samples, cutoff_hz=75.0)

    unfiltered_result = estimate_frequency_zero_crossing(sig.t, sig.samples)
    filtered_result = estimate_frequency_zero_crossing(sig.t, filtered)

    assert np.mean(filtered_result.freq_hz) == pytest.approx(
        np.mean(unfiltered_result.freq_hz), abs=tol_hz
    )


@pytest.mark.parametrize("sample_rate_hz,tol_hz", [(4000.0, 0.02), (10000.0, 0.005)])
@pytest.mark.parametrize("offset_hz", [-0.5, 0.0, 0.5])
def test_filter_then_estimate_recovers_offset(sample_rate_hz, tol_hz, offset_hz):
    sig = generate_mains_signal(
        duration_s=2.0,
        sample_rate_hz=sample_rate_hz,
        freq_offset_hz=offset_hz,
        harmonics={3: 0.2, 5: 0.1},
    )

    filtered = lowpass_filtfilt(sig.t, sig.samples, cutoff_hz=75.0)
    result = estimate_frequency_zero_crossing(sig.t, filtered)

    assert np.mean(result.freq_hz) == pytest.approx(50.0 + offset_hz, abs=tol_hz)
