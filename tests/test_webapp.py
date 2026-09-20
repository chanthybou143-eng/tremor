from __future__ import annotations

import itertools
import time

import pytest

from tremor.rocof import rocof_from_window
from tremor.webapp import (
    MAX_ROCOF_GAP_S,
    ROCOF_PLAUSIBILITY_LIMIT_HZ_S,
    ROCOF_WINDOW_S,
    GAP_THRESHOLD_S,
    STALE_THRESHOLD_S,
    _find_gaps,
    _gps_utc_delta_s,
    _UnitsState,
    _smoothed_history,
    create_app,
)
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


def test_api_ingest_spaces_batch_readings_by_reading_interval_not_compressed():
    # Regression test: an earlier version stamped a batch's readings
    # ~0.02s apart (modeled on per-mains-cycle spacing that never matched
    # any real client) instead of the real ~1s-per-reading cadence
    # wifi_unit_client.py actually uses. That compression divided a real
    # ~1s frequency delta by an apparent ~0.02s gap, inflating RoCoF by
    # ~50x -- invisible with only a frequency sparkline, but produced
    # physically-impossible spikes once the dashboard started plotting
    # RoCoF as its own line (a few tenths of a Hz/s is a large *real*
    # swing; several Hz/s is not physically plausible).
    app = create_app(simulated_units=[])
    client = app.test_client()
    try:
        # A gentle, realistic ramp: 0.01 Hz/s -- a full second apart, that's
        # a tiny per-reading step, easy to blow up if timestamps are wrong.
        readings = [{"frequency_hz": 50.0 + 0.01 * i} for i in range(8)]
        resp = client.post("/api/ingest", json={"unit_id": "unit-1", "readings": readings})
        assert resp.status_code == 202

        data = client.get("/api/units").get_json()
        unit1 = next(u for u in data if u["id"] == "unit-1")

        history_ts = [t for t, _ in unit1["history"]]
        gaps = [b - a for a, b in zip(history_ts, history_ts[1:])]
        assert all(gap == pytest.approx(1.0, abs=0.05) for gap in gaps)

        # True slope here is 0.01 Hz/s -- correct spacing should recover
        # something in that ballpark, not an order-of-magnitude-inflated value.
        assert abs(unit1["rocof_hz_s"]) < 0.5
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


# --- Part 1: RoCoF cross-batch/restart-boundary fix -------------------------
#
# Reproduces the production incident (7714d2a): a crash-triggered restart's
# first (small) batch landed close enough in real submission time to the
# tail of the pre-crash batch still in the 60s window that the OLD
# reconstructed-timestamp-based fit saw an artificially tiny apparent gap,
# producing a real -22.953 Hz/s reading. Mechanism: every batch's
# timestamps are independently reconstructed from that batch's own receipt
# time (see /api/ingest's docstring) -- two different batches' reconstructed
# times were never safe to compare directly, regardless of how small the
# apparent gap between them looked.

def test_reproduces_the_impossible_rocof_if_batches_were_naively_bridged():
    """Demonstrates the bug's mechanism concretely, independent of the
    server's own eligibility guard: feeding rocof_from_window the exact
    kind of naively-reconstructed timestamps two close-together batches
    would produce yields a physically impossible slope, the same order of
    magnitude as the production incident's -22.953 Hz/s. This is the
    "show the impossible value" reproduction the fix is judged against --
    the *next* test confirms the actual server code no longer produces it.

    The exact gap (20ms) is illustrative, not a measurement -- the real
    incident's underlying batch never got its timestamps logged before it
    crashed (see wifi_unit_client.py's git history), so the precise gap
    that occurred there is unknown. A copy of that night's device-side log
    (logs/wifi_soak_20260919_203209.log) shows the device's own
    crash-to-reconnect cycle was consistently ~1.0-1.02s -- but that's the
    DEVICE's reconnect time, not the SERVER-RECEIPT gap between the two
    POSTs that actually drives this bug: at a 1s gap even a large
    frequency swing stays under ROCOF_PLAUSIBILITY_LIMIT_HZ_S (a Δf of
    5+Hz would be needed, not "ordinary chunk noise" anymore), so
    whatever the real receipt-time gap was, it was necessarily much
    smaller than the device's own reconnect cycle -- consistent with 20ms
    being the right order of magnitude for a reproduction, even though
    the literal figure is chosen, not measured. What's reproduced here is
    the *mechanism* (two independently-reconstructed batch timestamps
    landing unrealistically close together in receipt time), which is
    what the fix addresses.
    """
    # Batch 1 (pre-crash): 8 readings/s ending at 50.00Hz, reconstructed
    # backward from receipt time 1000.0 (matches /api/ingest's own formula).
    n1 = 8
    batch1_ts = [1000.0 - (n1 - 1 - i) * 1.0 for i in range(n1)]
    batch1_fs = [50.00] * n1
    # Batch 2 (post-restart, landing only 20ms later in real submission
    # time -- a fast crash-to-reconnect cycle): a single reading at a
    # genuinely different but unremarkable frequency (0.5Hz swing, well
    # within normal chunk-to-chunk variation).
    batch2_ts = [1000.02]
    batch2_fs = [49.50]

    # The OLD behavior: bridge the last point of batch 1 with batch 2's
    # point purely on reconstructed time, no eligibility check at all.
    window_ts = batch1_ts[-1:] + batch2_ts
    window_fs = batch1_fs[-1:] + batch2_fs
    naive_slope = rocof_from_window(window_ts, window_fs)

    assert abs(naive_slope) > ROCOF_PLAUSIBILITY_LIMIT_HZ_S, (
        "expected this scenario to reproduce a physically impossible slope "
        f"(got {naive_slope} Hz/s) -- if this fails, the reproduction itself "
        "no longer matches the incident shape"
    )


def test_server_does_not_bridge_close_batches_without_gps_confirmation(monkeypatch):
    """The actual fix, exercised through the real /api/ingest HTTP path
    with the exact batch shape from the reproduction above (no gps_utc_s
    on either batch, matching a device that hasn't acquired PPS lock --
    also the incident's real condition, since GPS-synced-and-still-wrong
    was never the failure mode)."""
    app = create_app(simulated_units=[])
    client = app.test_client()
    try:
        # chain + repeat: the two POSTs need exactly these two values, but
        # snapshot()'s own `now = time.time()` call afterward needs one
        # more -- hold at the last value rather than run out.
        times = itertools.chain([1000.0, 1000.02], itertools.repeat(1000.02))
        monkeypatch.setattr("tremor.webapp.time.time", lambda: next(times))

        client.post("/api/ingest", json={
            "unit_id": "unit-1",
            "readings": [{"frequency_hz": 50.00} for _ in range(8)],
        })
        resp = client.post("/api/ingest", json={
            "unit_id": "unit-1",
            "readings": [{"frequency_hz": 49.50}],
        })
        assert resp.status_code == 202

        data = client.get("/api/units").get_json()
        unit1 = next(u for u in data if u["id"] == "unit-1")

        # No eligible cross-batch pair exists (different batch_ids, no GPS
        # on either side) -- batch 2's reading contributes no new RoCoF
        # point, so the last stored value stays whatever batch 1's own
        # (8 identical readings, same batch) least-squares fit produced --
        # ~0, modulo floating-point noise, never the naive-bridge value.
        assert unit1["rocof_hz_s"] == pytest.approx(0.0, abs=1e-6)
        assert all(abs(r) <= ROCOF_PLAUSIBILITY_LIMIT_HZ_S for _t, r in unit1["rocof_history"])
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_gps_confirmed_small_gap_bridges_batches_correctly():
    """The fix's positive case: two different batches, but BOTH readings
    carry a real GPS UTC timestamp confirming they really are close
    together -- this should compute a normal, correct RoCoF, not refuse
    just because it's a different batch_id. Otherwise the fix would be
    overcorrecting: a real unit that restarts cleanly with GPS already
    locked shouldn't lose a legitimate reading at the seam.
    """
    state = _UnitsState()
    b1 = state.next_batch_id()
    state.add_reading("unit-1", UnitReading(t=1000.0, freq_hz=50.00, gps_utc_s=41000.0), batch_id=b1)
    b2 = state.next_batch_id()
    # 0.5s later by GPS, a different batch, but the server's own `t` guess
    # could easily disagree slightly -- GPS should be preferred regardless.
    state.add_reading("unit-1", UnitReading(t=1050.0, freq_hz=50.02, gps_utc_s=41000.5), batch_id=b2)

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    # True slope: 0.02Hz / 0.5s = 0.04 Hz/s
    assert unit1["rocof_hz_s"] == pytest.approx(0.04, abs=0.01)


def test_gps_delta_folds_midnight_rollover():
    # 23:59:59.5 -> 00:00:00.5 is a real 1.0s gap, not a ~-86399s one.
    assert _gps_utc_delta_s(0.5, 86399.5) == pytest.approx(1.0)
    assert _gps_utc_delta_s(86399.5, 0.5) == pytest.approx(-1.0)


def test_rocof_plausibility_backstop_excludes_and_counts_implausible_slopes():
    state = _UnitsState()
    t = 0.0
    # Same batch (None batch_id, trusted `t`) but a wildly discontinuous
    # frequency jump -- the backstop must catch this independent of the
    # cross-batch guard, since same-batch data can still be bad data.
    for freq in [50.0, 50.0, 90.0]:
        state.add_reading("unit-1", UnitReading(t=t, freq_hz=freq))
        t += 0.1

    slot = state._slots["unit-1"]
    assert slot.rocof_skipped_implausible_count >= 1
    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert all(abs(r) <= ROCOF_PLAUSIBILITY_LIMIT_HZ_S for _t, r in unit1["rocof_history"])


def test_rocof_boundary_skip_is_counted():
    state = _UnitsState()
    b1 = state.next_batch_id()
    state.add_reading("unit-1", UnitReading(t=1000.0, freq_hz=50.0), batch_id=b1)
    state.add_reading("unit-1", UnitReading(t=1000.5, freq_hz=50.01), batch_id=b1)
    b2 = state.next_batch_id()
    # Different batch, close in reconstructed t, no GPS -- must be
    # skipped and counted, not silently bridged.
    state.add_reading("unit-1", UnitReading(t=1000.9, freq_hz=49.5), batch_id=b2)

    slot = state._slots["unit-1"]
    assert slot.rocof_skipped_boundary_count >= 1


def test_max_rocof_gap_rejects_a_real_but_too_large_gap_even_with_gps():
    # A gap inside ROCOF_WINDOW_S's candidate pool (so it's actually
    # considered) but beyond MAX_ROCOF_GAP_S's stricter bridging limit --
    # confirms the two constants do different jobs, not just one filter.
    assert MAX_ROCOF_GAP_S < ROCOF_WINDOW_S, "test assumes a distinguishable zone exists"
    gap_s = (MAX_ROCOF_GAP_S + ROCOF_WINDOW_S) / 2

    state = _UnitsState()
    b1 = state.next_batch_id()
    state.add_reading("unit-1", UnitReading(t=1000.0, freq_hz=50.0, gps_utc_s=41000.0), batch_id=b1)
    b2 = state.next_batch_id()
    state.add_reading(
        "unit-1",
        UnitReading(t=1000.0 + gap_s, freq_hz=50.5, gps_utc_s=41000.0 + gap_s),
        batch_id=b2,
    )
    slot = state._slots["unit-1"]
    assert slot.rocof_skipped_boundary_count >= 1
    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["rocof_hz_s"] == 0.0


# --- Part 2: gaps and completeness -------------------------------------

def test_find_gaps_detects_a_large_gap_but_not_normal_spacing():
    points = [(0.0, 50.0), (1.0, 50.0), (2.0, 50.0), (2.0 + GAP_THRESHOLD_S + 1, 50.0)]
    gaps = _find_gaps(points)
    assert gaps == [[2.0, 2.0 + GAP_THRESHOLD_S + 1]]


def test_completeness_pct_reflects_missing_samples():
    # completeness_pct is measured over the fixed COMPLETENESS_WINDOW_S
    # (60s, same as the rolling buffers' own retention) -- posting every
    # other second across the full window should read ~50%.
    state = _UnitsState()
    for i in range(0, 60, 2):
        state.add_reading("unit-1", UnitReading(t=float(i), freq_hz=50.0))

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["completeness_pct"] == pytest.approx(50.0, abs=5.0)


def test_completeness_pct_full_when_no_samples_missing():
    state = _UnitsState()
    for i in range(60):
        state.add_reading("unit-1", UnitReading(t=float(i), freq_hz=50.0))

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["completeness_pct"] == pytest.approx(100.0, abs=1.0)


def test_completeness_pct_drops_despite_a_burst_denser_than_1hz():
    # Regression test for the count/expected formula this replaced: a burst
    # of readings all landing within the same second could inflate a raw
    # reading count past "expected" and clamp to 100% even with a real
    # multi-second gap elsewhere in the window -- completeness_pct must be
    # measured by distinct seconds with data, not total reading count, so
    # a burst's extra readings can't paper over the gap.
    state = _UnitsState()
    # 40 readings crammed into second 0 alone (a burst denser than 1/s).
    for i in range(40):
        state.add_reading("unit-1", UnitReading(t=0.025 * i, freq_hz=50.0))
    # A real ~19s gap (t=1 to t=20), then one reading/s for the rest of the
    # 60s window.
    for t in range(20, 59):
        state.add_reading("unit-1", UnitReading(t=float(t), freq_hz=50.0))

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    # Old formula: count=79, expected=58 -> 136%, clamped to 100%.
    # New formula: 40 distinct seconds (1 from the burst + 39 resumed) of
    # the 60s window -> ~66.7%.
    assert unit1["completeness_pct"] == pytest.approx(66.7, abs=2.0)


# --- Part 3: status strip data -------------------------------------------

def test_stale_status_uses_wall_clock_not_reading_relative_time():
    # Regression test: staleness must be judged against real wall-clock
    # time since the server last heard from the unit, not against the
    # reading's own `t` -- SyntheticUnitFeed's `t` is a process-relative
    # elapsed counter starting near 0, which would make every synthetic
    # unit look "stale" (now - t ~= now, always huge) if staleness
    # incorrectly compared against `t` directly.
    state = _UnitsState()
    state.add_reading("unit-1", UnitReading(t=0.02, freq_hz=50.0))  # tiny, non-wall-clock t

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["status"] == "live"
    assert unit1["seconds_since_last_reading"] < 1.0


def test_stale_status_after_threshold_elapses(monkeypatch):
    state = _UnitsState()
    state.add_reading("unit-1", UnitReading(t=0.0, freq_hz=50.0))

    real_time = time.time
    monkeypatch.setattr(
        "tremor.webapp.time.time", lambda: real_time() + STALE_THRESHOLD_S + 1
    )
    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["status"] == "stale"


def test_gps_locked_reflects_most_recent_reading_only():
    state = _UnitsState()
    state.add_reading("unit-1", UnitReading(t=0.0, freq_hz=50.0, gps_utc_s=41000.0))
    state.add_reading("unit-1", UnitReading(t=1.0, freq_hz=50.0, gps_utc_s=None))  # lock lost

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    # last_gps_utc_s is sticky (last non-None value); gps_locked must NOT
    # be, or a unit that lost lock would still claim to be locked.
    assert unit1["gps_utc_s"] == 41000.0
    assert unit1["gps_locked"] is False


def test_rocof_gaps_reflects_the_rocof_series_not_the_frequency_series():
    # rocof has its own gap detection, independent of the frequency series'
    # `gaps`. A run of rapid, single-reading "restarts" (no GPS, so nothing
    # can bridge) skips RoCoF for each of them individually -- none of those
    # skips is a gap on its own -- but frequency keeps arriving every 1s
    # throughout, so by the time two readings finally land back in the same
    # batch and produce a RoCoF point again, real time has moved on far
    # enough that it shows up as a gap in the RoCoF series specifically.
    state = _UnitsState()

    b_a = state.next_batch_id()
    state.add_reading("unit-1", UnitReading(t=0.0, freq_hz=50.0), batch_id=b_a)
    state.add_reading("unit-1", UnitReading(t=1.0, freq_hz=50.1), batch_id=b_a)  # first RoCoF point, t=1.0

    for t in range(2, 8):  # t=2..7, each its own batch: every one skipped, never bridges
        state.add_reading("unit-1", UnitReading(t=float(t), freq_hz=50.0), batch_id=state.next_batch_id())

    b_b = state.next_batch_id()
    state.add_reading("unit-1", UnitReading(t=8.0, freq_hz=50.0), batch_id=b_b)
    state.add_reading("unit-1", UnitReading(t=9.0, freq_hz=50.2), batch_id=b_b)  # next RoCoF point, t=9.0

    slot = state._slots["unit-1"]
    assert slot.rocof_skipped_boundary_count >= 6

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    assert unit1["gaps"] == []  # frequency arrived every 1s throughout, never a real gap
    assert unit1["rocof_gaps"] == [[1.0, 9.0]]  # but RoCoF has an 8s hole where restarts kept skipping it


def test_samples_per_minute_counts_recent_readings():
    state = _UnitsState()
    for i in range(70):  # 70 readings, 1/s, t=0..69
        state.add_reading("unit-1", UnitReading(t=float(i), freq_hz=50.0))

    snapshot = state.snapshot()
    unit1 = next(u for u in snapshot if u["id"] == "unit-1")
    # latest_t=69.0; readings with t in [9.0, 69.0] satisfy `latest_t - t
    # <= 60.0` inclusive -- that's i=9..69, 61 readings, not a plain 60/70
    # split (the boundary is inclusive, same convention READOUT_WINDOW_S's
    # filter already uses elsewhere in this file).
    assert unit1["samples_per_minute"] == 61
