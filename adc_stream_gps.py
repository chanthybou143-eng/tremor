"""ADC sampling + GPS/PPS-synced UTC timestamps, on-device (Pico-side).

Sibling to adc_stream_timed.py, not a replacement for it -- see that file's
own docstring reasoning; the short version is that overnight_log.py,
live_frequency.py, and live_tail_frequency.py all depend on
adc_stream_timed.py's exact two-field line schema, so this keeps its own,
separate three-field schema ("t_us,voltage,utc_s") rather than touching it.

Timer-IRQ decoupled ADC acquisition: a hardware Timer fires at
ADC_SAMPLE_HZ and does only the minimal work of an ISR -- read the ADC,
read ticks_us(), push both into a small ring buffer. The main loop drains
that buffer and prints, which decouples *when a sample is acquired*
(precise, timer-driven, ~1030Hz) from *when it gets printed* (best-effort).
An earlier version sampled ADC inline in the same loop that also polled
GPS UART, which dropped throughput to ~200Hz -- unacceptable, since the
frequency estimator's filter design assumes ~1030Hz.

GPS UART is polled (`uart.any()` / bounded `uart.read(GPS_READ_CHUNK_BYTES)`
from the main loop), not interrupt-driven. An interrupt-driven version
(UART.irq(trigger=UART.IRQ_RXIDLE)) was built and tested, since polling has
a known, documented limitation (see below) that an IRQ-driven design could
plausibly remove structurally. It's reverted, not adopted: it introduced a
GPS-sync reliability regression (sync succeeding on only ~18 of 45 PPS
pulses in one bench run, vs. this polling version's ~97.9%+ sync coverage
sustained across a full 10.5h soak) that didn't have a confirmed root
cause the way the polling limitation below does, only a hypothesis
(IRQ_RXIDLE not firing reliably once per burst, occasionally merging two
seconds' bursts into one read and pairing the RMC against a stale PPS
edge). One rare, well-understood, bounded problem is a better trade than
one more frequent, unconfirmed one -- on record as tried, not unconsidered,
should UART IRQ reception be worth revisiting later.

Known limitation of the polling approach that's shipped here: a 10.5h soak
test lost ~2.1% of samples, concentrated in ~15 events of ~100-115ms each
(not spread evenly -- the rest of the run had ~0 loss). Per-sample analysis
showed every one of those events landed at nearly the same phase relative
to the PPS/UTC second boundary (~130-150ms in), across events spread hours
apart -- confirmed mechanism, not a guess: average bytes/call (~121, near
the 128-byte cap) was high but average call *duration* (~34.6ms) was far
below the ~126ms a full transfer would take at 9600 baud, meaning most
calls return fast because the data is already sitting in the hardware
buffer by the time the main loop polls. The rare ~100-115ms stalls happen
specifically when polling lands *before* the buffer has filled -- catching
the UART mid-transmission and being forced to wait for the rest. No amount
of GPS_READ_CHUNK_BYTES tuning removes this: any polling read can
coincidentally land mid-transmission. Accepted as a bounded, rare,
understood cost rather than chased further -- see git history for the
full investigation (throughput root-causing, the profiling methodology,
and the IRQ redesign attempt).

RING_CAPACITY sets how much lag between ADC acquisition and printing the
ring buffer can absorb before samples are dropped (counted in
overflow_count, not silently lost without a trace).
"""

from machine import ADC, UART, Pin, Timer
import array
import time

from pps_time_sync import PPSTimeSync

ADC_SAMPLE_HZ = 1030  # matches adc_stream_timed.py's measured real-world rate
RING_CAPACITY = 512   # ~497ms of buffering headroom at 1030Hz
MAX_DRAIN_PER_PASS = 128  # see main loop: caps the ring-buffer drain so GPS
                          # UART servicing and the status/exit checks always
                          # get a turn, even if the timer is refilling the
                          # buffer as fast as print() can drain it

# None = run forever (production). A number of seconds = exit the main loop
# and deinit the Timer cleanly after that long, then return -- for bring-up
# verification runs only. A running Timer is real hardware state, not a
# host-side process: killing `mpremote run` from the Mac side sends a
# Ctrl-C into whatever's executing on the device, which can land inside the
# Timer ISR itself and leaves the Timer still firing after mpremote
# disconnects. Bounding the run here lets the device exit on its own --
# no external interrupt needed -- which is the only reliable way to stop a
# script that holds a live Timer.
TEST_DURATION_S = None

adc = ADC(26)
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1), timeout=0, timeout_char=0)
sync = PPSTimeSync(pps_pin=15)

t0 = time.ticks_us()

# Ring buffer: preallocated fixed-size arrays, no allocation inside the ISR.
# Stores the RAW ticks_us() reading per sample (a single reading is always
# safe to store -- 'L', unsigned 32-bit, covers MicroPython's documented
# "at least 2**31" wrap period with room to spare). Converting that into a
# safe, ever-increasing "elapsed since script start" value -- the thing
# that's NOT safe to compute with a single ticks_diff() against a
# fixed, aging t0 once a run runs for hours -- happens in the main loop
# (see _elapsed_us_total below), not here.
ring_ticks = array.array("L", [0] * RING_CAPACITY)
ring_raw = array.array("H", [0] * RING_CAPACITY)  # EXPERIMENT: raw u16 counts, not volts
write_idx = 0
read_idx = 0
overflow_count = 0


def _on_adc_timer(timer):
    global write_idx, overflow_count
    next_write_idx = (write_idx + 1) % RING_CAPACITY
    if next_write_idx == read_idx:
        overflow_count += 1  # buffer full -- drop this sample, keep draining what's queued
        return
    ring_ticks[write_idx] = time.ticks_us()
    ring_raw[write_idx] = adc.read_u16()  # EXPERIMENT: no float division here
    write_idx = next_write_idx


# Incremental, wraparound-safe elapsed-time accumulator. time.ticks_diff()
# is only guaranteed correct when the true elapsed time between its two
# arguments is under ~half of MicroPython's ticks wrap period (spec
# guarantees the period is *at least* 2**31 ticks, i.e. correctness is only
# guaranteed under roughly 2**30us =~ 17.9 minutes) -- a single diff against
# a fixed t0 from hours ago is exactly the unsafe case. Diffing only between
# *consecutive* drained samples (always ~1ms apart) keeps every individual
# diff far inside the safe window, and accumulating into a plain Python int
# (arbitrary precision, never wraps) makes the *running total* safe forever,
# regardless of how long the script has been running. This bug was latent
# through every bench test under an hour and would have silently corrupted
# the printed t_us field and the UTC reconstruction partway through a
# multi-hour soak run -- caught before the 10.5h soak that validated this
# script, not after.
_last_consumed_ticks = t0
_elapsed_us_total = 0


adc_timer = Timer()
adc_timer.init(freq=ADC_SAMPLE_HZ, mode=Timer.PERIODIC, callback=_on_adc_timer)

MAX_GPS_BUF_BYTES = 1024
GPS_READ_CHUNK_BYTES = 128  # an unbounded uart.read() blocks for as long as
                            # it takes to receive whatever's actively
                            # arriving (~180-250ms for a typical burst at
                            # 9600 baud), not just what's already buffered
                            # -- more than enough to overflow the ring
                            # buffer on its own. Bounding to 128 bytes/call
                            # caps the worst-case block at ~133ms (~137 ADC
                            # samples at 1030Hz, well under
                            # RING_CAPACITY=512 even stacked on top of a
                            # full MAX_DRAIN_PER_PASS backlog), while
                            # staying large enough that a ~240-280-byte
                            # average burst only needs ~2 calls.
gps_buf = b""

STATUS_INTERVAL_S = 10
last_status_ticks = t0

while True:
    # Drain whatever the timer has queued since the last pass -- bounded,
    # not "until empty": if the timer is producing at >= the rate the main
    # loop can drain, an unbounded version of this loop would never exit,
    # which would starve GPS UART servicing and the status/exit checks
    # below forever (this was a real bug caught during bring-up testing,
    # not a hypothetical -- see git history).
    #
    # Lines are batched into one print() call per pass rather than one
    # print() per sample: an earlier version called print() per-sample and
    # measured only ~600Hz effective output (vs. the ~1030Hz the timer
    # actually acquires at), with the gap traced to print()'s own per-call
    # overhead, not data volume -- a batch of <=128 short lines is at most
    # a few KB, trivial to build and to send. Same wire format either way
    # (one line per sample, newline-terminated); this only changes how many
    # underlying print()/write() calls it takes to emit them.
    drained = 0
    lines = []
    while read_idx != write_idx and drained < MAX_DRAIN_PER_PASS:
        raw_ticks = ring_ticks[read_idx]
        raw = ring_raw[read_idx]  # EXPERIMENT: raw u16 count, host converts to volts
        read_idx = (read_idx + 1) % RING_CAPACITY
        drained += 1

        # Safe by construction: raw_ticks is always close to
        # _last_consumed_ticks (consecutive ~1030Hz samples, ~1ms apart),
        # so this diff is always far inside ticks_diff()'s guaranteed-safe
        # window even after hours of runtime -- see the accumulator's
        # docstring above for why a diff against a fixed, aging t0 was not.
        _elapsed_us_total += time.ticks_diff(raw_ticks, _last_consumed_ticks)
        _last_consumed_ticks = raw_ticks
        t_us = _elapsed_us_total

        utc_s = sync.ticks_to_utc(raw_ticks)
        utc_field = "" if utc_s is None else "{:.3f}".format(utc_s)
        lines.append(str(t_us) + "," + str(raw) + "," + utc_field)
    if lines:
        print("\n".join(lines))

    # Non-blocking GPS UART drain -- only touches the UART when there's
    # actually something waiting, so it doesn't add per-iteration overhead
    # to the (much more frequent) ring-buffer drain above.
    if uart.any():
        chunk = uart.read(GPS_READ_CHUNK_BYTES)
        if chunk:
            gps_buf += chunk
            while b"\n" in gps_buf:
                line_bytes, gps_buf = gps_buf.split(b"\n", 1)
                # Cheap raw-bytes pre-filter: only RMC carries the fix we
                # care about (see nmea_parser.py), but a burst is ~10-15
                # sentences/sec (GGA/GSA x4/GSV/VTG/...). A bytes-level
                # substring check here skips decode()+strip()+the full
                # parse for ~90% of lines that would just be rejected
                # inside parse_rmc() anyway, after paying that cost.
                if b"RMC" not in line_bytes:
                    continue
                try:
                    line = line_bytes.decode("ascii").strip()
                except UnicodeError:
                    continue  # garbled/partial bytes -- drop this line, keep going
                if line:
                    sync.feed_nmea(line)
            if len(gps_buf) > MAX_GPS_BUF_BYTES:
                gps_buf = gps_buf[-MAX_GPS_BUF_BYTES:]  # no newline for too long -- drop stale prefix

    now = time.ticks_us()
    if time.ticks_diff(now, last_status_ticks) >= STATUS_INTERVAL_S * 1_000_000:
        # Safe: last_status_ticks refreshes every ~10s, always inside the
        # guaranteed-safe ticks_diff() window regardless of total run length.
        last_status_ticks = now
        s = sync.status
        print("# STATUS elapsed_s={:.1f} synced={} pps={} sync={} rejected={} no_edge={} "
              "period_us={} date={} overflow={}".format(
            _elapsed_us_total / 1e6,
            s["synced"], s["pps_count"], s["sync_count"], s["rejected_count"],
            s["no_edge_count"], s["pps_period_us"], s["anchor_date"], overflow_count,
        ))

    # Uses the safe accumulator, not ticks_diff(now, t0) -- see its
    # docstring above: for a multi-hour TEST_DURATION_S, a direct diff
    # against the original t0 would silently become incorrect long before
    # the intended duration elapsed.
    if TEST_DURATION_S is not None and _elapsed_us_total >= TEST_DURATION_S * 1_000_000:
        adc_timer.deinit()
        s = sync.status
        print("# TEST DONE elapsed_s={:.1f} synced={} pps={} sync={} rejected={} no_edge={} "
              "period_us={} date={} overflow={}".format(
            _elapsed_us_total / 1e6,
            s["synced"], s["pps_count"], s["sync_count"], s["rejected_count"],
            s["no_edge_count"], s["pps_period_us"], s["anchor_date"], overflow_count,
        ))
        break
