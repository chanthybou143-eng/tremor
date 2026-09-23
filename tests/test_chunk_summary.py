from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chunk_summary import DegenerateTimestampsError, summarize_chunk  # noqa: E402
from freq_estimator import generate_synthetic_signal  # noqa: E402

# Pico's real measured ADC rate (see CLAUDE.md) -- exercising this at the
# actual target rate, not an arbitrary offline-DSP rate, is the whole point
# of this test (same reasoning as tests/test_frequency.py's sample_rate_hz
# parametrization).
FS_HZ = 1030.0


def test_recovers_clean_frequency_with_realistic_dc_offset():
    # amplitude/dc_offset in the ballpark of the real divider circuit's
    # measured ~0.7V swing riding on a ~1.65V mid-rail bias (see CLAUDE.md).
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, seed=1,
    )
    freq_hz, amplitude_v = summarize_chunk(ts, ys)
    assert freq_hz == pytest.approx(50.0, abs=0.05)
    assert amplitude_v == pytest.approx(0.72, rel=0.1)


def test_recovers_off_nominal_frequency():
    ts, ys = generate_synthetic_signal(
        freq_hz=49.85, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, seed=2,
    )
    freq_hz, _amplitude_v = summarize_chunk(ts, ys)
    assert freq_hz == pytest.approx(49.85, abs=0.05)


def test_survives_realistic_noise():
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, noise_std=0.01, seed=3,
    )
    freq_hz, _amplitude_v = summarize_chunk(ts, ys)
    assert freq_hz == pytest.approx(50.0, abs=0.05)


def test_raises_on_too_few_crossings():
    # hysteresis larger than the signal's own amplitude arms no crossings
    # -- same failure mode estimate_frequency itself documents.
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, seed=4,
    )
    with pytest.raises(ValueError):
        summarize_chunk(ts, ys, hysteresis_fraction=50.0)


def test_survives_a_duplicate_leading_timestamp():
    # Regression test for the production crash: two ring-buffer entries
    # landed on an identical elapsed-time value (see wifi_unit_client.py's
    # dup_timestamp_count / chunk_summary.py's docstring). The *old* code
    # computed the sample rate from just timestamps[1]-timestamps[0] and
    # raised a bare ZeroDivisionError here. A collision confined to the
    # first two samples shouldn't fail the whole chunk -- the rest of the
    # ~1s of samples is still perfectly good data.
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, seed=5,
    )
    ts = list(ts)
    ts[1] = ts[0]  # duplicate the second timestamp onto the first -- the exact collision shape observed in production

    freq_hz, amplitude_v = summarize_chunk(ts, ys)
    assert freq_hz == pytest.approx(50.0, abs=0.05)
    assert amplitude_v == pytest.approx(0.72, rel=0.1)


def test_raises_degenerate_timestamps_error_when_whole_chunk_is_identical():
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, seed=6,
    )
    ts = [ts[0]] * len(ts)  # every timestamp identical -- zero span across the whole chunk

    with pytest.raises(DegenerateTimestampsError):
        summarize_chunk(ts, ys)


def test_raises_degenerate_timestamps_error_on_too_few_timestamps():
    with pytest.raises(DegenerateTimestampsError):
        summarize_chunk([0.0], [1.65])


# --- Reused fixed-capacity buffers (the MemoryError-crash-loop fix) -------
#
# wifi_unit_client.py's real fix is to stop building fresh ~1030-element
# lists every chunk and instead reuse fixed-capacity buffers, passing the
# chunk's *valid* length as `n` -- these buffers can be longer than n, with
# stale data from a previous chunk past index n-1. The tests below cover
# both required properties: numeric equivalence with the old/default
# behaviour on valid data, and correctness when the buffers are oversized.

def _oversized(seq, capacity, filler=9999.0):
    return list(seq) + [filler] * (capacity - len(seq))


def test_reused_buffers_match_default_path_numerically():
    # Same recorded-shape chunk (synthetic, at the real measured ADC rate
    # -- no raw recorded ADC payloads exist anywhere in this repo or its
    # logs to replay instead; this is the same generator every other test
    # in this file already validates the pipeline against), run through
    # both the default (fresh-list) path and the reused-buffer path. The
    # two must agree exactly: this is purely a memory-allocation change,
    # not a numeric one.
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, noise_std=0.01, seed=7,
    )
    expected_freq, expected_amp = summarize_chunk(ts, ys)

    n = len(ts)
    filtered_buf = [0.0] * n
    freq, amp = summarize_chunk(ts, ys, n=n, filtered_buf=filtered_buf)

    assert freq == expected_freq
    assert amp == expected_amp


def test_reused_buffers_ignore_stale_data_past_n():
    # timestamps/raw_voltages are oversized (a fixed CHUNK_CAPACITY buffer
    # bigger than this chunk's actual length), with a garbage tail that
    # would produce a wildly different (or degenerate/erroring) result if
    # it were accidentally read. Passing n=<real length> must make the
    # result identical to calling summarize_chunk on the plain, exactly-
    # sized lists.
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, seed=8,
    )
    n = len(ts)
    capacity = n + 200

    expected_freq, expected_amp = summarize_chunk(ts, ys)

    ts_oversized = _oversized(ts, capacity, filler=ts[-1] + 1000.0)  # garbage timestamps, way out of order
    ys_oversized = _oversized(ys, capacity, filler=9999.0)           # garbage voltages
    filtered_buf = [-1.0] * capacity

    freq, amp = summarize_chunk(
        ts_oversized, ys_oversized, n=n, filtered_buf=filtered_buf)

    assert freq == pytest.approx(expected_freq, abs=1e-9)
    assert amp == pytest.approx(expected_amp, abs=1e-9)
    # filtered_buf past n-1 was never written -- proves moving_average
    # (called inside summarize_chunk) respected n too, not just this
    # function's own timestamp/voltage reads.
    assert filtered_buf[n:] == [-1.0] * (capacity - n)
