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
    real list of exactly `count` reading-dicts (a preallocated pool of
    max_readings dicts is mutated in place, then sliced to `[:count]`) --
    a single, exactly-sized allocation (MicroPython's list slicing is
    list_new(n) + a direct copy, not incremental append-growth -- verified
    against py/objlist.c earlier in this effort), not the doubling-growth
    pattern that caused every crash so far. It's the one allocation this
    file couldn't eliminate without either an unverified assumption that
    ujson.dumps can serialize a custom non-list iterable, or building the
    JSON string by hand here too (which would duplicate work http_post.py
    already does and expand this file's job well beyond buffering).

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

    def __init__(self, unit_id, post_fn, max_readings=600, max_readings_per_post=None):
        self.unit_id = unit_id
        self._post_fn = post_fn
        self._max_readings = max_readings
        self._max_readings_per_post = (
            max_readings if max_readings_per_post is None else max_readings_per_post
        )
        self._lock = _thread.allocate_lock()
        self.dropped_count = 0

        self._storages = [_RingStorage(max_readings), _RingStorage(max_readings)]
        self._active = 0

        # Reused across every flush() -- mutated in place, then sliced to
        # the actual count (see class docstring's note on that one
        # remaining allocation).
        self._reading_dicts = [
            {"frequency_hz": 0.0, "amplitude_v": None, "gps_utc_s": None}
            for _ in range(max_readings)
        ]

    def __len__(self):
        return self._storages[self._active].count  # unlocked -- see class docstring

    def append(self, frequency_hz, amplitude_v, gps_utc_s=None):
        self._lock.acquire()
        try:
            buf = self._storages[self._active]
            idx = (buf.head + buf.count) % self._max_readings
            if buf.count >= self._max_readings:
                buf.head = (buf.head + 1) % self._max_readings  # drop oldest
                self.dropped_count += 1
            else:
                buf.count += 1
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
        for i in range(n):
            idx = (buf.head + i) % self._max_readings
            d = self._reading_dicts[i]
            d["frequency_hz"] = buf.freq[idx]
            d["amplitude_v"] = buf.amp[idx] if buf.has_amp[idx] else None
            d["gps_utc_s"] = buf.gps[idx] if buf.has_gps[idx] else None
        return {
            "unit_id": self.unit_id,
            "readings": self._reading_dicts[:n],
        }

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
