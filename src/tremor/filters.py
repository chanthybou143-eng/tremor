"""Offline signal conditioning.

Uses scipy, which is not available on the target Pico hardware, and
``filtfilt``, which is non-causal (it needs the whole buffer, run forwards
and backwards, to cancel phase distortion). Both make this module suitable
only for offline analysis of recorded/synthetic buffers -- not for firmware.
A causal single-pole IIR is the intended on-device replacement when
firmware work starts; that substitution should not require any change to
``tremor.frequency``.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, filtfilt


def lowpass_filtfilt(
    t: np.ndarray, samples: np.ndarray, cutoff_hz: float, order: int = 4
) -> np.ndarray:
    """Zero-phase Butterworth low-pass filter.

    Sample rate is inferred from ``t`` (assumed uniformly spaced). Zero-phase
    filtering (via ``filtfilt``) avoids shifting the zero crossings that
    ``tremor.frequency`` depends on, at the cost of requiring the entire
    buffer up front.
    """
    t = np.asarray(t, dtype=float)
    sample_rate_hz = 1.0 / np.mean(np.diff(t))
    nyquist_hz = sample_rate_hz / 2.0

    b, a = butter(order, cutoff_hz / nyquist_hz, btype="low")
    return filtfilt(b, a, samples)
