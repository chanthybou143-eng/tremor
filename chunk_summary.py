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
                     filter_window_s=FILTER_WINDOW_S):
    """Reduce one chunk's worth of (timestamps, raw_voltages) -- ~1s of
    samples, same as overnight_log.py's CHUNK_S -- to (frequency_hz,
    amplitude_v), mirroring overnight_log.py's dc_offset -> amplitude ->
    hysteresis -> estimate_frequency chain.

    Raises ValueError (propagated from estimate_frequency) if fewer than 2
    zero crossings are found in this chunk -- same "don't silently return
    a bogus reading" contract as estimate_frequency itself; callers should
    skip this chunk rather than treat it as a reading of 0.
    """
    sample_rate_hz = 1.0 / (timestamps[1] - timestamps[0])
    window_samples = max(1, round(filter_window_s * sample_rate_hz))
    filtered = moving_average(raw_voltages, window_samples)

    dc_offset = _mean(filtered)
    variance = _mean([(v - dc_offset) ** 2 for v in filtered])
    amplitude = (2 * variance) ** 0.5 / _boxcar_gain(window_samples, sample_rate_hz)
    hysteresis = hysteresis_fraction * amplitude

    _mean_freq, per_cycle = estimate_frequency(
        timestamps, filtered, dc_offset=dc_offset,
        hysteresis=hysteresis, filter_window_s=0.0,
    )
    frequency_hz = _median([f for _, f in per_cycle])
    return frequency_hz, amplitude
