"""Live matplotlib dashboard: rolling frequency chart, RoCoF trace, and a
big numeric readout, driven by any ``FrequencySource``.

Run with ``python -m tremor.dashboard`` (or the ``tremor-dashboard`` console
script). Swapping the synthetic source for a real Pico serial feed later
only means constructing a different ``FrequencySource`` -- nothing in this
module is synthetic-feed-specific.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List, Optional, Tuple

import numpy as np
from matplotlib import pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button

from .frequency import FreqSample
from .rocof import rocof_from_window
from .source import FrequencySource, SyntheticFrequencySource

WINDOW_S = 60.0
ROCOF_WINDOW_S = 0.5
ROCOF_STARTUP_DISCARD_S = 1.0
ROCOF_Y_DEFAULT_HZ_S = 2.0
FREQ_Y_MARGIN_HZ = 0.5
READOUT_MEDIAN_WINDOW_S = 1.0
UPDATE_INTERVAL_MS = 150


class _DashboardState:
    """Thread-safe rolling buffers fed by the consumer thread, read by the
    animation callback on the main thread."""

    def __init__(self, window_s: float = WINDOW_S, rocof_window_s: float = ROCOF_WINDOW_S):
        self._lock = threading.Lock()
        self._window_s = window_s
        self._rocof_window_s = rocof_window_s
        self._start_t: Optional[float] = None
        self._freq: Deque[Tuple[float, float]] = deque()
        self._rocof: Deque[Tuple[float, float]] = deque()

    def add_sample(self, sample: FreqSample) -> None:
        with self._lock:
            if self._start_t is None:
                self._start_t = sample.t

            self._freq.append((sample.t, sample.freq_hz))
            self._trim(self._freq, sample.t)

            # The filter/detector haven't settled yet right after start (or
            # after the buffers briefly disagree following a step/ramp), so
            # skip the RoCoF trace for the first second -- otherwise that
            # startup transient dominates the sliding-fit slope and poisons
            # the panel's autoscale.
            if sample.t - self._start_t < ROCOF_STARTUP_DISCARD_S:
                return

            window = [
                (t, f) for t, f in self._freq if sample.t - t <= self._rocof_window_s
            ]
            if len(window) >= 2:
                ts = np.array([p[0] for p in window])
                fs = np.array([p[1] for p in window])
                slope = rocof_from_window(ts, fs)
                if slope is not None:
                    self._rocof.append((sample.t, slope))
                    self._trim(self._rocof, sample.t)

    @staticmethod
    def _trim(buf: Deque[Tuple[float, float]], latest_t: float) -> None:
        while buf and latest_t - buf[0][0] > WINDOW_S:
            buf.popleft()

    def snapshot(
        self,
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        with self._lock:
            return list(self._freq), list(self._rocof)


def _rocof_ylim(
    rocof_vals: np.ndarray, default_hz_s: float = ROCOF_Y_DEFAULT_HZ_S
) -> Tuple[float, float]:
    """Symmetric y-limits for the RoCoF panel: clamped to +/-default_hz_s,
    expanding only if the data actually exceeds that range."""
    y_max = max(default_hz_s, float(np.max(np.abs(rocof_vals))))
    return -y_max, y_max


def _readout_frequency(
    freq_ts: np.ndarray,
    freq_vals: np.ndarray,
    window_s: float = READOUT_MEDIAN_WINDOW_S,
) -> float:
    """Median frequency over the trailing ``window_s`` (``freq_ts`` is
    relative, i.e. <= 0, seconds-ago). A short rolling median rather than
    the single latest cycle keeps the readout from jittering on every
    per-cycle estimate."""
    recent = freq_vals[freq_ts >= -window_s]
    return float(np.median(recent))


def _consume(
    source: FrequencySource, state: _DashboardState, stop_event: threading.Event
) -> None:
    for sample in source.stream():
        if stop_event.is_set():
            return
        state.add_sample(sample)


def run_dashboard(source: Optional[FrequencySource] = None) -> None:
    if source is None:
        source = SyntheticFrequencySource()

    state = _DashboardState()
    stop_event = threading.Event()

    source.start()
    consumer_thread = threading.Thread(
        target=_consume, args=(source, state, stop_event), daemon=True
    )
    consumer_thread.start()

    fig = plt.figure(figsize=(9, 8))
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 2, 1.4], hspace=0.45)
    ax_freq = fig.add_subplot(gs[0])
    ax_rocof = fig.add_subplot(gs[1])
    ax_readout = fig.add_subplot(gs[2])
    ax_readout.axis("off")

    (line_freq,) = ax_freq.plot([], [], color="tab:blue", linewidth=1.2)
    ax_freq.set_title("Frequency (last 60 s)")
    ax_freq.set_ylabel("Hz")
    ax_freq.set_xlim(-WINDOW_S, 0)
    ax_freq.set_ylim(50.0 - FREQ_Y_MARGIN_HZ, 50.0 + FREQ_Y_MARGIN_HZ)
    ax_freq.axhline(50.0, color="grey", linewidth=0.6, linestyle="--")
    ax_freq.grid(True, alpha=0.3)

    (line_rocof,) = ax_rocof.plot([], [], color="tab:orange", linewidth=1.2)
    ax_rocof.set_title(f"RoCoF ({int(ROCOF_WINDOW_S * 1000)} ms sliding fit)")
    ax_rocof.set_ylabel("Hz/s")
    ax_rocof.set_xlabel("seconds ago")
    ax_rocof.set_xlim(-WINDOW_S, 0)
    ax_rocof.set_ylim(-ROCOF_Y_DEFAULT_HZ_S, ROCOF_Y_DEFAULT_HZ_S)
    ax_rocof.axhline(0.0, color="grey", linewidth=0.6, linestyle="--")
    ax_rocof.grid(True, alpha=0.3)

    readout_text = ax_readout.text(
        0.5,
        0.7,
        "-- Hz\n-- Hz/s",
        ha="center",
        va="center",
        fontsize=30,
        family="monospace",
        transform=ax_readout.transAxes,
    )

    btn_step_ax = fig.add_axes([0.15, 0.02, 0.22, 0.05])
    btn_ramp_ax = fig.add_axes([0.40, 0.02, 0.22, 0.05])
    btn_reset_ax = fig.add_axes([0.65, 0.02, 0.2, 0.05])
    btn_step = Button(btn_step_ax, "Step +0.3 Hz")
    btn_ramp = Button(btn_ramp_ax, "Ramp +0.5 Hz / 3s")
    btn_reset = Button(btn_reset_ax, "Reset")

    if isinstance(source, SyntheticFrequencySource):
        btn_step.on_clicked(lambda _event: source.inject_step(0.3))
        btn_ramp.on_clicked(lambda _event: source.inject_ramp(0.5, 3.0))
        btn_reset.on_clicked(lambda _event: source.reset())
    else:
        for btn in (btn_step, btn_ramp, btn_reset):
            btn.ax.set_visible(False)

    def update(_frame):
        freq_data, rocof_data = state.snapshot()
        if not freq_data:
            return line_freq, line_rocof, readout_text

        t_latest = freq_data[-1][0]
        freq_ts = np.array([p[0] for p in freq_data]) - t_latest
        freq_vals = np.array([p[1] for p in freq_data])
        line_freq.set_data(freq_ts, freq_vals)

        current_rocof = 0.0
        if rocof_data:
            rocof_ts = np.array([p[0] for p in rocof_data]) - t_latest
            rocof_vals = np.array([p[1] for p in rocof_data])
            line_rocof.set_data(rocof_ts, rocof_vals)
            current_rocof = rocof_vals[-1]
            ax_rocof.set_ylim(*_rocof_ylim(rocof_vals))

        readout_freq = _readout_frequency(freq_ts, freq_vals)
        readout_text.set_text(f"{readout_freq:.3f} Hz\n{current_rocof:+.3f} Hz/s")
        return line_freq, line_rocof, readout_text

    def on_close(_event):
        stop_event.set()
        source.stop()

    fig.canvas.mpl_connect("close_event", on_close)

    # Keep a reference so the animation isn't garbage-collected mid-run.
    # cache_frame_data=False: this runs indefinitely, so caching every
    # rendered frame (matplotlib's default) would grow memory unboundedly.
    ani = FuncAnimation(
        fig,
        update,
        interval=UPDATE_INTERVAL_MS,
        blit=False,
        cache_frame_data=False,
    )
    fig._tremor_animation = ani  # noqa: SLF001

    plt.show()


def main() -> None:
    run_dashboard()


if __name__ == "__main__":
    main()
