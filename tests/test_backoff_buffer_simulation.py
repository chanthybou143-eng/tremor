from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifi_ingest import IngestBuffer  # noqa: E402

# Mirrors wifi_unit_client.py's own constants (BACKOFF_MULTIPLIER,
# BACKOFF_CAP_S, POST_INTERVAL_S, MAX_READINGS_PER_POST,
# MAX_BUFFERED_READINGS) -- duplicated here, not imported, because that
# module constructs hardware objects and runs an infinite loop at module
# scope, so it can never be imported on the host (same constraint
# http_client.py's own module docstring documents for the POST-timeout
# logic). Keep these in sync by hand if the real constants change.
BACKOFF_MULTIPLIER = 2.0
BACKOFF_CAP_S = 120.0
POST_INTERVAL_S = 30.0
MAX_READINGS_PER_POST = 60
MAX_BUFFERED_READINGS = 600

# Measured from Trial 5 run 2's clean, failure-free hours (14:00 and 18:00
# UTC): (readings_sent_ok + buffered) grew by ~3,380 over ~3,590s in each,
# i.e. ~0.94 readings/s -- roughly one reading per ~1.06s chunk.
READING_ARRIVAL_HZ = 0.94

# A failed connect/tls_handshake/read_response stage is bounded by
# SOCKET_OP_TIMEOUT_S=4.0 in http_client.py, and that's what almost every
# real POST_FAIL in Trial 5's log actually measured (stage_duration_s
# 4.001-4.002 for the large majority of failures). A successful POST
# measured 2-4s typically; 3.0s is a representative round number.
FAILURE_ATTEMPT_DURATION_S = 4.0
SUCCESS_ATTEMPT_DURATION_S = 3.0


def _simulate_outage(outage_duration_s, total_sim_s):
    """Drive a real IngestBuffer through a simulated outage on a fake
    clock: every flush() attempt that starts before outage_duration_s
    fails; everything at or after it succeeds. Readings are appended
    continuously at READING_ARRIVAL_HZ regardless of POST outcome, exactly
    as the real main loop keeps sampling ADC data independent of WiFi
    state. Returns (buffer, attempt_log) where attempt_log is a list of
    (start_time_s, duration_s, ok) for every flush() call that actually
    attempted a POST.
    """
    clock = [0.0]
    attempt_log = []

    def post_fn(payload):
        start = clock[0]
        ok = start >= outage_duration_s
        duration = SUCCESS_ATTEMPT_DURATION_S if ok else FAILURE_ATTEMPT_DURATION_S
        attempt_log.append((start, duration, ok))
        clock[0] += duration
        return ok

    buffer = IngestBuffer(
        "test-unit", post_fn,
        max_readings=MAX_BUFFERED_READINGS,
        max_readings_per_post=MAX_READINGS_PER_POST,
    )

    interval = POST_INTERVAL_S
    next_reading_t = 0.0
    next_post_t = 0.0
    while clock[0] < total_sim_s:
        while next_reading_t <= clock[0]:
            buffer.append(50.0, 1.0, None)
            next_reading_t += 1.0 / READING_ARRIVAL_HZ
        if clock[0] >= next_post_t:
            ok = buffer.flush()
            interval = POST_INTERVAL_S if ok else min(interval * BACKOFF_MULTIPLIER, BACKOFF_CAP_S)
            next_post_t = clock[0] + interval
        else:
            clock[0] = next_post_t  # fake clock: jump straight to the next scheduled check
    return buffer, attempt_log


def test_240s_outage_causes_no_drops_and_document_n():
    """A 240s outage, matching the planned WiFi-off test from Trial 5, is
    short enough relative to the new 120s backoff cap that only a few
    attempts actually fail before the connection is back -- report N."""
    buffer, attempt_log = _simulate_outage(outage_duration_s=240.0, total_sim_s=400.0)
    failures = [a for a in attempt_log if not a[2]]
    n = len(failures)
    assert n == 3  # documents N for this specific 240s-outage scenario
    assert buffer.dropped_count == 0


def test_4_consecutive_failures_do_not_overflow_buffer():
    """Force exactly 4 consecutive failures (outage just long enough to
    fail attempt 4 but not attempt 5) and confirm no drops -- this is the
    scenario that, under the OLD 240s cap, was measured on real hardware
    to overflow the buffer (Trial 5 run 2, Episode 3: buffered hit 600
    ~615s after a real 4-failure run began). Under the new 120s cap the
    same run length finishes much sooner and stays under the fill time."""
    # Attempt starts at t=0, 64, 188, 312 all fail; attempt 5 at t=436 succeeds.
    buffer, attempt_log = _simulate_outage(outage_duration_s=436.0, total_sim_s=500.0)
    failures = [a for a in attempt_log if not a[2]]
    assert len(failures) == 4
    assert buffer.dropped_count == 0


def test_6_consecutive_failures_still_overflows_buffer():
    """A 6-failure run's span (~684s of backoff waits and attempt
    durations) still exceeds the buffer's ~600-640s fill time at the
    measured arrival rate even under the new 120s cap -- confirms the cap
    change narrows the failure window that causes drops, but does not
    eliminate it. This is expected and documented, not a bug."""
    # Attempt starts at t=0, 64, 188, 312, 436, 560 all fail; attempt 7 at t=684 succeeds.
    buffer, attempt_log = _simulate_outage(outage_duration_s=684.0, total_sim_s=750.0)
    failures = [a for a in attempt_log if not a[2]]
    assert len(failures) == 6
    assert buffer.dropped_count > 0
