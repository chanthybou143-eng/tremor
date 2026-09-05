"""
Overnight grid-frequency logger for Project TREMOR (milestone 5 groundwork).

Long-running counterpart to live_frequency.py: same mpremote connect +
auto-reconnect approach (aa07bb7) and the same milestone-4 estimation
pipeline (Butterworth pre-filter, per-chunk dc_offset/hysteresis, median
aggregation over freq_estimator.estimate_frequency's per-cycle output), but
instead of printing to the console it appends every chunk's reading to a
CSV file and keeps running indefinitely -- designed to survive hours
unattended, including multiple USB drops/reconnects.

Two output files per run (timestamped so repeated runs never clobber each
other):
    logs/frequency_<run_id>.csv   one row per chunk: timestamp, elapsed_s,
                                   frequency_hz, amplitude_v, min_v, max_v,
                                   n_cycles
    logs/events_<run_id>.csv      one row per connection state change:
                                   timestamp, elapsed_s, event
                                   (event is "disconnected" or "reconnected")

Kept as two separate files rather than one CSV with a "row type" column so
each stays a flat, uniform-width table that loads trivially later (e.g.
pandas.read_csv with no special-casing) -- exactly the "gap because of a
USB drop" vs "genuinely no data" distinction the plotting script
(plot_frequency_log.py) needs comes from joining frequency rows against
events rows by timestamp, not from parsing mixed row shapes.

Every row is flushed and fsync'd immediately after writing specifically so
a kill of the mpremote subprocess (or a real overnight power blip) can't
lose already-logged data -- this is the same property test_frequency's
"auto-reconnect" fix cares about, just extended to the log file as well as
the live plot.

Usage:
    python3 overnight_log.py [port] [adc_stream_path] [run_seconds]

Defaults:
    port              /dev/cu.usbmodem101
    adc_stream_path   adc_stream_timed.py (same folder as this script)
    run_seconds       0 (run forever; pass a number to stop after N seconds,
                       used for the short test runs before the real
                       overnight run)
"""

import csv
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
from scipy.signal import butter, lfilter, lfilter_zi

from freq_estimator import estimate_frequency

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem101"
SCRIPT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "adc_stream_timed.py"
)
RUN_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0  # 0 = forever

CHUNK_S = 1.0                # seconds of samples per estimator call
BUTTER_ORDER = 4
BUTTER_CUTOFF_HZ = 68.0      # see live_frequency.py: clears the ~150Hz 3rd harmonic
HYSTERESIS_FRACTION = 0.05   # of RMS amplitude, on the *filtered* signal
RECONNECT_DELAY_S = 1.0

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
FREQ_LOG_PATH = os.path.join(LOG_DIR, f"frequency_{RUN_ID}.csv")
EVENTS_LOG_PATH = os.path.join(LOG_DIR, f"events_{RUN_ID}.csv")

sample_q = queue.Queue()
event_q = queue.Queue()
status = {"connected": False, "proc": None}


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _spawn():
    return subprocess.Popen(
        [sys.executable, "-m", "mpremote", "connect", PORT, "run", SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _reader():
    # Same dropped-USB handling as live_plot.py/live_frequency.py: a jostled
    # cable/EMI can silently kill mpremote's subprocess, so keep respawning
    # it rather than stalling forever on the last sample received. Each
    # transition is pushed onto event_q with its own timestamp (taken right
    # at the transition, not later when the main loop happens to poll) so
    # the event log's timing is accurate even if the main loop is mid-chunk.
    first_connection = True
    while True:
        proc = _spawn()
        status["proc"] = proc
        status["connected"] = True
        if not first_connection:
            event_q.put((_iso_now(), "reconnected"))
        first_connection = False

        for line_text in proc.stdout:
            line_text = line_text.strip()
            if "," not in line_text:
                continue  # skip banner / non-data lines from mpremote
            t_field, v_field = line_text.split(",", 1)
            try:
                t_s = int(t_field) / 1e6
                voltage = float(v_field)
            except ValueError:
                continue
            sample_q.put((t_s, voltage))

        status["connected"] = False
        event_q.put((_iso_now(), "disconnected"))
        proc.wait()
        time.sleep(RECONNECT_DELAY_S)


def _drain_chunk(timeout_s):
    """Collect whatever samples arrive over the next timeout_s seconds."""
    deadline = time.monotonic() + timeout_s
    chunk = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            chunk.append(sample_q.get(timeout=remaining))
        except queue.Empty:
            break
    return chunk


def _drain_events():
    events = []
    while True:
        try:
            events.append(event_q.get_nowait())
        except queue.Empty:
            break
    return events


def _flush_row(writer, f, row):
    writer.writerow(row)
    f.flush()
    os.fsync(f.fileno())


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    print(f"Connecting to Pico at {PORT}, running {SCRIPT} ...")
    print(f"Logging frequency to {FREQ_LOG_PATH}")
    print(f"Logging connection events to {EVENTS_LOG_PATH}")
    if RUN_SECONDS > 0:
        print(f"Will stop after {RUN_SECONDS:.0f}s (test mode).\n")
    else:
        print("Running until interrupted (Ctrl+C) -- this is the overnight mode.\n")

    with open(FREQ_LOG_PATH, "a", newline="") as freq_f, \
         open(EVENTS_LOG_PATH, "a", newline="") as events_f:

        freq_writer = csv.writer(freq_f)
        events_writer = csv.writer(events_f)
        if freq_f.tell() == 0:
            _flush_row(freq_writer, freq_f, [
                "timestamp", "elapsed_s", "frequency_hz", "amplitude_v",
                "min_v", "max_v", "n_cycles",
            ])
        if events_f.tell() == 0:
            _flush_row(events_writer, events_f, ["timestamp", "elapsed_s", "event"])

        start = time.monotonic()
        butter_state = {"b": None, "a": None, "zi": None}

        try:
            while RUN_SECONDS <= 0 or time.monotonic() - start < RUN_SECONDS:
                elapsed = time.monotonic() - start

                for ts_iso, event in _drain_events():
                    print(f"[{elapsed:7.1f}s] EVENT: {event}")
                    _flush_row(events_writer, events_f, [ts_iso, f"{elapsed:.3f}", event])
                    # Filter state (zi) and design are only meaningful for a
                    # contiguous stream; a disconnect/reconnect gap breaks
                    # that, so force a redesign+reseed from the next chunk's
                    # own data rather than carrying stale state across it.
                    butter_state.update(b=None, a=None, zi=None)

                chunk = _drain_chunk(CHUNK_S)
                if not chunk:
                    state = "reconnecting" if not status["connected"] else "no samples"
                    print(f"[{elapsed:7.1f}s] {state} -- waiting for data")
                    continue

                timestamps = [t for t, _ in chunk]
                raw_voltages = np.array([v for _, v in chunk])
                span_s = timestamps[-1] - timestamps[0]
                fs_est = (len(chunk) - 1) / span_s if span_s > 0 else float("nan")

                # Butterworth filter is (re)designed whenever we don't have
                # one yet -- covers both the very first chunk and any chunk
                # right after a reconnect, since filter state doesn't carry
                # meaning across a gap in the data anyway.
                if butter_state["b"] is None:
                    nyquist_hz = fs_est / 2
                    b, a = butter(BUTTER_ORDER, BUTTER_CUTOFF_HZ / nyquist_hz, btype="low")
                    zi = lfilter_zi(b, a) * raw_voltages[0]
                    butter_state.update(b=b, a=a, zi=zi)

                filtered, butter_state["zi"] = lfilter(
                    butter_state["b"], butter_state["a"], raw_voltages,
                    zi=butter_state["zi"],
                )
                filtered = filtered.tolist()

                dc_offset = statistics.fmean(filtered)
                variance = statistics.fmean((v - dc_offset) ** 2 for v in filtered)
                amplitude = (2 * variance) ** 0.5
                hysteresis = HYSTERESIS_FRACTION * amplitude

                try:
                    _mean_freq, per_cycle = estimate_frequency(
                        timestamps, filtered, dc_offset=dc_offset,
                        hysteresis=hysteresis, filter_window_s=0.0,
                    )
                except ValueError as exc:
                    print(f"[{elapsed:7.1f}s] estimator failed: {exc}")
                    continue

                freq_hz = statistics.median(f for _, f in per_cycle)
                print(f"[{elapsed:7.1f}s] {freq_hz:8.4f} Hz "
                      f"({len(per_cycle)} cycles, amp {amplitude:.4f}V)")

                _flush_row(freq_writer, freq_f, [
                    _iso_now(), f"{elapsed:.3f}", f"{freq_hz:.4f}",
                    f"{amplitude:.5f}", f"{min(filtered):.5f}",
                    f"{max(filtered):.5f}", len(per_cycle),
                ])
        except KeyboardInterrupt:
            print("\nStopped by user.")

    if status["proc"] is not None:
        status["proc"].terminate()

    print(f"\nDone. Frequency log: {FREQ_LOG_PATH}")
    print(f"Events log: {EVENTS_LOG_PATH}")


if __name__ == "__main__":
    main()
