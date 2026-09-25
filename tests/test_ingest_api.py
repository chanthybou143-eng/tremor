from __future__ import annotations

import gzip
import os
import sqlite3

import pytest

from helpers import FakeClock, T0, US, utc, v1_batch, v1_reading, v1_series, v2_batch, v2_reading, v2_series
from tremor.retention import RetentionConfig
from tremor.store import StoreError
from tremor.webapp import HISTORY_MAX_LIMIT, create_app

BOOT = "9f3a51c07d2e4b18"
EXPORT_TOKEN = "export-token-0123456789abcdef"
AUTH = {"X-Tremor-Token": EXPORT_TOKEN}


@pytest.fixture
def ctx(tmp_path):
    clock = FakeClock(T0)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "readings.db"), clock=clock, export_token=EXPORT_TOKEN,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "exports"), raw_days=14,
                                                      export_chunk_rows=500, prune_chunk_rows=500))
    yield app.test_client(), clock, app, tmp_path
    app.config["TREMOR_SHUTDOWN"]()


def unit(client, uid="unit-1"):
    return next(u for u in client.get("/api/units").get_json() if u["id"] == uid)


# --- both payload generations -----------------------------------------------------------------

def test_a_legacy_payload_exactly_as_unit_1_sends_it_today_is_accepted(ctx):
    client, clock, _app, _ = ctx
    body = {"unit_id": "unit-1", "readings": [
        {"frequency_hz": 50.0123, "amplitude_v": 0.744, "gps_utc_s": float(f"{(T0 - 3 + i) % 86400:.3f}")}
        for i in range(3)]}
    r = client.post("/api/ingest", json=body)
    assert r.status_code == 202
    assert r.get_json() == {"accepted": 3, "inserted": 3, "duplicates": 0, "unlocked": 0, "implausible": 0}
    u = unit(client)
    assert u["status"] == "live" and u["gps_locked"] is True and len(u["history"]) == 3


def test_a_v2_payload_with_integer_time_is_accepted_and_exact(ctx):
    client, clock, app, _ = ctx
    b = v2_series("unit-1", BOOT, T0 - 5 + 0.000123, 5)
    assert client.post("/api/ingest", json=b).status_code == 202
    rows = client.get("/api/history", query_string={"unit": "unit-1", "resolution": "raw"}).get_json()["readings"]
    assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4] and rows[0]["gps_utc_us"] == int(round((T0 - 5 + 0.000123) * US))
    assert all(r["boot_id"] == BOOT and r["time_src"] == 2 for r in rows)


def test_v2_prefers_the_integer_time_over_a_float_alongside_it(ctx):
    client, _c, _a, _ = ctx
    b = v2_series("unit-1", BOOT, T0 - 3, 2)
    for r in b["readings"]:
        r["gps_utc_s"] = 12345.0                              # a stale/wrong float32 must be ignored
    assert client.post("/api/ingest", json=b).status_code == 202
    assert unit(client)["gps_utc"] == pytest.approx(T0 - 2, abs=1e-6)


@pytest.mark.parametrize("bad", [
    {"unit_id": "unit-1", "boot_id": BOOT, "readings": [{"frequency_hz": 50.0}]},                     # boot_id, no seq
    {"unit_id": "unit-1", "readings": [{"frequency_hz": 50.0, "seq": 1}]},                            # seq, no boot_id
    {"unit_id": "unit 1", "readings": [{"frequency_hz": 50.0}]},
    {"unit_id": "unit-1", "boot_id": BOOT, "readings": [{"frequency_hz": 50.0, "seq": 0, "gps": [1, 2]}]},
])
def test_inconsistent_payloads_get_a_400_and_store_nothing(ctx, bad):
    client, *_ = ctx
    r = client.post("/api/ingest", json=bad)
    assert r.status_code == 400 and "error" in r.get_json()
    assert client.get("/api/units").get_json() == []


def test_a_non_json_body_gets_a_400(ctx):
    client, *_ = ctx
    assert client.post("/api/ingest", data="nope", content_type="text/plain").status_code == 400


# --- retries and dedupe ---------------------------------------------------------------------------

def test_an_exact_retry_is_acknowledged_but_stored_once(ctx):
    client, clock, _a, _ = ctx
    b = v2_series("unit-1", BOOT, T0 - 30, 28)
    first = client.post("/api/ingest", json=b).get_json()
    clock.advance(60)
    again = client.post("/api/ingest", json=b)
    assert again.status_code == 202                                   # the device must be able to drop them
    assert (first["inserted"], again.get_json()["inserted"], again.get_json()["duplicates"]) == (28, 0, 28)
    u = unit(client)
    assert u["duplicates_ignored"] == 28 and len(u["history"]) == 28


def test_a_failed_post_followed_by_a_big_retry_leaves_no_hole_in_the_series(ctx):
    """The soak's 'dashboard holes': after a failed POST the OLD server stamped the retry batch
    by receipt time (~30 s late) and then evicted half of it from the 60 s window, so polls saw
    30-60 s gaps although every reading was delivered. Now every reading keeps its own GPS time,
    so the series is contiguous once the retry lands."""
    client, clock, _a, _ = ctx
    clock.t = T0
    assert client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 28, 28, seq0=0)).status_code == 202
    # POST #2 (readings T0 .. T0+27): the server DID store it, but the device timed out waiting
    # for the reply and treats it as failed...
    clock.t = T0 + 30
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0, 28, seq0=28))
    # ...so 60 s later it resends the OLDEST 60 buffered readings (the same 28 + 32 newer).
    clock.t = T0 + 90
    retry = client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0, 60, seq0=28)).get_json()
    assert (retry["inserted"], retry["duplicates"]) == (32, 28)
    # ...and the next normal batch carries the remainder.
    clock.t = T0 + 120
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 + 60, 28, seq0=88))

    hist = client.get("/api/history", query_string={"unit": "unit-1", "from": T0 - 30, "to": T0 + 200}).get_json()
    ts = [r["t"] for r in hist["readings"]]
    assert len(ts) == 28 + 60 + 28 and len({r["seq"] for r in hist["readings"]}) == 116
    assert all(b - a == pytest.approx(1.0, abs=1e-6) for a, b in zip(ts, ts[1:]))       # no hole, no duplicate
    u = unit(client)
    assert u["gaps"] == [] and u["completeness_pct"] > 90


def test_a_late_retry_of_older_readings_lands_in_the_right_place_not_at_the_end(ctx):
    client, clock, _a, _ = ctx
    clock.t = T0
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 10, 10, seq0=100))
    clock.t = T0 + 90
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 40, 30, seq0=70))      # old data arrives late
    ts = [r["t"] for r in client.get("/api/history", query_string={"unit": "unit-1", "from": T0 - 50, "to": T0}).get_json()["readings"]]
    assert ts == sorted(ts) and ts[0] == pytest.approx(T0 - 40) and ts[-1] == pytest.approx(T0 - 1)


def test_history_survives_an_app_restart_and_dedupe_still_works(tmp_path):
    clock = FakeClock(T0)
    path = str(tmp_path / "r.db")
    a1 = create_app(simulated_units=[], db_path=path, clock=clock)
    a1.test_client().post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 20, 20))
    a1.config["TREMOR_SHUTDOWN"]()
    a2 = create_app(simulated_units=[], db_path=path, clock=clock)          # "restart": new app, same DB
    try:
        c = a2.test_client()
        assert len(unit(c)["history"]) == 20
        assert c.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 20, 20)).get_json()["inserted"] == 0
    finally:
        a2.config["TREMOR_SHUTDOWN"]()


# --- storage failures -------------------------------------------------------------------------------

def test_a_storage_failure_answers_503_with_retry_after_so_the_device_keeps_its_readings(ctx, monkeypatch):
    client, _c, app, _ = ctx

    def boom(*_a, **_k):
        raise StoreError("database is locked")
    monkeypatch.setattr(app.config["TREMOR_STORE"], "ingest", boom)
    r = client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 3, 3))
    assert r.status_code == 503 and r.headers["Retry-After"] == "30"
    monkeypatch.undo()
    assert client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 3, 3)).get_json()["inserted"] == 3   # retry works


def test_reads_fail_soft_with_503_when_storage_is_unavailable(ctx, monkeypatch):
    client, _c, app, _ = ctx

    def boom(*_a, **_k):
        raise StoreError("disk I/O error")
    monkeypatch.setattr(app.config["TREMOR_STORE"], "unit_states", boom)
    assert client.get("/api/units").status_code == 503
    assert client.get("/api/health").status_code == 503


def test_maintenance_runs_after_the_response_is_sent(ctx, monkeypatch):
    client, _c, app, _ = ctx
    calls = []
    monkeypatch.setattr(app.config["TREMOR_RETENTION"], "maybe_step", lambda: calls.append(1))
    with client:                                              # keeps the response open until the block ends
        r = client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 3, 3))
        assert r.status_code == 202
    r.close()
    assert calls == [1]


# --- /api/units window, RoCoF ------------------------------------------------------------------------------

def test_the_live_window_is_relative_to_the_units_newest_gps_time(ctx):
    client, clock, *_ = ctx
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 200, 200))
    u = unit(client)
    assert 58 <= len(u["history"]) <= 61 and u["history"][-1][0] - u["history"][0][0] <= 60.0
    clock.advance(3600)                                       # an hour of silence: stale, but last values remain
    u = unit(client)
    assert u["status"] == "stale" and len(u["history"]) > 50 and u["seconds_since_last_reading"] > 3000


def test_rocof_is_not_bridged_across_a_gap_larger_than_1_5_seconds(ctx):
    client, *_ = ctx
    b = v2_batch("unit-1", BOOT, [v2_reading(50.0, 0, int((T0 - 10) * US)), v2_reading(50.0, 1, int((T0 - 9) * US)),
                                  v2_reading(50.5, 2, int((T0 - 5) * US))])           # 4 s later, +0.5 Hz
    client.post("/api/ingest", json=b)
    u = unit(client)
    assert [round(t - (T0 - 10)) for t, _ in u["rocof_history"]] == [1]              # the 4 s jump makes no RoCoF point
    assert u["rocof_hz_s"] == pytest.approx(0.0, abs=1e-9)


# --- /api/history --------------------------------------------------------------------------------------------

def test_history_defaults_to_the_last_hour_and_reports_truncation_and_a_cursor(ctx):
    client, clock, *_ = ctx
    for k in range(3):
        clock.t = T0 - 3600 + (k + 1) * 1000 + 3
        client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 3600 + k * 1000, 1000, seq0=k * 1000))
    clock.t = T0
    r = client.get("/api/history", query_string={"unit": "unit-1", "limit": 400}).get_json()
    assert r["count"] == 400 and r["truncated"] is True and r["limit"] == 400 and r["resolution"] == "raw"
    seen = [x["seq"] for x in r["readings"]]
    while r["truncated"]:
        r = client.get("/api/history", query_string={"unit": "unit-1", "limit": 400, "from": r["next_from_us"] / US,
                                                     "after_id": r["next_after_id"], "to": T0}).get_json()
        seen += [x["seq"] for x in r["readings"]]
    assert seen == list(range(3000))                                                  # every row exactly once


def test_history_hard_limit_is_enforced_even_if_asked_for_more(ctx):
    client, clock, *_ = ctx
    for k in range(11):
        clock.t = T0 - 11_000 + (k + 1) * 1000 + 3          # received just after its newest reading
        client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 11_000 + k * 1000, 1000, seq0=k * 1000))
    r = client.get("/api/history", query_string={"unit": "unit-1", "limit": 999_999, "from": T0 - 20_000,
                                                 "to": T0}).get_json()
    assert r["limit"] == HISTORY_MAX_LIMIT == 10_000 and r["count"] == 10_000 and r["truncated"] is True


def test_history_accepts_iso_times_and_validates_its_arguments(ctx):
    client, clock, *_ = ctx
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 30, 30))
    ok = client.get("/api/history", query_string={"unit": "unit-1", "from": "2026-09-25T01:59:30Z",
                                                  "to": "2026-09-25T01:59:40+00:00"}).get_json()
    assert ok["count"] == 11 and ok["readings"][0]["t"] == pytest.approx(utc(2026, 9, 25, 1, 59, 30))
    q = client.get
    assert q("/api/history").status_code == 400
    assert q("/api/history?unit=nobody").status_code == 404
    assert q("/api/history?unit=unit-1&limit=0").status_code == 400
    assert q("/api/history?unit=unit-1&limit=abc").status_code == 400
    assert q("/api/history?unit=unit-1&from=yesterday").status_code == 400
    assert q("/api/history?unit=unit-1&from=200&to=100").status_code == 400
    assert q("/api/history?unit=unit-1&resolution=hourly").status_code == 400


def test_history_lists_unlocked_readings_separately_only_when_asked(ctx):
    client, *_ = ctx
    client.post("/api/ingest", json=v1_batch("unit-1", [v1_reading(50.0, T0 - 2), v1_reading(50.1, None)]))
    plain = client.get("/api/history", query_string={"unit": "unit-1"}).get_json()
    assert plain["count"] == 1 and "unlocked" not in plain
    full = client.get("/api/history", query_string={"unit": "unit-1", "include_unlocked": 1}).get_json()
    assert len(full["unlocked"]) == 1 and full["unlocked"][0]["t"] is None and full["unlocked"][0]["flags"] & 1


# --- health, retention, export ---------------------------------------------------------------------------------

def test_health_warns_at_80_percent_of_the_quota(tmp_path):
    clock = FakeClock(T0)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "r.db"), clock=clock, quota_bytes=400_000,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "ex")))
    try:
        c = app.test_client()
        h = c.get("/api/health").get_json()
        assert h["status"] == "ok" and h["storage"]["warning"] is False and h["storage"]["quota_bytes"] == 400_000
        for k in range(8):
            clock.t = T0 - 8000 + (k + 1) * 1000 + 3
            c.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 8000 + k * 1000, 1000, seq0=k * 1000))
        h = c.get("/api/health").get_json()
        assert h["storage"]["used_fraction"] >= 0.8 and h["storage"]["warning"] is True and h["status"] == "warning"
        assert h["store"]["raw_rows"] == 8000 and h["units"][0]["readings_total"] == 8000
        assert h["retention"]["raw_days"] == 14
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_health_measures_the_whole_quota_root_when_configured(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "big.bin").write_bytes(b"x" * 300_000)
    app = create_app(simulated_units=[], db_path=str(root / "r.db"), clock=FakeClock(T0), quota_bytes=400_000,
                     quota_root=str(root), retention_config=RetentionConfig(export_dir=str(root / "ex")))
    try:
        h = app.test_client().get("/api/health").get_json()
        assert h["storage"]["used_bytes"] >= 300_000 and h["storage"]["warning"] is True
        assert "under" in h["storage"]["measured"]
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_old_data_flows_through_retention_and_is_still_reachable_as_aggregates_and_an_export(ctx):
    client, clock, app, tmp = ctx
    old = utc(2026, 9, 1, 3)                                              # 24 days before T0
    store = app.config["TREMOR_STORE"]
    from tremor.ingest import parse_payload
    store.ingest(parse_payload(v2_series("unit-1", BOOT, old, 600)), old + 603)     # plausible receipt time
    client.post("/api/ingest", json=v2_series("unit-1", BOOT, T0 - 5, 5, seq0=10_000))
    app.config["TREMOR_RETENTION"].run_until_idle()

    h = client.get("/api/health").get_json()
    assert h["status"] == "ok" and h["store"]["raw_rows"] == 5 and h["store"]["aggregate_rows"] >= 10
    # 'auto' picks raw for recent ranges and 1-minute aggregates for ranges older than raw_days
    recent = client.get("/api/history", query_string={"unit": "unit-1"}).get_json()
    assert recent["resolution"] == "raw" and recent["count"] == 5
    ancient = client.get("/api/history", query_string={"unit": "unit-1", "from": old - 60, "to": old + 700}).get_json()
    assert ancient["resolution"] == "1min" and sum(a["n"] for a in ancient["aggregates"]) == 600
    assert {"freq_mean", "freq_min", "freq_max", "freq_std", "rocof_max_abs", "n_unlocked"} <= set(ancient["aggregates"][0])
    # the gzip export can be pulled off the server, and matches
    dl = client.get("/api/export/unit-1/2026-09-01", headers=AUTH)
    assert dl.status_code == 200 and dl.mimetype == "application/gzip"
    lines = gzip.decompress(dl.data).decode().splitlines()
    assert len(lines) == 601 and lines[0].startswith("unit_id,")
    assert client.get("/api/export/unit-1/2026-09-02", headers=AUTH).status_code == 404
    assert client.get("/api/export/..%2Fetc/2026-09-01", headers=AUTH).status_code == 404
    assert client.get("/api/export/unit-1/not-a-date", headers=AUTH).status_code == 404


def test_health_flags_a_day_that_needs_attention(ctx):
    client, clock, app, tmp = ctx
    old = utc(2026, 9, 1, 3)
    from tremor.ingest import parse_payload
    app.config["TREMOR_STORE"].ingest(parse_payload(v2_series("unit-1", BOOT, old, 300)), old + 303)
    eng = app.config["TREMOR_RETENTION"]
    while True:
        st = app.config["TREMOR_STORE"].day_states("unit-1")
        if st and all(s.export_done and s.agg_done for s in st.values()):
            break
        eng.step()
    os.remove(next(iter(st.values())).export_path)
    eng.run_until_idle()
    h = client.get("/api/health").get_json()
    assert h["status"] == "attention" and h["store"]["days_needing_attention"][0]["unit_id"] == "unit-1"
    assert h["store"]["raw_rows"] == 300                                     # and nothing was deleted
