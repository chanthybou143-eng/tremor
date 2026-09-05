"""
Live tail viewer for a running Project TREMOR overnight_log.py session
(milestone 5 tooling).

Pure file-reading: it polls the frequency/events CSVs a running
overnight_log.py is appending to and redraws a live matplotlib chart. It
never touches the serial port or spawns mpremote, so it's safe to run
alongside an in-progress overnight_log.py without any port conflict --
overnight_log.py keeps sole ownership of the Pico connection.

Style mirrors live_plot.py (a live matplotlib window driven by
FuncAnimation, red "RECONNECTING" state when the underlying source is
down) and plot_frequency_log.py (dropout spans shaded the same way,
frequency-over-time layout) -- this is the live version of that static
chart, watching a run as it happens instead of after the fact.

Usage:
    python3 live_tail_frequency.py [run_id]

Defaults:
    run_id   the most recent logs/frequency_<run_id>.csv found (by
             filename, which sorts chronologically since run_id is
             YYYYMMDD_HHMMSS)
"""

import glob
import os
import sys
from datetime import datetime, timezone

import matplotlib
matplotlib.use("MacOSX")  # native, fast backend on macOS -- same as live_plot.py
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.dates as mdates

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
POLL_INTERVAL_MS = 500
DEFAULT_YLIM_PAD_HZ = 1.0  # min half-height around the data's own mean; expands if data exceeds it


def _latest_run_id():
    candidates = sorted(glob.glob(os.path.join(LOG_DIR, "frequency_*.csv")))
    if not candidates:
        return None
    name = os.path.basename(candidates[-1])
    return name[len("frequency_"):-len(".csv")]


class CsvTailer:
    """Incrementally reads complete new lines appended to a growing CSV,
    parsing each into a dict keyed by the header row. Only complete
    ('\\n'-terminated) lines are consumed -- a line still being written by
    overnight_log.py's flush is left in the buffer until it's whole, so a
    read never races a concurrent writer into seeing a half-written row."""

    def __init__(self, path):
        self.path = path
        self._f = None
        self._buffer = ""
        self._header = None

    def _ensure_open(self):
        if self._f is None and os.path.exists(self.path):
            self._f = open(self.path, "r", newline="")

    def poll(self):
        self._ensure_open()
        if self._f is None:
            return []
        chunk = self._f.read()
        if not chunk:
            return []
        self._buffer += chunk
        *complete_lines, self._buffer = self._buffer.split("\n")

        rows = []
        for line in complete_lines:
            line = line.rstrip("\r")
            if not line:
                continue
            if self._header is None:
                self._header = line.split(",")
                continue
            values = line.split(",")
            rows.append(dict(zip(self._header, values)))
        return rows


def _reconnect_spans(events):
    """Pair each 'disconnected' with the next 'reconnected' into (start, end)
    spans -- same logic as plot_frequency_log.py's helper of the same name,
    duplicated (not imported) because that module selects the non-interactive
    Agg backend at import time, which would clobber this script's need for an
    interactive backend. A trailing unmatched 'disconnected' (still down as
    of the most recent poll) is paired with None, meaning "still open"."""
    spans = []
    open_start = None
    for e in events:
        if e["event"] == "disconnected":
            open_start = e["t"]
        elif e["event"] == "reconnected" and open_start is not None:
            spans.append((open_start, e["t"]))
            open_start = None
    if open_start is not None:
        spans.append((open_start, None))
    return spans


def _freq_ylim(freqs):
    lo, hi = min(freqs), max(freqs)
    mid = (lo + hi) / 2
    half = max(DEFAULT_YLIM_PAD_HZ, (hi - lo) / 2 * 1.2)
    return mid - half, mid + half


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else _latest_run_id()
    if run_id is None:
        print(f"No frequency_*.csv files found in {LOG_DIR}")
        sys.exit(1)

    freq_path = os.path.join(LOG_DIR, f"frequency_{run_id}.csv")
    events_path = os.path.join(LOG_DIR, f"events_{run_id}.csv")
    if not os.path.exists(freq_path):
        print(f"{freq_path} does not exist")
        sys.exit(1)

    print(f"Tailing {freq_path}")
    print(f"Tailing {events_path} for reconnect events")
    print("Read-only: no serial/mpremote connection is opened by this script.\n")

    freq_tailer = CsvTailer(freq_path)
    events_tailer = CsvTailer(events_path)

    times = []
    freqs = []
    events = []          # list of {"t": datetime, "event": str}
    span_patches = []    # currently-drawn axvspan artists, redrawn each frame

    fig, ax = plt.subplots(figsize=(11, 4.5))
    line, = ax.plot([], [], linewidth=1, color="tab:blue")
    ax.set_xlabel("time")
    ax.set_ylabel("frequency (Hz)")
    ax.axhline(50.0, color="gray", linewidth=0.5, linestyle="--")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
    fig.tight_layout()

    def update(_frame):
        for row in freq_tailer.poll():
            times.append(datetime.fromisoformat(row["timestamp"]))
            freqs.append(float(row["frequency_hz"]))
        for row in events_tailer.poll():
            events.append({
                "t": datetime.fromisoformat(row["timestamp"]),
                "event": row["event"],
            })

        for patch in span_patches:
            patch.remove()
        span_patches.clear()

        currently_down = bool(events) and events[-1]["event"] == "disconnected"
        spans = _reconnect_spans(events)
        now = datetime.now(timezone.utc)
        # The x-axis should track the latest *known* timestamp (last reading
        # or last event), not wall-clock "now" -- otherwise tailing a run
        # that has already stopped (or is idling between chunks) stretches
        # the axis out to the real current time and squashes all the actual
        # data into a sliver on the left. "now" is only the right choice
        # while a dropout is genuinely still open, so its shaded span keeps
        # growing in real time even though no new rows are arriving.
        known_times = [t for t in (times[-1] if times else None,
                                    events[-1]["t"] if events else None)
                       if t is not None]
        latest_known = max(known_times) if known_times else now
        chart_end = now if currently_down else latest_known
        for start, end in spans:
            patch = ax.axvspan(start, end if end is not None else chart_end,
                                color="tab:red", alpha=0.2)
            span_patches.append(patch)

        if times:
            line.set_data(times, freqs)
            ax.set_xlim(times[0], chart_end)
            ax.set_ylim(*_freq_ylim(freqs))

        if currently_down:
            ax.set_title("TREMOR overnight frequency log -- RECONNECTING",
                          color="tab:red")
        else:
            n = len(freqs)
            latest = f"{freqs[-1]:.3f} Hz" if freqs else "no data yet"
            ax.set_title(f"TREMOR overnight frequency log -- {n} readings, "
                         f"latest {latest}")
        return line,

    ani = animation.FuncAnimation(
        fig, update, interval=POLL_INTERVAL_MS, blit=False, cache_frame_data=False,
    )
    plt.show()


if __name__ == "__main__":
    main()
