"""
Overnight grid-frequency logger for Project TREMOR (milestone 5 groundwork).

Long-running counterpart to live_frequency.py: same mpremote connect +
auto-reconnect approach (aa07bb7) and the same milestone-4 estimation
pipeline (Butterworth pre-filter, per-chunk dc_offset/hysteresis, median
aggregation over freq_estimator.estimate_frequency's per-cycle output), but
instead of printing to the console it appends every chunk's reading to a
CSV file and keeps running indefinitely -- designed to survive hours
unattended, including multiple USB drops/reconnects.

Three output files per run (timestamped so repeated runs never clobber
each other):
    logs/frequency_<run_id>.csv   one row per chunk: timestamp, elapsed_s,
                                   gps_utc_s, frequency_hz, amplitude_v,
                                   min_v, max_v, n_cycles ("timestamp" is
                                   the host's own wall clock at write time,
                                   same as before; "gps_utc_s" is the
                                   GPS/PPS-derived UTC seconds-of-day from
                                   the chunk's last sample, blank until the
                                   unit has PPS sync -- see
                                   _parse_sample_line())
    logs/events_<run_id>.csv      one row per connection state change or
                                   suspected sleep gap: timestamp, elapsed_s,
                                   event (event is "disconnected",
                                   "reconnected", or "possible_sleep_gap_Ns")
    logs/raw_snippets_<run_id>.csv   one row per raw sample, but only during
                                   a short snippet captured every
                                   RAW_SNIPPET_INTERVAL_S: snippet_id, t_s,
                                   voltage_v. t_s is the device's own
                                   per-sample clock (from adc_stream_gps.py's
                                   t_us field), so sample-to-sample spacing
                                   within one snippet_id is exact even though
                                   it isn't host wall-clock time -- see
                                   _iso_now()-stamped "raw_snippet_N_saved"
                                   events in events_<run_id>.csv for when
                                   each snippet was actually taken.

The per-chunk summary (frequency_hz, amplitude_v, min_v, max_v) is all this
script has ever kept -- raw_voltages and filtered are chunk-local variables,
discarded once the summary row is written, so there was previously no way
to ask a question like "does the ~150Hz 3rd harmonic's size relative to the
50Hz fundamental change with grid noise conditions" from an already-finished
run's log. Saving every sample all night would run to tens of GB at
~1030Hz; RAW_SNIPPET_DURATION_S (~5s, ~5000 samples) per
RAW_SNIPPET_INTERVAL_S (5 min) keeps a whole overnight run's raw snippets in
the tens-of-MB range while giving ~0.2Hz FFT bin resolution -- comfortably
enough to separate the 50Hz fundamental from its ~150Hz 3rd harmonic. A
snippet in progress when a disconnect/reconnect fires is discarded, not
saved: adc_stream_gps.py's t0 (and therefore every t_us it prints) resets
to zero when the on-device script restarts after a reconnect, so a snippet
spanning that boundary would have a corrupt, non-monotonic time axis.

A real disconnect/reconnect (mpremote's subprocess dying and respawning) is
not the only way an overnight run can silently lose data: if the machine
itself sleeps, the whole process tree freezes with it -- mpremote never
crashes, so no disconnect event fires, but no samples arrive either for
however long the sleep lasted. That gap is only visible as a mismatch
between wall-clock time (which keeps advancing through a sleep, once the
machine wakes and the OS clock catches up) and monotonic time (which
freezes along with the process, since time.monotonic() on macOS is
implemented as mach_absolute_time(), which pauses during actual system
sleep). _check_sleep_gap() compares the two once per loop iteration and
logs a "possible_sleep_gap_Ns" event when wall-clock time has run far ahead
of monotonic time with no corresponding disconnect event to explain it --
this was discovered and measured (~20 minutes silently lost, unlogged,
across five gaps in a single ~1hr test run) before this heuristic existed.

Kept as separate files rather than one CSV with a "row type" column so
each stays a flat, uniform-width table that loads trivially later (e.g.
pandas.read_csv with no special-casing) -- exactly the "gap because of a
USB drop" vs "genuinely no data" distinction the plotting script
(plot_frequency_log.py) needs comes from joining frequency rows against
events rows by timestamp, not from parsing mixed row shapes. Same reasoning
extends to raw_snippets_<run_id>.csv: a totally different row shape
(per-sample, not per-chunk) that would force special-casing onto every
existing reader if it shared a file with either of the other two.

Every row is flushed and fsync'd immediately after writing specifically so
a kill of the mpremote subprocess (or a real overnight power blip) can't
lose already-logged data -- this is the same property test_frequency's
"auto-reconnect" fix cares about, just extended to the log file as well as
the live plot.

_parse_sample_line() (same helper as live_frequency.py -- see that
module's docstring for the full story) accepts either wire schema that
can arrive on stdout: adc_stream_timed.py's 2-field "t_us,voltage" (no
GPS) or adc_stream_gps.py's 3-field "t_us,raw_u16,utc_s" (raw ADC counts
+ GPS UTC seconds-of-day, blank until PPS sync). Pointed at
adc_stream_gps.py's output, the old 2-field-only parser folded the
raw/utc fields together and float() raised on every line, silently
dropping all samples -- this is why adc_stream_gps.py is now the default
below instead of adc_stream_timed.py.

Usage:
    python3 overnight_log.py [port] [adc_stream_path] [run_seconds]

Defaults:
    port              /dev/cu.usbmodem101
    adc_stream_path   adc_stream_gps.py (same folder as this script)
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
    os.path.dirname(os.path.abspath(__file__)), "adc_stream_gps.py"
)
RUN_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0  # 0 = forever

CHUNK_S = 1.0                # seconds of samples per estimator call
BUTTER_ORDER = 4
BUTTER_CUTOFF_HZ = 68.0      # see live_frequency.py: clears the ~150Hz 3rd harmonic
HYSTERESIS_FRACTION = 0.05   # of RMS amplitude, on the *filtered* signal
RECONNECT_DELAY_S = 1.0
SLEEP_GAP_THRESHOLD_S = 5.0  # see _check_sleep_gap: comfortably above normal
                             # ~1s cadence, comfortably below any real gap seen

RAW_SNIPPET_INTERVAL_S = 300  # how often a raw snippet capture starts
RAW_SNIPPET_DURATION_S = 5    # ~5000 samples at ~1030Hz -- see module docstring

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
FREQ_LOG_PATH = os.path.join(LOG_DIR, f"frequency_{RUN_ID}.csv")
EVENTS_LOG_PATH = os.path.join(LOG_DIR, f"events_{RUN_ID}.csv")
RAW_SNIPPET_LOG_PATH = os.path.join(LOG_DIR, f"raw_snippets_{RUN_ID}.csv")

sample_q = queue.Queue()
event_q = queue.Queue()
status = {"connected": False, "proc": None}


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _parse_sample_line(line_text):
    """Parse one stdout line from either adc_stream_timed.py (2-field
    "t_us,voltage") or adc_stream_gps.py (3-field "t_us,raw_u16,utc_s").
    Returns (t_s, voltage, gps_utc_s) or None for non-data lines (mpremote
    banners, blank lines) or malformed fields. gps_utc_s is None for the
    2-field schema or before GPS/PPS sync (adc_stream_gps.py prints an
    empty utc field until then)."""
    fields = line_text.split(",")
    if len(fields) == 2:
        t_field, value_field, utc_field = fields[0], fields[1], ""
    elif len(fields) == 3:
        t_field, value_field, utc_field = fields
    else:
        return None
    try:
        t_s = int(t_field) / 1e6
        voltage = float(value_field) if len(fields) == 2 else int(value_field) * 3.3 / 65535
        gps_utc_s = float(utc_field) if utc_field else None
    except ValueError:
        return None
    return t_s, voltage, gps_utc_s


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
            parsed = _parse_sample_line(line_text.strip())
            if parsed is None:
                continue  # skip banner / non-data / malformed lines from mpremote
            sample_q.put(parsed)

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


def _check_sleep_gap(last_wall, last_monotonic):
    """Compare wall-clock elapsed time against monotonic elapsed time since
    the last check. A real disconnect/reconnect still advances both clocks
    together at their normal rate (the process keeps looping, just without
    samples), so this only fires for the specific "the whole machine was
    asleep" signature: monotonic time barely moved while wall-clock time
    jumped. Returns (gap_s or None, new_last_wall, new_last_monotonic)."""
    now_wall = datetime.now(timezone.utc)
    now_monotonic = time.monotonic()
    wall_delta = (now_wall - last_wall).total_seconds()
    monotonic_delta = now_monotonic - last_monotonic
    gap_s = wall_delta - monotonic_delta
    if gap_s <= SLEEP_GAP_THRESHOLD_S:
        gap_s = None
    return gap_s, now_wall, now_monotonic


def _flush_row(writer, f, row):
    writer.writerow(row)
    f.flush()
    os.fsync(f.fileno())


def _flush_rows(writer, f, rows):
    # One flush+fsync for the whole batch, not per row -- a raw snippet is
    # ~5000 rows, and fsyncing each individually (as _flush_row does for the
    # once-a-second summary/event rows) would stall the main loop for
    # seconds every RAW_SNIPPET_INTERVAL_S for no benefit here.
    writer.writerows(rows)
    f.flush()
    os.fsync(f.fileno())


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    print(f"Connecting to Pico at {PORT}, running {SCRIPT} ...")
    print(f"Logging frequency to {FREQ_LOG_PATH}")
    print(f"Logging connection events to {EVENTS_LOG_PATH}")
    print(f"Logging raw snippets ({RAW_SNIPPET_DURATION_S:.0f}s every "
          f"{RAW_SNIPPET_INTERVAL_S:.0f}s) to {RAW_SNIPPET_LOG_PATH}")
    if RUN_SECONDS > 0:
        print(f"Will stop after {RUN_SECONDS:.0f}s (test mode).\n")
    else:
        print("Running until interrupted (Ctrl+C) -- this is the overnight mode.\n")

    with open(FREQ_LOG_PATH, "a", newline="") as freq_f, \
         open(EVENTS_LOG_PATH, "a", newline="") as events_f, \
         open(RAW_SNIPPET_LOG_PATH, "a", newline="") as snippet_f:

        freq_writer = csv.writer(freq_f)
        events_writer = csv.writer(events_f)
        snippet_writer = csv.writer(snippet_f)
        if freq_f.tell() == 0:
            _flush_row(freq_writer, freq_f, [
                "timestamp", "elapsed_s", "gps_utc_s", "frequency_hz",
                "amplitude_v", "min_v", "max_v", "n_cycles",
            ])
        if events_f.tell() == 0:
            _flush_row(events_writer, events_f, ["timestamp", "elapsed_s", "event"])
        if snippet_f.tell() == 0:
            _flush_row(snippet_writer, snippet_f, ["snippet_id", "t_s", "voltage_v"])

        start = time.monotonic()
        butter_state = {"b": None, "a": None, "zi": None}
        last_wall = datetime.now(timezone.utc)
        last_monotonic = start
        snippet_id = 0
        snippet_collecting = False
        snippet_rows = []
        snippet_span_s = 0.0
        next_snippet_at = 0.0  # 0 => the first chunk starts collecting immediately

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
                    if snippet_collecting:
                        print(f"[{elapsed:7.1f}s] discarding in-progress raw "
                              f"snippet {snippet_id} (reconnect mid-capture)")
                        snippet_collecting = False

                chunk = _drain_chunk(CHUNK_S)

                gap_s, last_wall, last_monotonic = _check_sleep_gap(last_wall, last_monotonic)
                if gap_s is not None:
                    event = f"possible_sleep_gap_{gap_s:.0f}s"
                    print(f"[{elapsed:7.1f}s] EVENT: {event}")
                    _flush_row(events_writer, events_f, [_iso_now(), f"{elapsed:.3f}", event])
                    butter_state.update(b=None, a=None, zi=None)
                    if snippet_collecting:
                        print(f"[{elapsed:7.1f}s] discarding in-progress raw "
                              f"snippet {snippet_id} (sleep gap mid-capture)")
                        snippet_collecting = False
                if not chunk:
                    state = "reconnecting" if not status["connected"] else "no samples"
                    print(f"[{elapsed:7.1f}s] {state} -- waiting for data")
                    continue

                timestamps = [t for t, _, _ in chunk]
                raw_voltages = np.array([v for _, v, _ in chunk])
                gps_utc_values = [u for _, _, u in chunk if u is not None]
                span_s = timestamps[-1] - timestamps[0]
                fs_est = (len(chunk) - 1) / span_s if span_s > 0 else float("nan")

                # Raw snippet capture: periodically stash a few seconds of
                # unfiltered voltage for offline spectral analysis (see
                # module docstring). Uses this chunk's already-computed
                # raw_voltages/timestamps -- no extra sampling, just an
                # extra thing done with data already in hand.
                if not snippet_collecting and elapsed >= next_snippet_at:
                    snippet_collecting = True
                    snippet_id += 1
                    snippet_rows = []
                    snippet_span_s = 0.0

                if snippet_collecting:
                    snippet_rows.extend(
                        (snippet_id, f"{t:.6f}", f"{v:.5f}")
                        for t, v in zip(timestamps, raw_voltages)
                    )
                    snippet_span_s += span_s
                    if snippet_span_s >= RAW_SNIPPET_DURATION_S:
                        _flush_rows(snippet_writer, snippet_f, snippet_rows)
                        event = f"raw_snippet_{snippet_id}_saved"
                        print(f"[{elapsed:7.1f}s] EVENT: {event} "
                              f"({len(snippet_rows)} samples, {snippet_span_s:.1f}s)")
                        _flush_row(events_writer, events_f, [_iso_now(), f"{elapsed:.3f}", event])
                        snippet_collecting = False
                        next_snippet_at = elapsed + RAW_SNIPPET_INTERVAL_S

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
                gps_utc_s = gps_utc_values[-1] if gps_utc_values else None
                gps_utc_field = "" if gps_utc_s is None else f"{gps_utc_s:.3f}"
                print(f"[{elapsed:7.1f}s] {freq_hz:8.4f} Hz "
                      f"({len(per_cycle)} cycles, amp {amplitude:.4f}V) "
                      f"gps_utc={gps_utc_field or 'no sync'}")

                _flush_row(freq_writer, freq_f, [
                    _iso_now(), f"{elapsed:.3f}", gps_utc_field, f"{freq_hz:.4f}",
                    f"{amplitude:.5f}", f"{min(filtered):.5f}",
                    f"{max(filtered):.5f}", len(per_cycle),
                ])
        except KeyboardInterrupt:
            print("\nStopped by user.")

    if status["proc"] is not None:
        status["proc"].terminate()

    print(f"\nDone. Frequency log: {FREQ_LOG_PATH}")
    print(f"Events log: {EVENTS_LOG_PATH}")
    print(f"Raw snippets log: {RAW_SNIPPET_LOG_PATH}")


if __name__ == "__main__":
    main()
