from __future__ import annotations

import random
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifi_ingest import IngestBuffer  # noqa: E402


def _f32(x):
    """Round-trip x through IEEE-754 single precision -- the same lossy
    truncation array.array('f', ...) applies on assignment. IngestBuffer's
    storage is now array.array(FLOAT_TYPECODE, ...) (FLOAT_TYPECODE='f'),
    so a value written via append() and read back via flush()'s payload
    is no longer bit-identical to what went in -- it's rounded to float32
    precision, same as it genuinely will be on the real device (confirmed
    single-precision via check_float_precision.py in wifi_unit_client.py).
    Expected values below are put through this same round-trip so the
    tests check "is this the right value at float32 precision", not "is
    this the exact double" -- the latter is no longer true by design, not
    a bug.
    """
    if x is None:
        return None
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _flush_and_capture(buf, ok=True):
    """Flush once, returning the sent payload's readings list (or None if
    the buffer was empty). Reads buffer contents through the public
    flush()/post_fn contract rather than any private attribute -- there
    is no `_buf` to inspect directly any more (see wifi_ingest.py's
    docstring for why: fixed-capacity storage, not a growing list)."""
    captured = []

    def _post_fn(payload):
        captured.append(payload)
        return ok

    real_post_fn = buf._post_fn
    buf._post_fn = _post_fn
    try:
        buf.flush()
    finally:
        buf._post_fn = real_post_fn
    return captured[0]["readings"] if captured else None


def test_flush_on_empty_buffer_is_a_noop_success():
    buf = IngestBuffer("unit-1", post_fn=lambda payload: True)
    assert buf.flush() is True
    assert len(buf) == 0


def test_successful_flush_clears_the_buffer_and_sends_expected_shape():
    sent = []
    buf = IngestBuffer("unit-1", post_fn=lambda payload: sent.append(payload) or True)
    buf.append(49.98, 0.72, 41023.5)
    buf.append(50.01, 0.73, 41024.5)

    assert buf.flush() is True
    assert len(buf) == 0
    assert sent == [{
        "unit_id": "unit-1",
        "readings": [
            {"frequency_hz": _f32(49.98), "amplitude_v": _f32(0.72), "gps_utc_s": _f32(41023.5)},
            {"frequency_hz": _f32(50.01), "amplitude_v": _f32(0.73), "gps_utc_s": _f32(41024.5)},
        ],
    }]


def test_failed_flush_leaves_buffer_intact_for_retry():
    buf = IngestBuffer("unit-1", post_fn=lambda payload: False)
    buf.append(49.98, 0.72, None)

    assert buf.flush() is False
    assert len(buf) == 1  # still there -- next flush() will retry it


def test_post_fn_raising_is_treated_as_a_failed_flush():
    def _boom(payload):
        raise OSError("WiFi dropped mid-request")

    buf = IngestBuffer("unit-1", post_fn=_boom)
    buf.append(49.98, 0.72, None)

    assert buf.flush() is False
    assert len(buf) == 1


def test_overflow_drops_oldest_and_counts_it():
    buf = IngestBuffer("unit-1", post_fn=lambda payload: True, max_readings=3)
    for i in range(5):
        buf.append(50.0 + i, 0.7, None)

    assert len(buf) == 3
    assert buf.dropped_count == 2
    # oldest two (50.0, 51.0) were evicted -- only the most recent three remain
    readings = _flush_and_capture(buf)
    assert [r["frequency_hz"] for r in readings] == [_f32(52.0), _f32(53.0), _f32(54.0)]


def test_gps_utc_s_defaults_to_none_before_pps_sync():
    sent = []
    buf = IngestBuffer("unit-1", post_fn=lambda payload: sent.append(payload) or True)
    buf.append(49.98, 0.72)  # no gps_utc_s -- device hasn't synced yet

    buf.flush()
    assert sent[0]["readings"][0]["gps_utc_s"] is None


def test_amplitude_v_none_round_trips_as_none_too():
    # amplitude_v is never actually None from the real client (summarize_chunk
    # always returns a real float), but the public append() signature never
    # enforced that, and the fixed-capacity storage handles it via the same
    # has_amp flag scheme as gps_utc_s -- worth confirming directly.
    sent = []
    buf = IngestBuffer("unit-1", post_fn=lambda payload: sent.append(payload) or True)
    buf.append(49.98, None, None)

    buf.flush()
    assert sent[0]["readings"][0]["amplitude_v"] is None


def test_failed_flush_merges_batch_appended_during_the_post():
    # Simulate a reading arriving (via append(), as core 0 would) WHILE the
    # network call is in flight on the other core -- post_fn here plays the
    # role of that concurrent append before reporting failure.
    buf = IngestBuffer("unit-1", post_fn=None, max_readings=10)

    def _post_fn(payload):
        buf.append(52.0, 0.75, None)  # arrives mid-POST
        return False

    buf._post_fn = _post_fn
    buf.append(50.0, 0.70, None)  # already buffered before flush() starts

    assert buf.flush() is False
    # failed batch (50.0) must come before what arrived during the POST (52.0)
    readings = _flush_and_capture(buf)
    assert [r["frequency_hz"] for r in readings] == [_f32(50.0), _f32(52.0)]


def test_failed_flush_merge_respects_max_readings_bound():
    buf = IngestBuffer("unit-1", post_fn=None, max_readings=3)

    def _post_fn(payload):
        # two readings arrive during the POST -- combined with the 2 being
        # retried, that's 4 total against a bound of 3
        buf.append(101.0, 0.1, None)
        buf.append(102.0, 0.1, None)
        return False

    buf._post_fn = _post_fn
    buf.append(1.0, 0.1, None)
    buf.append(2.0, 0.1, None)

    assert buf.flush() is False
    assert len(buf) == 3
    assert buf.dropped_count == 1
    # oldest of the combined sequence (1.0) is the one dropped
    readings = _flush_and_capture(buf)
    assert [r["frequency_hz"] for r in readings] == [_f32(2.0), _f32(101.0), _f32(102.0)]


def test_wraparound_across_many_flush_cycles():
    # Ring buffer indices must wrap correctly past the array's physical
    # end many times over, not just once -- append+flush repeatedly, well
    # past max_readings*3 total readings, through a small capacity so
    # wraparound happens often.
    buf = IngestBuffer("unit-1", post_fn=lambda payload: True, max_readings=4)
    sent_order = []
    for cycle in range(20):
        for i in range(3):  # 3 < capacity=4, so each cycle succeeds without eviction
            buf.append(float(cycle * 3 + i), 0.7, None)
        readings = _flush_and_capture(buf)
        sent_order.extend(r["frequency_hz"] for r in readings)

    expected = [_f32(float(i)) for i in range(60)]
    assert sent_order == expected
    assert buf.dropped_count == 0


def test_wraparound_survives_a_failed_cycle_mid_stream():
    buf = IngestBuffer("unit-1", post_fn=lambda payload: True, max_readings=4)
    for i in range(3):
        buf.append(float(i), 0.7, None)
    _flush_and_capture(buf)  # 0,1,2 sent

    for i in range(3, 6):
        buf.append(float(i), 0.7, None)
    assert _flush_and_capture(buf, ok=False) == [
        {"frequency_hz": _f32(float(i)), "amplitude_v": _f32(0.7), "gps_utc_s": None}
        for i in (3, 4, 5)
    ]  # sent but failed -- still buffered

    buf.append(6.0, 0.7, None)  # arrives before the retry
    readings = _flush_and_capture(buf)
    assert [r["frequency_hz"] for r in readings] == [_f32(float(i)) for i in (3, 4, 5, 6)]


def test_max_readings_per_post_sends_only_the_oldest_slice():
    buf = IngestBuffer("unit-1", post_fn=None, max_readings=200, max_readings_per_post=3)
    for i in range(10):
        buf.append(float(i), 0.7, None)

    readings = _flush_and_capture(buf)
    assert [r["frequency_hz"] for r in readings] == [_f32(f) for f in (0.0, 1.0, 2.0)]
    assert len(buf) == 7  # the other 7 stayed buffered


def test_max_readings_per_post_remainder_drains_over_later_cycles_no_extra_post():
    sent_batches = []

    def _post_fn(payload):
        sent_batches.append([r["frequency_hz"] for r in payload["readings"]])
        return True

    buf = IngestBuffer("unit-1", post_fn=_post_fn, max_readings=200, max_readings_per_post=3)
    for i in range(10):
        buf.append(float(i), 0.7, None)

    # Each flush() call here represents one scheduled cycle -- exactly one
    # POST per call, never an internal retry-until-drained loop (that
    # would be an extra, unscheduled POST, exactly what capping is meant
    # to avoid).
    buf.flush()
    assert len(sent_batches) == 1
    assert len(buf) == 7

    while len(buf) > 0:
        buf.flush()

    assert len(sent_batches) == 4  # 1 + 3 more scheduled-cadence calls to drain 7 at cap 3
    assert sent_batches == [
        [_f32(float(i)) for i in (0.0, 1.0, 2.0)],
        [_f32(float(i)) for i in (3.0, 4.0, 5.0)],
        [_f32(float(i)) for i in (6.0, 7.0, 8.0)],
        [_f32(9.0)],
    ]


def test_max_readings_per_post_remainder_merges_correctly_with_new_appends():
    buf = IngestBuffer("unit-1", post_fn=None, max_readings=200, max_readings_per_post=2)

    def _post_fn(payload):
        buf.append(99.0, 0.7, None)  # arrives mid-POST, after the held-back remainder chronologically
        return True

    buf._post_fn = _post_fn
    for i in range(5):
        buf.append(float(i), 0.7, None)  # 0,1,2,3,4 buffered

    buf.flush()  # sends 0,1 (cap=2); 2,3,4 held back; 99 arrives mid-POST
    assert len(buf) == 4  # 2,3,4 (held back) + 99 (arrived during the POST)

    sent_order = []
    while len(buf) > 0:
        readings = _flush_and_capture(buf)
        sent_order.extend(r["frequency_hz"] for r in readings)
    assert sent_order == [_f32(f) for f in (2.0, 3.0, 4.0, 99.0)]


def test_max_readings_per_post_defaults_to_uncapped():
    # No max_readings_per_post given -- must reproduce the old, uncapped
    # behaviour exactly (all the tests above this point in the file rely
    # on this default).
    buf = IngestBuffer("unit-1", post_fn=None, max_readings=200)
    for i in range(150):
        buf.append(float(i), 0.7, None)

    readings = _flush_and_capture(buf)
    assert len(readings) == 150
    assert len(buf) == 0


def test_concurrent_append_and_flush_lose_nothing():
    # Real OS threads (CPython's _thread/threading genuinely preempts, even
    # under the GIL) -- this is the actual concurrency IngestBuffer now
    # needs to survive: append() from many "core 0" threads while flush()
    # runs repeatedly from a "core 1" thread, same shapes as
    # wifi_unit_client.py's dual-core split.
    sent_readings = []
    sent_lock = threading.Lock()

    def _post_fn(payload):
        with sent_lock:
            sent_readings.extend(payload["readings"])
        return True

    buf = IngestBuffer("unit-1", post_fn=_post_fn, max_readings=100_000)
    n_per_thread = 500
    n_appender_threads = 4
    stop = threading.Event()

    def _appender(offset):
        for i in range(n_per_thread):
            buf.append(float(offset + i), 0.7, None)

    def _flusher():
        while not stop.is_set():
            buf.flush()
            time.sleep(0.001)

    flusher_thread = threading.Thread(target=_flusher)
    flusher_thread.start()

    appender_threads = [
        threading.Thread(target=_appender, args=(t * n_per_thread,))
        for t in range(n_appender_threads)
    ]
    for t in appender_threads:
        t.start()
    for t in appender_threads:
        t.join()

    # drain whatever's left after the appenders finish
    for _ in range(50):
        buf.flush()
        if len(buf) == 0:
            break
        time.sleep(0.001)

    stop.set()
    flusher_thread.join()

    total_expected = n_per_thread * n_appender_threads
    assert len(sent_readings) == total_expected
    assert buf.dropped_count == 0
    # every value 0..total_expected-1 arrived exactly once -- no loss, no duplication
    assert sorted(r["frequency_hz"] for r in sent_readings) == [
        _f32(float(i)) for i in range(total_expected)
    ]


# ---------------------------------------------------------------------------
# Behavioural equivalence against the pre-fix implementation, on random
# sequences of operations. _OldIngestBuffer below is a verbatim copy of the
# plain-list implementation this file replaced (see git history, the commit
# immediately before this one) -- kept only for this comparison.
# ---------------------------------------------------------------------------

import _thread as _thread_mod  # noqa: E402


class _OldIngestBuffer:
    """Verbatim pre-fix implementation (growing list, unbounded .append())
    -- see this file's git history for the original. Kept only so the new
    fixed-capacity implementation can be proven equivalent against it."""

    def __init__(self, unit_id, post_fn, max_readings=600):
        self.unit_id = unit_id
        self._post_fn = post_fn
        self._max_readings = max_readings
        self._buf = []
        self._lock = _thread_mod.allocate_lock()
        self.dropped_count = 0

    def __len__(self):
        return len(self._buf)

    def append(self, frequency_hz, amplitude_v, gps_utc_s=None):
        self._lock.acquire()
        try:
            if len(self._buf) >= self._max_readings:
                self._buf.pop(0)
                self.dropped_count += 1
            self._buf.append((frequency_hz, amplitude_v, gps_utc_s))
        finally:
            self._lock.release()

    def flush(self):
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
                merged = to_send + self._buf
                overflow = len(merged) - self._max_readings
                if overflow > 0:
                    self.dropped_count += overflow
                    merged = merged[overflow:]
                self._buf = merged
            finally:
                self._lock.release()
        return ok


def test_behavioural_equivalence_against_old_implementation_on_random_sequences():
    # The whole script of operations (appends, flushes with predetermined
    # outcomes, mid-POST appends) is precomputed once per seed, then
    # replayed identically against both implementations -- no shared
    # mutable RNG state between the two runs, which a live/interleaved
    # approach would need to fight to keep synchronized. 150 operations,
    # 20 seeds -- enough to exercise repeated wraparound, eviction, and
    # failed-flush merging (including merges that themselves overflow)
    # many times over.
    for seed in range(20):
        rng = random.Random(seed)
        max_readings = rng.choice([3, 5, 10])

        script = []
        next_value = 0
        for _ in range(150):
            op = rng.choice(["append", "append", "append", "flush"])
            if op == "append":
                freq = float(next_value)
                next_value += 1
                amp = rng.uniform(0, 1)
                gps = rng.choice([None, rng.uniform(0, 86400)])
                script.append(("append", freq, amp, gps))
            else:
                flush_ok = rng.random() < 0.75
                mid_post_append = None
                if rng.random() < 0.3:
                    mid_post_append = (rng.uniform(-1000, 1000), rng.uniform(0, 1), None)
                script.append(("flush", flush_ok, mid_post_append))

        def _run(buf):
            log = []
            for step in script:
                if step[0] == "append":
                    _, freq, amp, gps = step
                    buf.append(freq, amp, gps)
                else:
                    _, flush_ok, mid_post_append = step

                    def _post_fn(payload, _ok=flush_ok, _mid=mid_post_append):
                        if _mid is not None:
                            buf.append(*_mid)
                        return _ok

                    buf._post_fn = _post_fn
                    result = buf.flush()
                    log.append((result, len(buf), buf.dropped_count))
            return log

        old = _OldIngestBuffer("unit-1", post_fn=None, max_readings=max_readings)
        new = IngestBuffer("unit-1", post_fn=None, max_readings=max_readings)
        old_log = _run(old)
        new_log = _run(new)

        assert old_log == new_log, (
            f"seed={seed}: (flush result, len, dropped_count) sequence diverged"
        )

        # Final drain: compare exact sent order/values, old at float32
        # precision (see _f32) since new's storage already rounds there.
        old_sent, new_sent = [], []
        while len(old) > 0:
            captured = []
            old._post_fn = lambda payload: captured.append(payload) or True
            old.flush()
            old_sent.extend(r["frequency_hz"] for r in captured[0]["readings"])
        while len(new) > 0:
            captured = []
            new._post_fn = lambda payload: captured.append(payload) or True
            new.flush()
            new_sent.extend(r["frequency_hz"] for r in captured[0]["readings"])

        assert [_f32(f) for f in old_sent] == new_sent, (
            f"seed={seed}: final drain order/values diverged"
        )
