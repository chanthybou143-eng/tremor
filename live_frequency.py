"""
Live grid-frequency reader for Project TREMOR (milestone 4).

Runs adc_stream_timed.py on the Pico via mpremote (same connect approach as
live_plot.py, including its dropped-USB auto-reconnect handling from
aa07bb7), decodes the timestamped voltage stream, and feeds ~1s chunks into
freq_estimator.estimate_frequency -- the zero-crossing + hysteresis estimator
already validated against synthetic 50Hz data in tests/test_freq_estimator.py
-- to print a live grid frequency reading.

adc_stream_timed.py (not adc_stream.py) is used here because the estimator
needs real per-sample timestamps: it infers sample rate from consecutive
timestamps to size its noise pre-filter, and a nominal "2kHz" assumption
(from adc_stream.py's time.sleep_us(500)) is wrong once print()/USB latency
is folded in -- the real measured rate on this hardware is ~1030Hz, not
2000Hz. adc_stream.py is left untouched so live_plot.py keeps working as
before.

Why a 4th-order Butterworth pre-filter instead of freq_estimator's own
moving-average (filter_window_s):
    An FFT of a real captured chunk (see git history / milestone-4 notes)
    showed the fundamental sitting right at ~49.8Hz as expected, but with a
    3rd harmonic at ~150Hz whose amplitude is ~80% of the fundamental's --
    almost certainly from the divider/clamp circuit, not just noise. A
    boxcar moving average has weak, non-monotonic rejection (nulls at
    fs/window, which for a ~1030Hz sample rate land uncomfortably close to
    50Hz itself before they've knocked the harmonic down enough) and a
    single-pole RC filter only manages ~9.5dB of attenuation between 50Hz
    and 150Hz -- neither suppresses the harmonic enough to stop it from
    registering as extra zero crossings, which is what caused early runs of
    this script to read 250-300Hz instead of 50Hz. A causal 4th-order
    Butterworth low-pass gives ~29dB of rejection at 150Hz for a 65-70Hz
    cutoff, which is enough. It's run with lfilter (not filtfilt) so it's
    causal and safe for a live per-chunk pipeline; filter state (zi) is
    carried across chunks so there's no re-settling transient every second.
    filter_window_s is left at 0.0 (freq_estimator's own pre-filter
    disabled) since this Butterworth stage replaces it.

dc_offset and hysteresis are recomputed from each chunk's own mean and
RMS amplitude rather than hardcoded, because the estimator's defaults
(dc_offset=0.0, hysteresis=0.05) were tuned against unit-amplitude synthetic
signals -- the real signal sits on a ~1.65V bias with a much smaller swing,
so the defaults would put the hysteresis band outside the signal entirely.
RMS amplitude (sine RMS = amplitude/sqrt(2)) is used instead of
(max-min)/2 because a handful of noise/glitch samples inflate a peak-based
estimate enough to make the hysteresis band wider than the signal's
*typical* per-cycle swing, which silently swallowed real crossings during
tuning.

The printed/aggregated frequency is the *median* of each chunk's per-cycle
estimates, not estimate_frequency()'s own returned mean: a single dropped
crossing merges two real cycles into one long low-frequency reading, and
(per this repo's existing convention -- see CLAUDE.md on
test_recovers_offset_with_noise) that kind of outlier blows up a mean much
more than a median.

Usage:
    python3 live_frequency.py [port] [adc_stream_path] [run_seconds]

Defaults:
    port              /dev/cu.usbmodem101
    adc_stream_path   adc_stream_timed.py (same folder as this script)
    run_seconds       35 (milestone 4 asks for >=30s of stable readings)
"""

import os
import queue
import statistics
import subprocess
import sys
import threading
import time

import numpy as np
from scipy.signal import butter, lfilter, lfilter_zi

from freq_estimator import estimate_frequency

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem101"
SCRIPT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "adc_stream_timed.py"
)
RUN_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 35.0

CHUNK_S = 1.0                # seconds of samples per estimator call
BUTTER_ORDER = 4
BUTTER_CUTOFF_HZ = 68.0      # see module docstring: clears the ~150Hz 3rd harmonic
HYSTERESIS_FRACTION = 0.05   # of RMS amplitude, on the *filtered* signal
SANITY_MIN_HZ, SANITY_MAX_HZ = 45.0, 55.0
RECONNECT_DELAY_S = 1.0

sample_q = queue.Queue()
status = {"connected": False, "proc": None}


def _spawn():
    return subprocess.Popen(
        [sys.executable, "-m", "mpremote", "connect", PORT, "run", SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _reader():
    # Same dropped-USB handling as live_plot.py: a jostled cable/EMI can
    # silently kill mpremote's subprocess, so keep respawning it rather than
    # stalling forever on the last sample received.
    while True:
        proc = _spawn()
        status["proc"] = proc
        status["connected"] = True

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


def _diagnose(chunk, dc_offset, hysteresis, amplitude, fs_est, exc=None):
    n = len(chunk)
    if n < 2:
        print(f"    diagnosis: only {n} sample(s) this chunk -- serial link "
              f"stalled or reconnecting")
        return
    print(f"    diagnosis: {n} samples, measured fs ~{fs_est:.0f} Hz, "
          f"filtered amplitude ~{amplitude:.4f}V, dc_offset {dc_offset:.4f}V, "
          f"hysteresis {hysteresis:.5f}V, butter cutoff={BUTTER_CUTOFF_HZ}Hz "
          f"order={BUTTER_ORDER}")
    if exc is not None:
        print(f"    estimator raised: {exc}")
    if fs_est < 400:
        print("    -> measured sample rate looks too low for reliable 50Hz "
              "zero-crossing detection (need several hundred Hz+ for enough "
              "samples per cycle); check for serial backpressure or "
              "print()/USB overhead on the Pico side")
    if amplitude < hysteresis:
        print("    -> hysteresis exceeds the filtered signal's own "
              "amplitude, so a crossing can never re-arm; check the AC "
              "source is actually connected")
    if fs_est > 0 and BUTTER_CUTOFF_HZ >= fs_est / 2:
        print("    -> Butterworth cutoff is at/above Nyquist for the "
              "measured sample rate -- the filter design is invalid for "
              "this fs")


def main():
    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    print(f"Connecting to Pico at {PORT}, running {SCRIPT} ...")
    print(f"Collecting {CHUNK_S:.1f}s chunks for {RUN_SECONDS:.0f}s total "
          f"(sanity range {SANITY_MIN_HZ}-{SANITY_MAX_HZ} Hz)\n")

    all_freqs = []
    start = time.monotonic()
    butter_state = {"b": None, "a": None, "zi": None}

    while time.monotonic() - start < RUN_SECONDS:
        elapsed = time.monotonic() - start
        chunk = _drain_chunk(CHUNK_S)
        if not chunk:
            state = "RECONNECTING" if not status["connected"] else "no samples"
            print(f"[{elapsed:5.1f}s] {state} -- waiting for data")
            continue

        timestamps = [t for t, _ in chunk]
        raw_voltages = np.array([v for _, v in chunk])
        span_s = timestamps[-1] - timestamps[0]
        fs_est = (len(chunk) - 1) / span_s if span_s > 0 else float("nan")

        # Design the Butterworth filter once, from the first chunk's
        # measured sample rate, and reuse it -- fs is stable in practice
        # (this hardware measures ~1027-1035Hz chunk to chunk).
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
            print(f"[{elapsed:5.1f}s] estimator failed")
            _diagnose(chunk, dc_offset, hysteresis, amplitude, fs_est, exc)
            continue

        chunk_median = statistics.median(f for _, f in per_cycle)
        in_range = SANITY_MIN_HZ <= chunk_median <= SANITY_MAX_HZ
        flag = "" if in_range else "  <-- OUT OF RANGE"
        print(f"[{elapsed:5.1f}s] {chunk_median:8.4f} Hz "
              f"({len(per_cycle)} cycles){flag}")
        if not in_range:
            _diagnose(chunk, dc_offset, hysteresis, amplitude, fs_est)

        all_freqs.extend(f for _, f in per_cycle)

    if status["proc"] is not None:
        status["proc"].terminate()

    if not all_freqs:
        print("\nNo frequency readings obtained -- see diagnostics above.")
        return

    median_freq = statistics.median(all_freqs)
    print(f"\n{len(all_freqs)} cycles over {time.monotonic() - start:.1f}s")
    print(f"median frequency: {median_freq:.4f} Hz "
          f"(min {min(all_freqs):.4f}, max {max(all_freqs):.4f})")


if __name__ == "__main__":
    main()
