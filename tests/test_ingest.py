from __future__ import annotations

import math

import pytest

from helpers import US, T0, float32_text, utc, v1_reading, v2_gps, v2_reading
from tremor.ingest import (
    FLAG_TIME_IMPLAUSIBLE,
    FLAG_UNLOCKED,
    MAX_READINGS_PER_BATCH,
    SRC_NONE,
    SRC_V1,
    SRC_V2,
    ParsedReading,
    PayloadError,
    parse_payload,
    reconstruct_v1_us,
    resolve_time,
)


# --- parsing ------------------------------------------------------------------

def test_parses_a_legacy_v1_payload_exactly_as_unit_1_sends_it_today():
    b = parse_payload({"unit_id": "unit-1", "readings": [
        {"frequency_hz": 50.0123, "amplitude_v": 0.744, "gps_utc_s": 53457.13},
        {"frequency_hz": 49.99, "amplitude_v": 0.7, "gps_utc_s": None}]})
    assert b.mode == 1 and b.boot_id is None
    assert b.readings[0].sod == 53457.13 and b.readings[0].seq is None and b.readings[0].gps is None
    assert b.readings[1].sod is None


def test_parses_a_v2_payload():
    b = parse_payload({"unit_id": "unit-1", "boot_id": "9f3a51c07d2e4b18", "readings": [
        {"seq": 7, "frequency_hz": 50.0, "amplitude_v": 0.7, "gps": v2_gps(1_790_000_000 * US + 5)}]})
    assert b.mode == 2 and b.boot_id == "9f3a51c07d2e4b18"
    assert b.readings[0].seq == 7 and b.readings[0].gps[2] == 5


@pytest.mark.parametrize("payload", [
    [],                                                        # not an object
    {"readings": [{"frequency_hz": 50}]},                      # no unit
    {"unit_id": "", "readings": [{"frequency_hz": 50}]},
    {"unit_id": "unit 1", "readings": [{"frequency_hz": 50}]},  # bad characters
    {"unit_id": "../etc", "readings": [{"frequency_hz": 50}]},
    {"unit_id": "u", "readings": []},
    {"unit_id": "u", "readings": "x"},
    {"unit_id": "u", "readings": [{"amplitude_v": 1}]},        # no frequency
    {"unit_id": "u", "readings": [{"frequency_hz": "abc"}]},
    {"unit_id": "u", "readings": [{"frequency_hz": True}]},
    {"unit_id": "u", "readings": [{"frequency_hz": float("nan")}]},
    {"unit_id": "u", "readings": [{"frequency_hz": 50, "amplitude_v": float("inf")}]},
    {"unit_id": "u", "readings": [{"frequency_hz": 50, "gps_utc_s": "x"}]},
    {"unit_id": "u", "readings": ["not an object"]},
])
def test_rejects_malformed_payloads(payload):
    with pytest.raises(PayloadError):
        parse_payload(payload)


@pytest.mark.parametrize("payload", [
    # boot_id without seq, seq without boot_id, partial seq, bad seq / boot_id / gps shapes
    {"unit_id": "u", "boot_id": "aa", "readings": [{"frequency_hz": 50}]},
    {"unit_id": "u", "readings": [{"seq": 1, "frequency_hz": 50}]},
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": 1, "frequency_hz": 50}, {"frequency_hz": 50}]},
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": -1, "frequency_hz": 50}]},
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": 1.5, "frequency_hz": 50}]},
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": True, "frequency_hz": 50}]},
    {"unit_id": "u", "boot_id": "bad id!", "readings": [{"seq": 1, "frequency_hz": 50}]},
    {"unit_id": "u", "boot_id": 5, "readings": [{"seq": 1, "frequency_hz": 50}]},
    {"unit_id": "u", "readings": [{"frequency_hz": 50, "gps": [1, 2, 3]}]},           # gps needs boot_id
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": 1, "frequency_hz": 50, "gps": [1, 2]}]},
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": 1, "frequency_hz": 50, "gps": [1, 2, 3.5]}]},
    {"unit_id": "u", "boot_id": "aa", "readings": [{"seq": 1, "frequency_hz": 50, "gps": "1,2,3"}]},
])
def test_rejects_inconsistent_v2_payloads(payload):
    with pytest.raises(PayloadError):
        parse_payload(payload)


def test_rejects_an_oversized_batch():
    with pytest.raises(PayloadError):
        parse_payload({"unit_id": "u", "readings": [{"frequency_hz": 50}] * (MAX_READINGS_PER_BATCH + 1)})


# --- resolving v1 seconds-of-day to an absolute instant -------------------------

def test_v1_reading_keeps_its_own_seconds_of_day_and_only_borrows_the_date():
    received = T0 + 3.2
    r = ParsedReading(None, 50.0, 0.7, float32_text((T0 + 0.5) % 86400), None)
    t = resolve_time(r, received)
    assert t.time_src == SRC_V1 and t.flags == 0 and t.gps_locked == 1
    # within the float32 resolution of the true instant, and NOT equal to receipt time
    assert abs(t.gps_utc_us / US - (T0 + 0.5)) < 0.01
    assert abs(t.gps_utc_us / US - received) > 2.0


def test_v1_midnight_rollover_reading_just_before_midnight_received_just_after():
    # UTC midnight == 09:30 ACST. Reading at 23:59:59.5 UTC on the 24th, batch received
    # at 00:00:05 UTC (= 09:30:05 ACST) on the 25th: belongs to the 24th, not the 25th.
    midnight = utc(2026, 9, 25)
    received = midnight + 5.0
    us = reconstruct_v1_us(86399.5, received)
    assert us == int(round((midnight - 0.5) * US))
    t = resolve_time(ParsedReading(None, 50.0, None, 86399.5, None), received)
    assert t.flags == 0 and t.gps_utc_us == us


def test_v1_midnight_rollover_reading_just_after_midnight_received_just_before():
    # device clock a couple of seconds ahead of the server: reading stamped 00:00:00.5 UTC
    # arrives at 23:59:58 UTC -> the NEXT day, 2.5 s in the future: within the 5 s skew allowance.
    midnight = utc(2026, 9, 25)
    received = midnight - 2.0
    t = resolve_time(ParsedReading(None, 50.0, None, 0.5, None), received)
    assert t.flags == 0 and t.gps_utc_us == int(round((midnight + 0.5) * US))
    # ...but 10 s in the future is not believable: flagged, raw value kept, no time invented.
    t = resolve_time(ParsedReading(None, 50.0, None, 0.5, None), midnight - 10.0)
    assert t.flags == FLAG_TIME_IMPLAUSIBLE and t.gps_utc_us is None and t.gps_raw == 0.5


def test_v1_same_reading_resolves_identically_whenever_a_retry_arrives():
    """Dedupe on (unit, gps_utc_us) is only sound if a retried reading maps to the same instant
    however late the retry is -- including a retry that lands after UTC midnight."""
    midnight = utc(2026, 9, 25)
    sod = 86398.75
    first = reconstruct_v1_us(sod, midnight - 0.5)        # first attempt, before midnight
    retry = reconstruct_v1_us(sod, midnight + 65.0)       # retry a minute later, after midnight
    assert first == retry == int(round((midnight - 1.25) * US))


def test_v1_invalid_seconds_of_day_and_stale_values_are_flagged_not_trusted():
    assert reconstruct_v1_us(-1.0, T0) is None and reconstruct_v1_us(90000.0, T0) is None
    t = resolve_time(ParsedReading(None, 50.0, None, 90000.0, None), T0)
    assert t.flags == FLAG_TIME_IMPLAUSIBLE and t.gps_utc_us is None
    # a reading 2 hours old is beyond the retry horizon
    t = resolve_time(ParsedReading(None, 50.0, None, (T0 - 7200) % 86400, None), T0)
    assert t.flags == FLAG_TIME_IMPLAUSIBLE and t.gps_utc_us is None


# --- v2 integer time ------------------------------------------------------------

def test_v2_time_is_exact_to_the_microsecond_and_uses_the_device_date():
    unix_us = int(T0 * US) + 123_456
    t = resolve_time(ParsedReading(3, 50.0, None, None, tuple(v2_gps(unix_us))), T0 + 1.0)
    assert t.time_src == SRC_V2 and t.flags == 0 and t.gps_utc_us == unix_us     # exact, no rounding
    assert t.gps_locked == 1


def test_v2_midnight_rollover_uses_the_device_date_not_the_servers():
    midnight_us = int(utc(2026, 9, 25) * US)
    before = midnight_us - 500_000                           # 23:59:59.5 on the 24th
    after = midnight_us + 250_000                            # 00:00:00.25 on the 25th
    received = utc(2026, 9, 25) + 2.0
    for us in (before, after):
        t = resolve_time(ParsedReading(0, 50.0, None, None, tuple(v2_gps(us))), received)
        assert t.flags == 0 and t.gps_utc_us == us
    # the triple really does straddle two different device dates
    assert v2_gps(before)[0] + 1 == v2_gps(after)[0]
    assert v2_gps(before)[1] == 86399 and v2_gps(after)[1] == 0


def test_v2_bad_or_implausible_time_is_flagged_and_never_replaced_by_receipt_time():
    good_day = v2_gps(int(T0 * US))[0]
    bad = [(good_day, 86400, 0), (good_day, 0, 1_000_000), (5, 0, 0), (good_day, -1, 0)]
    for triple in bad:
        t = resolve_time(ParsedReading(0, 50.0, None, None, triple), T0)
        assert t.flags == FLAG_TIME_IMPLAUSIBLE and t.gps_utc_us is None
    old = resolve_time(ParsedReading(0, 50.0, None, None, tuple(v2_gps(int((T0 - 7200) * US)))), T0)
    assert old.flags == FLAG_TIME_IMPLAUSIBLE and old.gps_utc_us is None and old.gps_raw == pytest.approx(T0 - 7200)


def test_a_reading_with_no_gps_at_all_is_unlocked_and_gets_no_time():
    t = resolve_time(ParsedReading(None, 50.0, 0.7, None, None), T0)
    assert t.time_src == SRC_NONE and t.flags == FLAG_UNLOCKED and t.gps_utc_us is None and t.gps_locked == 0


def test_v1_and_v2_agree_on_the_instant_to_within_float32_resolution():
    for offset in (0.0, 0.123456, 30_000.5, 45_678.9):
        unix = T0 + offset
        v2 = resolve_time(ParsedReading(0, 50.0, None, None, tuple(v2_gps(int(round(unix * US))))), unix + 1)
        v1 = resolve_time(ParsedReading(None, 50.0, None, float32_text(unix % 86400), None), unix + 1)
        assert abs(v1.gps_utc_us - v2.gps_utc_us) <= 8_000       # float32 at these magnitudes: <= 7.8 ms
