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

import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request

from .rocof import rocof_from_window
from .units import SyntheticUnitFeed, UnitFeed, UnitReading

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


def create_app(
    simulated_units: Optional[List[dict]] = None,
) -> Flask:
    simulated_units = SIMULATED_UNITS if simulated_units is None else simulated_units

    state = _UnitsState()
    stop_event = threading.Event()
    feeds_and_threads = []

    for cfg in simulated_units:
        unit_id = cfg["unit_id"]
        feed_kwargs = {k: v for k, v in cfg.items() if k != "unit_id"}
        feed = SyntheticUnitFeed(unit_id=unit_id, label=_default_label(unit_id), **feed_kwargs)
        feed.start()
        consumer = threading.Thread(
            target=_consume, args=(unit_id, feed, state, stop_event), daemon=True
        )
        consumer.start()
        feeds_and_threads.append((feed, consumer))

    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/units")
    def api_units():
        return jsonify(state.snapshot())

    @app.post("/api/ingest")
    def api_ingest():
        """Batched ingest for a real unit's readings -- what a Pico's WiFi
        client will eventually POST, one batch per uplink. Body shape:
            {"unit_id": "unit-1",
             "readings": [{"frequency_hz": 49.98, "amplitude_v": 0.72,
                            "gps_utc_s": 41023.5}, ...]}
        amplitude_v/gps_utc_s are optional per-reading (gps_utc_s is blank
        on the device until PPS sync, same as overnight_log.py's schema).
        unit_id isn't checked against a fixed roster -- a new unit_id gets
        its own dashboard card automatically on its first accepted batch
        (see _UnitsState.add_reading), no code change needed to "add" it.
        Readings are timestamped by server receipt time, not gps_utc_s --
        gps_utc_s is seconds-of-day and can be absent pre-sync, so it isn't
        safe as the window/RoCoF ordering key; it's stored alongside purely
        as metadata. Readings within a batch are spaced READING_INTERVAL_S
        apart, working backward from receipt time (the last reading in the
        batch lands ~now, earlier ones progressively before it) -- matching
        wifi_unit_client.py's real one-reading-per-second cadence, not
        compressed into a few milliseconds. Compressing them (an earlier
        version used a flat 0.02s step, modeled on per-mains-cycle spacing
        that never matched any real client) corrupted rocof_from_window's
        slope: dividing a real ~1s frequency delta by an apparent ~0.02s
        gap inflated RoCoF by ~50x, visible on the dashboard as physically
        impossible spikes and gave the chart's line a bursts-with-gaps
        shape instead of a continuous trace.
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="expected a JSON object"), 400

        unit_id = payload.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id.strip():
            return jsonify(error=f"invalid unit_id {unit_id!r}"), 400

        readings = payload.get("readings")
        if not isinstance(readings, list) or not readings:
            return jsonify(error="'readings' must be a non-empty list"), 400

        parsed = []
        for r in readings:
            if not isinstance(r, dict):
                return jsonify(error="each reading must be an object"), 400
            try:
                freq_hz = float(r["frequency_hz"])
                amplitude_v = float(r["amplitude_v"]) if r.get("amplitude_v") is not None else None
                gps_utc_s = float(r["gps_utc_s"]) if r.get("gps_utc_s") is not None else None
            except (KeyError, TypeError, ValueError):
                return jsonify(error="each reading needs a numeric frequency_hz"), 400
            parsed.append((freq_hz, amplitude_v, gps_utc_s))

        now = time.time()
        n = len(parsed)
        batch_id = state.next_batch_id()  # see _fit_time_for_point: every reading in
                                           # this POST shares one id, distinguishing
                                           # "same batch" from "different batch" for
                                           # the RoCoF cross-batch guard
        for i, (freq_hz, amplitude_v, gps_utc_s) in enumerate(parsed):
            state.add_reading(unit_id, UnitReading(
                t=now - (n - 1 - i) * READING_INTERVAL_S,
                freq_hz=freq_hz,
                amplitude_v=amplitude_v,
                gps_utc_s=gps_utc_s,
            ), batch_id=batch_id)

        return jsonify(accepted=len(parsed)), 202

    def shutdown() -> None:
        stop_event.set()
        for feed, _thread in feeds_and_threads:
            feed.stop()

    app.config["TREMOR_STATE"] = state
    app.config["TREMOR_SHUTDOWN"] = shutdown
    return app


def main() -> None:
    app = create_app()
    try:
        app.run(host="127.0.0.1", port=5000, debug=False)
    finally:
        app.config["TREMOR_SHUTDOWN"]()


if __name__ == "__main__":
    main()
