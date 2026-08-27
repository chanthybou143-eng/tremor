"""Signal conditioning: one offline, one on-device.

``lowpass_filtfilt`` uses scipy and is non-causal (needs the whole buffer,
run forwards and backwards, to cancel phase distortion) -- suitable only for
offline analysis of recorded/synthetic buffers, not for firmware. scipy is
imported lazily inside that function (not at module level) precisely so
that importing this module -- and using ``SinglePoleLowPass`` -- never
requires scipy.

``SinglePoleLowPass`` is the causal, sample-at-a-time replacement: a
first-order RC low-pass, pure Python arithmetic (no numpy, no scipy), so it
runs on the Pico under MicroPython. It's what live sources (``source.py``)
condition samples with before they reach the streaming zero-crossing
detector.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


def lowpass_filtfilt(
    t: np.ndarray, samples: np.ndarray, cutoff_hz: float, order: int = 4
) -> np.ndarray:
    """Zero-phase Butterworth low-pass filter.

    Sample rate is inferred from ``t`` (assumed uniformly spaced). Zero-phase
    filtering (via ``filtfilt``) avoids shifting the zero crossings that
    ``tremor.frequency`` depends on, at the cost of requiring the entire
    buffer up front.
    """
    from scipy.signal import butter, filtfilt

    t = np.asarray(t, dtype=float)
    sample_rate_hz = 1.0 / np.mean(np.diff(t))
    nyquist_hz = sample_rate_hz / 2.0

    b, a = butter(order, cutoff_hz / nyquist_hz, btype="low")
    return filtfilt(b, a, samples)


class SinglePoleLowPass:
    """Causal first-order (RC) low-pass filter, one sample at a time.

    ``y[n] = y[n-1] + alpha * (x[n] - y[n-1])``, with ``alpha`` derived from
    the cutoff and sample rate. Cheaper and less aggressive than the offline
    Butterworth+filtfilt (single pole vs. 4th order, plus the phase lag
    inherent to any causal filter), but it needs only the current sample and
    one running value, which is what makes it viable on-device.
    """

    def __init__(self, cutoff_hz: float, sample_rate_hz: float):
        rc = 1.0 / (2.0 * math.pi * cutoff_hz)
        dt = 1.0 / sample_rate_hz
        self.alpha = dt / (rc + dt)
        self._y: Optional[float] = None

    def process_sample(self, x: float) -> float:
        if self._y is None:
            self._y = x
        else:
            self._y = self._y + self.alpha * (x - self._y)
        return self._y
