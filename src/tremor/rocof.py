"""Rate-of-change-of-frequency (RoCoF) from a window of frequency estimates.

RoCoF is the slope (Hz/s) of frequency vs. time. A least-squares linear fit
over a short trailing window is the simplest estimator and is what both the
dashboard and future inertia-estimation work will start from.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


def rocof_from_window(t: np.ndarray, freq_hz: np.ndarray) -> Optional[float]:
    """Least-squares slope of ``freq_hz`` vs. ``t``, in Hz/s.

    Returns None if fewer than 2 points are given (a slope is undefined).
    """
    t = np.asarray(t, dtype=float)
    freq_hz = np.asarray(freq_hz, dtype=float)
    if t.size < 2:
        return None

    slope, _intercept = np.polyfit(t, freq_hz, 1)
    return float(slope)
