"""One-shot Pico-side diagnostic: measures whether network.WLAN.connect()
is actually non-blocking on this Pico 2 W's cyw43 driver, and how long a
real urequests.post() takes against the live /api/ingest endpoint.

Standalone and temporary -- not part of wifi_unit_client.py's pipeline.
Run once via mpremote to get real numbers before deciding on
wifi_unit_client.py's open _thread question (see conversation notes).

Prints incrementally so a genuinely-blocking call still leaves a trail on
the host's mpremote output up to the point it hung -- the actual escape
hatch is the host-side mpremote invocation's own timeout (a truly-blocking
call inside MicroPython can't be interrupted from within this same
single-threaded script).

Sends a handful of obviously-synthetic readings (frequency_hz=0.0,
amplitude_v=0.0) to the real unit configured in wifi_config.py -- these
will briefly appear on the live dashboard for one READOUT_WINDOW_S/
WINDOW_S cycle (tens of seconds) and then age out on their own once no
more readings arrive.
"""

import time

import network
import urequests

from wifi_config import INGEST_URL, UNIT_ID, WIFI_PASSWORD, WIFI_SSID

CONNECT_POLL_TIMEOUT_S = 20
N_POST_TRIALS = 5
POST_TRIAL_GAP_MS = 500

print("=== WiFi probe start ===")

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

print("calling wlan.connect() ...")
t_call_start = time.ticks_ms()
wlan.connect(WIFI_SSID, WIFI_PASSWORD)
call_duration_ms = time.ticks_diff(time.ticks_ms(), t_call_start)
print("wlan.connect() call itself returned after {}ms".format(call_duration_ms))
if call_duration_ms < 200:
    print("VERDICT: call returned near-instantly -- consistent with async/non-blocking connect")
else:
    print("VERDICT: call itself took a while to return -- may be (partially) blocking")

print("polling wlan.isconnected() (timeout {}s) ...".format(CONNECT_POLL_TIMEOUT_S))
t_poll_start = time.ticks_ms()
connected = False
while time.ticks_diff(time.ticks_ms(), t_poll_start) < CONNECT_POLL_TIMEOUT_S * 1000:
    if wlan.isconnected():
        connected = True
        break
    time.sleep_ms(100)
join_duration_ms = time.ticks_diff(time.ticks_ms(), t_poll_start)

if not connected:
    print("TIMEOUT: not connected after {}s of polling -- aborting".format(CONNECT_POLL_TIMEOUT_S))
    print("=== WiFi probe end (failed to connect) ===")
else:
    print("isconnected() became True after {}ms of polling".format(join_duration_ms))
    print("ifconfig: {}".format(wlan.ifconfig()))

    payload = {
        "unit_id": UNIT_ID,
        "readings": [{"frequency_hz": 0.0, "amplitude_v": 0.0, "gps_utc_s": None}],
    }

    latencies_ms = []
    for i in range(N_POST_TRIALS):
        t0 = time.ticks_ms()
        response = None
        try:
            response = urequests.post(INGEST_URL, json=payload)
            elapsed_ms = time.ticks_diff(time.ticks_ms(), t0)
            print("POST #{}: {}ms, status={}".format(i + 1, elapsed_ms, response.status_code))
            latencies_ms.append(elapsed_ms)
        except Exception as exc:
            elapsed_ms = time.ticks_diff(time.ticks_ms(), t0)
            print("POST #{}: FAILED after {}ms: {}".format(i + 1, elapsed_ms, exc))
        finally:
            if response is not None:
                response.close()
        time.sleep_ms(POST_TRIAL_GAP_MS)

    if latencies_ms:
        print("POST latency ms: min={} max={} mean={:.1f}".format(
            min(latencies_ms), max(latencies_ms), sum(latencies_ms) / len(latencies_ms),
        ))

    print("=== WiFi probe end ===")
