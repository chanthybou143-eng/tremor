"""The 16-byte-record ring in wifi_ingest.IngestBuffer: wraparound, drop-oldest, batch order, implicit seq,
in-flight eviction accounting, record packing limits, and the catch-up / backoff schedule."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wifi_ingest  # noqa: E402
from wifi_ingest import RECORD_BYTES, IngestBuffer, next_post_interval_s  # noqa: E402

BOOT = "3bf8ae13fd84b5d0"


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def sink(results=None):
    """post_fn recording what crosses the wire; results: iterable of bools (default: always True)."""
    sent = []
    it = iter(results) if results is not None else None

    def post(payload):
        sent.append(json.loads(json.dumps(payload)))
        return True if it is None else next(it)
    return sent, post


def fill(buf, n, start=0):
    for i in range(start, start + n):
        buf.append(50.0 + i * 1e-3, 0.7, None, gps=(20733, i % 86400, (i * 7919) % 1_000_000))


def seqs(payload):
    return [r["seq"] for r in payload["readings"]]


# --- storage ------------------------------------------------------------------------------------

def test_one_record_is_16_bytes_and_60_minutes_is_one_57_6_kb_allocation():
    assert RECORD_BYTES == struct.calcsize(wifi_ingest._REC) == 16
    assert IngestBuffer("u", None, max_readings=3600).storage_bytes == 57_600


def test_every_packed_field_is_a_micropython_small_int():
    # ints >= 2**30 are heap objects on MicroPython: the meta word must stay below that for every value
    worst_meta = wifi_ingest._US_MASK | wifi_ingest._SEC_HI | wifi_ingest._F_AMP | wifi_ingest._F_SOD | wifi_ingest._F_GPS
    assert worst_meta < 2 ** 30


@pytest.mark.parametrize("gps", [(0, 0, 0), (20733, 86399, 999_999), (20733, 86400, 5), (65_535, 86_399, 999_999),
                                 (20733, 65_535, 1), (20733, 65_536, 1)])
def test_the_integer_gps_time_round_trips_exactly_at_its_limits(gps):
    sent, post = sink()
    buf = IngestBuffer("u", post, boot_id=BOOT)
    buf.append(49.95, 0.73, None, gps=gps)
    buf.flush()
    assert sent[0]["readings"][0]["gps"] == list(gps) and buf.bad_time_count == 0


@pytest.mark.parametrize("gps", [(-1, 0, 0), (65_536, 0, 0), (20733, -1, 0), (20733, 0, 1_048_576), (20733, 2 ** 17, 0)])
def test_an_unrepresentable_time_is_stored_as_no_time_and_counted_never_wrapped(gps):
    sent, post = sink()
    buf = IngestBuffer("u", post, boot_id=BOOT)
    buf.append(49.95, 0.73, None, gps=gps)
    buf.flush()
    assert "gps" not in sent[0]["readings"][0] and buf.bad_time_count == 1


def test_values_survive_at_float32_precision_and_none_amplitude_stays_none():
    sent, post = sink()
    buf = IngestBuffer("u", post, boot_id=BOOT)
    buf.append(49.987654321, 0.123456789, None, gps=(20733, 1, 2))
    buf.append(50.0, None, None, gps=None)
    buf.flush()
    a, b = sent[0]["readings"]
    assert a["frequency_hz"] == f32(49.987654321) and a["amplitude_v"] == f32(0.123456789)
    assert b["amplitude_v"] is None and "gps" not in b


def test_the_derived_legacy_float_is_the_float32_seconds_of_day():
    sent, post = sink()
    buf = IngestBuffer("u", post, boot_id=BOOT, send_legacy_float=True)
    buf.append(50.0, 0.7, 999.0, gps=(20733, 12_345, 678_901))         # a passed float is superseded
    buf.flush()
    assert sent[0]["readings"][0]["gps_utc_s"] == f32(12_345.678901)


# --- wraparound, batch order, implicit seq --------------------------------------------------------------

def test_many_wraparounds_deliver_every_reading_once_in_order_with_exact_times():
    sent, post = sink()
    buf = IngestBuffer("u", post, max_readings=7, max_readings_per_post=3, boot_id=BOOT)
    total = 0
    for cycle in range(200):
        fill(buf, 2, start=total)
        total += 2
        buf.flush()
    while len(buf):
        buf.flush()
    flat = [r for p in sent for r in p["readings"]]
    assert [r["seq"] for r in flat] == list(range(total))
    assert [r["gps"] for r in flat] == [[20733, i % 86400, (i * 7919) % 1_000_000] for i in range(total)]
    assert [r["frequency_hz"] for r in flat] == [f32(50.0 + i * 1e-3) for i in range(total)]
    assert buf.dropped_count == 0


def test_a_batch_is_the_oldest_readings_capped_at_max_readings_per_post():
    sent, post = sink()
    buf = IngestBuffer("u", post, max_readings=100, max_readings_per_post=60, boot_id=BOOT)
    fill(buf, 75)
    buf.flush()
    assert seqs(sent[0]) == list(range(60)) and len(buf) == 15
    buf.flush()
    assert seqs(sent[1]) == list(range(60, 75)) and len(buf) == 0


def test_drop_oldest_keeps_seq_implicit_and_contiguous_and_the_gap_is_exactly_what_was_lost():
    sent, post = sink()
    buf = IngestBuffer("u", post, max_readings=10, max_readings_per_post=4, boot_id=BOOT)
    fill(buf, 23)                                                            # 13 dropped
    assert buf.dropped_count == 13 and len(buf) == 10 and buf.next_seq == 23
    while len(buf):
        buf.flush()
    flat = [r for p in sent for r in p["readings"]]
    assert [r["seq"] for r in flat] == list(range(13, 23))
    assert [r["gps"][1] for r in flat] == list(range(13, 23))               # each seq still carries its own time
    fill(buf, 2, start=23)
    buf.flush()
    assert seqs(sent[-1]) == [23, 24]


def test_a_failed_post_removes_nothing_and_resends_the_identical_batch():
    sent, post = sink([False, False, True, True])
    buf = IngestBuffer("u", post, max_readings=100, max_readings_per_post=5, boot_id=BOOT)
    fill(buf, 8)
    assert buf.flush() is False and buf.flush() is False and len(buf) == 8
    assert buf.flush() is True and buf.flush() is True
    assert sent[0] == sent[1] == sent[2] and seqs(sent[3]) == [5, 6, 7]


# --- eviction while a POST is in flight -----------------------------------------------------------------

def _inflight(ok, arrive):
    buf = IngestBuffer("u", None, max_readings=5, max_readings_per_post=3, boot_id=BOOT)
    sent = []

    def post(payload):
        sent.append(seqs(payload))
        fill(buf, arrive, start=buf.next_seq)                             # readings arriving during the POST
        return ok
    buf._post_fn = post
    fill(buf, 5)
    buf.flush()
    return buf, sent


def test_evicting_part_of_a_successful_in_flight_batch_is_not_a_drop():
    buf, sent = _inflight(ok=True, arrive=2)                               # evicts seq 0, 1: both were delivered
    assert sent == [[0, 1, 2]] and buf.dropped_count == 0
    assert len(buf) == 4 and buf._head_seq == 3                            # 3, 4, 5, 6 left


def test_evicting_part_of_a_failed_in_flight_batch_is_a_drop():
    buf, sent = _inflight(ok=False, arrive=2)
    assert buf.dropped_count == 2 and len(buf) == 5 and buf._head_seq == 2


def test_evicting_past_the_in_flight_batch_counts_only_the_unsent_ones():
    buf, _ = _inflight(ok=True, arrive=4)                                 # evicts 0, 1, 2 (sent) and 3 (never sent)
    assert buf.dropped_count == 1 and buf._head_seq == 4 and len(buf) == 5


def test_telemetry_rides_along_only_when_given():
    sent, post = sink()
    buf = IngestBuffer("u", post, boot_id=BOOT)
    fill(buf, 1)
    buf.flush(telemetry={"backlog": 1})
    fill(buf, 1, start=1)
    buf.flush()
    assert sent[0]["telemetry"] == {"backlog": 1} and "telemetry" not in sent[1]


# --- schedule ----------------------------------------------------------------------------------------------

SCHED = dict(base_s=30.0, catchup_s=10.0, catchup_above=60, multiplier=2.0, cap_s=120.0)


@pytest.mark.parametrize("prev,outcome,backlog,expected", [
    (30, "ok", 10, 30), (30, "ok", 60, 30), (30, "ok", 61, 10), (10, "ok", 500, 10), (120, "ok", 900, 10),
    (30, "fail", 900, 60), (10, "fail", 900, 60), (60, "fail", 0, 120), (120, "fail", 0, 120),
    (10, "offline", 900, 30), (120, "offline", 900, 120),
])
def test_next_post_interval(prev, outcome, backlog, expected):
    assert next_post_interval_s(prev, outcome, backlog, **SCHED) == expected
