from __future__ import annotations

import numpy as np
import pytest

from tremor.filters import SinglePoleLowPass, lowpass_filtfilt
from tremor.frequency import StreamingZeroCrossingDetector, estimate_frequency_zero_crossing
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


@pytest.mark.parametrize("sample_rate_hz", [4000.0, 10000.0])
def test_single_pole_attenuates_harmonic(sample_rate_hz):
    sig = generate_mains_signal(
        duration_s=2.0, sample_rate_hz=sample_rate_hz, harmonics={3: 0.2, 5: 0.1}
    )

    filt = SinglePoleLowPass(cutoff_hz=75.0, sample_rate_hz=sample_rate_hz)
    filtered = np.array([filt.process_sample(x) for x in sig.samples])

    n = filtered.size
    spectrum = np.abs(np.fft.rfft(filtered))
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    fundamental_bin = np.argmin(np.abs(freqs - 50.0))
    third_harmonic_bin = np.argmin(np.abs(freqs - 150.0))

    ratio = spectrum[third_harmonic_bin] / spectrum[fundamental_bin]
    # A single pole only rolls off at 6 dB/octave, much weaker than the
    # offline 4th-order Butterworth (test_lowpass_attenuates_harmonic,
    # ratio < 0.02) -- it noticeably reduces harmonic content (relative
    # amplitude 0.2 -> ~0.10) without eliminating it.
    assert ratio < 0.15


@pytest.mark.parametrize("sample_rate_hz,tol_hz", [(4000.0, 0.03), (10000.0, 0.01)])
@pytest.mark.parametrize("offset_hz", [-0.5, 0.0, 0.5])
def test_single_pole_then_streaming_estimate_recovers_offset(
    sample_rate_hz, tol_hz, offset_hz
):
    sig = generate_mains_signal(
        duration_s=2.0,
        sample_rate_hz=sample_rate_hz,
        freq_offset_hz=offset_hz,
        harmonics={3: 0.2, 5: 0.1},
    )

    filt = SinglePoleLowPass(cutoff_hz=75.0, sample_rate_hz=sample_rate_hz)
    detector = StreamingZeroCrossingDetector(min_interval_s=0.015)
    results = []
    for ti, xi in zip(sig.t, sig.samples):
        sample = detector.process_sample(ti, filt.process_sample(xi))
        if sample is not None:
            results.append(sample)

    freqs = np.array([r.freq_hz for r in results])
    assert np.mean(freqs) == pytest.approx(50.0 + offset_hz, abs=tol_hz)
