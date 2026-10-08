"""adc_chunker.ChunkBuilder: chunks close at exactly 1 s (per sample, not per 128-sample batch), no sample is
lost or duplicated, and the result does not depend on how the main loop happens to batch the drains."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adc_chunker import ChunkBuilder  # noqa: E402

TICKS_MASK = (1 << 30) - 1                     # MicroPython ticks_us wraps at 2**30
FS = 1030.0
INTERVAL_US = 1e6 / FS


def wrap_diff(a, b):
    return ((a - b + (1 << 29)) & TICKS_MASK) - (1 << 29)


def stream(seconds, start=0, jitter_us=3, gaps=(), seed=1):
    """ticks_us of samples at ~1030 Hz (with timer jitter), wrapped like the device, minus any
    (from_s, to_s) gaps (samples the ring overflowed)."""
    rng = random.Random(seed)
    out, k = [], 0
    while True:
        t = k * INTERVAL_US
        if t > seconds * 1e6:
            return out
        if not any(a * 1e6 <= t < b * 1e6 for a, b in gaps):
            out.append((start + int(round(t)) + rng.randint(-jitter_us, jitter_us)) & TICKS_MASK)
        k += 1


def run(samples, batches, capacity=1200, ring_capacity=12360):
    """Feed samples through a real ring in pseudo-random batch sizes from `batches`, draining like the main
    loop (max 128 per pass). Returns the chunks as (first_tick, last_tick, n, span_s, ticks list)."""
    ring_t = [0] * ring_capacity
    ring_r = [0] * ring_capacity
    w = r = 0
    cb = ChunkBuilder(capacity, 1_000_000, "f", wrap_diff, samples[0])
    chunks, i, it = [], 0, iter(batches)
    while i < len(samples) or r != w:
        for _ in range(next(it)):                       # the ISR adds a burst of samples (never past a full ring)
            if i < len(samples) and (w + 1) % ring_capacity != r:
                ring_t[w], ring_r[w] = samples[i], (i * 37) & 0xFFFF
                w = (w + 1) % ring_capacity
                i += 1
        r = cb.drain(ring_t, ring_r, r, w, ring_capacity, 128)
        if cb.complete:
            chunks.append((cb.ticks[0], cb.ticks[cb.n - 1], cb.n, cb.ts_s[cb.n - 1], list(cb.ticks[:cb.n])))
            cb.reset()
    return chunks, cb


def cyc(*sizes):
    while True:
        yield from sizes


@pytest.mark.parametrize("batches", [cyc(1), cyc(7), cyc(128), cyc(1000), cyc(3, 250, 1, 128, 4000)])
def test_every_chunk_closes_within_one_sample_of_one_second_whatever_the_batching(batches):
    chunks, _ = run(stream(30), batches)
    full = chunks[:-1] if chunks[-1][3] < 1.0 else chunks
    assert len(full) >= 28
    for _first, _last, n, span, _t in full:
        assert 1.0 <= span < 1.0 + (INTERVAL_US + 4) / 1e6, span          # was up to 1.124 s with 128-sample batches
        assert n in (1031, 1032)


def test_chunking_is_identical_for_any_batching():
    s = stream(20)
    ref = run(s, cyc(1))[0]
    for b in (cyc(5), cyc(128), cyc(4096), cyc(2, 600, 17)):
        assert run(s, b)[0] == ref


def test_no_sample_is_lost_or_duplicated_and_chunks_are_contiguous():
    s = stream(15)
    chunks, _ = run(s, cyc(128, 3, 900))
    flat = [t for c in chunks for t in c[4]]
    assert flat == s[:len(flat)] and len(s) - len(flat) < 1032         # only the unfinished last chunk is left out


def test_readings_are_one_second_apart_so_about_600_per_10_minutes():
    chunks, _ = run(stream(600), cyc(128))
    lasts = [c[1] for c in chunks]
    d = [wrap_diff(b, a) for a, b in zip(lasts, lasts[1:])]
    assert all(1_000_000 <= x <= 1_000_000 + 2 * INTERVAL_US + 10 for x in d)
    assert 598 <= len(chunks) <= 600


def test_ticks_wraparound_does_not_disturb_chunking():
    s = stream(10, start=TICKS_MASK - 3_000_000)                         # wraps ~3 s in
    chunks, cb = run(s, cyc(128))
    assert all(1.0 <= c[3] < 1.001 for c in chunks[:-1])
    assert abs(cb.elapsed_us - 10_000_000) < 2_000


def test_a_gap_from_ring_overflow_closes_the_chunk_at_the_first_sample_past_one_second():
    chunks, _ = run(stream(10, gaps=[(3.2, 3.5)]), cyc(128))
    spans = [c[3] for c in chunks[:-1]]
    assert all(1.0 <= x < 1.001 for x in spans)                         # the gap is inside a chunk, not stretching it
    assert sum(c[2] for c in chunks) < sum(1 for _ in stream(10)) - 300  # the ~309 overflowed samples are missing


def test_a_chunk_that_fills_before_one_second_is_closed_and_counted():
    fast = [int(i * 500) & TICKS_MASK for i in range(5000)]             # 2 kHz: 1200 samples = 0.6 s
    chunks, cb = run(fast, cyc(128), capacity=1200)
    assert all(c[2] == 1200 for c in chunks) and cb.capacity_closes == len(chunks) >= 4


def test_drain_does_nothing_until_a_complete_chunk_is_reset():
    s = stream(3)
    ring = list(s) + [0] * 10
    cb = ChunkBuilder(1200, 1_000_000, "f", wrap_diff, s[0])
    r = 0
    while not cb.complete:
        r = cb.drain(ring, [0] * len(ring), r, len(s), len(ring), 128)
    assert cb.drain(ring, [0] * len(ring), r, len(s), len(ring), 128) == r
    cb.reset()
    assert cb.drain(ring, [0] * len(ring), r, len(s), len(ring), 128) != r


def test_timestamps_are_chunk_relative_and_start_at_zero():
    chunks, _ = run(stream(5, start=987_654_321), cyc(128))
    assert all(1.0 <= c[3] < 1.001 for c in chunks[:-1])                 # relative span, not session time


def test_the_client_uses_the_builder_and_a_12_second_ring():
    src = (ROOT / "wifi_unit_client.py").read_text()
    assert "read_idx = chunker.drain(ring_ticks, ring_raw, read_idx, write_idx, RING_CAPACITY, MAX_DRAIN_PER_PASS)" in src
    assert "chunker.reset()" in src and "RING_CAPACITY = 12360" in src
    assert "_elapsed_us_total" not in src
