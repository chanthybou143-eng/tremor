from __future__ import annotations

import os
import sqlite3

import pytest

from helpers import T0, US, v1_batch, v1_reading, v1_series, v2_batch, v2_reading, v2_series
from tremor.ingest import FLAG_TIME_IMPLAUSIBLE, FLAG_UNLOCKED, parse_payload
from tremor.store import SqliteReadingStore, StoreError, open_store

BOOT = "9f3a51c07d2e4b18"


@pytest.fixture
def store(tmp_path):
    return open_store(str(tmp_path / "readings.db"))


def ingest(store, payload, received_at):
    return store.ingest(parse_payload(payload), received_at)


def all_rows(store, unit="unit-1"):
    return store.history(unit, 0, 2**62, 100_000, include_unlocked=True)


# --- basics -------------------------------------------------------------------

def test_uses_the_default_rollback_journal_not_wal(store):
    db = sqlite3.connect(store.path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_v1_readings_are_stored_under_their_own_gps_time_not_receipt_time(store):
    res = ingest(store, v1_series("unit-1", T0, 5), received_at=T0 + 30.0)
    assert (res.accepted, res.inserted, res.duplicates) == (5, 5, 0)
    rows = all_rows(store).rows
    assert [round(r.gps_utc_us / US - T0) for r in rows] == [0, 1, 2, 3, 4]
    assert all(r.received_at == T0 + 30.0 and r.time_src == 1 and r.flags == 0 and r.boot_id is None for r in rows)


def test_v2_readings_are_stored_with_exact_microsecond_time_boot_and_seq(store):
    ingest(store, v2_series("unit-1", BOOT, T0 + 0.000123, 3, seq0=40), received_at=T0 + 5)
    rows = all_rows(store).rows
    assert [r.seq for r in rows] == [40, 41, 42] and all(r.boot_id == BOOT and r.time_src == 2 for r in rows)
    assert rows[0].gps_utc_us == int(round((T0 + 0.000123) * US))


# --- deduplication ---------------------------------------------------------------

def test_exact_retry_of_a_v2_batch_is_ignored(store):
    batch = v2_series("unit-1", BOOT, T0, 28)
    a = ingest(store, batch, T0 + 30)
    b = ingest(store, batch, T0 + 90)                  # the device never saw the 202 and re-sent it
    assert (a.inserted, b.inserted, b.duplicates) == (28, 0, 28)
    assert len(all_rows(store).rows) == 28


def test_partial_overlap_retry_stores_each_reading_once(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 28, seq0=0), T0 + 30)              # seq 0..27 (server got these)
    retry = v2_series("unit-1", BOOT, T0, 56, seq0=0)                              # device resends 0..55
    res = ingest(store, retry, T0 + 90)
    assert (res.accepted, res.inserted, res.duplicates) == (56, 28, 28)
    rows = all_rows(store).rows
    assert [r.seq for r in rows] == list(range(56))
    # the 28 already-stored rows keep their ORIGINAL receipt time
    assert {r.received_at for r in rows[:28]} == {T0 + 30} and {r.received_at for r in rows[28:]} == {T0 + 90}


def test_legacy_v1_retry_is_deduped_on_unit_and_gps_time(store):
    batch = v1_series("unit-1", T0, 28)
    ingest(store, batch, T0 + 30)
    res = ingest(store, batch, T0 + 91)
    assert (res.inserted, res.duplicates) == (0, 28)
    half = ingest(store, v1_series("unit-1", T0 + 14, 28), T0 + 120)               # 14 old + 14 new
    assert (half.inserted, half.duplicates) == (14, 14)
    assert len(all_rows(store).rows) == 42


def test_legacy_retry_that_lands_after_utc_midnight_is_still_a_duplicate(store):
    from helpers import utc
    midnight = utc(2026, 9, 25)
    batch = v1_series("unit-1", midnight - 20, 20)                                  # 23:59:40 .. 23:59:59
    ingest(store, batch, midnight - 0.5)
    res = ingest(store, batch, midnight + 60)
    assert (res.inserted, res.duplicates) == (0, 20)


def test_same_seq_from_a_different_boot_or_unit_is_a_different_reading(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 3, seq0=0), T0 + 5)
    ingest(store, v2_series("unit-1", "aaaaaaaaaaaaaaaa", T0 + 100, 3, seq0=0), T0 + 105)   # rebooted: seq restarts
    ingest(store, v2_series("unit-2", BOOT, T0, 3, seq0=0), T0 + 5)
    assert len(all_rows(store, "unit-1").rows) == 6 and len(all_rows(store, "unit-2").rows) == 3


def test_a_seq_repeated_inside_one_batch_is_stored_once(store):
    b = v2_series("unit-1", BOOT, T0, 3)
    b["readings"].append(dict(b["readings"][0]))
    assert ingest(store, b, T0 + 5).inserted == 3


# --- ordering, restart, windows ----------------------------------------------------

def test_out_of_order_batches_come_back_in_gps_order(store):
    ingest(store, v2_series("unit-1", BOOT, T0 + 60, 5, seq0=60), T0 + 66)          # newer batch arrives first
    ingest(store, v2_series("unit-1", BOOT, T0, 5, seq0=0), T0 + 70)                # older (retried) batch later
    ingest(store, v2_series("unit-1", BOOT, T0 + 30, 5, seq0=30), T0 + 72)
    ts = [r.gps_utc_us for r in all_rows(store).rows]
    assert ts == sorted(ts) and len(ts) == 15
    assert [r.seq for r in all_rows(store).rows] == [0, 1, 2, 3, 4, 30, 31, 32, 33, 34, 60, 61, 62, 63, 64]


def test_restart_keeps_history_and_dedupe(tmp_path):
    path = str(tmp_path / "r.db")
    s1 = open_store(path)
    ingest(s1, v2_series("unit-1", BOOT, T0, 10), T0 + 12)
    ingest(s1, v1_series("unit-9", T0, 4), T0 + 5)
    s1.close()
    s2 = open_store(path)                                   # "new app instance, same DB"
    assert len(all_rows(s2).rows) == 10 and len(all_rows(s2, "unit-9").rows) == 4
    assert [u.unit_id for u in s2.unit_states()] == ["unit-9", "unit-1"]      # ordered by first_seen (unit-9 arrived first)
    assert ingest(s2, v2_series("unit-1", BOOT, T0, 10), T0 + 100).inserted == 0        # dedupe survives restart
    assert ingest(s2, v1_series("unit-9", T0, 4), T0 + 100).inserted == 0


def test_window_is_relative_to_the_units_newest_gps_time_not_the_wall_clock(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 200), T0 + 210)
    w = store.window("unit-1", 60.0)
    assert len(w) == 60 and w[0].gps_utc_us > w[-1].gps_utc_us - 60 * US
    assert store.window("nobody", 60.0) == []


# --- unlocked / implausible ---------------------------------------------------------

def test_unlocked_readings_are_stored_flagged_and_never_given_a_time(store):
    b = v1_batch("unit-1", [v1_reading(50.0, None), v1_reading(50.01, T0), v1_reading(50.02, None)])
    res = ingest(store, b, T0 + 3)
    assert (res.inserted, res.unlocked) == (3, 2)
    page = all_rows(store)
    assert len(page.rows) == 1 and len(page.unlocked) == 2
    assert all(r.gps_utc_us is None and r.flags & FLAG_UNLOCKED and r.gps_locked == 0 for r in page.unlocked)
    assert store.unit_states()[0].unlocked_total == 2
    assert len(store.window("unit-1", 60.0)) == 1              # not plotted


def test_unlocked_legacy_readings_are_deliberately_not_deduplicated_by_content(store):
    b = v1_batch("unit-1", [v1_reading(50.0, None, amp=0.7)])
    ingest(store, b, T0)
    ingest(store, b, T0 + 60)                                   # identical content, retried
    assert len(all_rows(store).unlocked) == 2


def test_unlocked_v2_readings_ARE_deduped_because_they_have_a_seq(store):
    b = v2_batch("unit-1", BOOT, [v2_reading(50.0, 0, None), v2_reading(50.01, 1, None)])
    ingest(store, b, T0)
    assert ingest(store, b, T0 + 60).inserted == 0
    assert len(all_rows(store).unlocked) == 2


def test_implausible_time_is_flagged_and_excluded_from_the_series(store):
    old = v2_series("unit-1", BOOT, T0 - 7200, 2, seq0=0)               # claims to be 2 h old
    ok = v2_series("unit-1", BOOT, T0, 2, seq0=2)
    res = ingest(store, {**ok, "readings": old["readings"] + ok["readings"]}, T0 + 2)
    assert (res.inserted, res.implausible) == (4, 2)
    rows = all_rows(store)
    assert len(rows.rows) == 2 and all(r.gps_utc_us is not None for r in rows.rows)
    assert len(rows.unlocked) == 2 and all(r.flags & FLAG_TIME_IMPLAUSIBLE and r.gps_utc_us is None for r in rows.unlocked)
    assert rows.unlocked[0].gps_raw == pytest.approx(T0 - 7200)         # raw claim kept for audit
    assert store.unit_states()[0].implausible_total == 2


# --- unit state -----------------------------------------------------------------------

def test_unit_state_tracks_last_reception_gps_lock_and_counters(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 5), T0 + 6)
    ingest(store, v2_series("unit-1", BOOT, T0, 5), T0 + 70)            # all duplicates: unit is still alive
    u = store.unit_states()[0]
    assert u.last_received_at == T0 + 70 and u.readings_total == 5 and u.duplicates_total == 5
    assert u.last_gps_locked and u.last_gps_us == int(round((T0 + 4) * US)) and u.last_boot_id == BOOT
    ingest(store, v1_batch("unit-1", [v1_reading(50.0, None)]), T0 + 100)
    assert store.unit_states()[0].last_gps_locked is False              # newest reading had no GPS


def test_a_late_old_batch_does_not_move_the_units_newest_gps_time_backwards(store):
    ingest(store, v2_series("unit-1", BOOT, T0 + 60, 5, seq0=60), T0 + 66)
    ingest(store, v2_series("unit-1", BOOT, T0, 5, seq0=0), T0 + 90)
    assert store.unit_states()[0].last_gps_us == int(round((T0 + 64) * US))


# --- history paging ----------------------------------------------------------------------

def test_history_limit_truncation_and_paging_return_every_row_exactly_once(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 250), T0 + 260)
    seen, frm, after = [], 0, 0
    for _ in range(20):
        page = store.history("unit-1", frm, 2**62, 100, after_id=after)
        seen += [r.seq for r in page.rows]
        assert len(page.rows) <= 100
        if not page.truncated:
            break
        frm, after = page.next_from_us, page.next_after_id
    assert seen == list(range(250))


def test_history_range_is_inclusive_and_filters_by_unit(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 10), T0 + 12)
    ingest(store, v2_series("unit-2", BOOT, T0, 10), T0 + 12)
    lo, hi = int((T0 + 2) * US), int((T0 + 5) * US)
    rows = store.history("unit-1", lo - 1, hi, 100).rows
    assert [r.seq for r in rows] == [2, 3, 4, 5] and {r.unit_id for r in rows} == {"unit-1"}


# --- failures ---------------------------------------------------------------------------------

def test_a_locked_database_raises_store_error_so_the_device_retries(tmp_path):
    path = str(tmp_path / "r.db")
    s = SqliteReadingStore(path, busy_timeout_s=0.1)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StoreError):
            ingest(s, v2_series("unit-1", BOOT, T0, 3), T0)
        with pytest.raises(StoreError):
            s.unit_states()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert ingest(s, v2_series("unit-1", BOOT, T0, 3), T0).inserted == 3    # and works again afterwards


def test_a_failed_ingest_leaves_nothing_half_written(store, monkeypatch):
    import tremor.store as st
    calls = {"n": 0}
    real = st.resolve_time

    def boom(r, received_at):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(r, received_at)
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(st, "resolve_time", boom)
    with pytest.raises(sqlite3.OperationalError):
        ingest(store, v2_series("unit-1", BOOT, T0, 5), T0)
    monkeypatch.undo()
    assert all_rows(store).rows == [] and store.unit_states() == []


def test_refuses_a_database_from_a_newer_schema(tmp_path):
    path = str(tmp_path / "r.db")
    open_store(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    with pytest.raises(StoreError):
        open_store(path)


def test_health_reports_sizes_counts_and_attention(store):
    ingest(store, v2_series("unit-1", BOOT, T0, 30), T0 + 31)
    h = store.health()
    assert h["backend"] == "sqlite" and h["raw_rows"] == 30 and h["db_bytes"] > 0
    assert h["days_needing_attention"] == [] and os.path.exists(store.path)
