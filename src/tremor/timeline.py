"""RoCoF over a GPS-time-ordered series of readings.

One implementation shared by the live ``/api/units`` view, the 1-minute
aggregates and the event detector in ``retention.py``, so all three agree.

Ordering is by the readings' own GPS time -- never receipt time -- so late,
retried or out-of-order batches land where they belong. A RoCoF fit is only
allowed to use points within ``max_gap_s`` of the newest point, and never
across a ``boot_id`` change (a device restart is a discontinuity: the fit
would otherwise bridge two unrelated timelines). The rule is the same as the
one webapp.py's ``_fit_time_for_point`` applies to live GPS-stamped readings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Same values as webapp.MAX_ROCOF_GAP_S / ROCOF_PLAUSIBILITY_LIMIT_HZ_S (webapp
# passes its own constants in explicitly; these are just the defaults).
DEFAULT_MAX_GAP_S = 1.5
DEFAULT_PLAUSIBILITY_HZ_S = 5.0


@dataclass(frozen=True)
class RocofSeries:
    points: List[Tuple[float, float]]       # (t_s, slope Hz/s)
    skipped_boundary: int                   # had an in-window neighbour, but across a boot_id change
    skipped_implausible: int                # |slope| above the plausibility backstop


def _slope(ts: Sequence[float], fs: Sequence[float]) -> Optional[float]:
    """Least-squares slope, closed form (identical to np.polyfit degree 1, but
    O(n) pure Python -- this runs once per reading over whole days)."""
    n = len(ts)
    if n < 2:
        return None
    mt = sum(ts) / n
    mf = sum(fs) / n
    den = sum((t - mt) ** 2 for t in ts)
    if den == 0.0:
        return None
    return sum((t - mt) * (f - mf) for t, f in zip(ts, fs)) / den


def rocof_series(
    points: Sequence[Tuple[float, float, Optional[str]]],
    max_gap_s: float = DEFAULT_MAX_GAP_S,
    plausibility_hz_s: float = DEFAULT_PLAUSIBILITY_HZ_S,
) -> RocofSeries:
    """``points``: ``(t_s, freq_hz, boot_id_or_None)`` sorted by ``t_s``."""
    out: List[Tuple[float, float]] = []
    skipped_boundary = 0
    skipped_implausible = 0
    for i, (t_i, f_i, b_i) in enumerate(points):
        ts = [t_i]
        fs = [f_i]
        crossed = False
        j = i - 1
        while j >= 0 and t_i - points[j][0] <= max_gap_s:
            t_j, f_j, b_j = points[j]
            if t_i - t_j <= 0:            # identical time: not a usable second point
                j -= 1
                continue
            if b_i is not None and b_j is not None and b_i != b_j:
                crossed = True
                break
            ts.append(t_j)
            fs.append(f_j)
            j -= 1
        if len(ts) >= 2:
            slope = _slope(ts, fs)
            if slope is None:
                continue
            if abs(slope) <= plausibility_hz_s:
                out.append((t_i, slope))
            else:
                skipped_implausible += 1
        elif crossed:
            skipped_boundary += 1
    return RocofSeries(out, skipped_boundary, skipped_implausible)
