"""End-to-end outage simulation on a fake clock: the real IngestBuffer and the real schedule
(wifi_ingest.next_post_interval_s) with wifi_unit_client.py's own constants, and a fake server that
dedupes on seq like the real one. Answers: how long an outage loses nothing, how long catching up takes,
and that nothing is lost or duplicated in between."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wifi_ingest import IngestBuffer, next_post_interval_s  # noqa: E402


def _client_constants():
    """wifi_unit_client.py cannot be imported on the host (hardware + an infinite loop at module
    scope), so its module-level numeric constants are read from the source instead -- no hand-kept
    copies to drift."""
    tree = ast.parse((ROOT / "wifi_unit_client.py").read_text())
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            try:
                out[n.targets[0].id] = ast.literal_eval(n.value)
            except ValueError:
                pass
    return out


C = _client_constants()
POST_INTERVAL_S = C["POST_INTERVAL_S"]
CATCHUP_POST_INTERVAL_S = C["CATCHUP_POST_INTERVAL_S"]
BACKOFF_MULTIPLIER = C["BACKOFF_MULTIPLIER"]
BACKOFF_CAP_S = C["BACKOFF_CAP_S"]
MAX_READINGS_PER_POST = C["MAX_READINGS_PER_POST"]
MAX_BUFFERED_READINGS = C["MAX_BUFFERED_READINGS"]

# Measured: ~580 readings per 10 min in normal operation (0.97/s); 1.0/s is the worst case for filling.
READING_HZ = 1.0
# A successful POST takes 2-4 s. A failed one is charged the hard abort time (the worst case: it
# blocks the longest, and readings keep arriving meanwhile).
FAIL_S = 10.0
OK_S = 3.0


def simulate(outage_s, total_s, reading_hz=READING_HZ):
    """POSTs fail while the clock < outage_s, succeed after. Readings arrive at reading_hz throughout,
    including during POSTs (the ADC ring keeps sampling). Returns (buffer, server_seqs, log)."""
    clock = 0.0
    server = []                       # every seq the server stored (deduped like the real one)
    seen = set()
    log = []

    def post_fn(payload):
        ok = clock >= outage_s
        log.append((clock, len(payload["readings"]), ok))
        if ok:
            for r in payload["readings"]:
                if r["seq"] not in seen:
                    seen.add(r["seq"])
                    server.append(r["seq"])
        return ok

    buf = IngestBuffer("u", post_fn, max_readings=MAX_BUFFERED_READINGS,
                       max_readings_per_post=MAX_READINGS_PER_POST, boot_id="0123456789abcdef")
    interval = POST_INTERVAL_S
    next_reading = 0.0
    next_post = POST_INTERVAL_S
    while clock < total_s:
        if next_reading <= next_post:
            clock = next_reading
            buf.append(50.0, 0.7, None, gps=(20733, int(clock) % 86400, 0))
            next_reading += 1.0 / reading_hz
            continue
        clock = next_post
        if len(buf):
            ok = buf.flush()
            dur = OK_S if ok else FAIL_S
            while next_reading < clock + dur:          # readings measured while the POST blocked
                buf.append(50.0, 0.7, None, gps=(20733, int(next_reading) % 86400, 0))
                next_reading += 1.0 / reading_hz
            interval = next_post_interval_s(interval, "ok" if ok else "fail", len(buf), POST_INTERVAL_S,
                                            CATCHUP_POST_INTERVAL_S, MAX_READINGS_PER_POST,
                                            BACKOFF_MULTIPLIER, BACKOFF_CAP_S)
        next_post = clock + interval
    return buf, server, log


def _caught_up_at(log, outage_s):
    """First time after the outage a POST carried a normal-size batch again (backlog drained)."""
    for t, n, ok in log:
        if ok and t >= outage_s and n < MAX_READINGS_PER_POST:
            return t
    return None


def test_the_constants_are_the_ones_decided_for_fw_resilience():
    assert (MAX_BUFFERED_READINGS, MAX_READINGS_PER_POST, POST_INTERVAL_S, CATCHUP_POST_INTERVAL_S) == (3600, 60, 30.0, 10.0)


def test_a_10_minute_outage_loses_nothing_and_catches_up_within_minutes():
    buf, server, log = simulate(outage_s=600, total_s=1800)
    assert buf.dropped_count == 0
    assert server == list(range(len(server))) and len(server) + len(buf) == buf.next_seq   # no gap, no duplicate
    t = _caught_up_at(log, 600)
    assert t is not None and t - 600 < 180 + BACKOFF_CAP_S                                  # + the last backoff wait


def test_a_45_minute_outage_loses_nothing_and_drains_in_under_15_minutes():
    buf, server, log = simulate(outage_s=45 * 60, total_s=90 * 60)
    assert buf.dropped_count == 0 and server == list(range(len(server)))
    t = _caught_up_at(log, 45 * 60)
    assert t is not None and t - 45 * 60 < 15 * 60


def test_a_55_minute_outage_still_loses_nothing():
    buf, server, _ = simulate(outage_s=55 * 60, total_s=120 * 60)
    assert buf.dropped_count == 0 and server == list(range(len(server)))


def test_a_90_minute_outage_loses_only_the_oldest_and_exactly_as_many_as_counted():
    buf, server, _ = simulate(outage_s=90 * 60, total_s=180 * 60)
    assert buf.dropped_count > 0
    first = server[0]                 # one gap, at the very start, exactly dropped_count long
    assert first == buf.dropped_count and server == list(range(first, first + len(server)))
    assert len(server) + len(buf) + buf.dropped_count == buf.next_seq


def test_catch_up_never_follows_a_failure():
    _, _, log = simulate(outage_s=600, total_s=1800)
    for (t0, _n0, ok0), (t1, _n1, _ok1) in zip(log, log[1:]):
        if not ok0:
            assert t1 - t0 >= 2 * POST_INTERVAL_S                     # a failure always backs off to >= 60 s
