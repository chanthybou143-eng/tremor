"""Overnight/soak-test wrapper for wifi_unit_client.py.

Reuses overnight_log.py's already-validated host-side auto-reconnect and
sleep-gap-detection machinery (proven across the original 10.5h/11h
GPS-polling soaks and tonight's earlier 4h run) rather than reinventing
it, so a soak of wifi_unit_client.py tests exactly one new thing -- the
dual-core redesign under real long-run conditions -- not a second,
unvalidated host-side mechanism at the same time.

Unlike overnight_log.py, there's no raw-sample parsing or frequency
estimation to do here: wifi_unit_client.py already does all of that
on-device and ships results over WiFi. This wrapper's only job is to keep
the mpremote connection alive across USB drops/reconnects, and persist
wifi_unit_client.py's own periodic "# STATUS" print lines to a host-side
log file, each stamped with the host's wall-clock time of receipt --
wifi_unit_client.py's own elapsed_s is relative to its own process start
and resets to 0 across a device-level reconnect/reset, so the host
timestamp is what keeps the combined log globally orderable across one.

Analysis afterward: since wifi_unit_client.py runs with TEST_DURATION_S=None
for a real soak (matching how the original GPS-polling soaks ran
adc_stream_gps.py -- no graceful on-device stop, just terminated from the
host when RUN_SECONDS elapses, same coarse stop this wrapper uses), there's
no on-device FINAL summary line. sync_count/pps_count/overflow_count are
cumulative counters printed every STATUS_INTERVAL_S, so the last "# STATUS"
line captured before termination is the equivalent of Test C's FINAL line.

Usage:
    python3 overnight_wifi_log.py [port] [script_path] [run_seconds]

Defaults:
    port         /dev/cu.usbmodem101
    script_path  wifi_unit_client.py (same folder as this script)
    run_seconds  0 (run forever; pass a number to stop after N seconds)
"""

import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem101"
SCRIPT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wifi_unit_client.py"
)
RUN_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0  # 0 = forever

RECONNECT_DELAY_S = 1.0
SLEEP_GAP_THRESHOLD_S = 5.0  # see overnight_log.py's _check_sleep_gap for the rationale

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH = os.path.join(LOG_DIR, f"wifi_soak_{RUN_ID}.log")

line_q = queue.Queue()
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
    # Same auto-reconnect loop as overnight_log.py's _reader() -- a
    # jostled cable/EMI can silently kill mpremote's subprocess, so keep
    # respawning it rather than stalling forever on the last line received.
    first_connection = True
    while True:
        proc = _spawn()
        status["proc"] = proc
        status["connected"] = True
        if not first_connection:
            line_q.put((_iso_now(), "EVENT reconnected"))
        first_connection = False

        for line_text in proc.stdout:
            line_q.put((_iso_now(), line_text.rstrip("\n")))

        status["connected"] = False
        line_q.put((_iso_now(), "EVENT disconnected"))
        proc.wait()
        time.sleep(RECONNECT_DELAY_S)


def _check_sleep_gap(last_wall, last_monotonic):
    # Identical logic to overnight_log.py's _check_sleep_gap: a real
    # disconnect/reconnect still advances both clocks together, so this
    # only fires for the specific "the whole machine was asleep" signature.
    now_wall = datetime.now(timezone.utc)
    now_monotonic = time.monotonic()
    wall_delta = (now_wall - last_wall).total_seconds()
    monotonic_delta = now_monotonic - last_monotonic
    gap_s = wall_delta - monotonic_delta
    if gap_s <= SLEEP_GAP_THRESHOLD_S:
        gap_s = None
    return gap_s, now_wall, now_monotonic


def _write_line(f, text):
    print(text)
    f.write(text + "\n")
    f.flush()
    os.fsync(f.fileno())  # survive a kill/power blip without losing already-logged data


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    print(f"Connecting to Pico at {PORT}, running {SCRIPT} ...")
    print(f"Logging to {LOG_PATH}")
    if RUN_SECONDS > 0:
        print(f"Will stop after {RUN_SECONDS:.0f}s.\n")
    else:
        print("Running until interrupted (Ctrl+C) -- this is the overnight mode.\n")

    start = time.monotonic()
    last_wall = datetime.now(timezone.utc)
    last_monotonic = start

    with open(LOG_PATH, "a") as f:
        try:
            while RUN_SECONDS <= 0 or time.monotonic() - start < RUN_SECONDS:
                gap_s, last_wall, last_monotonic = _check_sleep_gap(last_wall, last_monotonic)
                if gap_s is not None:
                    _write_line(f, f"{_iso_now()} EVENT possible_sleep_gap_{gap_s:.0f}s")

                try:
                    ts_iso, text = line_q.get(timeout=1.0)
                except queue.Empty:
                    continue
                _write_line(f, f"{ts_iso} {text}")
        except KeyboardInterrupt:
            print("\nStopped by user.")

    if status["proc"] is not None:
        status["proc"].terminate()

    print(f"\nDone. Log: {LOG_PATH}")


if __name__ == "__main__":
    main()
