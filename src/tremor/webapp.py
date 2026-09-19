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


@dataclass
class _UnitSlot:
    unit_id: str
    label: str
    freq: Deque[Tuple[float, float]] = field(default_factory=deque)
    rocof: Deque[Tuple[float, float]] = field(default_factory=deque)
    # Only populated by real ingested readings (see /api/ingest below) --
    # SyntheticUnitFeed's readings never carry amplitude_v, so this stays
    # empty for the synthetic units.
    amplitude: Deque[Tuple[float, float]] = field(default_factory=deque)
    last_gps_utc_s: Optional[float] = None


class _UnitsState:
    """Thread-safe rolling buffers per unit, fed by one consumer thread per
    registered feed (or a POST to /api/ingest), read by the HTTP handler
    thread. Slots are created lazily, on a unit's first reading -- see
    add_reading -- not pre-seeded, so snapshot() only ever reports units
    that have actually shown up."""

    def __init__(self):
        self._lock = threading.Lock()
        self._slots: Dict[str, _UnitSlot] = {}

    def add_reading(self, unit_id: str, reading: UnitReading) -> None:
        with self._lock:
            slot = self._slots.get(unit_id)
            if slot is None:
                slot = _UnitSlot(unit_id=unit_id, label=_default_label(unit_id))
                self._slots[unit_id] = slot
            slot.freq.append((reading.t, reading.freq_hz))
            self._trim(slot.freq, reading.t)

            if reading.amplitude_v is not None:
                slot.amplitude.append((reading.t, reading.amplitude_v))
                self._trim(slot.amplitude, reading.t)
            if reading.gps_utc_s is not None:
                slot.last_gps_utc_s = reading.gps_utc_s

            window = [
                (t, f) for t, f in slot.freq if reading.t - t <= ROCOF_WINDOW_S
            ]
            if len(window) >= 2:
                ts = [p[0] for p in window]
                fs = [p[1] for p in window]
                slope = rocof_from_window(ts, fs)
                if slope is not None:
                    slot.rocof.append((reading.t, slope))
                    self._trim(slot.rocof, reading.t)

    @staticmethod
    def _trim(buf: Deque[Tuple[float, float]], latest_t: float) -> None:
        while buf and latest_t - buf[0][0] > WINDOW_S:
            buf.popleft()

    def snapshot(self) -> List[dict]:
        with self._lock:
            out = []
            for slot in self._slots.values():
                if not slot.freq:
                    out.append(dict(
                        id=slot.unit_id, label=slot.label, status="offline",
                        freq_hz=None, rocof_hz_s=None, history=[],
                        amplitude_v=None, gps_utc_s=None,
                    ))
                    continue
                latest_t = slot.freq[-1][0]
                recent = [f for t, f in slot.freq if latest_t - t <= READOUT_WINDOW_S]
                recent_amplitude = [
                    a for t, a in slot.amplitude if latest_t - t <= READOUT_WINDOW_S
                ]
                out.append(dict(
                    id=slot.unit_id,
                    label=slot.label,
                    status="live",
                    freq_hz=statistics.median(recent),
                    rocof_hz_s=slot.rocof[-1][1] if slot.rocof else 0.0,
                    history=[list(p) for p in _smoothed_history(list(slot.freq))],
                    amplitude_v=statistics.median(recent_amplitude) if recent_amplitude else None,
                    gps_utc_s=slot.last_gps_utc_s,
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
        as metadata. A small per-reading offset (~1 mains cycle) keeps
        timestamps strictly increasing within one batch -- rocof_from_window's
        least-squares fit is poorly conditioned on near-duplicate timestamps
        (sub-microsecond spacing triggers numpy's RankWarning).
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
        for i, (freq_hz, amplitude_v, gps_utc_s) in enumerate(parsed):
            state.add_reading(unit_id, UnitReading(
                t=now + i * 0.02,
                freq_hz=freq_hz,
                amplitude_v=amplitude_v,
                gps_utc_s=gps_utc_s,
            ))

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
