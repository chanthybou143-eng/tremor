"""Standalone Pico-side WiFi client: samples the ADC, syncs GPS/PPS time,
reduces each ~1s chunk to a (frequency_hz, amplitude_v, gps_utc_s) reading
on-device, and ships batches of those readings to TREMOR's /api/ingest
endpoint over WiFi -- the eventual standalone replacement for the current
laptop-tethered mpremote + overnight_log.py setup.

NOT CURRENTLY DEPLOYED (2026-09-19). A ~10.5h soak of this exact script
surfaced a recurring MemoryError crash -- "memory allocation failed,
allocating 8192 bytes" at chunk_summary.py:82 (the per-chunk
`[(v - dc_offset) ** 2 for v in filtered]` list comprehension, ~1000
elements, allocated fresh every second) -- 23 times over ~4h45m, with the
interval between crashes shrinking as the run went on (~107s, then ~74s
between the last three), consistent with heap fragmentation building up
under the shared dual-core allocator rather than a one-off glitch. Likely
mechanism: this per-second large-list churn on core 0, concurrent with
core 1's TLS/urequests allocations on the *same* heap (see the "new
GC-pause risk" design note below), fragments the heap enough that an
8KB allocation occasionally fails outright even with adequate total free
memory. Each crash restarts the script from scratch, silently discarding
whatever was sitting unflushed in IngestBuffer at that moment (in RAM,
wiped on restart) -- real, uncounted data loss with no drop counter to
show it, unlike the ring-buffer overflow this redesign was built to fix.

It also produced a second-order symptom: 23 of 29 disconnect/reconnect
events logged that night were actually these crashes, not the USB drops
they were first assumed to be (mischaracterized as such in status updates
during the run, since disconnect/reconnect counts were checked without
grepping for the traceback itself). A crash-triggered restart, followed
quickly by a small post-restart batch, could land close enough in real
time to the tail of the pre-crash batch still in /api/ingest's 60s
server-side window to make rocof_from_window's fit see an artificially
tiny apparent time gap between two batches whose timestamps are each
independently reconstructed from "now" -- observed live as a
-22.953 Hz/s spike (physically impossible; the underlying frequency
reading itself was plausible). This is a distinct edge case from the
earlier same-night within-batch timestamp-compression bug (fixed in
d348cf1) -- that one was systematic and every batch; this one is an
occasional cross-batch boundary artifact that only appears around a
crash/restart.

Reverted to the pre-dual-core single-core version (git show
9dfdf08:wifi_unit_client.py) for actual deployment -- known-stable,
overflow behavior under load is bounded and already measured
(diagnostics/wifi_probe_concurrent.py), rather than an unbounded silent
loss with no counter. This file is kept, not deleted -- the dual-core
approach is still the right direction once the heap-fragmentation issue
is understood and fixed; that fix is unresolved follow-up work, not
attempted here. A fix will need to either reduce per-second allocation
churn on core 0 (e.g. reuse a preallocated buffer instead of a fresh
list comprehension every chunk) or move real allocation-heavy work off
the shared heap's contention path entirely -- to be scoped separately,
with its own measurement, not guessed at now.

Dual-core split (see conversation design notes -- justified by
diagnostics/wifi_probe_concurrent.py's measurement: real urequests.post()
latency under concurrent ADC/GPS load spiked to ~5.4-5.7s in ~18% of
cycles, overflowing RING_CAPACITY=4096 and dropping ~1400-1600 samples
per spike):

    Core 0 (this module's top level and main loop): ADC Timer IRQ,
    ring-buffer drain, chunk_summary, IngestBuffer.append(), GPS UART
    servicing, PPSTimeSync. Never touches `wlan` -- see _core1_main.

    Core 1 (_core1_main, launched via _thread): WiFi connect/reconnect
    and periodic IngestBuffer.flush(). Owns `wlan` exclusively; core 0
    only ever reads the plain _wifi_connected flag below for its status
    line, specifically to avoid any cross-core access to the cyw43
    driver itself (not just the Python-level WLAN object), which hasn't
    been verified safe to call from two cores concurrently.

IngestBuffer (wifi_ingest.py) is the only object touched from both cores,
and is now internally locked for exactly that reason -- see its docstring
for the minimal-hold-time design (the lock never spans the network call).

NOT YET RUN ON REAL HARDWARE past the diagnostics/ probes. Known
MicroPython WiFi/urequests gotchas this was written to survive -- see the
inline comments at each relevant point: no WiFi auto-reconnect
(_wifi_service), urequests responses must be .close()'d or sockets leak
(_post_batch), no default socket timeout in most urequests forks
(_post_batch).

TEST_DURATION_S follows adc_stream_gps.py's own bring-up convention: None
runs forever (production); a number bounds the run and prints a FINAL
summary in the same shape as diagnostics/dualcore_control_a.py's, so a
bounded run of *this* script is Test C in the Control A/B/C comparison --
not a separate stand-in script -- and is directly diffable against
Control A's numbers.

Additive, not a modification: imports pps_time_sync.PPSTimeSync exactly as
adc_stream_gps.py does, and freq_estimator.py (via chunk_summary.py)
exactly as units.py's SyntheticUnitFeed does. Neither of those files, nor
adc_stream_gps.py itself, is touched by this script.
"""

import array
import time

import _thread
import network
import urequests
from machine import ADC, UART, Pin, Timer

from pps_time_sync import PPSTimeSync
from chunk_summary import summarize_chunk
from wifi_ingest import IngestBuffer
from wifi_config import INGEST_URL, UNIT_ID, WIFI_PASSWORD, WIFI_SSID

TEST_DURATION_S = None  # None = run forever (production); a number = bounded
                        # verification run -- see module docstring

ADC_SAMPLE_HZ = 1030   # matches adc_stream_gps.py's measured real-world rate
# See diagnostics/wifi_probe_concurrent.py's measurement in the module
# docstring for why this is 4096 (~4s headroom, ~24KB RAM), not
# adc_stream_gps.py's 512 -- and why that headroom alone wasn't enough,
# which is what motivated the dual-core split below.
RING_CAPACITY = 4096
MAX_DRAIN_PER_PASS = 128  # see adc_stream_gps.py: bounds one drain so GPS
                          # UART servicing always gets a turn

CHUNK_S = 1.0            # one summarized reading per second, same cadence as overnight_log.py
POST_INTERVAL_S = 8.0    # batch several readings per POST rather than one per
                         # second -- each POST pays a DNS+TCP+TLS handshake cost
                         # that dominates a single small payload's transfer time
MAX_BUFFERED_READINGS = 600  # ~10 minutes at 1 reading/s -- see wifi_ingest.py's
                              # docstring; needs retuning against real gc.mem_free()

WIFI_RETRY_INTERVAL_S = 5    # how often to kick off a fresh connect attempt while down
STATUS_INTERVAL_S = 10

ADC_VOLTAGE_SCALE = 3.3 / 65535  # raw u16 -> volts, same conversion
                                  # overnight_log.py's _parse_sample_line() applies

adc = ADC(26)
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1), timeout=0, timeout_char=0)
sync = PPSTimeSync(pps_pin=15)

t0 = time.ticks_us()

# Ring buffer -- identical structure to adc_stream_gps.py's (ISR does the
# minimum possible work; the main loop converts/drains).
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

buffer = IngestBuffer(UNIT_ID, post_fn=None, max_readings=MAX_BUFFERED_READINGS)  # post_fn set below

# Cross-core status only -- never used for control flow on either side, so
# plain-variable reads/writes are fine without a lock (see module
# docstring: core 0 never touches `wlan` itself, only this flag).
_wifi_connected = False
_stop_flag = False  # core 0 sets this at TEST_DURATION_S; core 1 polls it to exit its loop


def _core1_main():
    """Runs entirely on core 1: WiFi connect/reconnect + periodic
    IngestBuffer.flush(). Everything that touches `wlan` or issues the
    network POST lives here and only here.
    """
    global _wifi_connected

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    last_wifi_attempt_ticks = time.ticks_us()
    last_post_ticks = time.ticks_us()

    def _wifi_service():
        """Call every loop pass -- never blocks. wlan.connect() itself is
        asynchronous (the cyw43 driver negotiates in the background;
        MicroPython's call returns immediately -- confirmed on this
        firmware by diagnostics/wifi_probe.py: 6-26ms call-return time),
        so this only ever *starts* an attempt at most once per
        WIFI_RETRY_INTERVAL_S and otherwise just checks isconnected().
        """
        nonlocal last_wifi_attempt_ticks
        global _wifi_connected
        _wifi_connected = wlan.isconnected()
        if _wifi_connected:
            return
        now = time.ticks_us()
        if time.ticks_diff(now, last_wifi_attempt_ticks) >= WIFI_RETRY_INTERVAL_S * 1_000_000:
            last_wifi_attempt_ticks = now
            try:
                wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            except OSError:
                pass  # e.g. "already connecting" -- next retry will catch a real failure

    def _post_batch(payload):
        """IngestBuffer's injected post_fn. Returns True only on a 2xx
        response. Every response is explicitly closed -- a missed
        .close() on urequests leaks the underlying socket, and repeated
        leaks exhaust the Pico's socket table over an unattended
        multi-hour run. NEEDS VERIFICATION ON HARDWARE: whether this
        urequests build supports a timeout kwarg at all -- without one, a
        dead/half-open connection can block this call indefinitely (this
        blocks only core 1 now, not the ADC/GPS path on core 0, but it
        would still stall this unit's own reporting).
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

    buffer._post_fn = _post_batch  # set here so _post_batch can close over this core's wlan

    while not _stop_flag:
        _wifi_service()
        now = time.ticks_us()
        if time.ticks_diff(now, last_post_ticks) >= POST_INTERVAL_S * 1_000_000:
            last_post_ticks = now
            buffer.flush()  # _post_batch checks wlan.isconnected() itself
        time.sleep_ms(50)  # core 1 has no hard-real-time work; a short sleep avoids busy-spinning


_thread.start_new_thread(_core1_main, ())

_last_consumed_ticks = t0
_elapsed_us_total = 0
_chunk_ticks = []      # raw time.ticks_us() per sample -- for gps_utc_s lookup
_chunk_ts_s = []       # elapsed seconds per sample -- summarize_chunk's timestamps
_chunk_counts = []     # raw u16 ADC counts per sample -- converted to volts below
_last_status_ticks = t0

while TEST_DURATION_S is None or _elapsed_us_total < TEST_DURATION_S * 1_000_000:
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

    now = time.ticks_us()
    if time.ticks_diff(now, _last_status_ticks) >= STATUS_INTERVAL_S * 1_000_000:
        _last_status_ticks = now
        s = sync.status
        print("# STATUS elapsed_s={:.1f} wifi={} synced={} pps={} sync={} rejected={} "
              "no_edge={} period_us={} buffered={} dropped={} overflow={}".format(
            _elapsed_us_total / 1e6, _wifi_connected, s["synced"], s["pps_count"],
            s["sync_count"], s["rejected_count"], s["no_edge_count"], s["pps_period_us"],
            len(buffer), buffer.dropped_count, overflow_count,
        ))

if TEST_DURATION_S is not None:
    _stop_flag = True
    adc_timer.deinit()
    time.sleep_ms(200)  # let core 1 notice _stop_flag and exit its loop
    s = sync.status
    print("=== Test C end ===")
    print("FINAL pps_count={} sync_count={} sync_ratio={:.4f} rejected={} no_edge={} "
          "pps_period_us={} overflow_count={} buffered={} dropped={}".format(
        s["pps_count"], s["sync_count"],
        (s["sync_count"] / s["pps_count"]) if s["pps_count"] else float("nan"),
        s["rejected_count"], s["no_edge_count"], s["pps_period_us"],
        overflow_count, len(buffer), buffer.dropped_count,
    ))
