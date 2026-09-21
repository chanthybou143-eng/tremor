"""One-shot Pico-side diagnostic: times socket.getaddrinfo() against the
real INGEST_URL host, five times in a row, plus one plain (non-TLS) TCP
connect -- written to investigate Trial 4's finding that
http_client.timeout_post() reported "connect: overall_deadline_exceeded"
(POST_DEADLINE_S=10s) on every attempt, while the SAME attempt's total
wall-clock duration measured via time.ticks_us()/ticks_diff (the clock
wifi_unit_client.py's longest_post_duration_s uses) was only ~0.147s.
That gap can only be explained by http_client.py's deadline clock itself
(now_fn, defaulting to time.time) misbehaving -- not by DNS/connect
actually being slow -- since ticks_us/ticks_diff independently timed the
whole attempt at well under a second.

This script measures EVERY step with BOTH clocks side by side
specifically to catch that: time.ticks_ms()/ticks_diff (assumed
reliable -- it's what the rest of wifi_unit_client.py already uses
everywhere else) and raw time.time() (the one under suspicion). A
healthy result: both clocks agree elapsed getaddrinfo()/connect() times
are on the order of tens to low hundreds of ms, matching the
historically-measured ~0.5-2.6s full-round-trip baseline. A confirming
result: ticks-based timings stay small while time.time() readings jump
by seconds between consecutive calls with no real delay in between --
directly reproducing what Trial 4's logs imply indirectly.

Standalone and temporary -- not part of wifi_unit_client.py's pipeline.
DO NOT RUN until explicitly told to: it must be run with the soak
stopped and the device otherwise idle, per the plan -- running it
against a live soak would mean two things fighting over the same
serial connection and WiFi radio at once.

Sends no data to /api/ingest -- this only resolves the hostname and
opens/closes a plain TCP connection to port 443, it never sends an HTTP
request.
"""

import socket
import time

import network

from wifi_config import INGEST_URL, WIFI_PASSWORD, WIFI_SSID

CONNECT_POLL_TIMEOUT_S = 20
N_GETADDRINFO_TRIALS = 5
TRIAL_GAP_MS = 500

# Minimal inline parse -- avoids importing http_client.py's parse_https_url
# so this script has no dependency on the file it's investigating.
assert INGEST_URL.startswith("https://")
_host_port, _, _path = INGEST_URL[len("https://"):].partition("/")
HOST = _host_port
PORT = 443

print("=== DNS/connect timing probe start ===")
print("target host={} port={}".format(HOST, PORT))

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

print("calling wlan.connect() ...")
wlan.connect(WIFI_SSID, WIFI_PASSWORD)

print("polling wlan.isconnected() (timeout {}s) ...".format(CONNECT_POLL_TIMEOUT_S))
t_poll_start = time.ticks_ms()
connected = False
while time.ticks_diff(time.ticks_ms(), t_poll_start) < CONNECT_POLL_TIMEOUT_S * 1000:
    if wlan.isconnected():
        connected = True
        break
    time.sleep_ms(100)

if not connected:
    print("TIMEOUT: not connected after {}s of polling -- aborting".format(CONNECT_POLL_TIMEOUT_S))
    print("=== DNS/connect timing probe end (failed to connect) ===")
else:
    print("isconnected() became True; ifconfig: {}".format(wlan.ifconfig()))
    print("time.time() right after WiFi connect: {}".format(time.time()))

    print("")
    print("--- getaddrinfo() x{} ---".format(N_GETADDRINFO_TRIALS))
    for i in range(N_GETADDRINFO_TRIALS):
        t_time_before = time.time()
        t_ticks_before = time.ticks_ms()
        try:
            result = socket.getaddrinfo(HOST, PORT)
            ticks_elapsed_ms = time.ticks_diff(time.ticks_ms(), t_ticks_before)
            t_time_after = time.time()
            print("getaddrinfo #{}: ticks_elapsed_ms={} time_before={} time_after={} "
                  "time_delta_s={} result={}".format(
                i + 1, ticks_elapsed_ms, t_time_before, t_time_after,
                t_time_after - t_time_before, result))
        except Exception as exc:
            ticks_elapsed_ms = time.ticks_diff(time.ticks_ms(), t_ticks_before)
            t_time_after = time.time()
            print("getaddrinfo #{}: FAILED ticks_elapsed_ms={} time_before={} time_after={} "
                  "time_delta_s={} error={}".format(
                i + 1, ticks_elapsed_ms, t_time_before, t_time_after,
                t_time_after - t_time_before, exc))
        time.sleep_ms(TRIAL_GAP_MS)

    print("")
    print("--- plain TCP connect() x1 (no TLS, no HTTP request sent) ---")
    try:
        addr_info = socket.getaddrinfo(HOST, PORT)
        addr = addr_info[0][-1]
        t_time_before = time.time()
        t_ticks_before = time.ticks_ms()
        s = socket.socket()
        s.settimeout(10)
        s.connect(addr)
        ticks_elapsed_ms = time.ticks_diff(time.ticks_ms(), t_ticks_before)
        t_time_after = time.time()
        print("connect(): ticks_elapsed_ms={} time_before={} time_after={} time_delta_s={}".format(
            ticks_elapsed_ms, t_time_before, t_time_after, t_time_after - t_time_before))
        s.close()
    except Exception as exc:
        ticks_elapsed_ms = time.ticks_diff(time.ticks_ms(), t_ticks_before)
        t_time_after = time.time()
        print("connect(): FAILED ticks_elapsed_ms={} time_before={} time_after={} "
              "time_delta_s={} error={}".format(
            ticks_elapsed_ms, t_time_before, t_time_after,
            t_time_after - t_time_before, exc))

    print("")
    print("=== DNS/connect timing probe end ===")
