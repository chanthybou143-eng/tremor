"""
Static chart for a Project TREMOR overnight frequency log (milestone 5).

Reads the (timestamp, elapsed_s, frequency_hz, ...) CSV produced by
overnight_log.py, plus its companion (timestamp, elapsed_s, event) events
CSV, and renders a frequency-over-time chart with USB disconnect/reconnect
periods shaded -- the static-chart equivalent of live_plot.py's red
"RECONNECTING" state, but as a finished PNG rather than a live view, so a
gap can be told apart as "USB drop" vs "genuinely no data" after the fact.

Usage:
    python3 plot_frequency_log.py <frequency_csv> [events_csv] [output_png]

Defaults:
    events_csv   frequency_<run_id>.csv -> events_<run_id>.csv (same run)
    output_png   same path as frequency_csv with .png instead of .csv
"""

import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")  # no display needed -- this renders a finished PNG
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime


def _default_events_path(freq_csv_path):
    directory, name = os.path.split(freq_csv_path)
    if name.startswith("frequency_"):
        return os.path.join(directory, "events_" + name[len("frequency_"):])
    return None


def _load_frequency_rows(path):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "t": datetime.fromisoformat(row["timestamp"]),
                "elapsed_s": float(row["elapsed_s"]),
                "freq_hz": float(row["frequency_hz"]),
                "amplitude_v": float(row["amplitude_v"]),
                "min_v": float(row["min_v"]),
                "max_v": float(row["max_v"]),
                "n_cycles": int(row["n_cycles"]),
            })
    return rows


def _load_events(path):
    events = []
    if path is None or not os.path.exists(path):
        return events
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            events.append({
                "t": datetime.fromisoformat(row["timestamp"]),
                "elapsed_s": float(row["elapsed_s"]),
                "event": row["event"],
            })
    return events


def _reconnect_spans(events):
    """Pair each 'disconnected' with the next 'reconnected' into (start, end)
    spans. A trailing unmatched 'disconnected' (still down when the log
    ends) is paired with None, meaning "runs to the end of the chart"."""
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


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <frequency_csv> [events_csv] [output_png]")
        sys.exit(1)

    freq_csv_path = sys.argv[1]
    events_csv_path = sys.argv[2] if len(sys.argv) > 2 else _default_events_path(freq_csv_path)
    output_png_path = sys.argv[3] if len(sys.argv) > 3 else os.path.splitext(freq_csv_path)[0] + ".png"

    rows = _load_frequency_rows(freq_csv_path)
    if not rows:
        print(f"No frequency readings in {freq_csv_path} -- nothing to plot.")
        sys.exit(1)
    events = _load_events(events_csv_path)
    spans = _reconnect_spans(events)

    times = [r["t"] for r in rows]
    freqs = [r["freq_hz"] for r in rows]

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(times, freqs, linewidth=1, color="tab:blue", label="frequency")

    chart_end = times[-1]
    for i, (start, end) in enumerate(spans):
        end = end if end is not None else chart_end
        ax.axvspan(start, end, color="tab:red", alpha=0.2,
                   label="USB dropout" if i == 0 else None)

    ax.set_ylabel("frequency (Hz)")
    ax.set_xlabel("time")
    ax.set_title("TREMOR overnight grid frequency log")
    ax.axhline(50.0, color="gray", linewidth=0.5, linestyle="--")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()
    if spans:
        ax.legend(loc="upper right")

    n_dropouts = len(spans)
    fig.text(0.01, 0.01,
              f"{len(rows)} readings, {n_dropouts} USB dropout(s)",
              fontsize=8, color="gray")

    fig.tight_layout()
    fig.savefig(output_png_path, dpi=150)
    print(f"Wrote {output_png_path}")
    print(f"{len(rows)} readings, {n_dropouts} reconnect gap(s) shaded")


if __name__ == "__main__":
    main()
