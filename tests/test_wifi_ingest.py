from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifi_ingest import IngestBuffer  # noqa: E402


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
            {"frequency_hz": 49.98, "amplitude_v": 0.72, "gps_utc_s": 41023.5},
            {"frequency_hz": 50.01, "amplitude_v": 0.73, "gps_utc_s": 41024.5},
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
    assert [f for f, _, _ in buf._buf] == [52.0, 53.0, 54.0]


def test_gps_utc_s_defaults_to_none_before_pps_sync():
    sent = []
    buf = IngestBuffer("unit-1", post_fn=lambda payload: sent.append(payload) or True)
    buf.append(49.98, 0.72)  # no gps_utc_s -- device hasn't synced yet

    buf.flush()
    assert sent[0]["readings"][0]["gps_utc_s"] is None


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
    assert [f for f, _, _ in buf._buf] == [50.0, 52.0]


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
    assert [f for f, _, _ in buf._buf] == [2.0, 101.0, 102.0]


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
    assert sorted(r["frequency_hz"] for r in sent_readings) == [float(i) for i in range(total_expected)]
