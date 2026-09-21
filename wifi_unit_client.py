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
Python. Known MicroPython WiFi gotchas this was written to survive -- see
the inline comments at each relevant point: no WiFi auto-reconnect
(_wifi_service), and a blocking POST can stall the main loop for longer
than the ring buffer's headroom (RING_CAPACITY).

That last one used to be theoretical. Trial 3 hit it for real: a ~2min
WiFi outage produced a ~7min main-loop freeze via urequests.post(),
which has no socket timeout in most MicroPython forks -- confirmed by
elapsed_s staying flat while wall-clock time advanced and the ADC ring
buffer's overflow counter jumping by ~400,000 during the stall. _post_batch
now calls http_client.timeout_post() instead, which bounds every
individual socket operation (SOCKET_OP_TIMEOUT_S) and the whole POST
attempt (POST_DEADLINE_S) -- see that module for why both are needed. A
machine.WDT backstops the case that software bound itself fails (see
WDT_TIMEOUT_MS_CANDIDATES below) -- if settimeout() is silently not
honoured during the TLS handshake specifically, this watchdog is the
ONLY bound on that stall. reset_cause() is captured at boot and printed
on every STATUS line, so a WDT-triggered reset is unmistakable in the
log.

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

try:
    import json
except ImportError:
    # No precedent for either name anywhere else in this repo, and no
    # vendored urequests.py locally to check what IT imported internally
    # (urequests.post(json=payload) did this same dumps() call, so this
    # isn't a new dependency, just now an explicit, visible one) -- the
    # live device's own /lib couldn't safely be checked either (would
    # mean touching the serial port of a running soak). Genuinely
    # unconfirmed which name this build uses, so don't guess: try both.
    import ujson as json

import micropython
import network
import machine
from machine import ADC, UART, Pin, Timer, WDT

from pps_time_sync import PPSTimeSync
from chunk_summary import summarize_chunk, DegenerateTimestampsError
from wifi_ingest import IngestBuffer
from http_client import PostStageError, parse_https_url, timeout_post
from wifi_config import INGEST_URL, UNIT_ID, WIFI_PASSWORD, WIFI_SSID

INGEST_HOST, INGEST_PORT, INGEST_PATH = parse_https_url(INGEST_URL)

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
# Fixed capacity for one chunk's worth of samples (~ADC_SAMPLE_HZ * CHUNK_S
# ~= 1030 nominal), preallocated once below and reused every chunk instead
# of building fresh lists each time. This is the fix for the MemoryError
# crash loop seen in production: every chunk, three ~1030-element lists
# were each built via .append()/listcomp (_chunk_ts_s etc. filling up,
# voltages = [...], and moving_average's own output list), and MicroPython
# grows a list's backing array by doubling on overflow -- at ~1030 items
# that's a transition from 1024 to 2048 slots, i.e. one fresh *contiguous*
# 8192-byte block (2048 slots * 4 bytes/slot on this 32-bit target) needed
# every single time, which can fail under heap fragmentation even with
# hundreds of KB nominally free (confirmed: every crash logged heap_free
# well over 350KB while failing to allocate exactly 8192 bytes). A margin
# over the nominal ~1030 covers normal timer jitter without ever growing.
# Lowering it further isn't well justified: with array.array buffers
# below, each extra slot of margin costs only its flat itemsize (a few
# bytes), not the boxed-float overhead a plain list paid per slot -- the
# margin is now cheap, and real chunks have been observed needing up to
# ~1148 samples (inferred from a crash requiring a slice sized past 1030
# during unrelated heap pressure), so 1200 stays a reasonable ceiling.
CHUNK_CAPACITY = 1200

# NEEDS VERIFICATION ON HARDWARE: this assumes single-precision floats
# (MICROPY_FLOAT_IMPL_FLOAT), the common default for the rp2 port since
# RP2040/RP2350 have no double-precision FPU -- but this has not been
# confirmed on this specific build. Run check_float_precision.py (repo
# root) on-device before flashing this file: if it prints "double", change
# this to 'd' first. Getting it wrong doesn't crash anything (array.array
# silently truncates values to fit 'f'), but it would store frequency/
# amplitude/timestamp readings at less precision than this build's floats
# actually support, and 'd' costs only 4 more bytes/slot.
FLOAT_TYPECODE = "f"
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

# Captured before anything else below runs, so it reflects why THIS boot
# happened, not some later state. Printed ONCE, in the # BOOT line below
# -- NOT repeated on every STATUS line -- because reset_cause=WDT_RESET
# at the very moment of a fresh launch is EXPECTED, not a signal of
# anything wrong: confirmed by reading mpremote's own source
# (transport_serial.py's enter_raw_repl(), used by `mpremote run` with
# its default soft_reset=True) that every `mpremote run <file>` sends
# Ctrl-D (MicroPython's soft-reset sequence) before executing the file,
# unconditionally. On the rp2 port specifically, that Ctrl-D soft reset
# is documented (not independently verified against this exact firmware
# build's C source, so held with high but not absolute confidence) to go
# through the SDK's watchdog_reboot() mechanism -- meaning reset_cause()
# legitimately reports WDT_RESET after an ORDINARY `mpremote run`, with
# or without this file's own machine.WDT ever existing. Trial 4's first
# attempt confirmed this empirically: WDT_RESET appeared on the very
# first boot line, which is impossible for THIS run's own watchdog to
# have caused (it didn't exist yet at the moment of that reset).
#
# So: WDT_RESET in THIS boot's # BOOT line is normal and not itself a
# stop condition. What WOULD be meaningful is WDT_RESET appearing in a
# LATER # BOOT line that follows an "EVENT reconnected" mid-log (i.e.
# overnight_wifi_log.py detected the device dropped out and relaunched
# it) -- that shape means something reset the board WHILE it was already
# running, which an ordinary `mpremote run` boot-time reset can't
# explain.
#
# Looked up via getattr with a sentinel default rather than `from
# machine import WDT_RESET, ...` directly -- NEEDS VERIFICATION ON
# HARDWARE: not confirmed that the rp2 port defines every one of these
# five constants machine.reset_cause() docs generally list; a missing
# one would crash this whole file at import time if imported by name
# instead.
_RESET_CAUSE_NAMES = {
    getattr(machine, "PWRON_RESET", -1): "PWRON_RESET",
    getattr(machine, "HARD_RESET", -2): "HARD_RESET",
    getattr(machine, "WDT_RESET", -3): "WDT_RESET",
    getattr(machine, "DEEPSLEEP_RESET", -4): "DEEPSLEEP_RESET",
    getattr(machine, "SOFT_RESET", -5): "SOFT_RESET",
}
BOOT_RESET_CAUSE = machine.reset_cause()
BOOT_RESET_CAUSE_NAME = _RESET_CAUSE_NAMES.get(BOOT_RESET_CAUSE, "UNKNOWN({})".format(BOOT_RESET_CAUSE))
print("# BOOT reset_cause={}".format(BOOT_RESET_CAUSE_NAME))

# Tried in order at boot; the first machine.WDT() accepts without raising
# is used. () or None disables the watchdog entirely -- this is a
# build-time choice, not a runtime one, since rp2's WDT has no
# deinit()/disable once started (only .feed(), or letting it expire) --
# set this to () and reflash before any mpremote maintenance session (a
# paused REPL, a long fs cp/ls -- anything that legitimately stops this
# loop from running for a while), since there's no way to pause an armed
# watchdog first.
#
# This is a backstop, not the primary defense: POST_DEADLINE_S
# (http_client.py) is what's SUPPOSED to bound a stuck POST to ~10s in
# software. This watchdog exists for the case that bound itself fails --
# e.g. if a low-level TLS handshake call doesn't actually honour
# sock.settimeout() the way plain socket send/recv do, a real risk this
# hasn't been run against real hardware to rule out (see the Trial 4
# report). If settimeout is silently not honoured during the handshake,
# THIS WATCHDOG IS THE ONLY BOUND on that stall -- there is no software
# fallback for it. http_client.timeout_post()'s feed_fn is wired below to
# wdt.feed, called at every stage transition and before every individual
# read -- not just once per full POST -- so this watchdog only needs to
# survive the GAP BETWEEN two feeds, not the whole POST_DEADLINE_S.
#
# 8000 (first try): 2x SOCKET_OP_TIMEOUT_S=4s margin over that gap for
# scheduler/GC jitter, chosen to stay under the RP2040 datasheet's
# ~8.388s single-period hardware watchdog cap (24-bit counter at a fixed
# tick rate) -- NEEDS VERIFICATION ON HARDWARE: whether the Pico 2's
# RP2350 (a different chip revision) shares that cap, and whether
# MicroPython's rp2 WDT driver enforces or extends it, is unconfirmed.
# 4000 (fallback, only used if 8000 is rejected): matches
# SOCKET_OP_TIMEOUT_S exactly, meaning if THIS is what actually gets
# used, the watchdog's margin over one legitimate (if slow) socket
# operation shrinks from 2x to 1x -- a genuinely slow-but-working
# operation right at its own 4s timeout could then race the watchdog.
# That degradation is deliberately visible (see WDT_ARMED below), not
# silent -- if the fallback is what's actually needed, SOCKET_OP_TIMEOUT_S
# itself should be revisited once real hardware says why 8000 didn't work,
# rather than leaving the margin thin indefinitely.
WDT_TIMEOUT_MS_CANDIDATES = (8000, 4000)

wdt = None
WDT_TIMEOUT_MS_ACTUAL = None
if WDT_TIMEOUT_MS_CANDIDATES:
    for _candidate_ms in WDT_TIMEOUT_MS_CANDIDATES:
        try:
            wdt = WDT(timeout=_candidate_ms)
            WDT_TIMEOUT_MS_ACTUAL = _candidate_ms
            break
        except (ValueError, OSError) as exc:
            print("# WDT_CANDIDATE_REJECTED timeout_ms={} type={} msg={}".format(
                _candidate_ms, type(exc).__name__, exc))
    if wdt is None:
        # Fail loudly, before any hardware below (ADC included) starts --
        # a watchdog was explicitly requested (WDT_TIMEOUT_MS_CANDIDATES
        # is non-empty) and NONE of the candidates worked. Proceeding
        # without one here would silently drop the freeze-fix's backstop
        # with no record of why.
        raise RuntimeError(
            "machine.WDT rejected every candidate timeout in {} -- refusing to start "
            "without the requested watchdog. Check machine.WDT's accepted range on "
            "this build (see the RP2040 ~8.388s hardware-watchdog-cap note above) "
            "and adjust WDT_TIMEOUT_MS_CANDIDATES.".format(WDT_TIMEOUT_MS_CANDIDATES))
    # machine.WDT does not document a public getter for the configured
    # timeout on rp2 -- print whatever's available via getattr rather
    # than assume either way; NEEDS VERIFICATION ON HARDWARE whether this
    # build exposes one at all (e.g. an undocumented `.timeout`).
    _wdt_effective = getattr(wdt, "timeout", None)
    print("# WDT_ARMED requested_ms={} effective_reported_ms={}".format(
        WDT_TIMEOUT_MS_ACTUAL,
        _wdt_effective if _wdt_effective is not None else "not_exposed_by_driver"))

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


def _feed_wdt():
    if wdt is not None:
        wdt.feed()


PRE_POST_GC_COLLECT = True  # named constant so this can be disabled -- e.g. to check
                            # whether the collect below is actually reducing/preventing
                            # ENOMEM failures, or whether they happen regardless


def _post_batch(payload):
    """IngestBuffer's injected post_fn. Returns True only on a 2xx
    response. Every socket opened by timeout_post() is closed inside
    that function itself (success or failure) -- see http_client.py.

    Uses http_client.timeout_post() rather than urequests.post(): the
    latter has no socket timeout in most MicroPython forks, so a dead/
    half-open connection could block this call indefinitely -- exactly
    what happened in Trial 3 (see module docstring). timeout_post()
    bounds every individual socket operation (SOCKET_OP_TIMEOUT_S) and
    the whole attempt (POST_DEADLINE_S), and raises PostStageError
    (tagging which stage failed) instead of leaving that ambiguous.

    Trial 1 showed heap_free swinging through deep troughs between
    readings taken 30s apart (as low as ~28KB total, ~12KB max contiguous
    free block), every one of which fully recovered by the next STATUS
    line's own gc.collect() (10s later) -- meaning most of each trough
    was reclaimable garbage sitting uncollected, not live data. gc.collect()
    is now called here too (gated by PRE_POST_GC_COLLECT above, so this
    can be A/B tested rather than assumed to help), immediately before
    mem_info()/the POST, so a POST never has to compete with garbage
    that simply hasn't been swept yet.

    Both mem_info() and gc.mem_free() are read/printed before AND after
    the collect, so the before/after is visible. gc.mem_free() returns a
    value, so both numbers land on the single PRE_POST line directly.
    micropython.mem_info() does not return anything -- it only prints,
    and there is no portable MicroPython API to read "max free sz" as a
    number (confirmed: this is an open feature request, not an oversight
    on this file's part -- see micropython/micropython#910) -- so its
    before/after can only be two separate printed blocks
    (MEM_INFO_PRE_COLLECT / MEM_INFO_POST_COLLECT), not merged into one
    line, but they're adjacent in the log and directly comparable by eye.
    """
    heap_free_before_collect = gc.mem_free()
    print("# MEM_INFO_PRE_COLLECT")
    micropython.mem_info()

    if PRE_POST_GC_COLLECT:
        gc.collect()

    heap_free_after_collect = gc.mem_free()
    print("# MEM_INFO_POST_COLLECT")
    micropython.mem_info()

    print("# PRE_POST heap_free_before_collect={} heap_free_after_collect={}".format(
        heap_free_before_collect, heap_free_after_collect))

    global longest_post_duration_s, stage_failure_counts

    if not wlan.isconnected():
        print("# POST_FAIL reason=wifi_disconnected stage=n/a heap_free={}".format(gc.mem_free()))
        return False
    try:
        # Captured at the moment this function actually starts the
        # request, so a failure line shows the state right before the
        # attempt instead of only the aftermath -- Trial 1's POST_FAIL
        # lines showed heap_free well above 350KB, read after the POST
        # call had already unwound and likely freed whatever it failed
        # to allocate, which told us almost nothing about the actual
        # moment of failure. This still isn't the literal instant of an
        # internal allocation failure (only instrumenting timeout_post()
        # itself could get that granularity), but it's the closest this
        # branch can get without doing that.
        heap_free_at_try_start = gc.mem_free()
        body_bytes = json.dumps(payload).encode("utf-8")
        # Timed around timeout_post() specifically (not this whole
        # function, which also does GC/mem_info bookkeeping with its own
        # separate instrumentation above) -- on BOTH success and failure,
        # so a stalled attempt that eventually raises still updates this;
        # that's often the more interesting case for spotting stalls.
        _post_start_ticks = time.ticks_us()
        try:
            status_code, _headers, _body = timeout_post(
                INGEST_HOST, INGEST_PATH, body_bytes, port=INGEST_PORT,
                feed_fn=_feed_wdt)
        finally:
            duration_s = time.ticks_diff(time.ticks_us(), _post_start_ticks) / 1e6
            if duration_s > longest_post_duration_s:
                longest_post_duration_s = duration_s
        ok = 200 <= status_code < 300
        if not ok:
            print("# POST_FAIL reason=http_status stage=n/a status={} heap_free_at_try_start={} "
                  "heap_free_now={}".format(
                status_code, heap_free_at_try_start, gc.mem_free()))
        return ok
    except PostStageError as exc:
        stage_failure_counts[exc.stage] = stage_failure_counts.get(exc.stage, 0) + 1
        print("# POST_FAIL reason={} stage={} elapsed_s={} stage_duration_s={} deadline_s={} "
              "heap_free_at_try_start={} heap_free_now={}".format(
            exc.reason, exc.stage, exc.elapsed_s, exc.stage_duration_s, exc.deadline_s,
            heap_free_at_try_start, gc.mem_free()))
        return False
    except Exception as exc:
        print("# POST_FAIL reason=exception stage=unknown type={} msg={} heap_free_at_try_start={} "
              "heap_free_now={}".format(
            type(exc).__name__, exc, heap_free_at_try_start, gc.mem_free()))
        return False


buffer = IngestBuffer(UNIT_ID, post_fn=_post_batch, max_readings=MAX_BUFFERED_READINGS)

_last_consumed_ticks = t0
_elapsed_us_total = 0
# Fixed-capacity buffers, allocated once here and reused every chunk by
# writing to index _chunk_len (never .append()'d/reallocated) -- see
# CHUNK_CAPACITY above for why. Only indices [0, _chunk_len) hold valid
# data for the *current* chunk; everything from _chunk_len onward is
# stale leftover from a previous chunk and must never be read -- every
# consumer below is bounded to _chunk_len, not len(...), specifically to
# guard against that.
#
# array.array, not plain lists: a plain list of floats still boxes each
# individual value (a separate heap object per element) even though the
# list itself is pre-sized -- only the backing pointer array was fixed,
# not the ~1030 float objects it points to, which were freshly allocated
# and freed every chunk regardless of the list-growth fix. array.array
# stores packed, unboxed values instead, so filling these buffers by
# index allocates nothing at all: indexing/reading/rewriting existing
# slots is unchanged (array.array supports the exact same arr[i]/
# arr[i]=x/len(arr) interface as a list), and the stale-data guards
# above are unaffected. counts uses 'H' (raw ADC u16, matches ring_raw's
# own typecode) and ticks uses 'I', both narrower than a list's 4-byte
# pointer slot for at least one of them; the float buffers use
# FLOAT_TYPECODE (see its own comment above -- unverified pending
# check_float_precision.py).
_chunk_ticks = array.array("I", [0] * CHUNK_CAPACITY)     # raw time.ticks_us() per sample -- for gps_utc_s lookup
_chunk_ts_s = array.array(FLOAT_TYPECODE, [0.0] * CHUNK_CAPACITY)    # chunk-relative elapsed seconds per sample -- summarize_chunk's timestamps
_chunk_counts = array.array("H", [0] * CHUNK_CAPACITY)    # raw u16 ADC counts per sample -- converted to volts below
_voltages = array.array(FLOAT_TYPECODE, [0.0] * CHUNK_CAPACITY)      # scratch buffer for the volts-converted chunk
_filtered_buf = array.array(FLOAT_TYPECODE, [0.0] * CHUNK_CAPACITY)  # scratch buffer for summarize_chunk's filtered signal
_chunk_len = 0
_chunk_start_us = 0  # _elapsed_us_total at the current chunk's first sample --
                      # see _chunk_ts_s's own comment in the main loop below
chunk_capacity_overflow_count = 0  # a chunk needed more than CHUNK_CAPACITY samples --
                                    # extra samples past capacity are dropped and counted here,
                                    # same "count, don't silently lose" contract as overflow_count
_last_post_ticks = t0
_last_status_ticks = t0
peak_buffered = 0  # highest len(buffer) observed -- see bench-run report in commit history
post_attempts = 0  # a flush() where the buffer was actually non-empty -- excludes no-op flushes
post_successes = 0
dup_timestamp_count = 0  # DegenerateTimestampsError occurrences -- see chunk_summary.py
longest_post_duration_s = 0.0  # wall-clock duration of the slowest _post_batch call so far,
                                # success or failure -- see _post_batch for how it's measured
# One counter per http_client.PostStageError.stage seen so far -- shows
# WHERE POST attempts are failing/stalling, not just how many. Includes
# both timeout_post()'s own POST_DEADLINE_S trips and any other
# exception at that stage (e.g. a genuine ECONNRESET, not only a
# settimeout-triggered timeout) -- NEEDS VERIFICATION ON HARDWARE:
# distinguishing a real socket timeout from another OSError by errno
# would need this build's actual errno for a timed-out socket op
# confirmed first, which wasn't done here; until then this counts every
# stage failure, not only confirmed timeouts, which is still the
# actionable signal (where do POSTs actually get stuck).
stage_failure_counts = {"dns": 0, "connect": 0, "tls_handshake": 0, "send": 0, "read_response": 0}

_wifi_service()

while True:
    _feed_wdt()  # covers everything in this iteration OTHER than a POST attempt --
                 # timeout_post() feeds it separately, at a finer grain, during one

    drained = 0
    while read_idx != write_idx and drained < MAX_DRAIN_PER_PASS:
        raw_ticks = ring_ticks[read_idx]
        raw_count = ring_raw[read_idx]
        read_idx = (read_idx + 1) % RING_CAPACITY
        drained += 1

        _elapsed_us_total += time.ticks_diff(raw_ticks, _last_consumed_ticks)
        _last_consumed_ticks = raw_ticks

        if _chunk_len < CHUNK_CAPACITY:
            if _chunk_len == 0:
                # First sample of a new chunk -- this becomes the
                # reference point _chunk_ts_s is measured from. See its
                # own comment below for why.
                _chunk_start_us = _elapsed_us_total
            _chunk_ticks[_chunk_len] = raw_ticks
            # Chunk-relative, not _elapsed_us_total/1e6 (session-cumulative)
            # directly: chunk_summary.py/freq_estimator.py only ever use
            # differences between _chunk_ts_s values (span, zero-crossing
            # interpolation), never an absolute value, so a chunk-relative
            # timestamp is numerically equivalent for every downstream
            # calculation -- but it keeps the magnitude bounded to
            # roughly [0, CHUNK_S] regardless of how long the process has
            # been running, instead of growing for hours. That bound
            # matters because FLOAT_TYPECODE='f' (single precision, this
            # is a rp2 board): float32's step size at ~1030s of session
            # time is already ~0.24ms, close to the ~0.97ms raw sample
            # interval at ADC_SAMPLE_HZ=1030, and by ~10h it's ~3.9ms --
            # *wider* than the sample interval, meaning consecutive
            # samples' cumulative timestamps could round to the same
            # float32 value. A chunk-relative value never exceeds
            # ~1.2s, where float32's step size is a few microseconds --
            # utterly negligible next to the ~970us sample interval, for
            # as long as this process runs.
            _chunk_ts_s[_chunk_len] = (_elapsed_us_total - _chunk_start_us) / 1e6
            _chunk_counts[_chunk_len] = raw_count
            _chunk_len += 1
        else:
            # CHUNK_CAPACITY's margin over the nominal ~1030 samples/chunk
            # wasn't enough this time -- drop the extra samples (same
            # "count, don't silently lose" contract as the ADC ring
            # buffer's own overflow_count) rather than grow the buffer,
            # which would defeat the whole point of preallocating it.
            chunk_capacity_overflow_count += 1

    # Once ~CHUNK_S worth of samples has accumulated, reduce it to one
    # (frequency_hz, amplitude_v, gps_utc_s) reading and buffer it. Every
    # index used below is bounded to _chunk_len, not len(...) -- these are
    # fixed-capacity buffers reused every chunk (see CHUNK_CAPACITY), so
    # indices past _chunk_len hold stale data from a previous chunk.
    if _chunk_len > 0 and (_chunk_ts_s[_chunk_len - 1] - _chunk_ts_s[0]) >= CHUNK_S:
        for _i in range(_chunk_len):
            _voltages[_i] = _chunk_counts[_i] * ADC_VOLTAGE_SCALE
        try:
            frequency_hz, amplitude_v = summarize_chunk(
                _chunk_ts_s, _voltages, n=_chunk_len, filtered_buf=_filtered_buf)
            gps_utc_s = sync.ticks_to_utc(_chunk_ticks[_chunk_len - 1])
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
                _elapsed_us_total / 1e6, _chunk_len,
                _chunk_ts_s[0] if _chunk_len else None,
                _chunk_ts_s[_chunk_len - 1] if _chunk_len else None,
                since_last_post_us, overflow_count, exc,
            ))
        except ValueError:
            pass  # too few crossings this chunk -- skip it, same as overnight_log.py
        _chunk_len = 0

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
              "post_successes={} dup_timestamp_count={} chunk_capacity_overflow={} "
              "longest_post_duration_s={:.3f} stage_fail_dns={} "
              "stage_fail_connect={} stage_fail_tls_handshake={} stage_fail_send={} "
              "stage_fail_read_response={}".format(
            _elapsed_us_total / 1e6, wlan.isconnected(), s["synced"],
            current_buffered, peak_buffered, buffer.dropped_count, overflow_count,
            gc.mem_free(), gc.mem_alloc(), post_attempts, post_successes,
            dup_timestamp_count, chunk_capacity_overflow_count,
            longest_post_duration_s,
            stage_failure_counts["dns"], stage_failure_counts["connect"],
            stage_failure_counts["tls_handshake"], stage_failure_counts["send"],
            stage_failure_counts["read_response"],
        ))
