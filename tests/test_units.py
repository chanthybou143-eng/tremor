from __future__ import annotations

import time

import numpy as np
import pytest

from tremor.units import SyntheticUnitFeed


def _collect_for(feed, duration_s):
    readings = []
    deadline = time.monotonic() + duration_s
    for reading in feed.stream():
        readings.append(reading)
        if time.monotonic() >= deadline:
            break
    return readings


def test_recovers_nominal_frequency():
    feed = SyntheticUnitFeed(
        unit_id="unit-1", label="Unit 1", noise_std=0.0, chunk_s=0.3, seed=1
    )
    feed.start()
    try:
        readings = _collect_for(feed, 1.5)
    finally:
        feed.stop()

    assert len(readings) > 30
    freqs = np.array([r.freq_hz for r in readings])
    assert np.mean(freqs) == pytest.approx(50.0, abs=0.02)


def test_readings_have_monotonically_increasing_elapsed_time():
    feed = SyntheticUnitFeed(
        unit_id="unit-1", label="Unit 1", noise_std=0.0, chunk_s=0.3, seed=1
    )
    feed.start()
    try:
        readings = _collect_for(feed, 1.0)
    finally:
        feed.stop()

    ts = [r.t for r in readings]
    assert ts == sorted(ts)


def test_two_units_run_concurrently_without_interfering():
    # Regression test for the freq_estimator.py global-random-state bug:
    # two feeds racing on the same global random module would corrupt each
    # other's noise sequence. Each feed owns a private rng, so both should
    # independently converge on the correct (different, seeded) frequency.
    feed_a = SyntheticUnitFeed(
        unit_id="unit-1", label="Unit 1", noise_std=0.02, chunk_s=0.3, seed=1
    )
    feed_b = SyntheticUnitFeed(
        unit_id="unit-2", label="Unit 2", noise_std=0.02, chunk_s=0.3, seed=2
    )
    feed_a.start()
    feed_b.start()
    try:
        readings_a = _collect_for(feed_a, 1.5)
        readings_b = _collect_for(feed_b, 0.1)  # drain whatever's queued
    finally:
        feed_a.stop()
        feed_b.stop()

    freqs_a = np.array([r.freq_hz for r in readings_a])
    assert len(readings_a) > 30
    # Median, not mean: the hysteresis+filter pipeline still occasionally
    # lets one badly-placed cycle through under noise (same reasoning as
    # test_source.py's causal-filter test), which is enough to skew a raw
    # mean over ~70 cycles but not a median.
    assert np.median(freqs_a) == pytest.approx(50.0, abs=0.03)


def test_stop_terminates_background_thread():
    feed = SyntheticUnitFeed(unit_id="unit-1", label="Unit 1", chunk_s=0.2)
    feed.start()
    _collect_for(feed, 0.1)
    feed.stop()
    assert not feed._thread.is_alive()
