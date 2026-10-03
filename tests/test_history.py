"""The /history page and /api/history/overview (history.py): source selection, downsampling,
exclusions, coverage, the daily table, known-bad boots, rate limiting, and speed."""

from __future__ import annotations

import math
import random
import shutil
import statistics
import time

import pytest

from helpers import FakeClock, US, utc, v2_series
from tremor.history import (KNOWN_BAD, MAX_POINTS, RAW_MAX_SPAN_S, RawDayCache, build_overview, nice_width)
from tremor.retention import RetentionConfig
from tremor.store import AggRow, DayState, open_store
from tremor.webapp import create_app

BOOT = "5c0ffee15c0ffee1"
BAD_BOOT = KNOWN_BAD[0].boot_id
DAY0 = utc(2026, 9, 20)                       # three closed days, then "today" (2026-09-23)
NOW = utc(2026, 9, 23, 6, 0)


def wave(t: float) -> float:
    return 50.0 + 0.03 * math.sin(t / 900.0)


def make_app(tmp_path, clock, **kw):
    return create_app(simulated_units=[], db_path=str(tmp_path / "readings.db"), clock=clock,
                      retention_config=RetentionConfig(export_dir=str(tmp_path / "exports"), raw_days=14), **kw)


def post_series(client, clock, start, n, seq0=0, boot=BOOT, freq=wave, amp=0.744, step=1.0):
    """Like the device: batches of <= 600 readings, each received right after its last reading."""
    done = 0
    while done < n:
        k = min(600, n - done)
        t0 = start + done * step
        clock.t = max(clock.t, t0 + k * step + 2)
        b = v2_series("unit-1", boot, t0, k, seq0=seq0 + done, step=step,
                      freq=lambda i, t0=t0: freq(t0 + i * step), amp=amp)
        r = client.post("/api/ingest", json=b)
        assert r.status_code == 202, r.get_json()
        done += k


@pytest.fixture
def ctx(tmp_path):
    clock = FakeClock(DAY0)
    app = make_app(tmp_path, clock)
    yield app.test_client(), clock, app
    app.config["TREMOR_SHUTDOWN"]()


def overview(client, **q):
    r = client.get("/api/history/overview", query_string=dict(unit="unit-1", **q))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


@pytest.fixture(scope="module")
def four_days_db(tmp_path_factory):
    """2026-09-20..22 every second from 00:00 to 06:00 (closed days, aggregated by retention), plus
    2026-09-23 00:00..06:00 raw only. Built once; each test gets its own copy."""
    d = tmp_path_factory.mktemp("four_days")
    clock = FakeClock(DAY0)
    app = make_app(d, clock)
    client = app.test_client()
    seq = 0
    for day in range(4):
        post_series(client, clock, DAY0 + day * 86400, 6 * 3600, seq0=seq)
        seq += 6 * 3600
    clock.t = NOW
    app.config["TREMOR_RETENTION"].run_until_idle()
    app.config["TREMOR_SHUTDOWN"]()
    return d / "readings.db"


@pytest.fixture
def four_days(four_days_db, tmp_path):
    shutil.copy(four_days_db, tmp_path / "readings.db")
    clock = FakeClock(NOW)
    app = make_app(tmp_path, clock)
    yield app.test_client(), clock, app
    app.config["TREMOR_SHUTDOWN"]()


# --- source selection ------------------------------------------------------------------------

def test_a_short_range_uses_raw_readings_and_a_fine_bucket(four_days):
    client, *_ = four_days
    j = overview(client, **{"from": NOW - 3600, "to": NOW})
    assert [s["source"] for s in j["sources"]] == ["raw"]
    assert j["freq"]["source"] == "raw" and j["freq"]["bucket_s"] == nice_width(3600 / 1000, 1) == 5
    assert j["rocof"]["bucket_s"] == 60 and len(j["rocof"]["t"]) == 60       # max |RoCoF| per minute
    # each 5 s bucket: mean/min/max of exactly those five readings
    t = j["freq"]["t"][10]
    fs = [wave(t + i) for i in range(5)]
    i = 10
    assert j["freq"]["n"][i] == 5
    assert j["freq"]["mean"][i] == pytest.approx(statistics.fmean(fs), abs=1e-6)
    assert j["freq"]["min"][i] == pytest.approx(min(fs), abs=1e-6)
    assert j["freq"]["max"][i] == pytest.approx(max(fs), abs=1e-6)


def test_a_long_range_uses_aggregates_for_aggregated_days_and_raw_for_today(four_days):
    client, *_ = four_days
    j = overview(client, **{"from": DAY0, "to": NOW})
    assert [s["source"] for s in j["sources"]] == ["1min", "raw"]
    assert j["sources"][1]["from_us"] == int(utc(2026, 9, 23) * US)
    assert j["freq"]["source"] == "1min" and j["freq"]["bucket_s"] >= 60
    assert [d["source"] for d in j["daily"]] == ["1min", "1min", "1min", "raw"]


def test_a_short_range_on_an_aggregated_day_still_uses_raw_while_raw_is_kept(four_days):
    client, *_ = four_days
    j = overview(client, **{"from": DAY0 + 3600, "to": DAY0 + 7200})
    assert [s["source"] for s in j["sources"]] == ["raw"]


def test_a_short_range_on_a_pruned_day_falls_back_to_aggregates(tmp_path):
    store = open_store(str(tmp_path / "r.db"))
    store.save_aggregates([AggRow("unit-1", int(DAY0 // 60) + m, 60, 0, 50.01, 50.0, 50.02, 0.005, 0.01, 0.74)
                           for m in range(120)])
    store.save_day_state(DayState("unit-1", int(DAY0 // 86400), export_done=True, agg_done=True,
                                  verified_at=DAY0 + 86400 * 15, pruned_rows=1000, pruned_done=True))
    j = build_overview(store, "unit-1", int(DAY0 * US), int((DAY0 + 3600) * US), NOW)
    assert [s["source"] for s in j["sources"]] == ["1min"] and j["freq"]["source"] == "1min"
    assert j["daily"][0]["n"] == 60 * 60 and j["daily"][0]["mean"] == pytest.approx(50.01)


# --- statistics agree between raw and aggregates ----------------------------------------------

def test_the_daily_table_is_the_same_whether_computed_from_raw_or_from_aggregates(four_days):
    client, clock, app = four_days
    store = app.config["TREMOR_STORE"]
    day = int(utc(2026, 9, 21) * US)
    from_raw = build_overview(store, "unit-1", day, day + 6 * 3600 * US, NOW)                 # short: raw
    from_agg = build_overview(store, "unit-1", day - 86400 * US, day + 86400 * US, NOW)      # long: 1min
    raw_row = from_raw["daily"][0]
    agg_row = next(d for d in from_agg["daily"] if d["day"] == "2026-09-21")
    assert raw_row["source"] == "raw" and agg_row["source"] == "1min"
    assert agg_row["n"] == raw_row["n"] == 6 * 3600
    for k in ("mean", "std", "min", "max"):
        assert agg_row[k] == pytest.approx(raw_row[k], abs=2e-6), k
    assert agg_row["rocof_max_abs"] == pytest.approx(raw_row["rocof_max_abs"], rel=1e-6)
    fs = [wave(utc(2026, 9, 21) + i) for i in range(6 * 3600)]
    assert raw_row["mean"] == pytest.approx(statistics.fmean(fs), abs=1e-6)
    assert raw_row["std"] == pytest.approx(statistics.pstdev(fs), abs=1e-6)


def test_coverage_counts_seconds_with_a_good_reading_against_elapsed_time(four_days):
    client, *_ = four_days
    j = overview(client, **{"from": utc(2026, 9, 22), "to": utc(2026, 9, 23)})
    day = j["daily"][0]
    assert day["coverage_pct"] == pytest.approx(25.0)                     # 6 h of 24
    cov = dict(zip(j["coverage"]["t"], j["coverage"]["good_pct"]))
    assert j["coverage"]["bucket_s"] == 3600 and len(cov) == 24
    assert cov[int(utc(2026, 9, 22, 2))] == 100.0 and cov[int(utc(2026, 9, 22, 12))] == 0.0
    # today: only the elapsed part of the range counts as expected
    j = overview(client, **{"from": utc(2026, 9, 23), "to": utc(2026, 9, 24)})
    assert j["daily"][0]["expected_s"] == 6 * 3600 and j["daily"][0]["coverage_pct"] == pytest.approx(100.0, abs=0.1)


def test_gaps_leave_holes_in_the_series_not_interpolated_points(ctx):
    client, clock, _ = ctx
    post_series(client, clock, DAY0, 600)
    post_series(client, clock, DAY0 + 1800, 600, seq0=600)
    clock.t = DAY0 + 3600
    j = overview(client, **{"from": DAY0, "to": DAY0 + 3600})
    ts = j["freq"]["t"]
    assert max(b - a for a, b in zip(ts, ts[1:])) >= 1200 - j["freq"]["bucket_s"]
    assert all(DAY0 <= t < DAY0 + 600 or DAY0 + 1800 <= t < DAY0 + 2400 for t in ts)


# --- exclusions ----------------------------------------------------------------------------------

def test_low_amplitude_and_out_of_band_readings_are_excluded_and_listed(ctx):
    client, clock, _ = ctx
    post_series(client, clock, DAY0, 600, freq=lambda t: 50.0)
    post_series(client, clock, DAY0 + 600, 120, seq0=600, freq=lambda t: 50.0 + (t % 7), amp=0.02)  # unplugged
    post_series(client, clock, DAY0 + 720, 600, seq0=720, freq=lambda t: 80.995 if t == DAY0 + 1000 else 50.0)
    clock.t = DAY0 + 1400
    j = overview(client, **{"from": DAY0, "to": DAY0 + 1320})
    d = j["daily"][0]
    assert d["n"] == 1199 and d["n_excluded"] == 121
    assert d["max"] == 50.0 and d["min"] == 50.0 and d["std"] == 0.0
    assert max(j["freq"]["max"]) == 50.0
    # no spurious RoCoF from the jump into or out of the unplugged stretch
    assert max(j["rocof"]["max_abs"]) == pytest.approx(0.0, abs=1e-9)
    periods = j["excluded"]["periods"]
    assert [p["reasons"] for p in periods] == [["low_amplitude"], ["freq_out_of_band"]]
    assert periods[0]["start_us"] == int((DAY0 + 600) * US) and periods[0]["end_us"] == int((DAY0 + 720) * US)
    assert periods[0]["n"] == 120 and periods[1]["n"] == 1
    assert j["excluded"]["rules"]["freq_band_hz"] == [47.0, 52.0]
    assert d["excluded_pct"] == pytest.approx(100 * 121 / 1320, abs=0.01)


def test_a_real_grid_excursion_inside_47_to_52_hz_is_kept(ctx):
    client, clock, _ = ctx
    post_series(client, clock, DAY0, 600, freq=lambda t: 49.2 if DAY0 + 300 <= t < DAY0 + 320 else 50.0)
    clock.t = DAY0 + 700
    j = overview(client, **{"from": DAY0, "to": DAY0 + 600})
    assert j["daily"][0]["min"] == pytest.approx(49.2) and j["excluded"]["periods"] == []


def test_a_minute_aggregate_with_an_out_of_band_reading_is_excluded_as_a_whole(tmp_path):
    store = open_store(str(tmp_path / "r.db"))
    m0 = int(DAY0 // 60)
    rows = [AggRow("unit-1", m0 + m, 60, 0, 50.0, 49.99, 50.01, 0.004, 0.01, 0.74) for m in range(1440)]
    rows[300] = AggRow("unit-1", m0 + 300, 60, 0, 50.52, 49.99, 80.995, 3.9, 0.5, 0.74)   # the glitch minute
    rows[800] = AggRow("unit-1", m0 + 800, 60, 0, 31.0, 0.0, 49.0, 20.0, None, 0.02)       # unplugged
    store.save_aggregates(rows)
    store.save_day_state(DayState("unit-1", int(DAY0 // 86400), export_done=True, agg_done=True))
    j = build_overview(store, "unit-1", int(DAY0 * US), int((DAY0 + 86400) * US), NOW)
    d = j["daily"][0]
    assert d["n"] == 1438 * 60 and d["n_excluded"] == 120
    assert d["max"] == 50.01 and d["min"] == 49.99 and d["mean"] == pytest.approx(50.0)
    assert d["rocof_max_abs"] == pytest.approx(0.01)
    assert [(p["start_us"] // US - DAY0, p["reasons"]) for p in j["excluded"]["periods"]] == [
        (300 * 60, ["freq_out_of_band"]), (800 * 60, ["freq_out_of_band", "low_amplitude"])]


def test_the_known_bad_boot_is_excluded_from_raw_readings_and_listed(tmp_path):
    kb = KNOWN_BAD[0]
    clock = FakeClock(kb.start_us / US)
    app = make_app(tmp_path, clock)
    client = app.test_client()
    try:
        start = kb.start_us / US - 600
        post_series(client, clock, start, 600, boot=BOOT, freq=lambda t: 50.0)
        post_series(client, clock, kb.start_us / US + 28, 289, boot=BAD_BOOT, freq=lambda t: 50.3)
        clock.t = kb.end_us / US + 60
        j = overview(client, **{"from": start, "to": kb.end_us / US})
        assert j["daily"][0]["max"] == 50.0 and j["daily"][0]["n_excluded"] == 289
        p = j["excluded"]["periods"]
        assert len(p) == 1 and p[0]["reasons"] == ["known_bad_boot"]
        assert p[0]["start_us"] == kb.start_us and p[0]["end_us"] == kb.end_us
        assert j["known_bad"] == [dict(boot_id=BAD_BOOT, start_us=kb.start_us, end_us=kb.end_us, note=kb.note)]
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_the_known_bad_period_is_listed_even_though_its_rows_are_deleted_and_masks_aggregates(tmp_path):
    kb = KNOWN_BAD[0]
    store = open_store(str(tmp_path / "r.db"))
    day = kb.start_us // (86400 * US)
    store.save_aggregates([AggRow("unit-1", day * 1440 + m, 60, 0, 50.0, 49.99, 50.01, 0.004, 0.01, 0.74)
                           for m in range(1440)])
    store.save_day_state(DayState("unit-1", day, export_done=True, agg_done=True))
    j = build_overview(store, "unit-1", day * 86400 * US, (day + 1) * 86400 * US, NOW + 86400 * 10)
    assert j["daily"][0]["n_excluded"] == 6 * 60                                     # 03:47..03:53
    assert [p["reasons"] for p in j["excluded"]["periods"]] == [["known_bad_boot"]]
    assert j["known_bad"][0]["boot_id"] == BAD_BOOT
    other = build_overview(store, "unit-2", day * 86400 * US, (day + 1) * 86400 * US, NOW + 86400 * 10)
    assert other["known_bad"] == []


# --- downsampling and limits ---------------------------------------------------------------------

@pytest.mark.parametrize("points", [100, 700, MAX_POINTS])
def test_points_cap_the_number_of_buckets(four_days, points):
    client, *_ = four_days
    for frm in (NOW - RAW_MAX_SPAN_S, DAY0):
        j = overview(client, **{"from": frm, "to": NOW, "points": points})
        assert 0 < len(j["freq"]["t"]) <= points + 1
        assert len(j["rocof"]["t"]) <= points + 1
        assert len(j["coverage"]["t"]) <= 802


def test_a_month_of_data_is_a_small_payload(tmp_path):
    store = open_store(str(tmp_path / "r.db"))
    for d in range(30):
        day = int(DAY0 // 86400) + d
        store.save_aggregates([AggRow("unit-1", day * 1440 + m, 60, 0, 50.0, 49.99, 50.01, 0.004, 0.01, 0.74)
                               for m in range(1440)])
        store.save_day_state(DayState("unit-1", day, export_done=True, agg_done=True))
    import json
    j = build_overview(store, "unit-1", int(DAY0 * US), int((DAY0 + 30 * 86400) * US), DAY0 + 31 * 86400)
    assert len(json.dumps(j)) < 150_000 and len(j["daily"]) == 30
    assert all(d["coverage_pct"] == 100.0 for d in j["daily"] if d["day"] != "2026-09-26")
    masked = next(d for d in j["daily"] if d["day"] == "2026-09-26")      # the known-bad boot's 6 minutes
    assert masked["excluded_pct"] == pytest.approx(100 * 6 / 1440, abs=0.01)
    assert masked["coverage_pct"] + masked["excluded_pct"] == pytest.approx(100.0, abs=0.01)


def test_all_starts_at_the_units_first_data_not_at_the_epoch(four_days):
    client, *_ = four_days
    j = overview(client)
    assert j["from_us"] == int(DAY0 * US) and j["daily"][0]["day"] == "2026-09-20"
    assert j["to_us"] == int(NOW * US)


# --- the endpoint ----------------------------------------------------------------------------------

def test_the_endpoint_validates_its_arguments(four_days):
    client, *_ = four_days
    get = lambda **q: client.get("/api/history/overview", query_string=q)
    assert get().status_code == 400
    assert get(unit="unit-9").status_code == 404
    assert get(unit="unit-1", points="5").status_code == 400
    assert get(unit="unit-1", points="many").status_code == 400
    assert get(unit="unit-1", **{"from": "yesterday"}).status_code == 400
    assert get(unit="unit-1", **{"from": NOW, "to": NOW - 60}).status_code == 400
    assert get(unit="unit-1", **{"from": "2026-09-22T00:00:00Z", "to": "2026-09-22T01:00:00Z"}).status_code == 200


def test_the_endpoint_shares_the_history_rate_limit(tmp_path):
    clock = FakeClock(DAY0)
    app = make_app(tmp_path, clock, history_rate=(3, 60.0))
    client = app.test_client()
    try:
        post_series(client, clock, DAY0, 60)
        assert client.get("/api/history", query_string={"unit": "unit-1"}).status_code == 200
        assert client.get("/api/history/overview", query_string={"unit": "unit-1"}).status_code == 200
        assert client.get("/api/history/overview", query_string={"unit": "unit-1"}).status_code == 200
        r = client.get("/api/history/overview", query_string={"unit": "unit-1"})
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_storage_failure_answers_503(four_days, monkeypatch):
    client, _clock, app = four_days
    from tremor.store import StoreError

    def boom(*a, **k):
        raise StoreError("disk I/O error")
    monkeypatch.setattr(app.config["TREMOR_STORE"], "day_states", boom)
    r = client.get("/api/history/overview", query_string={"unit": "unit-1"})
    assert r.status_code == 503 and r.get_json() == {"error": "storage unavailable"}


def test_the_history_page_is_public_lists_units_and_is_linked_from_the_dashboard(four_days):
    client, *_ = four_days
    r = client.get("/history")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'const UNITS = ["unit-1"]' in html and "/api/history/overview" in html
    assert 'href="/history"' in client.get("/").get_data(as_text=True)


def test_the_history_page_renders_with_no_units_yet(ctx):
    client, *_ = ctx
    r = client.get("/history")
    assert r.status_code == 200 and "const UNITS = []" in r.get_data(as_text=True)


# --- cache and speed -----------------------------------------------------------------------------

def test_raw_hours_are_cached_but_the_open_hour_is_refreshed(four_days):
    client, clock, app = four_days
    store = app.config["TREMOR_STORE"]
    cache = RawDayCache()
    to = int(NOW * US)
    calls = []
    real = store.read_points
    store.read_points = lambda *a: calls.append(a) or real(*a)
    try:
        long_from = int(DAY0 * US)
        build_overview(store, "unit-1", long_from, to, NOW, cache=cache)
        assert len(calls) == 1                         # today's six raw hours: one read for the run
        calls.clear()
        build_overview(store, "unit-1", long_from, to, NOW, cache=cache)
        assert calls == []                             # all cached (the open hour has a short TTL)
        post_series(client, clock, NOW, 120, seq0=10 ** 6)
        cache._mono = lambda: time.monotonic() + 31    # past the open hour's TTL only
        j = build_overview(store, "unit-1", long_from, int((NOW + 3600) * US), NOW + 125, cache=cache)
        # the hour that ended at NOW is still inside its settle window, so it is re-read too
        assert len(calls) == 1 and calls[0][1] == int((NOW - 3600) * US) - 10 * US
        assert j["daily"][-1]["n"] == 6 * 3600 + 120
    finally:
        store.read_points = real


def test_a_month_with_a_day_of_raw_readings_is_fast(tmp_path):
    """The cheap local stand-in for scripts/bench_history.py (run that on PythonAnywhere):
    30 days of aggregates + 26 h of 1/s raw readings, cold cache."""
    store = open_store(str(tmp_path / "r.db"), synchronous="OFF")
    now = utc(2026, 10, 3, 2, 0)
    raw_from = now - 26 * 3600
    first = int(now // 86400) - 30
    rng = random.Random(3)
    for day in range(first, int(raw_from // 86400)):
        store.save_aggregates([AggRow("unit-1", day * 1440 + m, 60, 0, 50.0 + rng.gauss(0, 0.02), 49.95, 50.05, 0.005,
                                      0.01, 0.74) for m in range(1440)])
        store.save_day_state(DayState("unit-1", day, export_done=True, agg_done=True))
    import sqlite3
    db = sqlite3.connect(store.path)
    db.executemany("INSERT INTO readings(unit_id, boot_id, seq, gps_utc_us, time_src, freq_hz, amplitude_v, "
                   "gps_locked, flags, received_at) VALUES ('unit-1', ?, ?, ?, 2, ?, 0.74, 1, 0, ?)",
                   [(BOOT, i, int((raw_from + i) * US), 50.0 + rng.gauss(0, 0.004), raw_from + i)
                    for i in range(26 * 3600)])
    db.commit()
    db.close()
    timings = {}
    for name, span in (("24h", 86400), ("30d", 30 * 86400)):
        t = time.perf_counter()
        j = build_overview(store, "unit-1", int((now - span) * US), int(now * US), now, cache=RawDayCache())
        timings[name] = time.perf_counter() - t
        assert j["freq"]["t"]
    # generous for a slow CI box; the real check is the PythonAnywhere benchmark in the runbook
    assert max(timings.values()) < 2.0, timings
