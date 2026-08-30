"""Local web dashboard: frequency + RoCoF for each of the 5 planned TREMOR
units, backed by synthetic per-unit feeds today.

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
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template

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

# All 5 planned units get a slot in the UI. Only the ones with a feed
# registered (see SIMULATED_UNITS) show live data -- the rest render as
# "no data yet" placeholders, which is exactly the swap-in point for real
# hardware: register_feed() is the only thing a real unit needs.
UNIT_SLOTS = [
    ("unit-1", "Unit 1"),
    ("unit-2", "Unit 2"),
    ("unit-3", "Unit 3"),
    ("unit-4", "Unit 4"),
    ("unit-5", "Unit 5"),
]

# Small, plausible per-unit calibration/noise variation -- all units track
# the same true_grid_freq_hz() (see units.py), only their independent ADC
# noise and small DC-offset calibration error differ, same as real units
# on a shared grid would.
SIMULATED_UNITS = [
    dict(unit_id="unit-1", noise_std=0.015, dc_offset=0.01, seed=1),
    dict(unit_id="unit-2", noise_std=0.02, dc_offset=-0.02, seed=2),
    dict(unit_id="unit-3", noise_std=0.03, dc_offset=0.0, seed=3),
]


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


class _UnitsState:
    """Thread-safe rolling buffers per unit, fed by one consumer thread per
    registered feed, read by the HTTP handler thread."""

    def __init__(self, unit_slots: List[Tuple[str, str]]):
        self._lock = threading.Lock()
        self._slots: Dict[str, _UnitSlot] = {
            unit_id: _UnitSlot(unit_id=unit_id, label=label)
            for unit_id, label in unit_slots
        }

    def add_reading(self, unit_id: str, reading: UnitReading) -> None:
        with self._lock:
            slot = self._slots[unit_id]
            slot.freq.append((reading.t, reading.freq_hz))
            self._trim(slot.freq, reading.t)

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
                    ))
                    continue
                latest_t = slot.freq[-1][0]
                recent = [f for t, f in slot.freq if latest_t - t <= READOUT_WINDOW_S]
                out.append(dict(
                    id=slot.unit_id,
                    label=slot.label,
                    status="live",
                    freq_hz=statistics.median(recent),
                    rocof_hz_s=slot.rocof[-1][1] if slot.rocof else 0.0,
                    history=[list(p) for p in _smoothed_history(list(slot.freq))],
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
    unit_slots: Optional[List[Tuple[str, str]]] = None,
) -> Flask:
    simulated_units = SIMULATED_UNITS if simulated_units is None else simulated_units
    unit_slots = UNIT_SLOTS if unit_slots is None else unit_slots
    labels_by_id = dict(unit_slots)

    state = _UnitsState(unit_slots)
    stop_event = threading.Event()
    feeds_and_threads = []

    for cfg in simulated_units:
        unit_id = cfg["unit_id"]
        feed_kwargs = {k: v for k, v in cfg.items() if k != "unit_id"}
        feed = SyntheticUnitFeed(unit_id=unit_id, label=labels_by_id[unit_id], **feed_kwargs)
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
