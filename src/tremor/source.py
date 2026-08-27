"""Live frequency-estimate sources.

``FrequencySource`` is the interface the dashboard (or any other live
consumer) programs against. ``SyntheticFrequencySource`` is the only
implementation today -- it runs the incremental signal generator
(``tremor.signal.MainsSignalGenerator``) through a causal low-pass
(``tremor.filters.SinglePoleLowPass``) and then the causal streaming
estimator (``tremor.frequency.StreamingZeroCrossingDetector``) in a
background thread, paced to real time, and yields the resulting estimates.
A future serial source reading real Pico frames should implement the same
interface so the dashboard doesn't need to change.
"""

from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Iterator, Optional

from .filters import SinglePoleLowPass
from .frequency import FreqSample, StreamingZeroCrossingDetector
from .signal import MainsSignalGenerator


class FrequencySource(ABC):
    """Interface for anything that can yield a live stream of frequency
    estimates -- a synthetic feed today, a serial connection to a Pico
    later."""

    def start(self) -> None:
        """Begin producing estimates, if the source needs an explicit
        start (e.g. to launch a background thread or open a port).
        Default is a no-op for sources that don't need one."""

    def stop(self) -> None:
        """Stop producing estimates and release any resources. Default is
        a no-op."""

    @abstractmethod
    def stream(self) -> Iterator[FreqSample]:
        """Yield ``FreqSample``s as they become available. Blocks between
        estimates; runs until ``stop()`` is called."""


@dataclass
class _Ramp:
    start_wall_t: float
    start_offset_hz: float
    target_offset_hz: float
    duration_s: float


class SyntheticFrequencySource(FrequencySource):
    """Generates a synthetic mains signal in real time and streams the
    frequency estimates recovered from it, with on-demand disturbances."""

    def __init__(
        self,
        sample_rate_hz: float = 8000.0,
        nominal_freq_hz: float = 50.0,
        amplitude: float = 1.0,
        harmonics: Optional[Dict[int, float]] = None,
        noise_std: float = 0.02,
        min_crossing_interval_s: float = 0.015,
        filter_cutoff_hz: float = 75.0,
        chunk_s: float = 0.05,
    ):
        self._generator = MainsSignalGenerator(
            sample_rate_hz=sample_rate_hz,
            nominal_freq_hz=nominal_freq_hz,
            amplitude=amplitude,
            harmonics=harmonics,
            noise_std=noise_std,
        )
        self._filter = SinglePoleLowPass(filter_cutoff_hz, sample_rate_hz)
        self._detector = StreamingZeroCrossingDetector(min_crossing_interval_s)
        self._chunk_s = chunk_s

        self._queue: "queue.Queue[FreqSample]" = queue.Queue()
        self._lock = threading.Lock()
        self._base_offset_hz = 0.0
        self._ramp: Optional[_Ramp] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # -- disturbance controls -------------------------------------------------

    def inject_step(self, delta_hz: float) -> None:
        """Immediately shift the frequency offset by ``delta_hz``."""
        with self._lock:
            self._ramp = None
            self._base_offset_hz += delta_hz

    def inject_ramp(self, delta_hz: float, duration_s: float) -> None:
        """Ramp the frequency offset linearly by ``delta_hz`` over
        ``duration_s`` seconds, then hold at the new value."""
        with self._lock:
            start_offset = self._base_offset_hz
            self._ramp = _Ramp(
                start_wall_t=time.monotonic(),
                start_offset_hz=start_offset,
                target_offset_hz=start_offset + delta_hz,
                duration_s=duration_s,
            )

    def reset(self) -> None:
        """Return the offset to nominal (0 Hz)."""
        with self._lock:
            self._ramp = None
            self._base_offset_hz = 0.0

    def _current_offset_hz(self) -> float:
        with self._lock:
            ramp = self._ramp
            if ramp is None:
                return self._base_offset_hz

            elapsed = time.monotonic() - ramp.start_wall_t
            if elapsed >= ramp.duration_s:
                self._base_offset_hz = ramp.target_offset_hz
                self._ramp = None
                return self._base_offset_hz

            frac = elapsed / ramp.duration_s
            return ramp.start_offset_hz + frac * (
                ramp.target_offset_hz - ramp.start_offset_hz
            )

    # -- FrequencySource interface --------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def stream(self) -> Iterator[FreqSample]:
        while not self._stop_event.is_set():
            try:
                yield self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

    # -- background generation loop -------------------------------------------

    def _run(self) -> None:
        samples_per_chunk = max(
            1, int(round(self._chunk_s * self._generator.sample_rate_hz))
        )
        next_wall_t = time.monotonic()

        while not self._stop_event.is_set():
            self._generator.set_offset(self._current_offset_hz())

            for _ in range(samples_per_chunk):
                t, x, _true_freq = self._generator.next_sample()
                x_filtered = self._filter.process_sample(x)
                sample = self._detector.process_sample(t, x_filtered)
                if sample is not None:
                    self._queue.put(sample)

            next_wall_t += self._chunk_s
            delay = next_wall_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_wall_t = time.monotonic()  # fell behind; resync
