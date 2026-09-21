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

Also, right after WiFi connects, prints time.time() and time.ticks_ms()
once a second for CLOCK_WATCH_DURATION_S -- a step in time.time() should
show up as a jump between consecutive seconds while ticks_ms advances by
a normal ~1000 every time, and the printed timestamps make it possible
to see whether any jump lines up with WiFi association (right at the
start of this window), DNS/connect (the getaddrinfo/connect trials run
right after this window, so a jump instead appearing there would show up
in THEIR before/after time_delta_s), or GPS sync -- though this script
deliberately never touches pps_time_sync/GPS UART at all (unlike
wifi_unit_client.py), so if a jump reproduces here anyway, that's
further evidence it's unrelated to GPS; if it does NOT reproduce here,
that would point back at GPS/PPS interaction as worth checking
separately after all.

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
CLOCK_WATCH_DURATION_S = 90

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
    print("--- clock watch: time.time() vs ticks_ms, 1/s for {}s ---".format(CLOCK_WATCH_DURATION_S))
    watch_start_ticks = time.ticks_ms()
    prev_time_val = time.time()
    for i in range(CLOCK_WATCH_DURATION_S):
        time.sleep(1)
        cur_ticks = time.ticks_ms()
        cur_time_val = time.time()
        ticks_elapsed_ms = time.ticks_diff(cur_ticks, watch_start_ticks)
        time_delta_s = cur_time_val - prev_time_val
        flag = " <== JUMP" if abs(time_delta_s - 1) > 0.5 else ""
        print("t+{}s: ticks_elapsed_ms={} time.time()={} time_delta_since_last_s={}{}".format(
            i + 1, ticks_elapsed_ms, cur_time_val, time_delta_s, flag))
        prev_time_val = cur_time_val

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
