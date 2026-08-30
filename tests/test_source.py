from __future__ import annotations

import time

import numpy as np
import pytest

from tremor.source import SyntheticFrequencySource


def _collect_for(source, duration_s):
    samples = []
    deadline = time.monotonic() + duration_s
    for sample in source.stream():
        samples.append(sample)
        if time.monotonic() >= deadline:
            break
    return samples


@pytest.fixture
def source():
    src = SyntheticFrequencySource(sample_rate_hz=8000.0, noise_std=0.0, chunk_s=0.02)
    src.start()
    yield src
    src.stop()


def test_recovers_nominal_frequency(source):
    samples = _collect_for(source, 1.0)

    assert len(samples) > 30
    freqs = np.array([s.freq_hz for s in samples])
    assert np.mean(freqs) == pytest.approx(50.0, abs=0.05)


def test_inject_step_changes_subsequent_frequency(source):
    before = _collect_for(source, 0.5)
    source.inject_step(0.5)
    after = _collect_for(source, 0.5)

    freq_before = np.mean([s.freq_hz for s in before])
    freq_after = np.mean([s.freq_hz for s in after[-10:]])
    assert freq_before == pytest.approx(50.0, abs=0.05)
    assert freq_after == pytest.approx(50.5, abs=0.05)


def test_inject_ramp_increases_frequency_over_time(source):
    _collect_for(source, 0.2)
    source.inject_ramp(1.0, duration_s=1.0)
    during = _collect_for(source, 1.5)

    freqs = np.array([s.freq_hz for s in during])
    early = np.mean(freqs[:5])
    late = np.mean(freqs[-5:])
    assert late > early + 0.3


def test_reset_returns_to_nominal(source):
    _collect_for(source, 0.2)
    source.inject_step(0.5)
    _collect_for(source, 0.3)
    source.reset()
    after_reset = _collect_for(source, 0.5)

    freq = np.mean([s.freq_hz for s in after_reset[-10:]])
    assert freq == pytest.approx(50.0, abs=0.05)


def test_causal_filter_recovers_offline_like_std():
    # Same noise/harmonics as the offline lowpass_filtfilt + batch-estimator
    # sanity check (~0.03 Hz std). The causal single-pole filter is a single
    # pole vs. the offline 4th-order zero-phase Butterworth, so it won't
    # match exactly, but it must land in the same ballpark -- not the
    # unfiltered case, where a single noisy sample can register a spurious
    # crossing and blow the per-cycle std up by orders of magnitude.
    #
    # A single pole is weak enough that, unlike the offline filtfilt case,
    # it occasionally still lets one badly-placed crossing through even
    # with the min-interval guard -- one outlier cycle is enough to blow
    # up a raw mean/std over ~150 cycles. Median/MAD (robust to a single
    # outlier) is what's actually being validated here; see the same
    # reasoning for the unfiltered estimator in test_frequency.py.
    src = SyntheticFrequencySource(
        sample_rate_hz=8000.0,
        noise_std=0.02,
        harmonics={3: 0.05, 5: 0.02},
        chunk_s=0.02,
    )
    src.start()
    try:
        samples = _collect_for(src, 3.0)
    finally:
        src.stop()

    freqs = np.array([s.freq_hz for s in samples])
    assert freqs.size > 100
    median = np.median(freqs)
    mad_std = np.median(np.abs(freqs - median)) * 1.4826  # MAD -> std-equivalent
    assert median == pytest.approx(50.0, abs=0.02)
    assert mad_std < 0.06


def test_stop_terminates_background_thread():
    src = SyntheticFrequencySource(sample_rate_hz=8000.0, chunk_s=0.02)
    src.start()
    _collect_for(src, 0.1)
    src.stop()
    assert not src._thread.is_alive()
