"""Buffers per-reading summaries and ships them to TREMOR's /api/ingest
endpoint in batches, retrying on failure instead of dropping data.

Deliberately isolated from network.WLAN/urequests (see wifi_unit_client.py
for that glue) so this buffering/eviction logic -- pure Python, zero
imports beyond the stdlib -- runs and is testable under desktop CPython,
same portability reasoning as freq_estimator.py. The actual HTTP POST is
injected as a callable rather than imported directly, so tests can swap in
a fake one without needing MicroPython's network stack.

Genuinely concurrent as of the wifi_unit_client.py dual-core redesign:
append() runs on core 0 (from the ADC-reduction path) while flush() runs
on core 1 (from the WiFi thread), so IngestBuffer is no longer
single-threaded and needs real locking -- see the class docstring below.
_thread is used rather than threading specifically because it's the one
threading-flavoured module both CPython and MicroPython provide under the
same name with a compatible allocate_lock() API, so this file needs no
platform branching to stay host-testable.
"""

import _thread


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

    Locking is deliberately minimal-hold-time: the lock protects only the
    list swap in append()/flush(), never the network call itself -- flush()
    swaps self._buf for an empty list under the lock, releases it, and only
    then builds the payload and calls post_fn. Holding the lock across the
    POST would serialize append() (core 0, real-time-ish) against a
    multi-second network call (core 1), which is exactly the stall this
    split was meant to avoid. On failure, the un-sent batch is merged back
    in chronological order (failed batch first, then whatever append()
    added during the POST) under a second short lock, with max_readings
    re-applied to the merged result.

    len(buffer) is read unlocked (see __len__) -- a plain list length read
    can't observe a torn/corrupted state, only a slightly stale count, and
    that's an acceptable tradeoff for a status readout, not something worth
    a lock acquisition for.

    max_readings default of 600 (~10 minutes at one reading/sec) is a
    starting point, not a measured ceiling -- see the WiFi client's design
    notes on Pico 2 W heap headroom under WiFi+TLS; retune once
    gc.mem_free() has actually been checked on real hardware.
    """

    def __init__(self, unit_id, post_fn, max_readings=600):
        self.unit_id = unit_id
        self._post_fn = post_fn
        self._max_readings = max_readings
        self._buf = []  # list of (frequency_hz, amplitude_v, gps_utc_s) tuples
        self._lock = _thread.allocate_lock()
        self.dropped_count = 0

    def __len__(self):
        return len(self._buf)  # unlocked -- see class docstring

    def append(self, frequency_hz, amplitude_v, gps_utc_s=None):
        self._lock.acquire()
        try:
            if len(self._buf) >= self._max_readings:
                self._buf.pop(0)  # drop oldest -- keep the buffer bounded, favor recent data
                self.dropped_count += 1
            self._buf.append((frequency_hz, amplitude_v, gps_utc_s))
        finally:
            self._lock.release()

    def flush(self):
        """Attempt to send everything currently buffered.

        Returns True if the batch was accepted (post_fn returned truthy).
        Returns False if the POST failed for any reason, including post_fn
        raising -- the un-sent batch is merged back into the buffer (see
        class docstring) to retry on the next call. A no-op (returns True)
        when the buffer is empty, so callers can call this unconditionally
        on a timer without checking len() first.
        """
        self._lock.acquire()
        try:
            to_send = self._buf
            self._buf = []
        finally:
            self._lock.release()

        if not to_send:
            return True

        payload = {
            "unit_id": self.unit_id,
            "readings": [
                {"frequency_hz": f, "amplitude_v": a, "gps_utc_s": g}
                for f, a, g in to_send
            ],
        }
        try:
            ok = self._post_fn(payload)
        except Exception:
            ok = False

        if not ok:
            self._lock.acquire()
            try:
                merged = to_send + self._buf  # failed batch first (older), then anything appended during the POST
                overflow = len(merged) - self._max_readings
                if overflow > 0:
                    self.dropped_count += overflow
                    merged = merged[overflow:]  # drop oldest -- same bound/policy as append()
                self._buf = merged
            finally:
                self._lock.release()
        return ok
