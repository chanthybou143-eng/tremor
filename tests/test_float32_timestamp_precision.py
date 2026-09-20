"""Simulates, on the host, the float32-precision question behind
wifi_unit_client.py's chunk-relative timestamp change: storing a
session-cumulative elapsed-seconds value (as the code did before this
change) in a single-precision buffer loses precision as the session
runs longer, because float32's step size (ULP) grows with magnitude
while the underlying ~970us ADC sample interval (1/ADC_SAMPLE_HZ at
1030Hz) does not. A chunk-relative value never exceeds roughly CHUNK_S
(~1s) regardless of session length, where float32's step size is
negligible.

wifi_unit_client.py itself can't be imported/tested on the host (it
requires MicroPython-only network/machine modules), so this simulates
the effect directly: take a normal chunk of (timestamps, samples) from
the same synthetic generator every other test in this repo uses, round
the timestamps to float32 precision the way a real array.array('f', ...)
buffer would, and compare the resulting frequency estimate against the
same chunk's timestamps kept chunk-relative (magnitude < ~1.2s) instead
of offset by a large cumulative elapsed time.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chunk_summary import summarize_chunk  # noqa: E402
from freq_estimator import generate_synthetic_signal  # noqa: E402

FS_HZ = 1030.0


def _to_float32(x: float) -> float:
    """Round-trip x through IEEE-754 single precision, same lossy
    truncation array.array('f', ...) applies on assignment."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _summarize_at_offset(ts, ys, offset_s):
    """Simulate storing (offset_s + t) for each chunk timestamp t into a
    float32 buffer -- the old, session-cumulative approach -- and running
    summarize_chunk on the resulting (lossy) timestamps."""
    ts_f32 = [_to_float32(offset_s + t) for t in ts]
    return summarize_chunk(ts_f32, ys)


def _summarize_chunk_relative_f32(ts, ys):
    """Simulate the fix: timestamps stay chunk-relative (no large offset
    added) before being rounded to float32."""
    ts_f32 = [_to_float32(t) for t in ts]
    return summarize_chunk(ts_f32, ys)


@pytest.mark.parametrize("hours,expected_ulp_ms", [(1, 0.2441), (10, 3.9062)])
def test_float32_step_size_at_1h_and_10h_matches_computed_ulp(hours, expected_ulp_ms):
    # Ground the test's offsets in the actual float32 ULP at these
    # magnitudes, computed directly rather than assumed, so the
    # parametrized offsets below are known to be realistic.
    seconds = hours * 3600.0
    f32_val = _to_float32(seconds)
    next_up = struct.unpack("<f", struct.pack("<I", struct.unpack("<I", struct.pack("<f", f32_val))[0] + 1))[0]
    ulp_ms = (next_up - f32_val) * 1000
    assert ulp_ms == pytest.approx(expected_ulp_ms, abs=0.001)


def test_chunk_relative_timestamps_are_numerically_equivalent_at_short_uptime():
    # At ~0 offset (process just started), the old (cumulative) and new
    # (chunk-relative) approaches are the same computation -- this proves
    # the change doesn't alter behaviour when there's nothing to fix yet.
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, noise_std=0.01, seed=20,
    )
    freq_at_zero_offset, amp_at_zero_offset = _summarize_at_offset(ts, ys, offset_s=0.0)
    freq_relative, amp_relative = _summarize_chunk_relative_f32(ts, ys)
    assert freq_relative == freq_at_zero_offset
    assert amp_relative == amp_at_zero_offset


def test_frequency_error_at_1h_and_10h_before_and_after_the_fix():
    ts, ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0,
        amplitude=0.72, dc_offset=1.65, noise_std=0.01, seed=21,
    )
    true_freq_hz = 50.0

    # "After": chunk-relative, regardless of session length -- this is
    # what wifi_unit_client.py now actually stores.
    freq_after, _ = _summarize_chunk_relative_f32(ts, ys)
    error_after = abs(freq_after - true_freq_hz)

    print("\nfrequency error, chunk-relative (session-length-independent): "
          "{:.6f} Hz".format(error_after))

    for hours in (1, 10):
        offset_s = hours * 3600.0
        try:
            freq_before, _ = _summarize_at_offset(ts, ys, offset_s)
            error_before = abs(freq_before - true_freq_hz)
            print("frequency error, cumulative @ {}h offset (old behaviour): "
                  "{:.6f} Hz".format(hours, error_before))
        except Exception as exc:  # noqa: BLE001 -- report and re-raise, don't hide the crash
            print("cumulative @ {}h offset (old behaviour): raised {}: {}".format(
                hours, type(exc).__name__, exc))
            raise

        # The whole point of the fix: the old approach's error grows with
        # session length; the new one's does not. This assertion is the
        # actual regression guard -- the printed numbers above are for a
        # human to read in -s output, this is what CI enforces.
        assert error_after <= error_before + 1e-9, (
            "chunk-relative timestamps should never be *worse* than "
            "session-cumulative ones at {}h of uptime".format(hours)
        )


def test_ten_hour_cumulative_timestamps_can_collide_or_reverse():
    # At 10h, float32's ULP (~3.9ms) exceeds the ~970us raw ADC sample
    # interval -- consecutive samples' cumulative timestamps can round to
    # the *same* float32 value, or even go non-monotonic across a chunk.
    # This is the concrete failure mode the chunk-relative fix avoids,
    # not just a precision nicety.
    ts, _ys = generate_synthetic_signal(
        freq_hz=50.0, fs_hz=FS_HZ, duration_s=1.0, seed=22,
    )
    offset_s = 10 * 3600.0
    ts_f32 = [_to_float32(offset_s + t) for t in ts]

    non_increasing = sum(
        1 for i in range(1, len(ts_f32)) if ts_f32[i] <= ts_f32[i - 1]
    )
    assert non_increasing > 0, (
        "expected at least one collided/reversed timestamp pair at 10h of "
        "cumulative float32 elapsed time -- if this fails, the ULP no "
        "longer exceeds the sample interval and the precision concern "
        "may no longer apply"
    )

    # chunk-relative timestamps at the same chunk never have this problem.
    ts_relative_f32 = [_to_float32(t) for t in ts]
    non_increasing_relative = sum(
        1 for i in range(1, len(ts_relative_f32)) if ts_relative_f32[i] <= ts_relative_f32[i - 1]
    )
    assert non_increasing_relative == 0
