"""Local web dashboard: frequency + RoCoF per TREMOR unit.

Units are dynamic, not a fixed roster: a unit gets a card the first time
it reports a reading (via a registered feed, or a real POST to
/api/ingest) and never before -- there's no pre-seeded "offline" slot for
a unit that hasn't reported yet. Up to 5 units are planned for the real
network, but nothing here hardcodes that count; a 6th would show up the
same way a 2nd does, with no code change.

Run with ``python -m tremor.webapp`` (or the ``tremor-web-dashboard``
console script), then open http://127.0.0.1:5000/. Swapping a real unit's
serial/network feed in later means implementing ``UnitFeed`` the same way
``SyntheticUnitFeed`` does (see units.py) and registering it in
``create_app`` -- nothing in the state/aggregation/HTTP layer below is
synthetic-specific.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request, send_file

from .ingest import PayloadError, parse_payload
from .retention import RetentionConfig, RetentionEngine, day_to_date
from .rocof import rocof_from_window
from .security import (TOKEN_HEADER, ExportAuth, IngestAuth, RateLimiter, client_ip, parse_rate)
from .store import DAY_US, US, ReadingStore, Row, StoreError, UnitState, open_store
from .timeline import rocof_series
from .units import SyntheticUnitFeed, UnitFeed, UnitReading

log = logging.getLogger("tremor.webapp")

WINDOW_S = 60.0
# Longer than the single-unit matplotlib dashboard's 500ms: per-unit
# readings still arrive roughly once per mains cycle, but this view polls
# at a much coarser ~1s cadence, so a slightly longer fit window keeps the
# RoCoF estimate from being dominated by single-cycle noise.
ROCOF_WINDOW_S = 2.0
# The displayed frequency is a short rolling median, not the latest raw
# single-cycle reading -- otherwise the number visibly jitters on ordinary
# per-cycle noise (the same lesson already applied to the single-unit
# matplotlib dashboard's readout).
READOUT_WINDOW_S = 2.0
# The sparkline is smoothed with the same kind of rolling median, applied
# per-point along the whole series rather than collapsed to one number --
# the raw per-cycle series genuinely does swing +/-0.15Hz cycle to cycle
# (this is real noise, not a plotting bug), which reads as far jumpier
# than the underlying frequency actually is. ~10 cycles at 50Hz.
SPARKLINE_SMOOTHING_WINDOW_S = 0.2
# Real per-reading cadence for an ingested batch -- matches
# wifi_unit_client.py's CHUNK_S and overnight_log.py's CHUNK_S (both 1.0s),
# the one-reading-per-second convention used throughout this project. Used
# to space out a batch's timestamps realistically (see /api/ingest).
READING_INTERVAL_S = 1.0
# The window completeness_pct is measured over -- deliberately the same
# WINDOW_S the rolling buffers themselves retain (60s), not some shorter
# adaptive span: long enough to span several real restart cycles (the
# client's own POST_INTERVAL_S is 30s) without a single missed POST making
# the figure swing wildly, and it matches what the UI already labels
# ("last 60s") so the two agree.
COMPLETENESS_WINDOW_S = WINDOW_S

# RoCoF cross-batch safety (see _fit_time_for_point). A batch/restart
# boundary's reconstructed `t` values are each independently anchored to
# that batch's own receipt time -- see /api/ingest's docstring -- so two
# points from different batches can land far closer together in
# reconstructed time than they really were, even though each side's `t` is
# individually reasonable. This is what produced a real -22.953 Hz/s
# reading in production (see 7714d2a and the incident writeup in
# wifi_unit_client.py's git history): a crash-triggered restart's first
# small batch landed close enough to the tail of the pre-crash batch still
# in the 60s window to fool rocof_from_window. Fixed by: preferring
# per-reading GPS UTC timestamps (a real, independent clock) for the time
# axis whenever both points have one, and refusing to bridge two different
# non-GPS-confirmed batches at all, rather than trusting reconstructed `t`
# across a boundary it was never safe to cross.
# Deliberately tighter than ROCOF_WINDOW_S (which only bounds the initial
# candidate pool -- see add_reading) so bridging a gap requires it to be
# comfortably inside that pool, not merely at its edge: READING_INTERVAL_S
# is 1.0s, so 1.5s already covers a couple of missed/delayed readings
# without approaching ROCOF_WINDOW_S's 2.0s pool radius.
MAX_ROCOF_GAP_S = 1.5
# Generous backstop independent of the above: real grid RoCoF protection
# settings (e.g. AEMO's) sit at a few Hz/s even for extreme contingencies,
# so several Hz/s of margin above that is still "physically impossible if
# exceeded" territory, not a tight clamp on genuine grid events.
ROCOF_PLAUSIBILITY_LIMIT_HZ_S = 5.0
# "Something's actually wrong" cadence for gap detection/chart breaking --
# comfortably above ordinary ~1s jitter, well below a real outage.
GAP_THRESHOLD_S = 5.0
# 3x the real client's POST_INTERVAL_S (30s) -- tolerates one missed POST
# cycle without immediately flagging a unit as stale, but flags a second
# consecutive miss. Status strip/chart greying (see snapshot()) uses this.
STALE_THRESHOLD_S = 90.0

GPS_SECONDS_PER_DAY = 86400.0


def _gps_utc_delta_s(a: float, b: float) -> float:
    """a - b in seconds, folding a UTC-seconds-of-day midnight wraparound
    into (-12h, 12h] -- mirrors pps_time_sync.py's feed_nmea() fold (the
    on-device equivalent); this is the one place the server does its own
    gps_utc_s arithmetic, so the same fold is needed here."""
    delta = a - b
    if delta > GPS_SECONDS_PER_DAY / 2:
        delta -= GPS_SECONDS_PER_DAY
    elif delta < -GPS_SECONDS_PER_DAY / 2:
        delta += GPS_SECONDS_PER_DAY
    return delta


@dataclass(frozen=True)
class _FreqPoint:
    """One stored frequency sample. batch_id/gps_utc_s are None for
    anything that isn't from a real /api/ingest batch (a synthetic feed's
    readings, or a direct _UnitsState.add_reading() call as tests do) --
    those sources produce `t` continuously and natively (never
    reconstructed from a batch's receipt time), so they're always trusted,
    matching this dashboard's behavior before the RoCoF fix below."""
    t: float
    freq_hz: float
    batch_id: Optional[int] = None
    gps_utc_s: Optional[float] = None


def _fit_time_for_point(
    newest: _FreqPoint, point: _FreqPoint, max_gap_s: float
) -> Optional[float]:
    """Returns the time value to use for `point` in a RoCoF least-squares
    fit anchored to `newest`'s own `t` axis, or None if it isn't safe to
    include `point` in the fit at all.

    Preference order: (1) if both points carry a real GPS UTC timestamp,
    use that -- it's an independent, trustworthy clock, immune to the
    batch-reconstruction issue, so it's preferred even for two points in
    the very same batch. (2) Otherwise, if `point` is from the same batch
    as `newest`, or either is from a trusted (non-ingest) source, its
    existing `t` is safe to use directly. (3) Otherwise -- two different
    real-ingest batches with no GPS confirmation available -- refuse;
    there is no safe way to compare their reconstructed timestamps.

    In all cases the resulting span must be positive and within
    max_gap_s, or the point is excluded regardless of source.
    """
    if point is newest:
        return newest.t

    if newest.gps_utc_s is not None and point.gps_utc_s is not None:
        gap = _gps_utc_delta_s(newest.gps_utc_s, point.gps_utc_s)
        if 0 < gap <= max_gap_s:
            return newest.t - gap
        return None  # GPS itself says too far apart (or non-monotonic)

    if newest.batch_id is None or point.batch_id is None or newest.batch_id == point.batch_id:
        if 0 < newest.t - point.t <= max_gap_s:
            return point.t
        return None

    return None  # different real-ingest batches, no GPS to confirm -- refuse

# Small, plausible per-unit calibration/noise variation -- all units track
# the same true_grid_freq_hz() (see units.py), only their independent ADC
# noise and small DC-offset calibration error differ, same as real units
# on a shared grid would.
SIMULATED_UNITS = [
    dict(unit_id="unit-1", noise_std=0.015, dc_offset=0.01, seed=1),
    dict(unit_id="unit-2", noise_std=0.02, dc_offset=-0.02, seed=2),
    dict(unit_id="unit-3", noise_std=0.03, dc_offset=0.0, seed=3),
]


def _default_label(unit_id: str) -> str:
    """Derives a display label straight from unit_id (e.g. "unit-2" ->
    "Unit 2") -- there's no separate label registry, since a unit that
    POSTs to /api/ingest never goes through Python code that could supply
    one explicitly."""
    return unit_id.replace("-", " ").replace("_", " ").title()


def _smoothed_history(
    points: List[Tuple[float, float]], window_s: float = SPARKLINE_SMOOTHING_WINDOW_S
) -> List[Tuple[float, float]]:
    """Rolling median of ``points`` (list of ``(t, freq_hz)``) over a
    trailing ``window_s``, one output point per input point. For display
    only -- a two-pointer sliding window, so O(n) amortized even over the
    full 60s history."""
    ts = [p[0] for p in points]
    fs = [p[1] for p in points]
    smoothed = []
    j = 0
    for i in range(len(points)):
        while ts[i] - ts[j] > window_s:
            j += 1
        smoothed.append((ts[i], statistics.median(fs[j : i + 1])))
    return smoothed


def _find_gaps(points: List[Tuple[float, float]]) -> List[List[float]]:
    """Returns [start_t, end_t] for each consecutive pair of ``points``
    (list of ``(t, freq_hz)``) more than GAP_THRESHOLD_S apart -- server
    is the single source of truth for what counts as a gap (one
    definition, used by both the completeness figure and the frontend's
    chart-breaking), rather than duplicating the threshold in JS."""
    gaps = []
    for (t0, _f0), (t1, _f1) in zip(points, points[1:]):
        if t1 - t0 > GAP_THRESHOLD_S:
            gaps.append([t0, t1])
    return gaps


@dataclass
class _UnitSlot:
    unit_id: str
    label: str
    freq: Deque[_FreqPoint] = field(default_factory=deque)
    rocof: Deque[Tuple[float, float]] = field(default_factory=deque)
    # Only populated by real ingested readings (see /api/ingest below) --
    # SyntheticUnitFeed's readings never carry amplitude_v, so this stays
    # empty for the synthetic units.
    amplitude: Deque[Tuple[float, float]] = field(default_factory=deque)
    last_gps_utc_s: Optional[float] = None
    # Whether the MOST RECENT reading specifically carried a gps_utc_s --
    # distinct from last_gps_utc_s (which only ever updates on a non-None
    # value, so it can't tell you if lock was lost since). This is the
    # honest "is GPS locked right now" signal for the status strip: real,
    # derived from what the device actually reported, not inferred/faked.
    gps_locked: bool = False
    # Counted, not silently dropped -- see _fit_time_for_point/module docstring.
    rocof_skipped_boundary_count: int = 0
    rocof_skipped_implausible_count: int = 0
    # Real wall-clock time.time() at the last add_reading() call --
    # deliberately NOT derived from the reading's own `t`, since `t` is on
    # a different timescale per source (real ingest: time.time()-based;
    # SyntheticUnitFeed: a process-relative elapsed counter starting at 0).
    # Staleness is a wall-clock question ("how long ago did the server
    # actually last hear from this unit") regardless of what timescale the
    # reading itself uses for chart ordering.
    last_seen_wall_time: float = field(default_factory=time.time)


class _UnitsState:
    """Thread-safe rolling buffers per unit, fed by one consumer thread per
    registered feed (or a POST to /api/ingest), read by the HTTP handler
    thread. Slots are created lazily, on a unit's first reading -- see
    add_reading -- not pre-seeded, so snapshot() only ever reports units
    that have actually shown up."""

    def __init__(self):
        self._lock = threading.Lock()
        self._slots: Dict[str, _UnitSlot] = {}
        self._next_batch_id = 1

    def next_batch_id(self) -> int:
        """One id per /api/ingest call, shared across units -- only
        uniqueness matters (see _fit_time_for_point), not per-unit scoping."""
        with self._lock:
            batch_id = self._next_batch_id
            self._next_batch_id += 1
            return batch_id

    def add_reading(
        self, unit_id: str, reading: UnitReading, batch_id: Optional[int] = None
    ) -> None:
        with self._lock:
            slot = self._slots.get(unit_id)
            if slot is None:
                slot = _UnitSlot(unit_id=unit_id, label=_default_label(unit_id))
                self._slots[unit_id] = slot

            point = _FreqPoint(
                t=reading.t, freq_hz=reading.freq_hz,
                batch_id=batch_id, gps_utc_s=reading.gps_utc_s,
            )
            slot.freq.append(point)
            self._trim(slot.freq, reading.t, key=lambda p: p.t)

            if reading.amplitude_v is not None:
                slot.amplitude.append((reading.t, reading.amplitude_v))
                self._trim(slot.amplitude, reading.t)
            if reading.gps_utc_s is not None:
                slot.last_gps_utc_s = reading.gps_utc_s
            slot.gps_locked = reading.gps_utc_s is not None
            slot.last_seen_wall_time = time.time()

            # Candidate pool: near in reconstructed `t` OR, when both sides
            # have GPS, confirmed near by that independent clock -- using
            # `t` alone here would let it reject a point GPS proves is
            # genuinely close but whose reconstructed `t` (built
            # independently per batch) happens to disagree, which is
            # exactly the kind of mistrust in `t` this fix exists to avoid.
            # _fit_time_for_point still does the authoritative eligibility
            # and span check below; this only decides the initial pool.
            candidates = [
                p for p in slot.freq
                if reading.t - p.t <= ROCOF_WINDOW_S
                or (
                    reading.gps_utc_s is not None and p.gps_utc_s is not None
                    and 0 <= _gps_utc_delta_s(reading.gps_utc_s, p.gps_utc_s) <= ROCOF_WINDOW_S
                )
            ]
            fit_ts: List[float] = []
            fit_fs: List[float] = []
            for p in candidates:
                fit_t = _fit_time_for_point(point, p, MAX_ROCOF_GAP_S)
                if fit_t is None:
                    continue
                fit_ts.append(fit_t)
                fit_fs.append(p.freq_hz)

            if len(fit_ts) >= 2:
                slope = rocof_from_window(fit_ts, fit_fs)
                if slope is not None:
                    if abs(slope) <= ROCOF_PLAUSIBILITY_LIMIT_HZ_S:
                        slot.rocof.append((reading.t, slope))
                        self._trim(slot.rocof, reading.t)
                    else:
                        slot.rocof_skipped_implausible_count += 1
            elif len(candidates) >= 2:
                # There were enough nearby points by time alone, but
                # eligibility filtering (batch boundary / no GPS
                # confirmation) knocked the usable set below 2 -- this is
                # the case the fix exists for, not just "not enough data
                # yet" (that's the len(candidates) < 2 case, left silent
                # exactly as before this fix).
                slot.rocof_skipped_boundary_count += 1

    @staticmethod
    def _trim(buf, latest_t: float, key=lambda item: item[0]) -> None:
        while buf and latest_t - key(buf[0]) > WINDOW_S:
            buf.popleft()

    def snapshot(self) -> List[dict]:
        now = time.time()
        with self._lock:
            out = []
            for slot in self._slots.values():
                if not slot.freq:
                    out.append(dict(
                        id=slot.unit_id, label=slot.label, status="offline",
                        freq_hz=None, rocof_hz_s=None, history=[], rocof_history=[],
                        amplitude_v=None, gps_utc_s=None, gps_locked=None,
                        seconds_since_last_reading=None, samples_per_minute=0,
                        completeness_pct=None, gaps=[], rocof_gaps=[],
                        rocof_suppressed_count=0,
                    ))
                    continue
                latest_t = slot.freq[-1].t
                seconds_since_last_reading = now - slot.last_seen_wall_time
                is_stale = seconds_since_last_reading > STALE_THRESHOLD_S
                recent = [p.freq_hz for p in slot.freq if latest_t - p.t <= READOUT_WINDOW_S]
                recent_amplitude = [
                    a for t, a in slot.amplitude if latest_t - t <= READOUT_WINDOW_S
                ]
                history_points = [(p.t, p.freq_hz) for p in slot.freq]
                # latest_t, not wall-clock `now` -- same reasoning as
                # last_seen_wall_time's comment: a reading's own `t` isn't
                # wall-clock-comparable for every source (SyntheticUnitFeed's
                # is process-relative), so "how many in the last 60s of this
                # unit's own timeline" uses that timeline's own reference point.
                samples_per_minute = len([p for p in slot.freq if latest_t - p.t <= 60.0])

                # Distinct whole seconds with at least one reading, not raw
                # reading count -- count/expected let a burst of readings
                # denser than 1/s (perfectly normal jitter, not an error)
                # paper over a genuine multi-second gap elsewhere in the
                # same window, since the extra readings inflated the
                # numerator enough to still hit the 100% cap. Counting
                # distinct seconds instead means only real time coverage
                # counts, regardless of how many readings land in any one
                # of them.
                seconds_with_data = {
                    int(p.t // 1.0) for p in slot.freq
                    if latest_t - p.t <= COMPLETENESS_WINDOW_S
                }
                completeness_pct = min(
                    100.0, 100.0 * len(seconds_with_data) / COMPLETENESS_WINDOW_S
                )

                out.append(dict(
                    id=slot.unit_id,
                    label=slot.label,
                    status="stale" if is_stale else "live",
                    freq_hz=statistics.median(recent),
                    rocof_hz_s=slot.rocof[-1][1] if slot.rocof else 0.0,
                    history=[list(p) for p in _smoothed_history(history_points)],
                    # Not smoothed like history -- rocof_from_window's least-squares
                    # fit over ROCOF_WINDOW_S is already far less noisy than raw
                    # per-cycle frequency, so there's nothing extra to gain here.
                    rocof_history=[list(p) for p in slot.rocof],
                    amplitude_v=statistics.median(recent_amplitude) if recent_amplitude else None,
                    gps_utc_s=slot.last_gps_utc_s,
                    gps_locked=slot.gps_locked,
                    seconds_since_last_reading=seconds_since_last_reading,
                    samples_per_minute=samples_per_minute,
                    completeness_pct=completeness_pct,
                    gaps=_find_gaps(history_points),
                    # RoCoF has its own, independent gaps -- the eligibility
                    # guard above can exclude a point from rocof_history even
                    # when the frequency line has no gap there at all.
                    rocof_gaps=_find_gaps([(t, r) for t, r in slot.rocof]),
                    # Both reasons a candidate RoCoF point was excluded rather
                    # than plotted (see _fit_time_for_point / the plausibility
                    # check above) -- surfaced as one total so an operator can
                    # see at a glance that suppression is happening, without
                    # needing to know the two internal reasons apart.
                    rocof_suppressed_count=(
                        slot.rocof_skipped_boundary_count
                        + slot.rocof_skipped_implausible_count
                    ),
                ))
            return out


def _consume(
    unit_id: str, feed: UnitFeed, state: _UnitsState, stop_event: threading.Event
) -> None:
    for reading in feed.stream():
        if stop_event.is_set():
            return
        state.add_reading(unit_id, reading)


# --- DB-backed units ---------------------------------------------------------
# Real (ingested) units are read from the ReadingStore, ordered by each
# reading's own GPS UTC time -- so a late or retried batch lands where it was
# actually measured, and nothing is stamped with receipt time. Synthetic feeds
# keep using _UnitsState above (they have no GPS time and no persistence).

HISTORY_DEFAULT_LIMIT = 1000
HISTORY_MAX_LIMIT = 10_000
HISTORY_DEFAULT_SPAN_S = 3600.0
QUOTA_WARNING_FRACTION = 0.8
# A 60-reading v2 batch is ~7 KB and the parser accepts at most 1,000 readings (~140 KB); anything
# bigger is refused before it is parsed, so an anonymous caller cannot tie up the single worker.
MAX_BODY_BYTES = 512 * 1024
DEFAULT_HISTORY_RATE = (30, 60.0)     # requests per seconds, per client
DEFAULT_EXPORT_RATE = (10, 60.0)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _db_unit_snapshot(u: UnitState, rows: List[Row], now: float) -> dict:
    seconds_since = now - u.last_received_at
    out = dict(
        id=u.unit_id,
        label=_default_label(u.unit_id),
        status="stale" if seconds_since > STALE_THRESHOLD_S else "live",
        # gps_utc_s keeps its old meaning (UTC seconds-of-day of the newest reading);
        # gps_utc is the same instant as absolute Unix seconds.
        gps_utc_s=(u.last_gps_us % DAY_US) / US if u.last_gps_us is not None else None,
        gps_utc=u.last_gps_us / US if u.last_gps_us is not None else None,
        gps_locked=u.last_gps_locked,
        seconds_since_last_reading=seconds_since,
        # Readings held without a usable GPS time: stored and flagged, never plotted.
        unlocked_count=u.unlocked_total,
        implausible_time_count=u.implausible_total,
        duplicates_ignored=u.duplicates_total,
    )
    if not rows:
        out.update(
            freq_hz=None, rocof_hz_s=None, history=[], rocof_history=[], amplitude_v=None,
            samples_per_minute=0, completeness_pct=None, gaps=[], rocof_gaps=[],
            rocof_suppressed_count=0,
        )
        return out
    pts = [(r.gps_utc_us / US, r.freq_hz, r.boot_id) for r in rows]
    latest_t = pts[-1][0]
    series = rocof_series(pts, MAX_ROCOF_GAP_S, ROCOF_PLAUSIBILITY_LIMIT_HZ_S)
    recent = [f for t, f, _b in pts if latest_t - t <= READOUT_WINDOW_S]
    recent_amp = [
        r.amplitude_v for r in rows
        if r.amplitude_v is not None and latest_t - r.gps_utc_us / US <= READOUT_WINDOW_S
    ]
    history_points = [(t, f) for t, f, _b in pts]
    seconds_with_data = {int(t // 1.0) for t, _f, _b in pts if latest_t - t <= COMPLETENESS_WINDOW_S}
    out.update(
        freq_hz=statistics.median(recent),
        rocof_hz_s=series.points[-1][1] if series.points else 0.0,
        history=[list(p) for p in _smoothed_history(history_points)],
        rocof_history=[list(p) for p in series.points],
        amplitude_v=statistics.median(recent_amp) if recent_amp else None,
        samples_per_minute=len([1 for t, _f, _b in pts if latest_t - t <= 60.0]),
        completeness_pct=min(100.0, 100.0 * len(seconds_with_data) / COMPLETENESS_WINDOW_S),
        gaps=_find_gaps(history_points),
        rocof_gaps=_find_gaps(series.points),
        rocof_suppressed_count=series.skipped_boundary + series.skipped_implausible,
    )
    return out


def _parse_time_us(v: str) -> int:
    """Unix seconds (float) or an ISO-8601 UTC timestamp -> integer microseconds."""
    try:
        return int(round(float(v) * US))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"cannot parse time {v!r} (use Unix seconds or ISO-8601 UTC)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(microseconds=1)


def _dir_size(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


class _TtlCache:
    def __init__(self):
        self._v: Dict[str, Tuple[float, object]] = {}

    def get(self, key: str, ttl_s: float, fn):
        now = time.monotonic()
        hit = self._v.get(key)
        if hit is not None and now - hit[0] < ttl_s:
            return hit[1]
        val = fn()
        self._v[key] = (now, val)
        return val


def _row_json(r: Row) -> dict:
    return dict(
        t=r.gps_utc_us / US if r.gps_utc_us is not None else None,
        gps_utc_us=r.gps_utc_us, freq_hz=r.freq_hz, amplitude_v=r.amplitude_v,
        boot_id=r.boot_id, seq=r.seq, flags=r.flags, time_src=r.time_src,
        gps_locked=bool(r.gps_locked), received_at=r.received_at, id=r.id,
    )


def create_app(
    simulated_units: Optional[List[dict]] = None,
    db_path: Optional[str] = None,
    clock=time.time,
    retention_config: Optional[RetentionConfig] = None,
    quota_bytes: Optional[int] = None,
    quota_root: Optional[str] = None,
    ingest_auth: Optional[IngestAuth] = None,
    export_token: Optional[str] = None,
    history_rate: Optional[tuple] = None,
    export_rate: Optional[tuple] = None,
    client_ip_header: Optional[str] = None,
) -> Flask:
    """``db_path`` (else $TREMOR_DB_PATH, else an ephemeral temp file) is where
    every ingested reading is stored permanently. ``clock`` supplies receipt time
    and staleness -- injectable so tests can freeze it."""
    simulated_units = SIMULATED_UNITS if simulated_units is None else simulated_units

    db_path = db_path or os.environ.get("TREMOR_DB_PATH")
    if not db_path:
        db_path = os.path.join(tempfile.mkdtemp(prefix="tremor-"), "readings.db")
        log.warning("TREMOR_DB_PATH not set: using an EPHEMERAL database at %s", db_path)
    store: ReadingStore = open_store(
        db_path, synchronous=os.environ.get("TREMOR_SQLITE_SYNCHRONOUS", "FULL"))
    cfg = retention_config or RetentionConfig.from_env(
        os.path.join(os.path.dirname(os.path.abspath(db_path)), "exports"))
    engine = RetentionEngine(store, cfg, clock)
    if quota_bytes is None:
        quota_bytes = int(float(os.environ.get("TREMOR_QUOTA_MB", "512")) * 1024 * 1024)
    quota_root = quota_root or os.environ.get("TREMOR_QUOTA_ROOT") or None
    size_cache = _TtlCache()

    # --- access control (see security.py). Misconfiguration raises at startup, on purpose.
    auth = ingest_auth if ingest_auth is not None else IngestAuth.from_env(os.environ, clock=clock, log=log)
    export_auth = ExportAuth(export_token if export_token is not None else (os.environ.get("TREMOR_EXPORT_TOKEN") or None))
    h_lim, h_per = history_rate or parse_rate(os.environ.get("TREMOR_HISTORY_RATE"), DEFAULT_HISTORY_RATE)
    e_lim, e_per = export_rate or parse_rate(os.environ.get("TREMOR_EXPORT_RATE"), DEFAULT_EXPORT_RATE)
    history_limiter = RateLimiter(h_lim, h_per, clock)
    export_limiter = RateLimiter(e_lim, e_per, clock)
    ip_header = client_ip_header if client_ip_header is not None else (os.environ.get("TREMOR_CLIENT_IP_HEADER") or None)

    def _rate_limited(limiter):
        ok, retry = limiter.allow(client_ip(request, ip_header))
        if ok:
            return None
        import math
        resp = jsonify(error="rate limit exceeded", retry_after_s=round(retry, 1))
        resp.status_code = 429
        resp.headers["Retry-After"] = str(max(1, math.ceil(retry)))
        return resp

    state = _UnitsState()
    stop_event = threading.Event()
    feeds_and_threads = []

    for cfg_u in simulated_units:
        unit_id = cfg_u["unit_id"]
        feed_kwargs = {k: v for k, v in cfg_u.items() if k != "unit_id"}
        feed = SyntheticUnitFeed(unit_id=unit_id, label=_default_label(unit_id), **feed_kwargs)
        feed.start()
        consumer = threading.Thread(
            target=_consume, args=(unit_id, feed, state, stop_event), daemon=True
        )
        consumer.start()
        feeds_and_threads.append((feed, consumer))

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/units")
    def api_units():
        out = state.snapshot()                                  # synthetic feeds only
        try:
            now = clock()
            db_units = []
            for u in store.unit_states():
                db_units.append(_db_unit_snapshot(u, store.window(u.unit_id, WINDOW_S), now))
        except StoreError as exc:
            log.error("/api/units: storage unavailable: %s", exc)
            return jsonify(error="storage unavailable"), 503
        ids = {d["id"] for d in db_units}
        return jsonify([s for s in out if s["id"] not in ids] + db_units)

    @app.post("/api/ingest")
    def api_ingest():
        """Batched ingest for a real unit's readings -- what a Pico's WiFi
        client POSTs, one batch per uplink. Two payload generations (see
        ingest.py): legacy v1 (``gps_utc_s`` seconds-of-day float) and v2
        (``boot_id`` + per-reading ``seq`` + integer ``gps`` time).

        Readings are stored under their own GPS UTC time. Receipt time is only
        metadata (and, for v1 alone, the hint that picks which calendar day the
        device's seconds-of-day belongs to). Retried batches are idempotent:
        duplicates are ignored, so any failure here is safe for the device to
        retry -- which is why a storage failure answers 503, not 200.
        """
        try:
            batch = parse_payload(request.get_json(silent=True))
        except PayloadError as exc:
            return jsonify(error=str(exc)), 400
        decision = auth.check(batch.unit_id, request.headers.get(TOKEN_HEADER))
        if not decision.allowed:
            # identical for "missing" and "wrong": no oracle, and never any token in the body/log
            resp = jsonify(error="unauthorized")
            resp.status_code = 401
            return resp
        try:
            res = store.ingest(batch, clock())
        except StoreError as exc:
            log.error("/api/ingest: storage unavailable: %s", exc)
            resp = jsonify(error="storage unavailable")
            resp.status_code = 503
            resp.headers["Retry-After"] = "30"
            return resp
        resp = jsonify(accepted=res.accepted, inserted=res.inserted, duplicates=res.duplicates,
                       unlocked=res.unlocked, implausible=res.implausible)
        resp.status_code = 202
        resp.call_on_close(engine.maybe_step)       # after the device has its answer
        return resp

    @app.get("/api/history")
    def api_history():
        limited = _rate_limited(history_limiter)
        if limited is not None:
            return limited
        unit = request.args.get("unit", "")
        if not unit:
            return jsonify(error="unit is required"), 400
        try:
            limit = int(request.args.get("limit", HISTORY_DEFAULT_LIMIT))
            after_id = int(request.args.get("after_id", 0))
            if limit < 1:
                raise ValueError("limit must be >= 1")
            limit = min(limit, HISTORY_MAX_LIMIT)
            include_unlocked = request.args.get("include_unlocked", "0") in ("1", "true", "yes")
            res_mode = request.args.get("resolution", "auto")
            if res_mode not in ("raw", "1min", "auto"):
                raise ValueError("resolution must be raw, 1min or auto")
            to_us = _parse_time_us(request.args["to"]) if "to" in request.args else None
            from_us = _parse_time_us(request.args["from"]) if "from" in request.args else None
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        try:
            states = {u.unit_id: u for u in store.unit_states()}
            if unit not in states:
                return jsonify(error=f"unknown unit {unit!r}"), 404
            if to_us is None:
                to_us = states[unit].last_gps_us if states[unit].last_gps_us is not None else int(clock() * US)
            if from_us is None:
                from_us = to_us - int(HISTORY_DEFAULT_SPAN_S * US)
            if from_us > to_us:
                return jsonify(error="from must not be after to"), 400
            if res_mode == "auto":
                raw_floor = (clock() - cfg.raw_days * 86400) * US
                res_mode = "raw" if from_us >= raw_floor else "1min"
            if res_mode == "1min":
                aggs = store.aggregates(unit, from_us // (60 * US), to_us // (60 * US), limit + 1)
                truncated = len(aggs) > limit
                aggs = aggs[:limit]
                return jsonify(
                    unit=unit, resolution="1min", from_us=from_us, to_us=to_us, limit=limit,
                    count=len(aggs), truncated=truncated,
                    next_from_us=(aggs[-1].minute + 1) * 60 * US if truncated else None,
                    aggregates=[dict(minute=a.minute, t=a.minute * 60.0, n=a.n, n_unlocked=a.n_unlocked,
                                     freq_mean=a.freq_mean, freq_min=a.freq_min, freq_max=a.freq_max,
                                     freq_std=a.freq_std, rocof_max_abs=a.rocof_max_abs,
                                     amp_mean=a.amp_mean) for a in aggs])
            # Unlocked rows have no GPS time; without an explicit `to`, list every one received
            # up to now rather than cutting them off at the newest GPS-timed reading.
            page = store.history(unit, from_us, to_us, limit, after_id=after_id,
                                 include_unlocked=include_unlocked,
                                 unlocked_to_us=None if "to" in request.args else int(clock() * US))
        except StoreError as exc:
            log.error("/api/history: storage unavailable: %s", exc)
            return jsonify(error="storage unavailable"), 503
        body = dict(unit=unit, resolution="raw", from_us=from_us, to_us=to_us, limit=limit,
                    count=len(page.rows), truncated=page.truncated,
                    next_from_us=page.next_from_us, next_after_id=page.next_after_id,
                    readings=[_row_json(r) for r in page.rows])
        if include_unlocked:
            body["unlocked"] = [_row_json(r) for r in page.unlocked]
        return jsonify(body)

    @app.get("/api/health")
    def api_health():
        try:
            h = store.health()
            units = store.unit_states()
        except StoreError as exc:
            return jsonify(status="error", error=str(exc)), 503
        exports = size_cache.get("exports", 60.0, lambda: _dir_size(cfg.export_dir))
        if quota_root:
            used = size_cache.get("root", 600.0, lambda: _dir_size(quota_root))
            measured = f"all files under {quota_root}"
        else:
            used = h["db_bytes"] + h["journal_bytes"] + exports
            measured = "database + exports only (set TREMOR_QUOTA_ROOT to measure the whole quota)"
        frac = used / quota_bytes if quota_bytes else 0.0
        warning = frac >= QUOTA_WARNING_FRACTION
        status = "attention" if h["days_needing_attention"] else ("warning" if warning else "ok")
        now = clock()
        return jsonify(
            status=status, server_time=now,
            storage=dict(db_bytes=h["db_bytes"], journal_bytes=h["journal_bytes"], exports_bytes=exports,
                         used_bytes=used, quota_bytes=quota_bytes, used_fraction=round(frac, 4),
                         warning=warning, warning_threshold=QUOTA_WARNING_FRACTION, measured=measured),
            store=h,
            ingest_auth=auth.summary(),
            export=dict(enabled=export_auth.enabled),
            history_rate_limit=dict(requests=h_lim, per_seconds=h_per, client_ip_header=ip_header,
                                    your_address_as_seen=client_ip(request, ip_header)),
            retention=dict(raw_days=cfg.raw_days, rocof_event_hz_s=cfg.rocof_event_hz_s,
                           freq_band=[cfg.freq_lo, cfg.freq_hi], event_margin_s=cfg.event_margin_s,
                           export_dir=cfg.export_dir),
            units=[dict(unit_id=u.unit_id, last_received_at=u.last_received_at,
                        seconds_since_last_reading=now - u.last_received_at,
                        readings_total=u.readings_total, unlocked_total=u.unlocked_total,
                        duplicates_total=u.duplicates_total, implausible_total=u.implausible_total)
                   for u in units],
        )

    @app.get("/api/export/<unit>/<day>")
    def api_export(unit, day):
        """Download one day's gzip CSV export (so it can be pulled off the server).
        Needs TREMOR_EXPORT_TOKEN, sent as the X-Tremor-Token header (never in the URL, which
        ends up in access logs); disabled entirely if no token is configured. Rate limited so
        an anonymous caller cannot tie up the single web worker."""
        if not export_auth.enabled:
            return jsonify(error="export is disabled on this server"), 403
        limited = _rate_limited(export_limiter)
        if limited is not None:
            return limited
        if not export_auth.check(request.headers.get(TOKEN_HEADER)):
            return jsonify(error="unauthorized"), 401
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", unit) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return jsonify(error="not found"), 404
        try:
            d = (datetime.strptime(day, "%Y-%m-%d").date() - day_to_date(0)).days
        except ValueError:
            return jsonify(error="not found"), 404
        path = engine.export_path(unit, d)
        if not os.path.isfile(path):
            return jsonify(error="not found"), 404
        return send_file(path, mimetype="application/gzip", as_attachment=True,
                         download_name=os.path.basename(path))

    def shutdown() -> None:
        stop_event.set()
        for feed, _thread in feeds_and_threads:
            feed.stop()
        store.close()

    app.config["TREMOR_STATE"] = state
    app.config["TREMOR_STORE"] = store
    app.config["TREMOR_RETENTION"] = engine
    app.config["TREMOR_INGEST_AUTH"] = auth
    app.config["TREMOR_SHUTDOWN"] = shutdown
    return app


def main() -> None:
    db_path = os.environ.get("TREMOR_DB_PATH") or os.path.join(
        os.path.expanduser("~"), "tremor_data", "readings.db")
    app = create_app(db_path=db_path)
    try:
        app.run(host="127.0.0.1", port=5000, debug=False)
    finally:
        app.config["TREMOR_SHUTDOWN"]()


if __name__ == "__main__":
    main()
