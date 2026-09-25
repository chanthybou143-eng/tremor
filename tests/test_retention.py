from __future__ import annotations

import csv
import gzip
import hashlib
import math
import os
import sqlite3

import pytest

from helpers import FakeClock, T0, US, utc, v1_batch, v1_reading, v2_batch, v2_reading, v2_series
from tremor.ingest import parse_payload
from tremor.retention import RetentionConfig, RetentionEngine, _cli, day_to_date
from tremor.store import DAY_US, open_store

BOOT = "9f3a51c07d2e4b18"
NOW = utc(2026, 9, 25, 12, 0)                       # frozen "today", noon UTC
DAY = lambda days_ago: int((NOW - days_ago * 86400) // 86400)      # UTC day number


def day_start(days_ago: int) -> float:
    return DAY(days_ago) * 86400.0


def freq_at(i: int) -> float:
    return 50.0 + 0.02 * math.sin(i / 37.0)


def fill_day(store, days_ago: int, n: int = 1000, freq=freq_at, seq0: int | None = None, boot: str = BOOT,
             start_offset: float = 3600.0, unit: str = "unit-1"):
    """n readings at 1 Hz starting `start_offset` s into the UTC day, ingested with a plausible
    receipt time (3 s after the newest reading in each batch). seq ranges never overlap between
    days (same boot_id), otherwise the store would -- correctly -- treat a later day as a retry."""
    if seq0 is None:
        seq0 = days_ago * 100_000
    t = day_start(days_ago) + start_offset
    for lo in range(0, n, 500):
        m = min(500, n - lo)
        batch = v2_series(unit, boot, t + lo, m, seq0=seq0 + lo, freq=lambda i, lo=lo: freq(lo + i))
        store.ingest(parse_payload(batch), t + lo + m + 3.0)
    return t


@pytest.fixture
def env(tmp_path):
    store = open_store(str(tmp_path / "r.db"))
    cfg = RetentionConfig(export_dir=str(tmp_path / "exports"), raw_days=14,
                          export_chunk_rows=300, prune_chunk_rows=200)
    clock = FakeClock(NOW)
    return store, cfg, RetentionEngine(store, cfg, clock), clock, tmp_path


def read_export(path):
    with gzip.open(path, "rt", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def row_count(store, unit="unit-1"):
    return len(store.history(unit, 0, 2**62, 1_000_000, include_unlocked=True).rows) + \
        len(store.history(unit, 0, 2**62, 1_000_000, include_unlocked=True).unlocked)


# --- lifecycle -----------------------------------------------------------------------------

def test_old_days_are_exported_aggregated_verified_then_pruned_and_recent_days_are_left_alone(env):
    store, cfg, eng, _clock, _ = env
    for ago in (20, 16, 3):
        fill_day(store, ago)
    eng.run_until_idle()

    states = store.day_states("unit-1")
    for ago in (20, 16):
        st = states[DAY(ago)]
        assert st.export_done and st.agg_done and st.verified_at and st.pruned_done and not st.attention
        assert st.pruned_rows == 1000 and store.day_row_counts("unit-1", DAY(ago)) == (0, 0)
        header, rows = read_export(st.export_path)
        assert header[0] == "unit_id" and len(rows) == 1000
        assert st.export_sha256 == hashlib.sha256(open(st.export_path, "rb").read()).hexdigest()
    recent = states[DAY(3)]
    assert recent.export_done and recent.agg_done and not recent.pruned_done          # exported, but raw rows kept
    assert store.day_row_counts("unit-1", DAY(3)) == (1000, 0)


def test_export_contains_every_field_and_round_trips_exactly(env):
    store, _cfg, eng, _c, _ = env
    fill_day(store, 20, n=50, seq0=0)
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    header, rows = read_export(st.export_path)
    assert header == ["unit_id", "boot_id", "seq", "gps_utc_us", "gps_raw", "time_src", "freq_hz",
                      "amplitude_v", "gps_locked", "flags", "received_at"]
    first = rows[0]
    assert first[0] == "unit-1" and first[1] == BOOT and first[2] == "0"
    assert int(first[3]) == int(round((day_start(20) + 3600) * US)) and float(first[6]) == freq_at(0)
    assert [int(r[2]) for r in rows] == list(range(50))


def test_one_minute_aggregates_keep_mean_min_max_std_count_and_max_abs_rocof(env):
    store, _cfg, eng, _c, _ = env

    def f(i):
        return 50.0 + (0.5 if 100 <= i < 105 else 0.0) + 0.001 * (i % 7)
    fill_day(store, 20, n=600, freq=f)
    eng.run_until_idle()
    start = day_start(20) + 3600
    first_minute = int(start // 60)
    aggs = {a.minute: a for a in store.aggregates("unit-1", first_minute, first_minute + 20, 100)}
    assert sum(a.n for a in aggs.values()) == 600 and all(a.n_unlocked == 0 for a in aggs.values())
    for k in range(10):
        vals = [f(i) for i in range(k * 60, k * 60 + 60)]
        a = aggs[first_minute + k]
        mean = sum(vals) / 60
        assert a.n == 60 and a.freq_mean == pytest.approx(mean) and a.freq_min == min(vals) and a.freq_max == max(vals)
        assert a.freq_std == pytest.approx((sum((v - mean) ** 2 for v in vals) / 60) ** 0.5)
    # the 0.5 Hz step in minute 1 (readings 100..104) is what the max |RoCoF| column preserves
    assert aggs[first_minute + 1].rocof_max_abs == pytest.approx(0.5, abs=0.05)
    assert aggs[first_minute + 5].rocof_max_abs < 0.05


def test_unlocked_readings_are_exported_counted_per_minute_and_pruned(env):
    store, _cfg, eng, _c, _ = env
    fill_day(store, 20, n=120)
    recv = day_start(20) + 7200.0
    store.ingest(parse_payload(v1_batch("unit-1", [v1_reading(50.0 + i * 0.001, None) for i in range(5)])), recv)
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.export_rows == 125 and st.pruned_done and store.day_row_counts("unit-1", DAY(20)) == (0, 0)
    _hdr, rows = read_export(st.export_path)
    assert sum(1 for r in rows if r[3] == "") == 5                            # gps_utc_us empty for unlocked rows
    aggs = store.aggregates("unit-1", int(recv // 60), int(recv // 60), 5)
    assert aggs[0].n == 0 and aggs[0].n_unlocked == 5 and aggs[0].freq_mean is None


# --- events are kept -------------------------------------------------------------------------

def test_raw_rows_around_a_rocof_event_or_a_band_excursion_are_kept_and_the_rest_pruned(env):
    store, _cfg, eng, _c, _ = env

    def f(i):
        if 2000 <= i < 2006:
            return 50.6                                     # step: |RoCoF| ~0.6 Hz/s at the edges
        if 6000 <= i < 6003:
            return 49.7                                      # below the 49.85 band, but a slow ramp
        return 50.0
    fill_day(store, 20, n=9000, freq=f, start_offset=0.0)
    eng.run_until_idle()
    start = day_start(20)
    rows = store.history("unit-1", 0, 2**62, 1_000_000).rows
    kept_t = [r.gps_utc_us / US - start for r in rows]
    assert len(rows) < 9000 and store.day_states("unit-1")[DAY(20)].pruned_done
    ev = store.events("unit-1")
    assert 1 <= len(ev) <= 2 and any("rocof" in e["reason"] for e in ev) and any("freq_band" in e["reason"] for e in ev)
    # rows within +/-300 s of the first trigger stay; rows just outside its window go
    assert all(t in kept_t for t in range(2000 - 300, 2005 + 300 + 1, 50))
    assert 2000 - 320 not in kept_t and 2006 + 320 not in kept_t
    assert 100 not in kept_t and 4000 not in kept_t and 8500 not in kept_t
    # the export still holds ALL 9000 rows (it is written before anything is pruned)
    assert store.day_states("unit-1")[DAY(20)].export_rows == 9000
    # and the events table carries the peak RoCoF
    assert max(e["peak_abs_rocof"] or 0 for e in ev) > 0.4


def test_event_thresholds_are_configurable(env):
    store, cfg, _eng, clock, _ = env
    fill_day(store, 20, n=600, start_offset=0.0, freq=lambda i: 50.6 if 300 <= i < 305 else 50.0)
    cfg2 = RetentionConfig(export_dir=cfg.export_dir, rocof_event_hz_s=5.0, freq_lo=49.0, freq_hi=51.0,
                           export_chunk_rows=300, prune_chunk_rows=200)
    RetentionEngine(store, cfg2, clock).run_until_idle()
    assert store.events("unit-1") == [] and store.day_row_counts("unit-1", DAY(20)) == (0, 0)


# --- the guards: never prune what isn't verified -------------------------------------------------

def _run_until(eng, store, day, predicate, limit=2000):
    for _ in range(limit):
        st = store.day_states("unit-1").get(day)
        if st is not None and predicate(st):
            return st
        if eng.step() is None:
            break
    raise AssertionError("condition never reached")


def test_a_missing_export_file_blocks_pruning(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=400)
    st = _run_until(eng, store, DAY(20), lambda s: s.export_done and s.agg_done)
    os.remove(st.export_path)
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.attention and "export file missing" in st.attention and not st.pruned_done
    assert store.day_row_counts("unit-1", DAY(20)) == (400, 0)                 # nothing deleted
    assert store.health()["days_needing_attention"][0]["day"] == DAY(20)


def test_a_modified_export_file_blocks_pruning(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=400)
    st = _run_until(eng, store, DAY(20), lambda s: s.export_done and s.agg_done)
    with open(st.export_path, "ab") as fh:
        fh.write(b"tampered")
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.attention and "checksum" in st.attention and store.day_row_counts("unit-1", DAY(20)) == (400, 0)


def test_aggregates_that_disagree_with_the_database_block_pruning(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=400)
    _run_until(eng, store, DAY(20), lambda s: s.export_done and s.agg_done)
    db = sqlite3.connect(store.path)
    db.execute("UPDATE readings_1min SET n = n + 1 WHERE minute = (SELECT min(minute) FROM readings_1min)")
    db.commit()
    db.close()
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.attention and "aggregates" in st.attention and store.day_row_counts("unit-1", DAY(20)) == (400, 0)


def test_a_row_that_appears_after_the_export_blocks_pruning(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=400)
    _run_until(eng, store, DAY(20), lambda s: s.export_done and s.agg_done)
    late = day_start(20) + 3600 + 5000                                   # a reading the export never saw
    store.ingest(parse_payload(v2_series("unit-1", BOOT, late, 1, seq0=99_999)), late + 2)
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.attention and store.day_row_counts("unit-1", DAY(20)) == (401, 0) and not st.pruned_done


def test_a_day_needing_attention_is_never_retried_or_pruned(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=100)
    st = _run_until(eng, store, DAY(20), lambda s: s.export_done and s.agg_done)
    os.remove(st.export_path)
    eng.run_until_idle()
    assert eng.step() is None and store.day_row_counts("unit-1", DAY(20)) == (100, 0)


# --- resumability ---------------------------------------------------------------------------------

def test_export_resumes_after_a_restart_between_chunks(env):
    store, cfg, eng, clock, _ = env
    fill_day(store, 20, n=1000, seq0=0)
    for _ in range(2):
        assert eng.step().startswith("export:")                 # two 300-row chunks done, 400 to go
    eng2 = RetentionEngine(store, cfg, clock)                     # "process restarted"
    eng2.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.pruned_done and st.export_rows == 1000 and not st.attention
    _h, rows = read_export(st.export_path)
    assert [int(r[2]) for r in rows] == list(range(1000))         # each row exactly once, in order


def test_bytes_written_after_the_last_saved_state_are_discarded_on_resume(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=1000, seq0=0)
    assert eng.step().startswith("export:")
    partial = eng.export_path("unit-1", DAY(20)) + ".partial"
    with gzip.open(partial, "ab") as gz:                           # crash: a chunk written, state never saved
        gz.write(b"unit-1,bogus,999,1,1,2,50.0,,1,0,1.0\n" * 50)
    eng.run_until_idle()
    st = store.day_states("unit-1")[DAY(20)]
    assert st.export_rows == 1000 and st.pruned_done and not st.attention
    _h, rows = read_export(st.export_path)
    assert len(rows) == 1000 and all(r[1] == BOOT for r in rows)


def test_empty_days_between_data_create_no_files_and_the_engine_settles_to_idle(env):
    store, cfg, eng, _cl, _ = env
    fill_day(store, 20, n=50)
    fill_day(store, 16, n=50)                           # days 19, 18, 17 in between hold nothing
    eng.run_until_idle()
    states = store.day_states("unit-1")
    assert set(states) == {DAY(20), DAY(16)} and all(s.pruned_done for s in states.values())
    files = [f for _r, _d, fs in os.walk(cfg.export_dir) for f in fs]
    assert len(files) == 2 and eng.step() is None


def test_an_empty_closed_day_after_the_oldest_row_is_recorded_once_without_a_file(env):
    store, cfg, eng, _cl, _ = env
    fill_day(store, 20, n=50)
    fill_day(store, 16, n=50)
    fill_day(store, 3, n=50)                            # recent: keeps the scan going past the empty days
    # keep day 20 from being pruned (so it stays the oldest row) by making it too young to prune
    cfg2 = RetentionConfig(export_dir=cfg.export_dir, raw_days=40, export_chunk_rows=300, prune_chunk_rows=200)
    e2 = RetentionEngine(store, cfg2, FakeClock(NOW))
    e2.run_until_idle()
    st = store.day_states("unit-1")
    assert st[DAY(18)].pruned_done and st[DAY(18)].export_path is None and st[DAY(18)].export_rows == 0
    assert not st[DAY(20)].pruned_done                  # raw rows kept: younger than raw_days=40
    assert e2.step() is None


# --- scheduling ---------------------------------------------------------------------------------------

def test_maybe_step_is_rate_limited_single_flight_and_never_raises(env, monkeypatch):
    store, cfg, eng, clock, _ = env
    fill_day(store, 20, n=100)
    assert eng.maybe_step() is not None and eng.maybe_step() is None      # second call inside min_interval_s
    clock.advance(cfg.min_interval_s + 1)
    assert eng._lock.acquire(blocking=False)
    try:
        assert eng.maybe_step() is None                                   # another run in flight
    finally:
        eng._lock.release()
    clock.advance(cfg.min_interval_s + 1)
    monkeypatch.setattr(eng, "step", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert eng.maybe_step() is None                                        # swallowed, ingest unaffected


def test_a_step_does_a_bounded_amount_of_work(env):
    store, _c, eng, _cl, _ = env
    fill_day(store, 20, n=1000)
    res = eng.run(budget_s=0.0)                                            # zero budget: exactly one chunk
    assert len(res["work"]) == 1 and res["idle"] is False


def test_old_ingest_log_rows_are_pruned(env):
    store, cfg, eng, clock, _ = env
    store.ingest(parse_payload(v2_series("unit-1", BOOT, NOW - 40 * 86400, 3)), NOW - 40 * 86400 + 4)
    store.ingest(parse_payload(v2_series("unit-1", BOOT, NOW - 100, 3, seq0=10)), NOW - 96)
    out = eng.run_until_idle()
    assert any(o.startswith("prune_ingests:1") for o in out)
    db = sqlite3.connect(store.path)
    assert db.execute("SELECT count(*) FROM ingests").fetchone()[0] == 1


# --- CLI ------------------------------------------------------------------------------------------------------

def test_cli_status_run_and_prune_exports(env, capsys):
    store, cfg, _eng, _cl, tmp = env
    fill_day(store, 20, n=100)
    args = ["--db", store.path, "--export-dir", cfg.export_dir]
    assert _cli(args + ["status"]) == 0 and '"backend": "sqlite"' in capsys.readouterr().out
    # (the CLI uses the real clock, under which the day is ancient and prunable)
    assert _cli(args + ["run", "--max-seconds", "20"]) == 0
    assert "idle" in capsys.readouterr().out
    assert _cli(args + ["prune-exports", "--older-than-days", "0"]) == 2          # refuses without confirmation
    files = [os.path.join(r, f) for r, _d, fs in os.walk(cfg.export_dir) for f in fs]
    assert files
    assert _cli(args + ["prune-exports", "--older-than-days", "0", "--confirm-downloaded"]) == 0
    assert not [os.path.join(r, f) for r, _d, fs in os.walk(cfg.export_dir) for f in fs]


def test_day_to_date_round_trip():
    assert day_to_date(DAY(0)).isoformat() == "2026-09-25"
