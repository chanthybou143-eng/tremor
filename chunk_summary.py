"""On-device (MicroPython-safe) per-chunk frequency/amplitude summary --
the standalone-Pico equivalent of overnight_log.py's per-chunk processing,
but using freq_estimator.moving_average as the pre-filter instead of
overnight_log.py's scipy Butterworth (scipy doesn't exist under
MicroPython -- see CLAUDE.md's portability boundary). Pure Python, no
numpy/scipy/statistics, so it runs unmodified on the Pico and is testable
under desktop Python (see tests/test_chunk_summary.py).

Used by wifi_unit_client.py once per second's worth of raw ADC samples --
the same cadence and (frequency_hz, amplitude_v) shape overnight_log.py's
CSV rows already use, so numbers collected this way are directly
comparable to the laptop-tethered pipeline's.
"""

import math

from freq_estimator import estimate_frequency, moving_average


class DegenerateTimestampsError(ValueError):
    """Raised when a chunk has fewer than 2 timestamps, or its first and
    last timestamps are equal (a non-positive span) -- can't derive a
    sample rate from it. Subclasses ValueError so any caller that already
    catches ValueError broadly (the existing "too few zero crossings"
    contract below) keeps working unchanged; callers that want to count
    or log this specific, rarer failure mode separately can catch this
    subclass first. See wifi_unit_client.py's dup_timestamp_count for why
    this needs to be distinguishable: a duplicate/degenerate timestamp
    caused a real production crash (ZeroDivisionError, since fixed) and
    is worth tracking on its own, not folding into the routine
    "too few crossings" skip path.
    """

HYSTERESIS_FRACTION = 0.05  # of amplitude -- same constant overnight_log.py uses

# Chosen so a boxcar of this length nulls the ~150Hz 3rd harmonic at the
# real ~1030Hz ADC rate: a boxcar's frequency response has zeros at
# multiples of fs/N, so N = round(1030 / 150) ~= 7 samples ~= 0.0068s.
# Unlike overnight_log.py's 4th-order Butterworth (flat passband, steep
# cutoff), a boxcar this short is *not* flat below its null -- at N=7 it
# attenuates the 50Hz fundamental itself by ~18% (measured empirically:
# see test_chunk_summary.py and _boxcar_gain() below, which corrects for
# exactly this so amplitude_v isn't systematically low). Frequency
# estimation is far less sensitive to this than amplitude is, since it
# only needs zero crossings, not absolute magnitude. Starting point, not a
# calibration -- retune against real ADC data once hardware is available.
FILTER_WINDOW_S = 0.0068

NOMINAL_GRID_FREQ_HZ = 50.0  # for the gain-compensation formula below --
                              # real grid frequency deviates from this by at
                              # most a few hundred mHz, negligible effect on
                              # a boxcar's gain at a fixed window length


def _boxcar_gain(window_samples, sample_rate_hz, freq_hz=NOMINAL_GRID_FREQ_HZ):
    """Magnitude response of an N-sample boxcar moving average at freq_hz,
    used to compensate the amplitude estimate for the attenuation the
    filter itself introduces at the fundamental (see FILTER_WINDOW_S).
    """
    if window_samples <= 1:
        return 1.0
    theta = math.pi * freq_hz / sample_rate_hz
    return abs(math.sin(window_samples * theta) / (window_samples * math.sin(theta)))


def _mean(values):
    return sum(values) / len(values)


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def summarize_chunk(timestamps, raw_voltages,
                     hysteresis_fraction=HYSTERESIS_FRACTION,
                     filter_window_s=FILTER_WINDOW_S,
                     n=None, filtered_buf=None):
    """Reduce one chunk's worth of (timestamps, raw_voltages) -- ~1s of
    samples, same as overnight_log.py's CHUNK_S -- to (frequency_hz,
    amplitude_v), mirroring overnight_log.py's dc_offset -> amplitude ->
    hysteresis -> estimate_frequency chain.

    Raises DegenerateTimestampsError (a ValueError subclass) if the chunk
    has fewer than 2 timestamps, or a non-positive span (first == last --
    e.g. two ring-buffer entries landed on an identical elapsed-time value).
    Previously this computed the rate from just the first two timestamps
    (1.0 / (timestamps[1] - timestamps[0])), which crashed with
    ZeroDivisionError in production when those two specific samples
    happened to collide, even though the rest of the chunk was fine.
    Using the whole chunk's span is both more robust (a single early
    collision no longer zeroes out the whole calculation, since the
    chunk's overall span stays positive) and a more accurate rate
    estimate in the general case (averaged over ~1s of samples, not just
    the first gap).

    Also raises ValueError (propagated from estimate_frequency, not this
    class) if fewer than 2 zero crossings are found -- same "don't
    silently return a bogus reading" contract as estimate_frequency
    itself; callers should skip this chunk rather than treat it as a
    reading of 0. Both are ValueError, so a caller that doesn't care to
    distinguish them can catch just ValueError, same as before.

    n/filtered_buf (both optional, default None): a caller that reuses
    fixed-capacity buffers across chunks (see wifi_unit_client.py -- this
    is the fix for the MemoryError crash loop caused by building a fresh
    ~1030-element list every ~1s) passes the *valid length* of `timestamps`/
    `raw_voltages` as `n` (they may be longer, oversized buffers with stale
    data past index n-1) and a reusable scratch list as `filtered_buf` for
    moving_average to write into instead of allocating. Every read of
    `timestamps`/`raw_voltages`/the filtered signal below is bounded to
    `n`, so stale data past that index is never touched. Default behaviour
    (n=None) is unchanged from before this parameter existed: n is taken
    from len(timestamps) and no buffer is reused.
    """
    reusing_buffers = n is not None
    if n is None:
        n = len(timestamps)
    if n < 2:
        raise DegenerateTimestampsError(
            "need at least 2 timestamps to compute a sample rate, got {}".format(n))
    ts = timestamps[:n] if reusing_buffers else timestamps
    span = ts[-1] - ts[0]
    if span <= 0:
        raise DegenerateTimestampsError(
            "non-positive timestamp span ({}) across {} samples -- "
            "first={} last={}".format(span, n, ts[0], ts[-1]))
    sample_rate_hz = (n - 1) / span
    window_samples = max(1, round(filter_window_s * sample_rate_hz))
    filtered_out = moving_average(raw_voltages, window_samples, n=n, out=filtered_buf)
    filtered = filtered_out[:n] if reusing_buffers else filtered_out

    dc_offset = _mean(filtered)
    # Accumulator loop instead of _mean([(v-dc_offset)**2 for v in
    # filtered]): that listcomp is exactly what crashed most often in
    # production (chunk_summary.py:121, 14 of 19 MemoryErrors in a single
    # soak) -- MicroPython grows a listcomp's result via repeated append
    # the same as a manual loop, so even though `filtered` here is already
    # a right-sized, already-allocated list, building a *second* ~1030
    # element list right next to it (both alive at once, mid-chunk, right
    # after `filtered` and `raw_voltages` were themselves just built) is
    # exactly the kind of back-to-back large-allocation burst that trips
    # the doubling-growth allocator under fragmentation. An accumulator
    # never allocates a list at all, regardless of whether filtered_buf is
    # in use, so this fix applies unconditionally, not just in reuse mode.
    variance_acc = 0.0
    for v in filtered:
        variance_acc += (v - dc_offset) ** 2
    variance = variance_acc / len(filtered)
    amplitude = (2 * variance) ** 0.5 / _boxcar_gain(window_samples, sample_rate_hz)
    hysteresis = hysteresis_fraction * amplitude

    _mean_freq, per_cycle = estimate_frequency(
        ts, filtered, dc_offset=dc_offset,
        hysteresis=hysteresis, filter_window_s=0.0,
    )
    frequency_hz = _median([f for _, f in per_cycle])
    return frequency_hz, amplitude
