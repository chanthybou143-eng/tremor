from __future__ import annotations

import numpy as np
import pytest

from tremor.frequency import StreamingZeroCrossingDetector
from tremor.signal import generate_mains_signal


def _run_detector(t, samples, min_interval_s=0.015):
    detector = StreamingZeroCrossingDetector(min_interval_s=min_interval_s)
    results = []
    for ti, xi in zip(t, samples):
        sample = detector.process_sample(ti, xi)
        if sample is not None:
            results.append(sample)
    return results


@pytest.mark.parametrize("sample_rate_hz,tol_hz", [(4000.0, 0.02), (10000.0, 0.005)])
@pytest.mark.parametrize("offset_hz", [-0.5, -0.1, 0.0, 0.2, 0.5])
def test_streaming_matches_batch_on_clean_signal(sample_rate_hz, tol_hz, offset_hz):
    sig = generate_mains_signal(
        duration_s=2.0, sample_rate_hz=sample_rate_hz, freq_offset_hz=offset_hz
    )

    results = _run_detector(sig.t, sig.samples)

    freqs = np.array([r.freq_hz for r in results])
    assert np.mean(freqs) == pytest.approx(50.0 + offset_hz, abs=tol_hz)


# Symmetric +/-1 transitions so each crossing time is exactly the midpoint
# of its bracketing samples: p0/p1 is a clean crossing at t=0.005; p2/p3 is
# a spurious wiggle back through zero at t=0.01125 (6.25ms later -- well
# inside a 15ms guard); p4/p5 is the next real crossing at t=0.022 (17ms
# after the first, i.e. outside the guard).
_T = np.array([0.000, 0.010, 0.011, 0.0115, 0.012, 0.032])
_SAMPLES = np.array([-1.0, 1.0, -1.0, 1.0, -1.0, 1.0])


def test_min_interval_guard_rejects_spurious_crossing():
    detector = StreamingZeroCrossingDetector(min_interval_s=0.015)
    results = [
        s
        for ti, xi in zip(_T, _SAMPLES)
        if (s := detector.process_sample(ti, xi)) is not None
    ]

    # Only the crossing at t=0.022 is far enough (17ms) from the first
    # crossing (t=0.005) to be accepted; the wiggle at t=0.01125 (6.25ms
    # later) must be rejected rather than counted as its own cycle.
    assert len(results) == 1
    assert results[0].t == pytest.approx((0.005 + 0.022) / 2.0)
    assert results[0].freq_hz == pytest.approx(1.0 / 0.017)


def test_without_guard_spurious_crossing_would_be_counted():
    detector = StreamingZeroCrossingDetector(min_interval_s=0.0)
    results = [
        s
        for ti, xi in zip(_T, _SAMPLES)
        if (s := detector.process_sample(ti, xi)) is not None
    ]

    # With no guard, the spurious wiggle is treated as a real crossing,
    # producing an extra (wrong) high-frequency estimate.
    assert len(results) == 2
