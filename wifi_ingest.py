"""Buffers per-reading summaries and ships them to TREMOR's /api/ingest
endpoint in batches, retrying on failure instead of dropping data.

Deliberately isolated from network.WLAN/sockets (see wifi_unit_client.py
for that glue) so this buffering/eviction logic -- pure Python, only
struct/_thread beyond the stdlib -- runs and is testable under desktop
CPython, same portability reasoning as freq_estimator.py. The actual HTTP
POST is injected as a callable rather than imported directly, so tests can
swap in a fake one without needing MicroPython's network stack.

Storage (fw-resilience, 2026-10): ONE preallocated bytearray of fixed
16-byte records, used as a ring -- sized for ~60 minutes of readings
(wifi_unit_client.MAX_BUFFERED_READINGS) so an afternoon uplink outage no
longer overflows it. It is allocated once at boot and never resized: the
same "no growing containers" rule as the earlier array.array version, which
was itself the fix for a list-growth MemoryError crash loop (git history).
The earlier version kept TWO parallel-array rings (29 bytes/slot each, so
58 bytes per reading) and swapped between them so a slow POST's batch could
never be overwritten; that is no longer needed, because flush() serializes
its batch into the payload BEFORE the POST -- an append() that evicts an
in-flight record during the POST only overwrites a slot whose contents are
already copied out.

Record layout, little-endian ("<ffIHH"), chosen so every field read or
written is a MicroPython small int (< 2**30: no heap allocation per reading):
    f32  frequency_hz
    f32  amplitude_v                 (meaningful only if _F_AMP)
    u32  meta: microsecond (bits 0-19) | second_of_day bit 16 (bit 20) | _F_AMP | _F_SOD | _F_GPS
    u16  days_since_1970             } _F_GPS: the integer GPS time
    u16  second_of_day bits 0-15     }
         (_F_SOD instead: these last 4 bytes hold the legacy float32 seconds-of-day)
seq is NOT stored: drop-oldest only ever removes the oldest record, so the
buffered readings always carry consecutive seq numbers, and the oldest's
seq (_head_seq) is enough to number every one of them.
"""

import _thread
import struct

RECORD_BYTES = 16
_REC = "<ffIHH"
_US_MASK = 0xFFFFF                 # 20 bits: microsecond 0..999999
_SEC_HI = 1 << 20                  # second_of_day bit 16 (seconds run to 86399, or 86400 in a leap second)
_F_AMP = 1 << 21
_F_SOD = 1 << 22
_F_GPS = 1 << 23


def _pack_gps(gps):
    """(days, second_of_day, microsecond) -> (meta, days, second bits 0-15), or None if a
    field cannot be represented (it is then stored as "no time" and counted, never wrapped)."""
    days, sec, usec = gps
    if not (0 <= days <= 0xFFFF and 0 <= sec <= 0x1FFFF and 0 <= usec <= _US_MASK):
        return None
    return usec | (_SEC_HI if sec & 0x10000 else 0) | _F_GPS, days, sec & 0xFFFF


class IngestBuffer:
    """A bounded FIFO of pending (frequency_hz, amplitude_v, time) readings
    for one unit.

    append() during normal operation; flush() POSTs the oldest
    max_readings_per_post of them. A failed POST (post_fn returns falsy or
    raises) leaves every record in place for the next flush(): nothing is
    removed until the server has accepted it. Only a genuinely full buffer
    drops anything -- always the oldest reading -- and every drop is counted
    in dropped_count. A reading evicted while it is part of an in-flight
    POST is counted only if that POST then fails (if it succeeds, the reading
    was delivered).

    boot_id=None -> the legacy (v1) wire format. A boot_id switches to v2:
    every reading carries a per-boot sequence number and the integer GPS
    time, and the batch carries the boot_id, so the server can drop retried
    duplicates exactly. seq is assigned on append(), for EVERY reading
    including ones later dropped, so a gap the server sees is exactly what
    this device lost.

    send_legacy_float (v2 only): also send the old float32 "gps_utc_s",
    derived from the integer time, so a rolled-back pre-v2 server can still
    place the readings. Off by default -- ~24 bytes per reading.

    Thread-safe (one lock around every state change), though the deployed
    client is single-core. len() is read unlocked -- a status readout.
    """

    def __init__(self, unit_id, post_fn, max_readings=600, max_readings_per_post=None, boot_id=None,
                 send_legacy_float=False):
        self.unit_id = unit_id
        self.boot_id = boot_id
        self.send_legacy_float = send_legacy_float
        self._post_fn = post_fn
        self._cap = max_readings
        # None -> uncapped (everything buffered in one POST). The deployed client always passes a
        # small cap: the payload is a fresh list of dicts, and one JSON body per flush().
        self._per_post = max_readings if max_readings_per_post is None else max_readings_per_post
        self._lock = _thread.allocate_lock()
        self._buf = bytearray(max_readings * RECORD_BYTES)
        self._head = 0                   # slot index of the oldest record
        self._count = 0
        self._head_seq = 0               # seq of the oldest record; the next append gets _head_seq + _count
        self._inflight_end = None        # seq just past the batch currently being POSTed, else None
        self._inflight_evicted = 0
        self._f32 = bytearray(4)         # scratch for rounding a derived legacy float to float32
        self.dropped_count = 0
        self.bad_time_count = 0          # GPS triples that could not be stored (stored as "no time")

    def __len__(self):
        return self._count

    @property
    def capacity(self):
        return self._cap

    @property
    def storage_bytes(self):
        return len(self._buf)

    @property
    def next_seq(self):
        return self._head_seq + self._count

    def append(self, frequency_hz, amplitude_v, gps_utc_s=None, gps=None):
        """gps: optional (days_since_1970, second_of_day, microsecond) ints from
        PPSTimeSync.ticks_to_gps() -- used in v2. gps_utc_s: the legacy float32
        seconds-of-day -- stored only when there is no integer time (v1)."""
        meta = days = seclo = 0
        if gps is not None and self.boot_id is not None:
            packed = _pack_gps(gps)
            if packed is None:
                self.bad_time_count += 1
            else:
                meta, days, seclo = packed
        if amplitude_v is not None:
            meta |= _F_AMP
        self._lock.acquire()
        try:
            if self._count >= self._cap:
                if self._inflight_end is not None and self._head_seq < self._inflight_end:
                    self._inflight_evicted += 1      # counted only if its POST fails (see flush)
                else:
                    self.dropped_count += 1
                self._head = (self._head + 1) % self._cap
                self._head_seq += 1
                self._count -= 1
            off = ((self._head + self._count) % self._cap) * RECORD_BYTES
            buf = self._buf
            if not (meta & _F_GPS) and gps_utc_s is not None and (self.boot_id is None or self.send_legacy_float):
                struct.pack_into("<ffIf", buf, off, frequency_hz, amplitude_v or 0.0, meta | _F_SOD, gps_utc_s)
            else:
                struct.pack_into(_REC, buf, off, frequency_hz, amplitude_v or 0.0, meta, days, seclo)
            self._count += 1
        finally:
            self._lock.release()

    def _legacy_float(self, sec, usec):
        sod = sec + usec / 1e6
        struct.pack_into("<f", self._f32, 0, sod)
        return struct.unpack_from("<f", self._f32, 0)[0]

    def _build_payload(self, n, telemetry):
        # A fresh list of n dicts every call, never a reused pool: a post_fn that keeps a reference
        # to payload["readings"] must not see it change on a later flush() (a real aliasing bug in an
        # earlier version -- see git history). Small because n <= max_readings_per_post.
        readings = []
        v2 = self.boot_id is not None
        buf = self._buf
        for i in range(n):
            off = ((self._head + i) % self._cap) * RECORD_BYTES
            freq, amp, meta, days, seclo = struct.unpack_from(_REC, buf, off)
            sec = seclo | (0x10000 if meta & _SEC_HI else 0)
            r = {"frequency_hz": freq, "amplitude_v": amp if meta & _F_AMP else None}
            if not v2 or self.send_legacy_float:
                if meta & _F_SOD:
                    r["gps_utc_s"] = struct.unpack_from("<f", buf, off + 12)[0]
                elif meta & _F_GPS:
                    r["gps_utc_s"] = self._legacy_float(sec, meta & _US_MASK)
                else:
                    r["gps_utc_s"] = None
            if v2:
                r["seq"] = self._head_seq + i
                if meta & _F_GPS:
                    r["gps"] = [days, sec, meta & _US_MASK]
            readings.append(r)
        payload = {"unit_id": self.unit_id, "readings": readings}
        if v2:
            payload["boot_id"] = self.boot_id
        if telemetry:
            payload["telemetry"] = telemetry
        return payload

    def flush(self, telemetry=None):
        """POST the oldest (at most max_readings_per_post) buffered readings, once.

        Returns True if the batch was accepted (post_fn returned truthy) -- those
        readings are then removed. Returns False on any failure, including post_fn
        raising; nothing is removed, so the same readings (same seq, same order) go
        out again next time. A no-op returning True when the buffer is empty.

        Never loops to send a remainder: anything left waits for the caller's next
        scheduled flush() (see next_post_interval_s), since every POST is a blocking
        call competing with ADC sampling.

        telemetry: optional dict sent as the batch-level "telemetry" object.
        """
        self._lock.acquire()
        try:
            if self._count == 0:
                return True
            n = min(self._count, self._per_post)
            first = self._head_seq
            payload = self._build_payload(n, telemetry)
            self._inflight_end = first + n
            self._inflight_evicted = 0
        finally:
            self._lock.release()

        try:
            ok = self._post_fn(payload)
        except Exception:
            ok = False

        self._lock.acquire()
        try:
            if ok:
                k = first + n - self._head_seq        # sent records not already evicted meanwhile
                if k > 0:
                    self._head = (self._head + k) % self._cap
                    self._head_seq += k
                    self._count -= k
            else:
                self.dropped_count += self._inflight_evicted
            self._inflight_end = None
            self._inflight_evicted = 0
        finally:
            self._lock.release()
        return bool(ok)


def next_post_interval_s(prev_s, outcome, backlog, base_s, catchup_s, catchup_above, multiplier, cap_s):
    """Seconds until the next scheduled flush(), after one that ended in ``outcome``:

    "ok"      -- accepted. Back to the normal cadence (base_s), or the faster catch-up
                 cadence (catchup_s) while more than catchup_above readings are still
                 waiting: only ever right after a success, so a struggling link is never
                 hammered.
    "fail"    -- attempted and failed: normal exponential backoff, starting from base_s
                 even if the previous interval was the shorter catch-up one, capped at cap_s.
    "offline" -- not attempted (Wi-Fi down; reconnecting has its own timer): keep the
                 current interval, but never shorter than base_s.
    """
    if outcome == "ok":
        return catchup_s if backlog > catchup_above else base_s
    if outcome == "fail":
        return min(max(prev_s, base_s) * multiplier, cap_s)
    return max(prev_s, base_s)


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
