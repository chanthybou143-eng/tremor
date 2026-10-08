"""Per-POST device telemetry (ingest.TELEMETRY_FIELDS -> the telemetry table -> /api/health), and the
late-arrival horizons that a 60-minute device backlog depends on (ingest.MAX_AGE_S, retention settle_s)."""

from __future__ import annotations

import sqlite3

import pytest

from helpers import FakeClock, T0, utc, v2_series
from tremor.ingest import FUTURE_SKEW_S, MAX_AGE_S, TELEMETRY_FIELDS, parse_payload, parse_telemetry
from tremor.retention import RetentionConfig
from tremor.store import SCHEMA_VERSION, open_store
from tremor.webapp import create_app

BOOT = "3bf8ae13fd84b5d0"

GOOD = {"rssi_dbm": -67, "die_temp_c": 31.4, "backlog": 412, "dropped_total": 0, "skipped_chunks_total": 7,
        "wifi_reconnects_total": 2, "last_post_ms": 2350, "heap_free": 301_234, "uptime_s": 86_400,
        "adc_overflow_total": 0, "post_aborts_total": 1, "slow_posts_total": 0, "pps_spread_us_max": 103}


@pytest.fixture
def ctx(tmp_path):
    clock = FakeClock(T0)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "readings.db"), clock=clock,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "exports")))
    yield app.test_client(), clock, app, tmp_path
    app.config["TREMOR_SHUTDOWN"]()


def health_unit(client, uid="unit-1"):
    return next(u for u in client.get("/api/health").get_json()["units"] if u["unit_id"] == uid)


# --- parsing: best effort, never an error ------------------------------------------------------

def test_every_known_field_is_kept():
    assert parse_telemetry(GOOD) == GOOD
    assert set(GOOD) == set(TELEMETRY_FIELDS)


@pytest.mark.parametrize("bad", [None, "rssi=-60", 42, [1, 2], {}, {"unknown": 1}])
def test_a_missing_or_malformed_object_is_simply_absent(bad):
    assert parse_telemetry(bad) is None


def test_bad_values_are_dropped_one_by_one_and_the_rest_kept():
    t = parse_telemetry({"rssi_dbm": "strong", "die_temp_c": float("nan"), "backlog": -1, "dropped_total": True,
                         "last_post_ms": 2.5, "heap_free": 1e12, "uptime_s": 10, "wifi_reconnects_total": None,
                         "skipped_chunks_total": {"x": 1}, "post_aborts_total": [0], "unknown_field": 3})
    assert t == {"uptime_s": 10}


def test_integer_valued_temperature_is_accepted_as_a_float():
    assert parse_telemetry({"die_temp_c": 30}) == {"die_temp_c": 30.0}


def test_parse_payload_carries_telemetry_and_never_rejects_a_batch_because_of_it():
    b = v2_series("unit-1", BOOT, T0 - 5, 5)
    assert parse_payload(b).telemetry is None
    assert parse_payload({**b, "telemetry": GOOD}).telemetry == GOOD
    for junk in ("x", 7, [None], {"rssi_dbm": "x"}, {"a": {"b": "c"}}):
        assert len(parse_payload({**b, "telemetry": junk}).readings) == 5


# --- storage and /api/health -------------------------------------------------------------------------

def test_telemetry_is_stored_per_post_and_the_latest_per_unit_is_in_health(ctx):
    client, clock, _app, _ = ctx
    assert client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 5, 5)).status_code == 202
    assert health_unit(client)["telemetry"] is None                         # never sent any yet
    assert client.post("/api/ingest", json={**v2_series("unit-1", BOOT, T0 - 4, 5, seq0=5),
                                            "telemetry": GOOD}).status_code == 202
    clock.advance(30)
    newer = dict(GOOD, backlog=12, rssi_dbm=-71)
    assert client.post("/api/ingest", json={**v2_series("unit-2", "aaaa", T0 + 25, 3), "telemetry": {"rssi_dbm": -50}}
                       ).status_code == 202
    assert client.post("/api/ingest", json={**v2_series("unit-1", BOOT, T0 + 26, 5, seq0=10),
                                            "telemetry": newer}).status_code == 202
    clock.advance(4)
    t = health_unit(client)["telemetry"]
    assert {k: t[k] for k in TELEMETRY_FIELDS} == newer
    assert t["boot_id"] == BOOT and t["received_at"] == T0 + 30 and t["seconds_since"] == 4.0
    t2 = health_unit(client, "unit-2")["telemetry"]
    assert t2["rssi_dbm"] == -50 and t2["backlog"] is None                 # fields not sent are null


def test_junk_telemetry_never_costs_a_reading(ctx):
    client, _clock, app, _ = ctx
    r = client.post("/api/ingest", json={**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": {"rssi_dbm": "??"}})
    assert r.status_code == 202 and r.get_json()["inserted"] == 5
    r = client.post("/api/ingest", json={**v2_series("unit-1", BOOT, T0 - 3, 2, seq0=5), "telemetry": "garbage"})
    assert r.status_code == 202 and r.get_json()["inserted"] == 2
    assert health_unit(client)["telemetry"] is None
    assert app.config["TREMOR_STORE"].latest_telemetry() == {}


def test_telemetry_is_pruned_with_the_ingest_log(tmp_path):
    store = open_store(str(tmp_path / "r.db"))
    store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0)
    store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 + 95, 5, seq0=5), "telemetry": GOOD}), T0 + 100)
    store.prune_ingests(T0 + 50)
    with sqlite3.connect(str(tmp_path / "r.db")) as db:
        assert db.execute("SELECT received_at FROM telemetry").fetchall() == [(T0 + 100,)]


# --- schema: added without a version bump, so a rollback still starts ------------------------------------

def test_the_table_is_added_to_an_existing_database_without_changing_its_version(tmp_path):
    path = str(tmp_path / "r.db")
    open_store(path)
    with sqlite3.connect(path) as db:                                      # a database from before this change
        db.execute("DROP TABLE telemetry")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    store = open_store(path)                                               # the new code opens it ...
    store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0)
    assert store.latest_telemetry()["unit-1"]["rssi_dbm"] == -67
    with sqlite3.connect(path) as db:                                      # ... and leaves the version alone,
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1 == SCHEMA_VERSION   # so older code still opens it


# --- late arrivals: a 60-minute device backlog must still reach the permanent aggregates -------------------

def test_settle_delay_exceeds_the_age_horizon():
    assert RetentionConfig(export_dir="x").settle_s > MAX_AGE_S + FUTURE_SKEW_S


def test_readings_delivered_hours_late_after_midnight_still_reach_the_aggregates_with_no_attention(tmp_path):
    clock = FakeClock(utc(2026, 10, 6, 23, 0))
    app = create_app(simulated_units=[], db_path=str(tmp_path / "readings.db"), clock=clock,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "exports")))
    client = app.test_client()
    eng, store = app.config["TREMOR_RETENTION"], app.config["TREMOR_STORE"]
    start = utc(2026, 10, 6, 22, 0)
    for k in range(60):                                                    # 22:00-23:00 delivered on time
        clock.t = start + k * 60 + 61
        assert client.post("/api/ingest", json=v2_series("unit-1", BOOT, start + k * 60, 60, seq0=k * 60)).status_code == 202
    # 23:00-24:00 is measured during an outage and only delivered from 05:00 the next morning (5-6 h late)
    clock.t = utc(2026, 10, 7, 5, 0)
    eng.run_until_idle()                                                   # the day must not be processed yet
    late0 = utc(2026, 10, 6, 23, 0)
    for k in range(60):
        clock.t = utc(2026, 10, 7, 5, 0) + k * 10
        r = client.post("/api/ingest", json=v2_series("unit-1", BOOT, late0 + k * 60, 60, seq0=3600 + k * 60))
        assert r.status_code == 202 and r.get_json()["implausible"] == 0
    clock.t = utc(2026, 10, 7, 8, 0)
    eng.run_until_idle()
    st = store.day_states("unit-1")[(int(late0) // 86400)]
    assert st.export_done and st.agg_done and st.attention is None and st.export_rows == 7200
    aggs = store.aggregates("unit-1", int(start // 60), int(start // 60) + 120, 1000)
    assert sum(a.n for a in aggs) == 7200
    app.config["TREMOR_SHUTDOWN"]()


# --- best effort: a telemetry failure never costs a reading -------------------------------------------------

def _rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT count(*) FROM readings").fetchone()[0], db.execute("SELECT count(*) FROM telemetry").fetchone()[0]


def test_readings_commit_when_the_telemetry_insert_fails(tmp_path):
    path = str(tmp_path / "r.db")
    store = open_store(path)
    with sqlite3.connect(path) as db:                                      # every telemetry insert now fails
        db.execute("CREATE TRIGGER boom BEFORE INSERT ON telemetry BEGIN SELECT RAISE(ABORT, 'disk says no'); END")
    res = store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0)
    assert res.inserted == 5 and _rows(path) == (5, 0) and store.telemetry_errors == 1
    u = store.unit_states()[0]
    assert u.readings_total == 5 and u.last_received_at == T0              # the rest of the transaction committed too
    store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0 + 1)   # a retry
    assert _rows(path) == (5, 0)                                           # still deduplicated normally


def test_readings_commit_when_the_telemetry_table_is_missing(tmp_path):
    path = str(tmp_path / "r.db")
    store = open_store(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE telemetry")
    res = store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0)
    assert res.inserted == 5 and store.telemetry_errors == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM readings").fetchone()[0] == 5


def test_the_http_answer_is_still_202_and_health_counts_the_skipped_telemetry(ctx):
    client, _clock, app, tmp = ctx
    with sqlite3.connect(str(tmp / "readings.db")) as db:
        db.execute("CREATE TRIGGER boom BEFORE INSERT ON telemetry BEGIN SELECT RAISE(ABORT, 'x'); END")
    r = client.post("/api/ingest", json={**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD})
    assert r.status_code == 202 and r.get_json()["inserted"] == 5
    h = client.get("/api/health").get_json()
    assert h["store"]["telemetry_insert_errors"] == 1 and h["units"][0]["readings_total"] == 5


# --- a field added after the table was created gets its column at startup ------------------------------

def test_an_existing_telemetry_table_gains_the_new_column_at_startup_without_a_version_bump(tmp_path):
    path = str(tmp_path / "r.db")
    open_store(path)
    with sqlite3.connect(path) as db:                                      # the table as first deployed (no pps column)
        db.execute("DROP TABLE telemetry")
        db.execute("CREATE TABLE telemetry (id INTEGER PRIMARY KEY, ingest_id INTEGER, unit_id TEXT NOT NULL, "
                   "boot_id TEXT, received_at REAL NOT NULL, rssi_dbm INTEGER, backlog INTEGER)")
        db.execute("INSERT INTO telemetry(unit_id, received_at, rssi_dbm, backlog) VALUES ('unit-1', 1.0, -70, 3)")
    store = open_store(path)
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(telemetry)")}
        assert set(TELEMETRY_FIELDS) <= cols
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1 == SCHEMA_VERSION
        assert db.execute("SELECT rssi_dbm, backlog, pps_spread_us_max FROM telemetry").fetchall() == [(-70, 3, None)]
    store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0)
    assert store.latest_telemetry()["unit-1"]["pps_spread_us_max"] == 103 and store.telemetry_errors == 0
    open_store(path)                                                       # idempotent on the next start


def test_a_column_that_cannot_be_added_is_left_out_of_inserts_not_fatal(tmp_path):
    path = str(tmp_path / "r.db")
    store = open_store(path)
    store._telemetry_cols.discard("pps_spread_us_max")                     # as if ALTER TABLE had failed
    store.ingest(parse_payload({**v2_series("unit-1", BOOT, T0 - 5, 5), "telemetry": GOOD}), T0)
    t = store.latest_telemetry()["unit-1"]
    assert t["rssi_dbm"] == -67 and t["pps_spread_us_max"] is None and store.telemetry_errors == 0
