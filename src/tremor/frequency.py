"""Zero-crossing frequency estimation.

Pure numpy, no filtering, no assumptions beyond "samples are a roughly
sinusoidal waveform crossing zero once per cycle". This module is meant to
stay small enough to eventually port to MicroPython running on the Pico, so
any conditioning (e.g. harmonic rejection) is deliberately kept out of it --
see ``tremor.filters`` for the offline preprocessing step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class FrequencyEstimate:
    t: np.ndarray
    freq_hz: np.ndarray


@dataclass
class FreqSample:
    """A single timestamped frequency estimate, as produced one at a time by
    a live/streaming source (as opposed to ``FrequencyEstimate``, which holds
    a whole batch)."""

    t: float
    freq_hz: float


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


class StreamingZeroCrossingDetector:
    """Causal, sample-at-a-time counterpart to
    ``estimate_frequency_zero_crossing``.

    Offline, noise-induced spurious crossings are handled by taking a median
    over a batch of estimates (see ``tests/test_frequency.py``). A live
    source has no buffer of future cycles to take a median over, so instead
    a minimum-interval guard rejects any rising crossing that arrives too
    soon after the last *accepted* one -- ``min_interval_s`` should be well
    under half a mains cycle (a 50 Hz half-cycle is 10 ms) so it only
    rejects spurious crossings, never real ones.
    """

    def __init__(self, min_interval_s: float = 0.015):
        self.min_interval_s = min_interval_s
        self._prev_t: Optional[float] = None
        self._prev_x: Optional[float] = None
        self._last_accepted_t: Optional[float] = None

    def process_sample(self, t: float, x: float) -> Optional[FreqSample]:
        """Feed one new sample in. Returns a ``FreqSample`` when this sample
        completes a cycle against the last accepted crossing, else None."""
        result: Optional[FreqSample] = None

        if self._prev_x is not None and self._prev_x < 0.0 <= x:
            frac = -self._prev_x / (x - self._prev_x)
            crossing_t = self._prev_t + frac * (t - self._prev_t)

            if (
                self._last_accepted_t is None
                or (crossing_t - self._last_accepted_t) >= self.min_interval_s
            ):
                if self._last_accepted_t is not None:
                    interval = crossing_t - self._last_accepted_t
                    t_est = (self._last_accepted_t + crossing_t) / 2.0
                    result = FreqSample(t=t_est, freq_hz=1.0 / interval)
                self._last_accepted_t = crossing_t
            # else: spurious crossing too close to the last one -- rejected,
            # last_accepted_t is left unchanged.

        self._prev_t, self._prev_x = t, x
        return result
