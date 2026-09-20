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

import micropython
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
# After a POST actually attempted and failed (not wifi_disconnected --
# that's already handled by _wifi_service()'s own independent 5s retry
# timer, a different failure mode), the effective interval between
# attempts doubles, capped, instead of retrying every POST_INTERVAL_S
# regardless -- an overnight soak's failure runs were consistently 12-17
# consecutive attempts 30s apart before crashing, so backing off gives a
# struggling connection room rather than hammering it at a fixed rate.
# BACKOFF_CAP_S=240 (8x) is a reasoned starting point matching that
# observed run length, not a measured optimum -- NEEDS VERIFICATION ON
# HARDWARE whether it actually shortens or prevents a failure run.
BACKOFF_MULTIPLIER = 2.0
BACKOFF_CAP_S = 240.0
# After this many consecutive failures, force an extra gc.collect() (and
# explicitly drop this frame's socket/response reference first) rather
# than waiting for the routine ones -- see _post_batch's docstring.
CONSECUTIVE_FAILURE_GC_THRESHOLD = 3
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


PRE_POST_GC_COLLECT = True  # named constant so this can be disabled -- e.g. to check
                            # whether the collect below is actually reducing/preventing
                            # ENOMEM failures, or whether they happen regardless

_consecutive_failures = 0
_current_post_interval_s = POST_INTERVAL_S  # read by the main loop instead of the
                                              # POST_INTERVAL_S constant directly --
                                              # see BACKOFF_MULTIPLIER's comment


def _post_batch(payload):
    """IngestBuffer's injected post_fn. Returns True only on a 2xx
    response. Every response is explicitly closed -- a missed .close()
    on urequests leaks the underlying socket, and repeated leaks exhaust
    the Pico's socket table over an unattended multi-hour run (see module
    docstring). NEEDS VERIFICATION ON HARDWARE: whether this urequests
    build supports a timeout kwarg at all -- without one, a dead/half-open
    connection can block this call indefinitely.

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
    A third snapshot (MEM_INFO_TRY_START) repeats this right at the top
    of the try block, as close to the actual urequests.post() call as
    code can get without modifying it -- unavoidably almost identical to
    MEM_INFO_POST_COLLECT a few lines above (nothing but the
    wlan.isconnected() check runs in between), but captured separately
    since it's the tightest bound available on "state immediately before
    the risky call" without instrumenting urequests itself (branch
    fix-memcrash-manual-post does that; this branch doesn't).

    Backoff and extra cleanup on a failure run: see BACKOFF_MULTIPLIER/
    CONSECUTIVE_FAILURE_GC_THRESHOLD's own comments. Only a POST actually
    attempted and failed (http_status or exception) counts -- not
    wifi_disconnected, which returns before any of this and is already
    retried on its own independent timer by _wifi_service().
    """
    global _consecutive_failures, _current_post_interval_s

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

    if not wlan.isconnected():
        print("# POST_FAIL reason=wifi_disconnected heap_free={}".format(gc.mem_free()))
        return False

    response = None
    ok = False
    try:
        print("# MEM_INFO_TRY_START")
        micropython.mem_info()
        # Captured at the moment this function actually starts the
        # request, so a failure line shows the state right before the
        # attempt instead of only the aftermath -- Trial 1's POST_FAIL
        # lines showed heap_free well above 350KB, read after
        # urequests.post() had already unwound and likely freed whatever
        # it failed to allocate, which told us almost nothing about the
        # actual moment of failure. This still isn't the literal instant
        # of the internal allocation failure inside urequests (only
        # instrumenting urequests itself -- branch
        # fix-memcrash-manual-post, not part of this branch -- can get
        # that granularity), but it's the closest this branch can get
        # without doing that.
        heap_free_at_try_start = gc.mem_free()
        response = urequests.post(INGEST_URL, json=payload)
        ok = 200 <= response.status_code < 300
        if not ok:
            print("# POST_FAIL reason=http_status status={} heap_free_at_try_start={} "
                  "heap_free_now={}".format(
                response.status_code, heap_free_at_try_start, gc.mem_free()))
    except Exception as exc:
        print("# POST_FAIL reason=exception type={} msg={} heap_free_at_try_start={} "
              "heap_free_now={}".format(
            type(exc).__name__, exc, heap_free_at_try_start, gc.mem_free()))
        ok = False
    finally:
        if response is not None:
            response.close()

    if ok:
        _consecutive_failures = 0
        _current_post_interval_s = POST_INTERVAL_S
    else:
        _consecutive_failures += 1
        _current_post_interval_s = min(
            _current_post_interval_s * BACKOFF_MULTIPLIER, BACKOFF_CAP_S)
        if _consecutive_failures >= CONSECUTIVE_FAILURE_GC_THRESHOLD:
            response = None  # explicitly drop the (already-closed) reference
                               # before forcing this extra collect, in case
                               # anything it still held onto is what's
                               # keeping a would-be-free block unreachable
            print("# MEM_INFO_BEFORE_EXTRA_COLLECT consecutive_failures={}".format(
                _consecutive_failures))
            micropython.mem_info()
            gc.collect()
            print("# MEM_INFO_AFTER_EXTRA_COLLECT")
            micropython.mem_info()
    return ok


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
    # _current_post_interval_s, not the POST_INTERVAL_S constant directly --
    # it backs off past 30s after consecutive failures and resets to 30s on
    # the next success (see _post_batch's docstring / BACKOFF_MULTIPLIER).
    if time.ticks_diff(now, _last_post_ticks) >= _current_post_interval_s * 1_000_000:
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
              "post_successes={} dup_timestamp_count={} chunk_capacity_overflow={}".format(
            _elapsed_us_total / 1e6, wlan.isconnected(), s["synced"],
            current_buffered, peak_buffered, buffer.dropped_count, overflow_count,
            gc.mem_free(), gc.mem_alloc(), post_attempts, post_successes,
            dup_timestamp_count, chunk_capacity_overflow_count,
        ))
