from __future__ import annotations

import time

import pytest

from tremor.webapp import _UnitsState, _smoothed_history, create_app
from tremor.units import UnitReading


def test_smoothed_history_suppresses_single_cycle_spike():
    # A stable series with one isolated outlier -- the kind of single bad
    # cycle the hysteresis+filter pipeline occasionally still lets through
    # (see test_units.py). The smoothed value at the spike should be much
    # closer to its neighbours than to the raw spike itself.
    points = [(i * 0.02, 50.0) for i in range(10)]
    points[5] = (points[5][0], 53.0)  # single-cycle spike

    smoothed = _smoothed_history(points, window_s=0.2)

    assert smoothed[5][1] == pytest.approx(50.0)
    assert len(smoothed) == len(points)


def test_smoothed_history_preserves_a_real_trend():
    points = [(i * 0.02, 50.0 + 0.5 * i * 0.02) for i in range(20)]  # 0.5Hz/s ramp

    smoothed = _smoothed_history(points, window_s=0.2)

    # A genuine trend should survive smoothing, not flatten to the mean.
    assert smoothed[0][1] < smoothed[-1][1]


def test_unitsstate_reports_no_units_before_any_reading():
    # No pre-seeded slots -- a unit that has never reported shouldn't
    # appear at all, not as an "offline" placeholder.
    state = _UnitsState()
    assert state.snapshot() == []


def test_unitsstate_reports_live_after_readings():
    state = _UnitsState()
    t = 0.0
    for freq in [49.98, 50.01, 49.99, 50.02, 50.00]:
        state.add_reading("unit-1", UnitReading(t=t, freq_hz=freq))
        t += 0.02

    snapshot = state.snapshot()

    # Only the unit that actually reported gets a card -- nothing else
    # appears alongside it.
    assert len(snapshot) == 1
    unit1 = snapshot[0]
    assert unit1["id"] == "unit-1"
    assert unit1["label"] == "Unit 1"  # derived from unit_id, not a fixed registry
    assert unit1["status"] == "live"
    assert unit1["freq_hz"] == pytest.approx(50.0, abs=0.05)
    assert len(unit1["history"]) == 5


def test_unitsstate_rocof_from_ramping_readings():
    state = _UnitsState()
    t = 0.0
    slope_hz_s = 0.5
    while t < 3.0:
        state.add_reading("unit-1", UnitReading(t=t, freq_hz=50.0 + slope_hz_s * t))
        t += 0.02

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["rocof_hz_s"] == pytest.approx(slope_hz_s, abs=0.05)
    # rocof_history is the full time series (for the dashboard's chart),
    # not just the latest value -- same last value as rocof_hz_s.
    assert len(unit1["rocof_history"]) > 1
    assert unit1["rocof_history"][-1][1] == pytest.approx(slope_hz_s, abs=0.05)


def test_index_route_serves_html():
    app = create_app(simulated_units=[])
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"TREMOR" in resp.data
    app.config["TREMOR_SHUTDOWN"]()


def test_api_units_route_shape_with_no_simulated_units():
    # No feeds registered and nothing ingested yet -- the dashboard should
    # show no units at all, not 5 permanent "offline" placeholders.
    app = create_app(simulated_units=[])
    client = app.test_client()
    resp = client.get("/api/units")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data == []
    app.config["TREMOR_SHUTDOWN"]()


def test_api_units_route_goes_live_with_a_simulated_unit():
    app = create_app(simulated_units=[dict(unit_id="unit-1", noise_std=0.0, chunk_s=0.3, seed=1)])
    client = app.test_client()
    time.sleep(1.0)
    try:
        data = client.get("/api/units").get_json()
        unit1 = next(u for u in data if u["id"] == "unit-1")
        assert unit1["status"] == "live"
        assert unit1["freq_hz"] == pytest.approx(50.0, abs=0.05)
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_api_ingest_accepts_a_batch_and_updates_the_unit():
    app = create_app(simulated_units=[])
    client = app.test_client()
    try:
        resp = client.post("/api/ingest", json={
            "unit_id": "unit-1",
            "readings": [
                {"frequency_hz": 49.98, "amplitude_v": 0.72, "gps_utc_s": 41023.5},
                {"frequency_hz": 50.01, "amplitude_v": 0.73, "gps_utc_s": 41023.52},
            ],
        })
        assert resp.status_code == 202
        assert resp.get_json()["accepted"] == 2

        data = client.get("/api/units").get_json()
        unit1 = next(u for u in data if u["id"] == "unit-1")
        assert unit1["status"] == "live"
        assert unit1["freq_hz"] == pytest.approx(49.995, abs=0.01)
        assert unit1["amplitude_v"] == pytest.approx(0.725, abs=0.01)
        assert unit1["gps_utc_s"] == pytest.approx(41023.52)
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_api_ingest_accepts_a_new_unit_id_and_it_appears_automatically():
    # The whole point of the dynamic roster: a unit_id nothing has seen
    # before is accepted outright and shows up on the dashboard on its
    # first batch -- no code change, no pre-registration.
    app = create_app(simulated_units=[])
    client = app.test_client()
    try:
        resp = client.post("/api/ingest", json={
            "unit_id": "unit-7",
            "readings": [{"frequency_hz": 50.0}],
        })
        assert resp.status_code == 202

        data = client.get("/api/units").get_json()
        assert len(data) == 1
        assert data[0]["id"] == "unit-7"
        assert data[0]["label"] == "Unit 7"
        assert data[0]["status"] == "live"
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_api_ingest_rejects_empty_unit_id():
    app = create_app(simulated_units=[])
    client = app.test_client()
    try:
        resp = client.post("/api/ingest", json={
            "unit_id": "",
            "readings": [{"frequency_hz": 50.0}],
        })
        assert resp.status_code == 400
        assert client.get("/api/units").get_json() == []
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_api_ingest_rejects_missing_frequency():
    app = create_app(simulated_units=[])
    client = app.test_client()
    try:
        resp = client.post("/api/ingest", json={
            "unit_id": "unit-1",
            "readings": [{"amplitude_v": 0.72}],
        })
        assert resp.status_code == 400
    finally:
        app.config["TREMOR_SHUTDOWN"]()
