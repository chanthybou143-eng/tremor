"""Control A for the wifi_unit_client.py dual-core redesign: core 0's real
workload (ADC Timer, ring-buffer drain, chunk_summary, PPSTimeSync + GPS
UART servicing) with NO network activity at all. Establishes the baseline
overflow_count and PPS sync_count/pps_count ratio attributable only to the
already-understood GPS-poll-stall mechanism, uncontaminated by WiFi.

Test C (the _thread-based wifi_unit_client.py, run bounded via
TEST_DURATION_S) should be compared against THIS baseline, not against
Control B (today's already-collected single-core-with-WiFi numbers, which
is the broken case being fixed, not the target to match).

Same RING_CAPACITY as wifi_unit_client.py (4096) so a buffer-size
difference can't confound the comparison -- the only variable being
isolated here is "does moving POST to a second core change core 0's own
overflow/PPS behaviour," and that requires matching everything else.

Bounded run, temporary diagnostic -- not part of the deployed pipeline.
"""

import array
import time

from machine import ADC, UART, Pin, Timer

from pps_time_sync import PPSTimeSync
from chunk_summary import summarize_chunk

TEST_DURATION_S = 90  # matches diagnostics/wifi_probe_concurrent.py's Test B run

ADC_SAMPLE_HZ = 1030
RING_CAPACITY = 4096
MAX_DRAIN_PER_PASS = 128
CHUNK_S = 1.0
GPS_READ_CHUNK_BYTES = 128
MAX_GPS_BUF_BYTES = 1024
STATUS_INTERVAL_S = 10

adc = ADC(26)
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1), timeout=0, timeout_char=0)
sync = PPSTimeSync(pps_pin=15)

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

print("=== Control A start ({}s, no network) ===".format(TEST_DURATION_S))

t_start = time.ticks_us()
_last_consumed_ticks = t_start
_elapsed_us_total = 0
_chunk_ticks = []
_chunk_ts_s = []
_chunk_counts = []
_last_status_ticks = t_start
_readings_ok = 0
_readings_failed = 0
gps_buf = b""
ADC_VOLTAGE_SCALE = 3.3 / 65535

while time.ticks_diff(time.ticks_us(), t_start) < TEST_DURATION_S * 1_000_000:
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

    if _chunk_ts_s and (_chunk_ts_s[-1] - _chunk_ts_s[0]) >= CHUNK_S:
        voltages = [count * ADC_VOLTAGE_SCALE for count in _chunk_counts]
        try:
            summarize_chunk(_chunk_ts_s, voltages)
            _readings_ok += 1
        except ValueError:
            _readings_failed += 1
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
        print("# STATUS elapsed_s={:.1f} synced={} pps={} sync={} rejected={} no_edge={} "
              "period_us={} overflow={} readings_ok={} readings_failed={}".format(
            _elapsed_us_total / 1e6, s["synced"], s["pps_count"], s["sync_count"],
            s["rejected_count"], s["no_edge_count"], s["pps_period_us"],
            overflow_count, _readings_ok, _readings_failed,
        ))

adc_timer.deinit()
s = sync.status
print("=== Control A end ===")
print("FINAL pps_count={} sync_count={} sync_ratio={:.4f} rejected={} no_edge={} "
      "pps_period_us={} overflow_count={} readings_ok={} readings_failed={}".format(
    s["pps_count"], s["sync_count"],
    (s["sync_count"] / s["pps_count"]) if s["pps_count"] else float("nan"),
    s["rejected_count"], s["no_edge_count"], s["pps_period_us"],
    overflow_count, _readings_ok, _readings_failed,
))
