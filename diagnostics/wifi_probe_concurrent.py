"""Diagnostic: measures real urequests.post() latency AND ring-buffer
occupancy while the ADC Timer + GPS UART loop is actually running
concurrently -- the condition that matters for a deployed unit, not the
isolated case diagnostics/wifi_probe.py already measured (that one showed
~2.3-2.6s latency with nothing else running). Answers the open question
left in wifi_unit_client.py's design notes: does POST latency under real
interrupt/loop contention eat meaningfully into RING_CAPACITY=4096's
~4s headroom?

Deliberately simplified vs. wifi_unit_client.py: the drain loop below
discards samples rather than running them through chunk_summary/
IngestBuffer -- this isolates exactly the mechanism being measured (does
the ring buffer fill up during a blocking POST) without conflating it
with the frequency-estimation path. Uses the same RING_CAPACITY/
MAX_DRAIN_PER_PASS as wifi_unit_client.py so the numbers are directly
comparable.

Bounded run (TEST_DURATION_S) -- temporary, run once via mpremote, not
part of the deployed pipeline. Same non-blocking-connect pattern as
wifi_probe.py (already confirmed there); this one adds the concurrent
ADC/GPS load on top and reports backlog immediately before/after each
POST, which is the concrete number the RING_CAPACITY-vs-_thread decision
turns on.
"""

import array
import time

import network
import urequests
from machine import ADC, UART, Pin, Timer

from wifi_config import INGEST_URL, UNIT_ID, WIFI_PASSWORD, WIFI_SSID

TEST_DURATION_S = 90  # ~11 POST cycles at POST_INTERVAL_S=8 -- enough for a real sample

ADC_SAMPLE_HZ = 1030
RING_CAPACITY = 4096   # matches wifi_unit_client.py -- this is exactly what's being validated
MAX_DRAIN_PER_PASS = 128
POST_INTERVAL_S = 8.0
GPS_READ_CHUNK_BYTES = 128
MAX_GPS_BUF_BYTES = 1024

adc = ADC(26)
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1), timeout=0, timeout_char=0)
wlan = network.WLAN(network.STA_IF)

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

print("=== Concurrent WiFi probe start ({}s, ADC Timer + GPS UART running) ===".format(TEST_DURATION_S))

wlan.active(True)
wlan.connect(WIFI_SSID, WIFI_PASSWORD)
t_wait = time.ticks_ms()
while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t_wait) < 20000:
    time.sleep_ms(100)

if not wlan.isconnected():
    print("WiFi failed to connect within 20s -- aborting")
    adc_timer.deinit()
else:
    print("WiFi connected:", wlan.ifconfig())

    payload = {
        "unit_id": UNIT_ID,
        "readings": [{"frequency_hz": 0.0, "amplitude_v": 0.0, "gps_utc_s": None}],
    }

    gps_buf = b""
    t_start = time.ticks_ms()
    last_post_ticks = time.ticks_ms()
    post_latencies = []

    while time.ticks_diff(time.ticks_ms(), t_start) < TEST_DURATION_S * 1000:
        drained = 0
        while read_idx != write_idx and drained < MAX_DRAIN_PER_PASS:
            read_idx = (read_idx + 1) % RING_CAPACITY
            drained += 1

        if uart.any():
            chunk = uart.read(GPS_READ_CHUNK_BYTES)
            if chunk:
                gps_buf += chunk
                while b"\n" in gps_buf:
                    line_bytes, gps_buf = gps_buf.split(b"\n", 1)
                if len(gps_buf) > MAX_GPS_BUF_BYTES:
                    gps_buf = gps_buf[-MAX_GPS_BUF_BYTES:]

        now = time.ticks_ms()
        if time.ticks_diff(now, last_post_ticks) >= POST_INTERVAL_S * 1000:
            last_post_ticks = now
            backlog_before = (write_idx - read_idx) % RING_CAPACITY
            overflow_before = overflow_count
            t0 = time.ticks_ms()
            response = None
            try:
                response = urequests.post(INGEST_URL, json=payload)
                elapsed_ms = time.ticks_diff(time.ticks_ms(), t0)
                backlog_after = (write_idx - read_idx) % RING_CAPACITY
                overflow_during = overflow_count - overflow_before
                print("POST: {}ms status={} backlog {}->{} of {} ({:.1f}%->{:.1f}%) overflow_during={}".format(
                    elapsed_ms, response.status_code, backlog_before, backlog_after,
                    RING_CAPACITY, 100 * backlog_before / RING_CAPACITY,
                    100 * backlog_after / RING_CAPACITY, overflow_during,
                ))
                post_latencies.append(elapsed_ms)
            except Exception as exc:
                elapsed_ms = time.ticks_diff(time.ticks_ms(), t0)
                print("POST FAILED after {}ms: {}".format(elapsed_ms, exc))
            finally:
                if response is not None:
                    response.close()

    adc_timer.deinit()
    print("=== Concurrent WiFi probe end ===")
    print("total overflow_count={}".format(overflow_count))
    if post_latencies:
        print("POST latency ms: min={} max={} mean={:.1f} n={}".format(
            min(post_latencies), max(post_latencies),
            sum(post_latencies) / len(post_latencies), len(post_latencies),
        ))
