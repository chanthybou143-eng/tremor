from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freq_estimator import (  # noqa: E402
    estimate_frequency,
    find_zero_crossings,
    frequency_from_crossings,
    generate_synthetic_signal,
    moving_average,
)


def test_recovers_clean_offset():
    ts, ys = generate_synthetic_signal(freq_hz=50.2, fs_hz=4000.0, duration_s=2.0, seed=1)
    mean_freq, per_cycle = estimate_frequency(ts, ys)
    assert mean_freq == pytest.approx(50.2, abs=0.005)
    assert len(per_cycle) > 50


def test_naive_breaks_under_heavy_noise_but_hysteresis_fix_recovers():
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=4000.0, duration_s=2.0, noise_std=0.1, seed=42
    )
    naive_freq, _ = estimate_frequency(ts, ys, hysteresis=0.0, filter_window_s=0.0)
    fixed_freq, _ = estimate_frequency(ts, ys, hysteresis=0.2, filter_window_s=0.00125)

    assert abs(naive_freq - 50.0) > 50.0  # corrupted by orders of magnitude
    assert fixed_freq == pytest.approx(50.0, abs=0.05)


def test_dc_offset_does_not_bias_frequency_compensated_or_not():
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=4000.0, duration_s=2.0, dc_offset=0.1, seed=7
    )
    compensated, _ = estimate_frequency(ts, ys, dc_offset=0.1)
    uncompensated, _ = estimate_frequency(ts, ys, dc_offset=0.0)

    assert compensated == pytest.approx(50.0, abs=1e-6)
    assert uncompensated == pytest.approx(50.0, abs=1e-6)


def test_raises_on_insufficient_crossings():
    ts, ys = generate_synthetic_signal(duration_s=2.0, seed=1)
    with pytest.raises(ValueError):
        estimate_frequency(ts, ys, hysteresis=2.0)  # exceeds signal amplitude


def test_filter_window_s_scales_with_sample_rate():
    # A 1.25ms window should be ~5 samples at 4kHz and ~1 sample (a no-op)
    # at 400Hz -- confirms the window is derived from the actual sample
    # rate rather than being a fixed, unscaled sample count.
    ts_4k, ys_4k = generate_synthetic_signal(fs_hz=4000.0, duration_s=0.5, seed=1)
    ts_400, ys_400 = generate_synthetic_signal(fs_hz=400.0, duration_s=0.5, seed=1)

    freq_4k, _ = estimate_frequency(ts_4k, ys_4k, filter_window_s=0.00125)
    freq_400, _ = estimate_frequency(ts_400, ys_400, filter_window_s=0.00125)

    assert freq_4k == pytest.approx(50.0, abs=0.02)
    assert freq_400 == pytest.approx(50.0, abs=0.02)


def test_generate_synthetic_signal_reproducible_with_seed():
    ts1, ys1 = generate_synthetic_signal(noise_std=0.05, seed=123)
    ts2, ys2 = generate_synthetic_signal(noise_std=0.05, seed=123)
    assert ys1 == ys2


def test_generate_synthetic_signal_does_not_touch_global_random_state():
    random.seed(999)
    state_before = random.getstate()
    generate_synthetic_signal(duration_s=0.1, fs_hz=1000.0, noise_std=0.1, seed=123)
    assert random.getstate() == state_before


def test_generate_synthetic_signal_accepts_private_rng_for_continuity():
    rng = random.Random(5)
    ts1, ys1 = generate_synthetic_signal(duration_s=0.05, fs_hz=1000.0, noise_std=0.1, rng=rng)
    ts2, ys2 = generate_synthetic_signal(duration_s=0.05, fs_hz=1000.0, noise_std=0.1, rng=rng)
    # Same rng object reused -> second call continues the stream, so it
    # must NOT reproduce the first call's samples.
    assert ys1 != ys2


def test_moving_average_is_noop_for_window_one():
    samples = [1.0, 2.0, 3.0]
    assert moving_average(samples, 1) == samples


def test_moving_average_reused_buffer_matches_default_allocation_path():
    # Numeric equivalence: the default (n=None, out=None) path and the
    # reused-buffer path must produce identical output for the same valid
    # data, for both the window<=1 no-op branch and the real filter.
    samples = [1.0, 2.0, 1.0, 3.0, 2.0, 4.0, 3.0]
    expected_w1 = moving_average(samples, 1)
    expected_w3 = moving_average(samples, 3)

    out = [0.0] * len(samples)
    result_w1 = moving_average(samples, 1, n=len(samples), out=out)
    assert result_w1 == expected_w1

    out = [0.0] * len(samples)
    result_w3 = moving_average(samples, 3, n=len(samples), out=out)
    assert result_w3 == expected_w3


def test_moving_average_ignores_stale_data_past_n():
    # The whole point of the reused-buffer path: samples/out may be
    # oversized, fixed-capacity buffers with garbage past index n-1 (as
    # wifi_unit_client.py's CHUNK_CAPACITY buffers are, reused chunk to
    # chunk) -- moving_average must never read or write past n.
    n = 5
    capacity = 10
    samples = [1.0, 2.0, 1.0, 3.0, 2.0] + [9999.0] * (capacity - n)  # tail is stale garbage
    sentinel = -1.0
    out = [sentinel] * capacity

    result = moving_average(samples, 3, n=n, out=out)

    expected = moving_average(samples[:n], 3)
    assert result[:n] == expected
    # Nothing past n-1 was written -- the stale tail was never touched,
    # which also proves it was never read into an accumulating sum either
    # (a window=3 filter reading ahead into the 9999.0 tail would have
    # visibly corrupted the last couple of in-range outputs).
    assert out[n:] == [sentinel] * (capacity - n)


def test_moving_average_window_one_ignores_stale_data_past_n():
    n = 3
    capacity = 6
    samples = [5.0, 6.0, 7.0] + [9999.0] * (capacity - n)
    sentinel = -1.0
    out = [sentinel] * capacity

    result = moving_average(samples, 1, n=n, out=out)

    assert result[:n] == [5.0, 6.0, 7.0]
    assert out[n:] == [sentinel] * (capacity - n)


def test_frequency_from_crossings_matches_known_period():
    crossings = [0.0, 0.02, 0.04, 0.06]  # 20ms period -> 50Hz
    estimates = frequency_from_crossings(crossings)
    assert all(f == pytest.approx(50.0) for _, f in estimates)


def test_find_zero_crossings_ignores_stale_data_past_n():
    ts, ys = generate_synthetic_signal(freq_hz=50.0, fs_hz=4000.0, duration_s=0.1, seed=9)
    n = len(ts)
    capacity = n + 500
    ts_oversized = list(ts) + [ts[-1] + 1000.0] * (capacity - n)  # garbage, way out of order
    ys_oversized = list(ys) + [9999.0] * (capacity - n)           # garbage amplitude

    expected = find_zero_crossings(ts, ys)
    actual = find_zero_crossings(ts_oversized, ys_oversized, n=n)
    assert actual == expected


def test_estimate_frequency_ignores_stale_data_past_n():
    ts, ys = generate_synthetic_signal(freq_hz=50.0, fs_hz=4000.0, duration_s=0.5, seed=10)
    n = len(ts)
    capacity = n + 500
    ts_oversized = list(ts) + [ts[-1] + 1000.0] * (capacity - n)
    ys_oversized = list(ys) + [9999.0] * (capacity - n)

    expected_freq, expected_per_cycle = estimate_frequency(ts, ys)
    actual_freq, actual_per_cycle = estimate_frequency(ts_oversized, ys_oversized, n=n)

    assert actual_freq == expected_freq
    assert actual_per_cycle == expected_per_cycle
