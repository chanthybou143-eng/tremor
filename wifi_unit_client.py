"""Standalone Pico-side WiFi client: samples the ADC, syncs GPS/PPS time,
reduces each ~1s chunk to a (frequency_hz, amplitude_v, gps_utc_s) reading
on-device, and ships batches of those readings to TREMOR's /api/ingest
endpoint over WiFi -- the deployed replacement for the laptop-tethered
mpremote + overnight_log.py setup. This is the known-good single-core
client (originally 9dfdf08); a dual-core (_thread) redesign was tried and
reverted after a MemoryError crash loop from heap fragmentation, and
stays single-core-only going forward (see wifi_ingest.py/git history).

Written against adc_stream_gps.py's proven ADC-ring-buffer/GPS-UART-polling
design (same wraparound-safe ticks accumulator, same GPS_READ_CHUNK_BYTES
reasoning), with chunk_summary.py and wifi_ingest.py tested under desktop
Python. Known MicroPython WiFi/urequests gotchas this was written to
survive -- see the inline comments at each relevant point: no WiFi
auto-reconnect (_wifi_service), urequests responses must be .close()'d or
sockets leak (_post_batch), no default socket timeout in most urequests
forks (_post_batch), and a blocking POST can stall the main loop for
longer than the ring buffer's headroom (RING_CAPACITY).

POST_INTERVAL_S raised from 8s to 30s to reduce how often the ~2.1-2.6s
DNS+TCP+TLS handshake gets paid per hour -- a persistent/keep-alive
connection was investigated and found not viable against this specific
PythonAnywhere deployment (reproducible ~15s response delay on any
connection not explicitly closed; see the keepalive-single-core branch's
diagnostics), so batching more readings per POST is the remaining lever.

gc.mem_free()/gc.mem_alloc() are logged alongside the existing periodic
status line (with an explicit gc.collect() first, so the numbers reflect
reclaimed state, not a mid-accumulation snapshot) specifically so a long
soak's heap trend is visible -- the dual-core incident's fragmentation
was only caught after the fact, from a crash, not from watching a trend.

Additive, not a modification: imports pps_time_sync.PPSTimeSync exactly as
adc_stream_gps.py does, and freq_estimator.py (via chunk_summary.py)
exactly as units.py's SyntheticUnitFeed does. Neither of those files, nor
adc_stream_gps.py itself, is touched by this script.
"""

import array
import gc
import time

import network
import urequests
from machine import ADC, UART, Pin, Timer

from pps_time_sync import PPSTimeSync
from chunk_summary import summarize_chunk, DegenerateTimestampsError
from wifi_ingest import IngestBuffer
from wifi_config import INGEST_URL, UNIT_ID, WIFI_PASSWORD, WIFI_SSID

ADC_SAMPLE_HZ = 1030   # matches adc_stream_gps.py's measured real-world rate
# adc_stream_gps.py's RING_CAPACITY=512 (~497ms headroom) assumed only
# short USB/GPS-poll stalls. Unlike WiFi reconnect (made non-blocking
# above), urequests.post() itself IS a blocking call with no async
# alternative in stock urequests -- DNS+TCP+TLS+transfer for a small JSON
# batch is plausibly ~0.5-2s, occasionally more, and the main loop can't
# drain the ring buffer while blocked inside it. 4096 samples (~4s
# headroom, ~24KB RAM) is a cheap stopgap so a typical POST doesn't
# overflow it -- NOT a guarantee against a pathological multi-second
# stall. OPEN DESIGN QUESTION for real hardware: if measured POST
# latency threatens this headroom, the more robust fix is running the
# POST on the Pico 2's second core via _thread (ADC/ring-buffer draining
# stays on core 0, uninterrupted) rather than keep enlarging this buffer --
# worth deciding once real latency numbers exist, not guessed now.
RING_CAPACITY = 4096
MAX_DRAIN_PER_PASS = 128  # see adc_stream_gps.py: bounds one drain so GPS
                          # UART servicing and WiFi/POST bookkeeping always get a turn

CHUNK_S = 1.0            # one summarized reading per second, same cadence as overnight_log.py
# Single source of truth for how often buffer.flush() POSTs a batch.
# Raised from 8s to 30s: a persistent/keep-alive connection was
# investigated and found not viable against this specific PythonAnywhere
# deployment (reproducible ~15s response delay on any connection not
# explicitly told "Connection: close" -- see the keepalive-single-core
# branch's diagnostics; not revisited here, keep-alive is dead). With
# reuse off the table, the remaining lever to cut total time spent on
# the ~2.1-2.6s DNS+TCP+TLS handshake each POST pays is fewer POSTs per
# hour, not cheaper ones -- 30s batches 3-4x more readings per handshake
# than 8s did, at the cost of a longer worst-case delay before a reading
# reaches the dashboard.
POST_INTERVAL_S = 30.0
# ~30 readings/batch at POST_INTERVAL_S=30s and one reading/s (CHUNK_S)
# -- 600 is a ~20x margin over one normal batch, and the outage-tolerance
# semantics (drop-oldest once buffered readings span ~10 minutes) are
# unchanged by the interval bump, since this bound is independent of how
# often flush() is called. See the bench-run report (commit history) for
# the actually-observed peak.
MAX_BUFFERED_READINGS = 600

WIFI_RETRY_INTERVAL_S = 5    # how often to kick off a fresh connect attempt while down
STATUS_INTERVAL_S = 10

ADC_VOLTAGE_SCALE = 3.3 / 65535  # raw u16 -> volts, same conversion
                                  # overnight_log.py's _parse_sample_line() applies

adc = ADC(26)
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1), timeout=0, timeout_char=0)
sync = PPSTimeSync(pps_pin=15)
wlan = network.WLAN(network.STA_IF)

t0 = time.ticks_us()

# Ring buffer -- identical structure to adc_stream_gps.py's, see that
# file's docstring for why (ISR does the minimum possible work; the main
# loop converts/drains). RING_CAPACITY=512 at ADC_SAMPLE_HZ=1030 is only
# ~497ms of headroom (same as adc_stream_gps.py) -- this is why WiFi
# connect/reconnect below MUST be non-blocking: a single blocking connect
# attempt of even a couple of seconds would overflow this buffer and drop
# most samples acquired during the stall.
ring_ticks = array.array("L", [0] * RING_CAPACITY)
ring_raw = array.array("H", [0] * RING_CAPACITY)
write_idx = 0
read_idx = 0
overflow_count = 0


def _on_adc_timer(timer):
    global write_idx, overflow_count
    next_write_idx = (write_idx + 1) % RING_CAPACITY
    if next_write_idx == read_idx:
        overflow_count += 1
        return
    ring_ticks[write_idx] = time.ticks_us()
    ring_raw[write_idx] = adc.read_u16()
    write_idx = next_write_idx


adc_timer = Timer()
adc_timer.init(freq=ADC_SAMPLE_HZ, mode=Timer.PERIODIC, callback=_on_adc_timer)

MAX_GPS_BUF_BYTES = 1024
GPS_READ_CHUNK_BYTES = 128  # see adc_stream_gps.py for the throughput/latency
                            # tradeoff this bounds
gps_buf = b""


wlan.active(True)
_last_wifi_attempt_ticks = time.ticks_us()


def _wifi_service():
    """Call every loop pass -- never blocks. wlan.connect() itself is
    asynchronous (the cyw43 driver negotiates in the background; MicroPython's
    call returns immediately), so this only ever *starts* an attempt at most
    once per WIFI_RETRY_INTERVAL_S and otherwise just checks isconnected() --
    it never busy-waits, which matters given the ring buffer's ~497ms
    headroom (see its comment above). NEEDS VERIFICATION ON HARDWARE: that
    wlan.connect() on the Pico 2 W's cyw43 driver really is non-blocking in
    the way ESP32 MicroPython ports document -- if it isn't, this call needs
    to move off the main loop (e.g. a second thread via _thread) instead.
    """
    global _last_wifi_attempt_ticks
    if wlan.isconnected():
        return
    now = time.ticks_us()
    if time.ticks_diff(now, _last_wifi_attempt_ticks) >= WIFI_RETRY_INTERVAL_S * 1_000_000:
        _last_wifi_attempt_ticks = now
        try:
            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        except OSError:
            pass  # e.g. "already connecting" -- next retry will catch a real failure


def _post_batch(payload):
    """IngestBuffer's injected post_fn. Returns True only on a 2xx
    response. Every response is explicitly closed -- a missed .close()
    on urequests leaks the underlying socket, and repeated leaks exhaust
    the Pico's socket table over an unattended multi-hour run (see module
    docstring). NEEDS VERIFICATION ON HARDWARE: whether this urequests
    build supports a timeout kwarg at all -- without one, a dead/half-open
    connection can block this call indefinitely.
    """
    if not wlan.isconnected():
        return False
    response = None
    try:
        response = urequests.post(INGEST_URL, json=payload)
        return 200 <= response.status_code < 300
    except Exception:
        return False
    finally:
        if response is not None:
            response.close()


buffer = IngestBuffer(UNIT_ID, post_fn=_post_batch, max_readings=MAX_BUFFERED_READINGS)

_last_consumed_ticks = t0
_elapsed_us_total = 0
_chunk_ticks = []      # raw time.ticks_us() per sample -- for gps_utc_s lookup
_chunk_ts_s = []       # elapsed seconds per sample -- summarize_chunk's timestamps
_chunk_counts = []     # raw u16 ADC counts per sample -- converted to volts below
_last_post_ticks = t0
_last_status_ticks = t0
peak_buffered = 0  # highest len(buffer) observed -- see bench-run report in commit history
post_attempts = 0  # a flush() where the buffer was actually non-empty -- excludes no-op flushes
post_successes = 0
dup_timestamp_count = 0  # DegenerateTimestampsError occurrences -- see chunk_summary.py

_wifi_service()

while True:
    drained = 0
    while read_idx != write_idx and drained < MAX_DRAIN_PER_PASS:
        raw_ticks = ring_ticks[read_idx]
        raw_count = ring_raw[read_idx]
        read_idx = (read_idx + 1) % RING_CAPACITY
        drained += 1

        _elapsed_us_total += time.ticks_diff(raw_ticks, _last_consumed_ticks)
        _last_consumed_ticks = raw_ticks

        _chunk_ticks.append(raw_ticks)
        _chunk_ts_s.append(_elapsed_us_total / 1e6)
        _chunk_counts.append(raw_count)

    # Once ~CHUNK_S worth of samples has accumulated, reduce it to one
    # (frequency_hz, amplitude_v, gps_utc_s) reading and buffer it.
    if _chunk_ts_s and (_chunk_ts_s[-1] - _chunk_ts_s[0]) >= CHUNK_S:
        voltages = [count * ADC_VOLTAGE_SCALE for count in _chunk_counts]
        try:
            frequency_hz, amplitude_v = summarize_chunk(_chunk_ts_s, voltages)
            gps_utc_s = sync.ticks_to_utc(_chunk_ticks[-1])
            buffer.append(frequency_hz, amplitude_v, gps_utc_s)
        except DegenerateTimestampsError as exc:
            # Distinguished from the routine ValueError skip below
            # specifically so this rarer failure is counted and its
            # context logged -- see chunk_summary.py's docstring: this is
            # what used to crash the process with a bare ZeroDivisionError.
            # since_last_post_us tells us whether this coincided with a
            # POST attempt (a blocking urequests.post() call stalls the
            # main loop, so a collision caused by a burst of catch-up
            # drains right after a long stall would show a small value
            # here); ring buffer overflow_count is logged alongside since
            # the same blocking-stall mechanism is the known cause of that
            # too, so a correlation between the two would be visible.
            dup_timestamp_count += 1
            since_last_post_us = time.ticks_diff(time.ticks_us(), _last_post_ticks)
            print("# DUP_TIMESTAMP elapsed_s={:.1f} n={} first={} last={} "
                  "since_last_post_us={} overflow_count={} error={}".format(
                _elapsed_us_total / 1e6, len(_chunk_ts_s),
                _chunk_ts_s[0] if _chunk_ts_s else None,
                _chunk_ts_s[-1] if _chunk_ts_s else None,
                since_last_post_us, overflow_count, exc,
            ))
        except ValueError:
            pass  # too few crossings this chunk -- skip it, same as overnight_log.py
        _chunk_ticks = []
        _chunk_ts_s = []
        _chunk_counts = []

    if uart.any():
        chunk = uart.read(GPS_READ_CHUNK_BYTES)
        if chunk:
            gps_buf += chunk
            while b"\n" in gps_buf:
                line_bytes, gps_buf = gps_buf.split(b"\n", 1)
                if b"RMC" not in line_bytes:
                    continue
                try:
                    line = line_bytes.decode("ascii").strip()
                except UnicodeError:
                    continue
                if line:
                    sync.feed_nmea(line)
            if len(gps_buf) > MAX_GPS_BUF_BYTES:
                gps_buf = gps_buf[-MAX_GPS_BUF_BYTES:]

    _wifi_service()

    current_buffered = len(buffer)
    if current_buffered > peak_buffered:
        peak_buffered = current_buffered

    now = time.ticks_us()
    if time.ticks_diff(now, _last_post_ticks) >= POST_INTERVAL_S * 1_000_000:
        _last_post_ticks = now
        if current_buffered > 0:
            post_attempts += 1
            if buffer.flush():  # _post_batch checks wlan.isconnected() itself; a
                                 # no-op (returns False, buffer untouched) while down
                post_successes += 1
        else:
            buffer.flush()  # genuinely nothing to send -- not counted as an attempt

    if time.ticks_diff(now, _last_status_ticks) >= STATUS_INTERVAL_S * 1_000_000:
        _last_status_ticks = now
        s = sync.status
        gc.collect()  # so mem_free()/mem_alloc() reflect reclaimable garbage,
                       # not a snapshot mid-accumulation -- see module docstring
        print("# STATUS elapsed_s={:.1f} wifi={} synced={} buffered={} peak_buffered={} "
              "dropped={} overflow={} heap_free={} heap_alloc={} post_attempts={} "
              "post_successes={} dup_timestamp_count={}".format(
            _elapsed_us_total / 1e6, wlan.isconnected(), s["synced"],
            current_buffered, peak_buffered, buffer.dropped_count, overflow_count,
            gc.mem_free(), gc.mem_alloc(), post_attempts, post_successes,
            dup_timestamp_count,
        ))
