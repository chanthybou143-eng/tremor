"""Zero-crossing frequency estimation.

Pure numpy, no filtering, no assumptions beyond "samples are a roughly
sinusoidal waveform crossing zero once per cycle". This module is meant to
stay small enough to eventually port to MicroPython running on the Pico, so
any conditioning (e.g. harmonic rejection) is deliberately kept out of it --
see ``tremor.filters`` for the offline preprocessing step.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class FrequencyEstimate:
    t: np.ndarray
    freq_hz: np.ndarray


def find_rising_crossings(t: np.ndarray, samples: np.ndarray) -> np.ndarray:
    """Linearly-interpolated times of rising (negative-to-positive) zero
    crossings in ``samples``."""
    t = np.asarray(t, dtype=float)
    samples = np.asarray(samples, dtype=float)

    below = samples < 0.0
    at_or_above = ~below
    rising = below[:-1] & at_or_above[1:]
    idx = np.nonzero(rising)[0]

    x0, x1 = samples[idx], samples[idx + 1]
    t0, t1 = t[idx], t[idx + 1]
    frac = -x0 / (x1 - x0)
    return t0 + frac * (t1 - t0)


def estimate_frequency_zero_crossing(
    t: np.ndarray, samples: np.ndarray
) -> FrequencyEstimate:
    """Estimate frequency per cycle from rising zero-crossing intervals.

    Returns one estimate per consecutive pair of crossings, timestamped at
    the midpoint of the pair. ``samples`` is used as given -- callers who
    need to reject harmonics/noise should filter first.
    """
    crossings = find_rising_crossings(t, samples)
    if crossings.size < 2:
        raise ValueError(
            "need at least 2 zero crossings to estimate frequency, "
            f"found {crossings.size}"
        )

    intervals = np.diff(crossings)
    freq_hz = 1.0 / intervals
    t_est = (crossings[:-1] + crossings[1:]) / 2.0
    return FrequencyEstimate(t=t_est, freq_hz=freq_hz)
