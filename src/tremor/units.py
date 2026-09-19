"""Multi-unit synthetic ingestion for the multi-unit web dashboard.

Reuses the pure-Python signal generator and hysteresis-guarded zero-crossing
estimator from the repo-root ``freq_estimator.py`` draft (the same algorithm
intended to eventually run on each Pico) rather than
``tremor.signal``/``tremor.frequency``, so a simulated unit's behaviour
matches what a real unit's on-device pipeline will actually produce.

``freq_estimator.py`` isn't part of the installed ``tremor`` package -- it's
still being iterated on at the repo root -- so it's imported via a small
sys.path shim below. This is a deliberate, temporary coupling: if
``freq_estimator.py`` is ever formalised into the package, the shim goes
away and this becomes a normal import.
"""

from __future__ import annotations

import queue
import random
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from freq_estimator import estimate_frequency, generate_synthetic_signal  # noqa: E402


@dataclass
class UnitReading:
    t: float
    freq_hz: float
    # Populated by real ingested readings (see webapp.py's /api/ingest);
    # SyntheticUnitFeed never sets these, so both default to None.
    amplitude_v: Optional[float] = None
    gps_utc_s: Optional[float] = None


class UnitFeed(ABC):
    """Interface for a single unit's live frequency feed -- the web app
    programs against this, not against any particular implementation. A
    real serial/network connection to a Pico should implement it the same
    way a synthetic one does, so the web app doesn't change."""

    unit_id: str
    label: str

    def start(self) -> None:
        """Begin producing readings, if the feed needs an explicit start.
        Default is a no-op."""

    def stop(self) -> None:
        """Stop producing readings and release resources. Default is a
        no-op."""

    @abstractmethod
    def stream(self) -> Iterator[UnitReading]:
        """Yield ``UnitReading``s as they become available."""


def true_grid_freq_hz(_t: float) -> float:
    """The shared 'ground truth' grid frequency every simulated unit
    measures a noisy version of. Constant for now -- this is the hook for
    a future shared disturbance (all 5 units should see the same
    underlying event, just through their own noise and at their own
    arrival time, which is the whole point of the network)."""
    return 50.0


class SyntheticUnitFeed(UnitFeed):
    """Simulates one unit: repeatedly generates a short chunk of synthetic
    mains signal via freq_estimator.generate_synthetic_signal and runs it
    through freq_estimator.estimate_frequency (hysteresis + moving-average
    zero-crossing), streaming the resulting per-cycle readings paced to
    real time.

    Each instance owns a private random.Random (see the rng note on
    generate_synthetic_signal) so multiple units can run concurrently in
    their own threads without racing on shared global random state.
    """

    def __init__(
        self,
        unit_id: str,
        label: str,
        fs_hz: float = 4000.0,
        noise_std: float = 0.02,
        dc_offset: float = 0.0,
        hysteresis: float = 0.05,
        filter_window_s: float = 0.00125,
        chunk_s: float = 1.0,
        seed: Optional[int] = None,
    ):
        self.unit_id = unit_id
        self.label = label
        self.fs_hz = fs_hz
        self.noise_std = noise_std
        self.dc_offset = dc_offset
        self.hysteresis = hysteresis
        self.filter_window_s = filter_window_s
        self.chunk_s = chunk_s
        self._rng = random.Random(seed)

        self._queue: "queue.Queue[UnitReading]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._elapsed_t = 0.0

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def stream(self) -> Iterator[UnitReading]:
        while not self._stop_event.is_set():
            try:
                yield self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

    def _run(self) -> None:
        next_wall_t = time.monotonic()
        while not self._stop_event.is_set():
            freq_hz = true_grid_freq_hz(self._elapsed_t)
            ts, ys = generate_synthetic_signal(
                freq_hz=freq_hz,
                fs_hz=self.fs_hz,
                duration_s=self.chunk_s,
                dc_offset=self.dc_offset,
                noise_std=self.noise_std,
                rng=self._rng,
            )

            try:
                _mean_freq, per_cycle = estimate_frequency(
                    ts, ys, dc_offset=self.dc_offset,
                    hysteresis=self.hysteresis,
                    filter_window_s=self.filter_window_s,
                )
            except ValueError:
                per_cycle = []

            for t_local, f in per_cycle:
                self._queue.put(UnitReading(t=self._elapsed_t + t_local, freq_hz=f))

            self._elapsed_t += self.chunk_s
            next_wall_t += self.chunk_s
            delay = next_wall_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_wall_t = time.monotonic()  # fell behind; resync
