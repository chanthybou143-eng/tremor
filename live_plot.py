"""
Live waveform viewer for Project TREMOR.

Runs adc_stream.py on the Pico via mpremote, reads the printed voltage
values over serial, and plots the last few seconds live in a scrolling
matplotlib window.

Usage:
    python3 live_plot.py [port] [adc_stream_path]

Defaults:
    port              /dev/cu.usbmodem101
    adc_stream_path   adc_stream.py (same folder as this script)
"""

import subprocess
import sys
import os
import threading
import time
from collections import deque

import matplotlib
matplotlib.use("MacOSX")  # native, fast backend on macOS
import matplotlib.pyplot as plt
import matplotlib.animation as animation

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem101"
SCRIPT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "adc_stream.py"
)

WINDOW_SECONDS = 3.0
SAMPLE_INTERVAL_S = 0.0005  # matches time.sleep_us(500) on the Pico
MAX_POINTS = int(WINDOW_SECONDS / SAMPLE_INTERVAL_S)

buf = deque(maxlen=MAX_POINTS)
buf_lock = threading.Lock()
status = {"connected": False, "proc": None}
RECONNECT_DELAY_S = 1.0  # give the USB device a moment to re-enumerate

def _spawn():
    # mpremote's console-script entry point isn't guaranteed to be on PATH
    # (it wasn't in this environment -- it's pip-installed under
    # ~/Library/Python/3.9/bin, which isn't on PATH), so invoke it as a
    # module via the current interpreter instead of relying on shell PATH
    # resolution.
    return subprocess.Popen(
        [sys.executable, "-m", "mpremote", "connect", PORT, "run", SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

def _reader():
    # proc.stdout.readline() blocks until a line arrives, so draining it
    # inside the matplotlib animation callback (as "read until nothing's
    # left") doesn't work: at ~2kHz that either stalls the first frame for
    # seconds filling the buffer, or -- once the buffer is full -- only
    # drains one line per frame while the OS pipe backs up and eventually
    # backpressures the Pico's own print() calls. Instead, drain
    # continuously in a background thread and let the UI just snapshot
    # whatever's accumulated, the same producer/consumer split used
    # elsewhere in this project's live dashboards.
    #
    # The outer while loop handles a dropped USB connection: a jostled
    # cable or EMI can make macOS briefly re-enumerate the device (seen
    # in practice -- "hardware connection lost" immediately followed by
    # re-enumeration in the system log), which kills mpremote's subprocess
    # silently. Without this, the plot would just freeze on its last frame
    # forever with no indication anything was wrong, indistinguishable
    # from the signal genuinely going flat.
    while True:
        proc = _spawn()
        status["proc"] = proc
        status["connected"] = True

        for line_text in proc.stdout:
            line_text = line_text.strip()
            try:
                value = float(line_text)
            except ValueError:
                continue  # skip banner / non-numeric lines from mpremote
            with buf_lock:
                buf.append(value)

        status["connected"] = False
        proc.wait()
        time.sleep(RECONNECT_DELAY_S)

reader_thread = threading.Thread(target=_reader, daemon=True)
reader_thread.start()

fig, ax = plt.subplots(figsize=(9, 4))
line, = ax.plot([], [], linewidth=1)
ax.set_ylim(0, 3.3)
ax.set_xlabel("samples (most recent on the right)")
ax.set_ylabel("ADC voltage (V)")
ax.set_title("TREMOR — live GP26 ADC waveform")
ax.axhline(1.65, color="gray", linewidth=0.5, linestyle="--")

def update(_frame):
    with buf_lock:
        ys = list(buf)
    line.set_data(range(len(ys)), ys)
    ax.set_xlim(0, max(MAX_POINTS, 1))

    if status["connected"]:
        ax.set_title("TREMOR — live GP26 ADC waveform")
        line.set_color("tab:blue")
    else:
        ax.set_title("TREMOR — RECONNECTING to Pico...")
        line.set_color("tab:red")
    return line,

ani = animation.FuncAnimation(fig, update, interval=30, blit=False, cache_frame_data=False)

try:
    plt.show()
finally:
    if status["proc"] is not None:
        status["proc"].terminate()
