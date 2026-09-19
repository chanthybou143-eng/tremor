from __future__ import annotations

import sys
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
