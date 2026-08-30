from __future__ import annotations

import time

import pytest

from tremor.webapp import UNIT_SLOTS, _UnitsState, create_app
from tremor.units import UnitReading


def test_unitsstate_reports_offline_before_any_reading():
    state = _UnitsState(UNIT_SLOTS)
    snapshot = state.snapshot()

    assert len(snapshot) == 5
    assert all(u["status"] == "offline" for u in snapshot)
    assert all(u["freq_hz"] is None for u in snapshot)


def test_unitsstate_reports_live_after_readings():
    state = _UnitsState(UNIT_SLOTS)
    t = 0.0
    for freq in [49.98, 50.01, 49.99, 50.02, 50.00]:
        state.add_reading("unit-1", UnitReading(t=t, freq_hz=freq))
        t += 0.02

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    others = [u for u in snapshot if u["id"] != "unit-1"]

    assert unit1["status"] == "live"
    assert unit1["freq_hz"] == pytest.approx(50.0, abs=0.05)
    assert len(unit1["history"]) == 5
    assert all(u["status"] == "offline" for u in others)


def test_unitsstate_rocof_from_ramping_readings():
    state = _UnitsState(UNIT_SLOTS)
    t = 0.0
    slope_hz_s = 0.5
    while t < 3.0:
        state.add_reading("unit-1", UnitReading(t=t, freq_hz=50.0 + slope_hz_s * t))
        t += 0.02

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["rocof_hz_s"] == pytest.approx(slope_hz_s, abs=0.05)


def test_index_route_serves_html():
    app = create_app(simulated_units=[])
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"TREMOR" in resp.data
    app.config["TREMOR_SHUTDOWN"]()


def test_api_units_route_shape_with_no_simulated_units():
    app = create_app(simulated_units=[])
    client = app.test_client()
    resp = client.get("/api/units")
    data = resp.get_json()

    assert resp.status_code == 200
    assert len(data) == 5
    assert all(u["status"] == "offline" for u in data)
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
