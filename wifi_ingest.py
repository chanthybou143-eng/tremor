"""Buffers per-reading summaries and ships them to TREMOR's /api/ingest
endpoint in batches, retrying on failure instead of dropping data.

Deliberately isolated from network.WLAN/urequests (see wifi_unit_client.py
for that glue) so this buffering/eviction logic -- pure Python, only
array/_thread beyond the stdlib -- runs and is testable under desktop
CPython, same portability reasoning as freq_estimator.py. The actual HTTP
POST is injected as a callable rather than imported directly, so tests can
swap in a fake one without needing MicroPython's network stack.

Genuinely concurrent as of the wifi_unit_client.py dual-core redesign:
append() runs on core 0 (from the ADC-reduction path) while flush() runs
on core 1 (from the WiFi thread), so IngestBuffer is no longer
single-threaded and needs real locking -- see the class docstring below.
_thread is used rather than threading specifically because it's the one
threading-flavoured module both CPython and MicroPython provide under the
same name with a compatible allocate_lock() API, so this file needs no
platform branching to stay host-testable.

Fixed-capacity storage, not a plain growing list: the previous
implementation (self._buf = []; self._buf.append(...)) was the actual
crash site in a 12.5-hour overnight soak of the array.array chunk-buffer
fix -- 4 restarts, all at this file's old line 76 (self._buf.append),
every one preceded by a run of failed POSTs that left the buffer growing
by one reading per second with nothing draining it. That's the exact same
bug class as the original chunk_summary.py crash this whole effort started
from (MicroPython grows a list's backing array by doubling on overflow,
needing a fresh contiguous block every transition, which can fail under
heap fragmentation even with plenty nominally free) -- just in a buffer
nobody had reason to suspect until it grew large enough, during a failure
run, to hit one of those transitions itself.
"""

import _thread
import array

FLOAT_TYPECODE = "f"  # NEEDS VERIFICATION ON HARDWARE, not assumed: matches
                       # wifi_unit_client.py's FLOAT_TYPECODE and the same
                       # check_float_precision.py result -- use 'd' here
                       # too if that script prints "double".


class _RingStorage:
    """Fixed-capacity storage for one FIFO's worth of buffered readings --
    preallocated once, filled/read by index, never resized. IngestBuffer
    keeps two of these (see its own docstring for why) and swaps which one
    is "active" instead of allocating a fresh one on every flush().

    amplitude_v/gps_utc_s can legitimately be None (amplitude_v isn't in
    practice, given summarize_chunk always returns a real float, but the
    public append() signature never enforced that; gps_utc_s genuinely is
    None before PPS sync) -- array('f', ...) can't hold None, so a
    parallel has_amp/has_gps flag byte per slot records whether the float
    slot is meaningful, same idea the task asked for GPS specifically,
    applied to both for a uniform, simple scheme.
    """

    def __init__(self, capacity):
        self.freq = array.array(FLOAT_TYPECODE, [0.0] * capacity)
        self.amp = array.array(FLOAT_TYPECODE, [0.0] * capacity)
        self.has_amp = bytearray(capacity)
        self.gps = array.array(FLOAT_TYPECODE, [0.0] * capacity)
        self.has_gps = bytearray(capacity)
        # v2 payload fields (see IngestBuffer's boot_id). Integer typecodes only:
        # seq is a per-boot reading counter; the gps triple is the device's
        # full-precision integer UTC from PPSTimeSync.ticks_to_gps().
        self.seq = array.array("I", [0] * capacity)
        self.gday = array.array("H", [0] * capacity)      # days since 1970-01-01
        self.gsec = array.array("I", [0] * capacity)      # second of day
        self.gus = array.array("I", [0] * capacity)       # microsecond
        self.has_gpsi = bytearray(capacity)
        self.head = 0
        self.count = 0


class IngestBuffer:
    """A bounded FIFO of pending (frequency_hz, amplitude_v, gps_utc_s)
    readings for one unit, safe to append() from one core while flush()
    runs concurrently on another.

    append() during normal operation; flush() attempts to POST everything
    currently buffered. A failed POST (post_fn returns falsy or raises)
    puts the un-sent batch back for the next flush() call -- same
    "retry, don't silently drop" philosophy as adc_stream_gps.py's ADC
    ring buffer (which drops and *counts* an overflow rather than losing
    data with no trace). Only a genuinely full buffer drops anything here,
    and every drop is counted in dropped_count.

    Storage is two preallocated _RingStorage instances, swapped by
    flipping self._active -- the same swap-not-copy trick the previous
    plain-list implementation used (to_send = self._buf; self._buf = []),
    just with both sides preallocated so neither append() nor flush()
    allocates a new list/array. A single ring buffer can't do this safely:
    resetting its own head/count to "empty" so append() could reuse freed
    slots would let a fast append() overwrite data still being read out
    for the slow, network-bound POST still in flight. With two buffers,
    the moment flush() flips self._active, every new append() goes to the
    *other*, already-empty one -- the buffer being sent is never touched
    again until flush() itself either resets it (success) or merges its
    leftover content back in (failure), both after the POST has returned.

    On failure, the un-sent batch is merged back in chronological order
    (failed batch first/older, then whatever append() added during the
    POST) with max_readings re-applied, dropping the oldest of the *failed*
    batch first if the combined total overflows -- same policy as before.
    The merge works by extending the buffer that received in-flight
    appends *backward* into its own spare capacity (a ring buffer can
    prepend by moving head back, no data movement) rather than needing a
    third buffer.

    flush()'s one remaining allocation: building the wire payload needs a
    real list of dicts. An earlier version of this file preallocated a
    max_readings-sized pool of dicts and mutated them in place -- removed
    after it turned out to be a genuine bug, not just an optimization
    that happened to be safe: any post_fn (or anything downstream of it)
    that retains a *reference* to payload["readings"] past its own call
    -- rather than fully, synchronously consuming it, e.g. a test
    collecting sent readings for later inspection, exactly what
    tests/test_wifi_ingest.py's own concurrency test does -- would later
    see those same dict objects mutated by a *subsequent* flush(),
    corrupting whatever it thought it had captured. Reproduced directly
    (see commit history): with the shared pool, a 4-thread concurrent
    append/flush stress test lost entire threads' worth of readings to
    exactly this aliasing, replaced by duplicates of later data -- not a
    locking bug (the ring-buffer locking was and is correct), and not a
    timing flake either (it reproduced deterministically once a
    downstream consumer retained references, and vanished once it copied
    values out instead). The real client's own post_fn (urequests.post(
    url, json=payload), which serializes to a request body synchronously
    within that one call and never retains payload afterward) likely
    never hit this in practice, but "likely fine given how the one
    current caller happens to behave" isn't a safe contract for a
    reusable buffer.

    Fixed by building a fresh, small list of dicts every flush() instead
    of reusing one: safe specifically *because* max_readings_per_post
    bounds it to a small number (60 by default -- see
    wifi_unit_client.py's MAX_READINGS_PER_POST) well below the size
    where MicroPython's list-growth doubling becomes a fragmentation risk
    (the crash pattern this whole effort exists to avoid needed
    something in the hundreds of elements, not a few dozen). This
    tradeoff only holds if a caller actually passes a small
    max_readings_per_post; the default (None -> max_readings, i.e.
    uncapped, for backward compatibility with callers that don't set
    one) reintroduces that original risk for that specific call pattern
    -- documented here rather than hidden, since the real deployment
    always passes an explicit small cap and this is the one path that
    doesn't.

    len(buffer) is read unlocked (see __len__) -- a plain count read can't
    observe a torn/corrupted state, only a slightly stale value, and
    that's an acceptable tradeoff for a status readout, not something
    worth a lock acquisition for.

    max_readings default of 600 (~10 minutes at one reading/sec) is a
    starting point, not a measured ceiling -- see the WiFi client's design
    notes on Pico 2 W heap headroom under WiFi+TLS; retune once
    gc.mem_free() has actually been checked on real hardware.

    max_readings_per_post (default: max_readings, i.e. uncapped -- the
    caller passes a real value, see wifi_unit_client.py's
    MAX_READINGS_PER_POST) bounds how many readings a single flush()
    call ever sends: at most the *oldest* max_readings_per_post of
    whatever's currently buffered. A fully-buffered flush is otherwise a
    single JSON body proportional to max_readings (48,635 bytes measured
    at 600) -- one large contiguous allocation nobody had reason to bound
    before the buffer itself could actually reach that size. Anything
    left over after a capped send stays buffered for the next scheduled
    flush() at the normal cadence -- this never triggers an extra POST of
    its own, since an unscheduled extra network call would itself be one
    more multi-second blocking stall.
    """

    def __init__(self, unit_id, post_fn, max_readings=600, max_readings_per_post=None, boot_id=None):
        self.unit_id = unit_id
        # boot_id=None -> the legacy (v1) wire format, exactly as before. A
        # boot_id (see make_boot_id) switches to v2: every reading carries a
        # per-boot sequence number and the integer GPS time, and the batch
        # carries the boot_id, so the server can drop retried duplicates exactly
        # and never confuses readings from two different power-ups.
        self.boot_id = boot_id
        self._next_seq = 0
        self._post_fn = post_fn
        self._max_readings = max_readings
        self._max_readings_per_post = (
            max_readings if max_readings_per_post is None else max_readings_per_post
        )
        self._lock = _thread.allocate_lock()
        self.dropped_count = 0

        self._storages = [_RingStorage(max_readings), _RingStorage(max_readings)]
        self._active = 0

    def __len__(self):
        return self._storages[self._active].count  # unlocked -- see class docstring

    def append(self, frequency_hz, amplitude_v, gps_utc_s=None, gps=None):
        """gps: optional (days_since_1970, second_of_day, microsecond) ints from
        PPSTimeSync.ticks_to_gps(); only sent when a boot_id is set. seq is
        assigned here, for EVERY reading including ones later dropped from a
        full buffer, so a gap in the sequence the server sees is exactly the
        readings this device lost."""
        self._lock.acquire()
        try:
            buf = self._storages[self._active]
            idx = (buf.head + buf.count) % self._max_readings
            if buf.count >= self._max_readings:
                buf.head = (buf.head + 1) % self._max_readings  # drop oldest
                self.dropped_count += 1
            else:
                buf.count += 1
            buf.seq[idx] = self._next_seq
            self._next_seq += 1
            if gps is None:
                buf.has_gpsi[idx] = 0
            else:
                buf.gday[idx] = gps[0]
                buf.gsec[idx] = gps[1]
                buf.gus[idx] = gps[2]
                buf.has_gpsi[idx] = 1
            buf.freq[idx] = frequency_hz
            if amplitude_v is None:
                buf.has_amp[idx] = 0
            else:
                buf.amp[idx] = amplitude_v
                buf.has_amp[idx] = 1
            if gps_utc_s is None:
                buf.has_gps[idx] = 0
            else:
                buf.gps[idx] = gps_utc_s
                buf.has_gps[idx] = 1
        finally:
            self._lock.release()

    def _build_payload(self, buf, n):
        # A fresh list of n dicts every call, not a reused/mutated pool --
        # see the class docstring for why reuse was a real aliasing bug,
        # not just an optimization. Safe as a fresh allocation specifically
        # because n is bounded small by max_readings_per_post; this is not
        # safe to call with a large n (see the docstring's caveat about the
        # uncapped default).
        readings = []
        v2 = self.boot_id is not None
        for i in range(n):
            idx = (buf.head + i) % self._max_readings
            r = {
                "frequency_hz": buf.freq[idx],
                "amplitude_v": buf.amp[idx] if buf.has_amp[idx] else None,
                # kept in v2 too: an older server (rollback) still gets a usable
                # (float32) time; a v2 server prefers "gps" below
                "gps_utc_s": buf.gps[idx] if buf.has_gps[idx] else None,
            }
            if v2:
                r["seq"] = buf.seq[idx]
                if buf.has_gpsi[idx]:
                    r["gps"] = [buf.gday[idx], buf.gsec[idx], buf.gus[idx]]
            readings.append(r)
        payload = {
            "unit_id": self.unit_id,
            "readings": readings,
        }
        if v2:
            payload["boot_id"] = self.boot_id
        return payload

    def _merge_failed_batch(self, send_buf, current):
        """Prepend send_buf's un-sent readings (chronologically older) in
        front of current's readings (arrived during the POST, so
        chronologically newer) by extending current backward into its own
        free capacity. Drops send_buf's own oldest first if the combined
        total would exceed max_readings -- walking backward from
        send_buf's newest item and stopping after the number of slots
        actually available naturally keeps the newest items and skips the
        oldest, without needing to compute which indices to skip
        separately.
        """
        free_slots = self._max_readings - current.count
        n_to_prepend = send_buf.count
        if n_to_prepend > free_slots:
            self.dropped_count += n_to_prepend - free_slots
            n_to_prepend = free_slots

        for i in range(n_to_prepend):
            src_idx = (send_buf.head + send_buf.count - 1 - i) % self._max_readings
            current.head = (current.head - 1) % self._max_readings
            current.freq[current.head] = send_buf.freq[src_idx]
            current.amp[current.head] = send_buf.amp[src_idx]
            current.has_amp[current.head] = send_buf.has_amp[src_idx]
            current.gps[current.head] = send_buf.gps[src_idx]
            current.has_gps[current.head] = send_buf.has_gps[src_idx]
            current.seq[current.head] = send_buf.seq[src_idx]
            current.gday[current.head] = send_buf.gday[src_idx]
            current.gsec[current.head] = send_buf.gsec[src_idx]
            current.gus[current.head] = send_buf.gus[src_idx]
            current.has_gpsi[current.head] = send_buf.has_gpsi[src_idx]
        current.count += n_to_prepend

        send_buf.head = 0
        send_buf.count = 0

    def flush(self):
        """Attempt to send buffered readings, at most
        max_readings_per_post of them (the oldest first) in this one call.

        Returns True if the batch was accepted (post_fn returned truthy).
        Returns False if the POST failed for any reason, including post_fn
        raising -- the un-sent batch is merged back into the buffer (see
        class docstring) to retry on the next call. A no-op (returns True)
        when the buffer is empty, so callers can call this unconditionally
        on a timer without checking len() first.

        Whether the POST succeeded or not, anything past the first
        max_readings_per_post readings is left for a later flush() call --
        never sent by looping again here. That reuses the exact same
        merge-back-in-chronological-order logic either way (a held-back
        remainder after a successful partial send and an un-sent batch
        after a failure are both "readings still waiting to go out,
        oldest first, in front of whatever arrived since").
        """
        self._lock.acquire()
        try:
            send_buf = self._storages[self._active]
            if send_buf.count == 0:
                return True
            self._active = 1 - self._active  # new append()s go to the other, empty buffer
        finally:
            self._lock.release()

        n_to_send = min(send_buf.count, self._max_readings_per_post)
        payload = self._build_payload(send_buf, n_to_send)
        try:
            ok = self._post_fn(payload)
        except Exception:
            ok = False

        self._lock.acquire()
        try:
            if ok:
                # advance past exactly what was sent -- any remainder
                # (buffered readings beyond max_readings_per_post) stays
                # in send_buf, to be merged back below same as a failure
                send_buf.head = (send_buf.head + n_to_send) % self._max_readings
                send_buf.count -= n_to_send
            if send_buf.count > 0:
                current = self._storages[self._active]
                self._merge_failed_batch(send_buf, current)
        finally:
            self._lock.release()
        return ok


def make_boot_id(urandom=None, fallback=None):
    """A fresh 16-hex-character id for THIS power-up, sent with every batch.

    The server dedupes on (unit_id, boot_id, seq), so ids from two boots must
    not collide: 64 random bits. ``urandom`` defaults to os.urandom (MicroPython's
    is backed by the RP2350's hardware RNG); on a build without it ``fallback``
    (a callable returning 8 bytes, e.g. mixing machine.unique_id() and
    time.ticks_us()) is used. Plain string formatting only -- no bytes.hex(),
    which older MicroPython builds lack."""
    if urandom is None:
        import os
        urandom = getattr(os, "urandom", None)
    raw = None
    if urandom is not None:
        try:
            raw = urandom(8)
        except (OSError, NotImplementedError):
            raw = None
    if raw is None:
        if fallback is None:
            raise RuntimeError("no random source for boot_id")
        raw = fallback()
    return "".join("{:02x}".format(b) for b in raw)
