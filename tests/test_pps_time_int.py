"""Integer-only GPS time on the device side: nmea_parser.parse_rmc_int and
pps_time_sync.PPSTimeSync.ticks_to_gps, run on the host behind a fake `machine`
module and a MicroPython-style wrap-around ticks_diff."""

from __future__ import annotations

import datetime as dt
import struct
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nmea_parser  # noqa: E402
from nmea_parser import parse_rmc, parse_rmc_both, parse_rmc_int  # noqa: E402

TICKS_MASK = (1 << 30) - 1                    # MicroPython's ticks_us wraps at 2**30 on most ports


def _ticks_diff(a, b):
    """time.ticks_diff semantics: signed, wrap-aware."""
    return ((a - b + (1 << 29)) & TICKS_MASK) - (1 << 29)


class _FakePin:
    IN, IRQ_RISING = 0, 1

    def __init__(self, *a, **k):
        self.handler = None

    def irq(self, trigger=None, handler=None, hard=False):
        self.handler = handler
        self.hard = hard


@pytest.fixture
def pps(monkeypatch):
    fake = types.ModuleType("machine")
    fake.Pin = _FakePin
    monkeypatch.setitem(sys.modules, "machine", fake)
    sys.modules.pop("pps_time_sync", None)
    import pps_time_sync
    clock = {"t": 0}
    monkeypatch.setattr(time, "ticks_us", lambda: clock["t"] & TICKS_MASK, raising=False)
    monkeypatch.setattr(time, "ticks_diff", _ticks_diff, raising=False)
    yield pps_time_sync, clock
    sys.modules.pop("pps_time_sync", None)


def rmc(hhmmss: str, ddmmyy: str, status: str = "A") -> str:
    body = f"GNRMC,{hhmmss},{status},3446.79805,S,13837.52156,E,0.066,,{ddmmyy},,,A,V"
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}"


def sync_at(mod, clock, tick, hhmmss, ddmmyy):
    """A PPSTimeSync whose anchor is (RMC hhmmss on ddmmyy) at the given ticks_us value."""
    s = mod.PPSTimeSync()
    clock["t"] = tick
    s._on_pps(None)
    s.feed_nmea(rmc(hhmmss, ddmmyy))
    assert s.sync_count == 1
    return s


# --- days_from_civil ----------------------------------------------------------------------------------

def test_days_from_civil_matches_datetime_for_every_day_of_2000_to_2100(pps):
    mod, _ = pps
    d0 = dt.date(1970, 1, 1)
    d = dt.date(2000, 1, 1)
    while d <= dt.date(2100, 12, 31):
        assert mod.days_from_civil(d.year, d.month, d.day) == (d - d0).days
        d += dt.timedelta(days=1)


def test_days_from_civil_known_values(pps):
    mod, _ = pps
    assert mod.days_from_civil(1970, 1, 1) == 0
    assert mod.days_from_civil(2000, 2, 29) == 11016 and mod.days_from_civil(2100, 3, 1) == 47541
    assert mod.days_from_civil(2026, 9, 25) == 20721


# --- integer RMC parsing ---------------------------------------------------------------------------------

@pytest.mark.parametrize("time_field,expected", [
    ("063004.00", (6 * 3600 + 30 * 60 + 4, 0)),
    ("063004", (23404, 0)),
    ("063004.5", (23404, 500_000)),
    ("063004.123", (23404, 123_000)),
    ("063004.123456", (23404, 123_456)),
    ("063004.1234567", (23404, 123_456)),                    # truncated to microseconds
    ("235959.99", (86399, 990_000)),
    ("000000.00", (0, 0)),
])
def test_parse_rmc_int_reads_time_without_floats(time_field, expected):
    sod, usec, date = parse_rmc_int(rmc(time_field, "250926"))
    assert (sod, usec) == expected and date == (2026, 9, 25)
    assert type(sod) is int and type(usec) is int


@pytest.mark.parametrize("line", [
    rmc("063004.00", "250926", status="V"),                   # void fix
    rmc("06300x.00", "250926"),                               # garbage digit
    rmc("063004x00", "250926"),                               # bad separator
    rmc("063004.1x", "250926"),
    rmc("063004.00", "2509"),                                 # short date
    rmc("0630", "250926"),                                    # short time
    rmc("063004.00", "250926")[:-2] + "00",                   # bad checksum
    "$GNGGA,063004.00,3446.79805,S,13837.52156,E,1,12,0.5,23.3,M,-3.1,M,,*77",
    "",
])
def test_parse_rmc_int_rejects_what_parse_rmc_rejects(line):
    assert parse_rmc_int(line) is None


def test_the_legacy_float_parser_is_unchanged_and_agrees_with_the_integer_one():
    line = rmc("063004.25", "250926")
    utc_s, date = parse_rmc(line)
    sod, usec, date_i = parse_rmc_int(line)
    assert utc_s == pytest.approx(sod + usec / 1e6) and date == date_i
    both = parse_rmc_both(line)
    assert both == ((utc_s, date), (sod, usec, date_i))
    assert parse_rmc_both(rmc("063004.25", "250926", status="V")) is None


# --- PPSTimeSync.ticks_to_gps ---------------------------------------------------------------------------------

def test_ticks_to_gps_returns_days_second_and_microsecond_as_ints(pps):
    mod, clock = pps
    s = sync_at(mod, clock, 5_000_000, "063004.00", "250926")
    day, sod = 20721, 6 * 3600 + 30 * 60 + 4
    assert s.ticks_to_gps(5_000_000) == (day, sod, 0)
    assert s.ticks_to_gps(5_000_000 + 250_123) == (day, sod, 250_123)
    assert s.ticks_to_gps(5_000_000 + 2_500_000) == (day, sod + 2, 500_000)
    got = s.ticks_to_gps(5_000_000 + 999_999)
    assert got == (day, sod, 999_999) and all(type(x) is int for x in got)


def test_a_reading_slightly_before_the_anchor_is_handled_with_floor_arithmetic(pps):
    mod, clock = pps
    s = sync_at(mod, clock, 5_000_000, "063004.25", "250926")
    day, sod = 20721, 23404
    assert s.ticks_to_gps(5_000_000 - 100_000) == (day, sod, 150_000)
    assert s.ticks_to_gps(5_000_000 - 250_000) == (day, sod, 0)
    assert s.ticks_to_gps(5_000_000 - 250_001) == (day, sod - 1, 999_999)


def test_utc_midnight_rollover_forward_09_30_acst(pps):
    """00:00:00 UTC == 09:30:00 ACST. An anchor at 23:59:59.00 UTC on the 25th; a reading 1.5 s later is
    00:00:00.5 UTC on the 26th -- the device date must roll forward, not wrap within the day."""
    mod, clock = pps
    s = sync_at(mod, clock, 5_000_000, "235959.00", "250926")
    assert s.ticks_to_gps(5_000_000) == (20721, 86399, 0)
    assert s.ticks_to_gps(5_000_000 + 1_000_000) == (20722, 0, 0)
    assert s.ticks_to_gps(5_000_000 + 1_500_000) == (20722, 0, 500_000)
    assert dt.date(1970, 1, 1) + dt.timedelta(days=20722) == dt.date(2026, 9, 26)


def test_utc_midnight_rollover_backward_a_reading_before_an_anchor_just_after_midnight(pps):
    mod, clock = pps
    s = sync_at(mod, clock, 5_000_000, "000000.10", "260926")             # anchor at 00:00:00.1 on the 26th
    assert s.ticks_to_gps(5_000_000 - 200_000) == (20721, 86399, 900_000)  # 23:59:59.9 on the 25th


def test_month_and_year_rollover_uses_the_device_calendar(pps):
    mod, clock = pps
    s = sync_at(mod, clock, 1_000_000, "235959.50", "311225")             # 31 Dec 2025
    assert s.ticks_to_gps(1_000_000 + 600_000) == (mod.days_from_civil(2026, 1, 1), 0, 100_000)
    s = sync_at(mod, clock, 1_000_000, "235959.00", "280224")             # 28 Feb 2024 (leap year)
    assert s.ticks_to_gps(1_000_000 + 1_000_000) == (mod.days_from_civil(2024, 2, 29), 0, 0)


def test_the_newest_anchor_wins_and_wrap_around_ticks_are_handled(pps):
    mod, clock = pps
    near_wrap = TICKS_MASK - 500_000                                       # ticks_us about to wrap
    s = sync_at(mod, clock, near_wrap, "063004.00", "250926")
    later = (near_wrap + 1_000_000) & TICKS_MASK                           # the next PPS edge: exactly 1 s later, wrapped past zero
    assert later < near_wrap
    assert s.ticks_to_gps(later + 250_000) == (20721, 23405, 250_000)     # +1.25 s across the wrap
    clock["t"] = later
    s._on_pps(None)                                                        # (the interval filter needs a real 1 s spacing)
    s.feed_nmea(rmc("063005.00", "250926"))
    assert s.ticks_to_gps(later + 10) == (20721, 23405, 10)


def test_not_synced_and_a_stale_anchor_return_none(pps):
    mod, clock = pps
    s = mod.PPSTimeSync()
    assert s.ticks_to_gps(123) is None
    s = sync_at(mod, clock, 5_000_000, "063004.00", "250926")
    assert s.ticks_to_gps(5_000_000 + 299_000_000) is not None
    assert s.ticks_to_gps(5_000_000 + 301_000_000) is None                 # > 300 s: same rule as ticks_to_utc
    assert s.ticks_to_gps(5_000_000 - 301_000_000) is None


def test_every_intermediate_fits_micropythons_31_bit_small_ints(pps):
    mod, _ = pps
    small_int_max = (1 << 30) - 1
    assert mod._ANCHOR_MAX_AGE_US + 999_999 < small_int_max               # us = anchor_usec + delta_us
    assert 86_399 + mod._ANCHOR_MAX_AGE_US // 1_000_000 + 1 < small_int_max
    assert mod.days_from_civil(2100, 12, 31) < small_int_max


def test_the_float_path_and_the_feed_logic_are_unchanged(pps):
    mod, clock = pps
    s = sync_at(mod, clock, 5_000_000, "063004.00", "250926")
    assert s.ticks_to_utc(5_000_000 + 500_000) == pytest.approx(23404.5)
    assert s.status["anchor_date"] == (2026, 9, 25)
    clock["t"] = 6_000_000
    s._on_pps(None)
    s.feed_nmea(rmc("073004.00", "250926"))                                # an hour off: sanity check rejects it
    assert s.rejected_count == 1 and s.sync_count == 1
    assert s.ticks_to_gps(5_000_000) == (20721, 23404, 0)                  # rejected sentence did not move the anchor


def test_why_the_float_path_is_not_good_enough_and_the_integer_path_is():
    """Documents the reason for the integer format: at a typical seconds-of-day value, single
    precision cannot even represent a 250 microsecond offset, while the integer triple is exact."""
    sod = 55_000.000250
    f32 = struct.unpack("<f", struct.pack("<f", sod))[0]
    assert abs(f32 - sod) > 1e-4                                          # >100 us error, up to ~4 ms
    day_sec_us = (20721, 55_000, 250)
    assert day_sec_us[2] == 250                                           # exact, by construction
