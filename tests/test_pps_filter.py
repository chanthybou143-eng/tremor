"""PPS interval filter (pps_time_sync.PPSTimeSync._on_pps), run on the host behind a fake `machine`
module and a MicroPython-style wrap-around ticks_diff.

Background: plugpack switching puts glitches on the PPS line (up to ~9 spurious edges within seconds).
The filter accepts an edge only if it arrives ~1 s (+/-50 ms) -- or a whole number of seconds, when a
real edge was missed -- after the last ACCEPTED edge, and only accepted edges are paired with NMEA.
"""

from __future__ import annotations

import ast
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TICKS_MASK = (1 << 30) - 1
S = 1_000_000                                   # one second in microseconds
MS = 1000


def _ticks_diff(a, b):
    return ((a - b + (1 << 29)) & TICKS_MASK) - (1 << 29)


class _FakePin:
    IN, IRQ_RISING = 0, 1

    def __init__(self, *a, **k):
        self.handler = None

    def irq(self, trigger=None, handler=None):
        self.handler = handler


@pytest.fixture
def pps(monkeypatch):
    fake = types.ModuleType("machine")
    fake.Pin = _FakePin
    monkeypatch.setitem(sys.modules, "machine", fake)
    sys.modules.pop("pps_time_sync", None)
    import pps_time_sync
    clock = {"t": 0}
    monkeypatch.setattr(time, "ticks_us", lambda: clock["t"] & TICKS_MASK, raising=False)
    monkeypatch.setattr(time, "ticks_diff", _ticks_diff, raising=False)
    yield pps_time_sync, clock
    sys.modules.pop("pps_time_sync", None)


def rmc(hhmmss, ddmmyy="250926"):
    body = f"GNRMC,{hhmmss},A,3446.79805,S,13837.52156,E,0.066,,{ddmmyy},,,A,V"
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}"


def hhmmss(sec_of_day):
    return f"{sec_of_day // 3600:02d}{sec_of_day // 60 % 60:02d}{sec_of_day % 60:02d}.00"


class Rig:
    """Drives one PPSTimeSync with edges at explicit tick values."""

    def __init__(self, mod, clock):
        self.mod, self.clock = mod, clock
        self.s = mod.PPSTimeSync()

    def edge(self, tick):
        self.clock["t"] = tick
        self.s._on_pps(None)

    def counts(self):
        s = self.s
        return (s.pps_count, s.pps_accepted, s.pps_rejected, s.pps_resync)

    def second(self, sod, tick):
        """The receiver's real second `sod`: PPS edge at `tick`, then the NMEA sentence for it."""
        self.edge(tick)
        self.s.feed_nmea(rmc(hhmmss(sod)))


T0 = 10 * S
SOD0 = 23404                                    # 06:30:04 UTC


@pytest.fixture
def rig(pps):
    return Rig(*pps)


# --- normal operation -----------------------------------------------------------------------------------

def test_the_first_edge_is_accepted_because_there_is_nothing_to_compare_it_with(rig):
    rig.edge(T0)
    assert rig.counts() == (1, 1, 0, 0)


def test_a_clean_1_pps_train_is_all_accepted_with_no_resyncs(rig):
    for i in range(60):
        rig.second(SOD0 + i, T0 + i * S)
    assert rig.counts() == (60, 60, 0, 0)
    assert rig.s.sync_count == 60 and rig.s.rejected_count == 0 and rig.s.no_edge_count == 0
    assert rig.s.ticks_to_gps(T0 + 59 * S) == (20721, SOD0 + 59, 0)
    assert rig.s._pps_period_us == S


@pytest.mark.parametrize("offset_us", [-50 * MS, -10 * MS, 0, 10 * MS, 50 * MS])
def test_an_edge_within_50_ms_of_a_whole_second_is_accepted(rig, offset_us):
    rig.edge(T0)
    rig.edge(T0 + S + offset_us)
    assert rig.counts() == (2, 2, 0, 0)


@pytest.mark.parametrize("offset_us", [-60 * MS, -51 * MS, 51 * MS, 60 * MS, 250 * MS, 500 * MS - 1])
def test_an_edge_more_than_50_ms_from_a_whole_second_is_rejected(rig, offset_us):
    rig.edge(T0)
    rig.edge(T0 + S + offset_us)
    assert rig.counts() == (2, 1, 1, 0)


def test_the_tolerance_is_a_constructor_argument(pps):
    mod, clock = pps
    s = mod.PPSTimeSync(tolerance_us=5 * MS)
    r = Rig(mod, clock)
    r.s = s
    r.edge(T0)
    r.edge(T0 + S + 10 * MS)                     # fine at 50 ms, a glitch at 5 ms
    assert (s.pps_accepted, s.pps_rejected) == (1, 1)
    r.edge(T0 + S + 4 * MS)
    assert (s.pps_accepted, s.pps_rejected) == (2, 1)


def test_the_reference_is_the_last_accepted_edge_not_the_last_edge_seen(rig):
    """A glitch must not shift the reference: the real edge 1 s after the LAST GOOD edge still passes
    even though it is only 0.6 s after the glitch."""
    rig.edge(T0)
    rig.edge(T0 + 400 * MS)                      # glitch
    rig.edge(T0 + S)                             # real: 1.0 s after T0, 0.6 s after the glitch
    assert rig.counts() == (3, 2, 1, 0)


def test_jitter_does_not_accumulate_because_each_edge_is_judged_against_the_previous_good_one(rig):
    """+40 ms each second (drifting far more than a real receiver would): every step is 1.04 s, inside
    the window, so all are accepted -- the filter bounds edge-to-edge spacing, not absolute phase."""
    t = T0
    rig.edge(t)
    for _ in range(20):
        t += S + 40 * MS
        rig.edge(t)
    assert rig.counts() == (21, 21, 0, 0)


# --- glitch bursts (the observed failure) -----------------------------------------------------------------

def test_a_burst_of_glitches_between_real_edges_is_rejected_and_pairing_stays_correct(rig):
    """~9 spurious edges within a couple of seconds (what plugpack switching produced). Only real edges
    count; each NMEA sentence pairs with the REAL edge of its second, so the anchor tracks the true PPS."""
    rig.second(SOD0, T0)
    glitches = [T0 + 130 * MS, T0 + 260 * MS, T0 + 310 * MS, T0 + 440 * MS, T0 + 470 * MS,
                T0 + 590 * MS, T0 + 720 * MS, T0 + 850 * MS, T0 + 910 * MS]
    for g in glitches:
        rig.edge(g)
    assert rig.counts() == (10, 1, 9, 0)
    rig.second(SOD0 + 1, T0 + S)                 # the real next edge, then its sentence
    assert rig.counts() == (11, 2, 9, 0)
    assert rig.s.sync_count == 2 and rig.s.rejected_count == 0
    assert rig.s.ticks_to_gps(T0 + S) == (20721, SOD0 + 1, 0)       # anchored on the REAL edge, not a glitch


def test_a_glitch_between_the_real_edge_and_its_sentence_does_not_steal_the_pairing(rig):
    """The old code overwrote the pending edge with every edge, so this glitch (edge 200 ms after the
    real one, before the RMC arrives) would have become the anchor -- a 200 ms timing error."""
    rig.second(SOD0, T0)
    rig.edge(T0 + S)                             # real edge for second SOD0+1 ...
    rig.edge(T0 + S + 200 * MS)                  # ... then a glitch before the sentence arrives
    rig.s.feed_nmea(rmc(hhmmss(SOD0 + 1)))
    assert rig.counts() == (3, 2, 1, 0)
    assert rig.s.sync_count == 2 and rig.s.rejected_count == 0
    assert rig.s.ticks_to_gps(T0 + S) == (20721, SOD0 + 1, 0)


def test_the_sanity_check_is_no_longer_what_stops_a_glitch(rig):
    """Previously a mis-paired glitch could only be caught by feed_nmea's 250 ms sanity check
    (rejected_count). Now glitches never reach it: a glitch 120 ms after every real edge is rejected
    by the filter, and every sentence pairs with its real edge."""
    rig.second(SOD0, T0)
    for i in range(1, 30):
        rig.second(SOD0 + i, T0 + i * S)
        rig.edge(T0 + i * S + 120 * MS)
    assert rig.s.rejected_count == 0 and rig.s.sync_count == 30
    assert rig.s.pps_accepted == 30 and rig.s.pps_rejected == 29


def test_known_limitation_a_glitch_just_before_a_real_edge_can_be_accepted_and_the_real_edge_lost(rig):
    """The window cannot tell a glitch from a real edge if the glitch itself lands within +/-50 ms of the
    expected time. Here a glitch at +0.97 s is accepted (interval 0.97 s) and the real edge at +1.00 s is
    then rejected (only 30 ms after the accepted one). The NMEA sentence pairs with the glitch, so the
    anchor is early by 30 ms -- bounded by the tolerance, never worse. Documented, not fixable without a
    second time reference; the tolerance is the knob (PPS_TOLERANCE_US)."""
    rig.second(SOD0, T0)
    rig.edge(T0 + 970 * MS)                      # glitch inside the window: accepted
    rig.edge(T0 + S)                             # the real edge, 30 ms later: rejected
    rig.s.feed_nmea(rmc(hhmmss(SOD0 + 1)))
    assert rig.counts() == (3, 2, 1, 0)
    assert rig.s.sync_count == 2
    # the anchor sits on the glitch, i.e. 30 ms early; ticks_to_gps at the true edge reads +30 ms late
    assert rig.s.ticks_to_gps(T0 + S) == (20721, SOD0 + 1, 30 * MS)
    err_us = rig.s.ticks_to_gps(T0 + S)[2]
    assert err_us <= rig.mod.PPS_TOLERANCE_US
    # ... and the next real edge (1 s after the REAL previous one = 1.03 s after the accepted one) is fine
    rig.second(SOD0 + 2, T0 + 2 * S)
    assert rig.s.ticks_to_gps(T0 + 2 * S) == (20721, SOD0 + 2, 0)     # self-corrects immediately
    assert rig.counts() == (4, 3, 1, 0)


# --- missed edges / resync ---------------------------------------------------------------------------------

def test_a_missed_edge_is_accepted_at_2_s_and_counted_as_a_resync(rig):
    rig.second(SOD0, T0)
    rig.second(SOD0 + 2, T0 + 2 * S)             # the edge for SOD0+1 never came
    assert rig.counts() == (2, 2, 0, 1)
    assert rig.s.sync_count == 2 and rig.s.rejected_count == 0
    assert rig.s.ticks_to_gps(T0 + 2 * S) == (20721, SOD0 + 2, 0)


def test_the_2_s_gap_does_not_pollute_the_measured_pps_period(rig):
    rig.edge(T0)
    rig.edge(T0 + S + 3)
    assert rig.s._pps_period_us == S + 3
    rig.edge(T0 + 3 * S + 3)                     # 2 s gap: accepted, resync, period untouched
    assert rig.s._pps_period_us == S + 3


@pytest.mark.parametrize("k", [2, 3, 4, 5])
def test_gaps_of_up_to_five_seconds_are_accepted_as_missed_edges(rig, k):
    rig.edge(T0)
    rig.edge(T0 + k * S + 30 * MS)
    assert rig.counts() == (2, 2, 0, 1)


def test_a_missed_edge_followed_by_a_glitch_is_still_handled(rig):
    rig.second(SOD0, T0)
    rig.edge(T0 + 1300 * MS)                     # glitch (0.3 s past a whole second)
    rig.second(SOD0 + 2, T0 + 2 * S)             # real edge, 2 s after the last accepted one
    assert rig.counts() == (3, 2, 1, 1)
    assert rig.s.ticks_to_gps(T0 + 2 * S) == (20721, SOD0 + 2, 0)


def test_gaps_longer_than_five_seconds_are_rejected_until_the_reanchor_timeout(rig):
    rig.edge(T0)
    rig.edge(T0 + 5 * S + 700 * MS)              # k = 6 (5.7 s): not a plausible missed-edge count
    assert rig.counts() == (2, 1, 1, 0)
    rig.edge(T0 + 5 * S + 999 * MS)              # still < 6 s: rejected (k=6)
    assert rig.counts() == (3, 1, 2, 0)


def test_after_the_reanchor_timeout_the_next_edge_is_taken_unconditionally(rig):
    rig.edge(T0)
    rig.edge(T0 + 6 * S + 400 * MS)              # 6.4 s after the last accepted edge: a fresh start
    assert rig.counts() == (2, 2, 0, 1)
    rig.edge(T0 + 7 * S + 400 * MS)              # and normal 1 s tracking continues from it
    assert rig.counts() == (3, 3, 0, 1)


def test_a_bad_first_reference_cannot_lock_the_filter_out(rig):
    """The very first edge is a glitch. Real edges then arrive 1 s apart from each other but at odd
    offsets from the glitch, so they are rejected -- until PPS_REANCHOR_US passes, and a real edge
    becomes the new reference. Recovery takes ~6 s."""
    rig.edge(T0)                                 # glitch, taken as the first reference
    accepted_before = rig.s.pps_accepted
    t = T0 + 370 * MS                            # true edges: 0.37 s after the glitch, then every second
    for i in range(6):
        rig.edge(t + i * S)                      # 0.37, 1.37, 2.37, 3.37, 4.37, 5.37 s after T0: all rejected
    assert rig.s.pps_accepted == accepted_before and rig.s.pps_rejected == 6
    rig.edge(t + 6 * S)                          # 6.37 s after the glitch: re-anchor
    assert rig.s.pps_accepted == 2 and rig.s.pps_resync == 1
    rig.edge(t + 7 * S)                          # tracking resumes
    assert rig.s.pps_accepted == 3 and rig.s.pps_rejected == 6


def test_a_glitch_burst_cannot_starve_the_reanchor_of_its_own_edge(rig):
    """Glitches never move the reference, so a burst neither delays nor speeds the timeout."""
    rig.edge(T0)
    for sec in range(6):
        rig.edge(T0 + sec * S + 300 * MS)        # glitches at x.3 s and x.7 s for six seconds: none near
        rig.edge(T0 + sec * S + 700 * MS)        # a whole second from the reference
    assert rig.s.pps_accepted == 1
    rig.edge(T0 + 6 * S + 1)
    assert rig.s.pps_accepted == 2 and rig.s.pps_resync == 1


def test_an_edge_arriving_almost_simultaneously_is_rejected_not_divided_by_zero(rig):
    rig.edge(T0)
    rig.edge(T0)                                  # interval 0 -> k = 0
    rig.edge(T0 + 1)
    assert rig.counts() == (3, 1, 2, 0)


# --- tick wrap-around ------------------------------------------------------------------------------------

def test_the_filter_works_across_the_ticks_wraparound(rig):
    start = TICKS_MASK - 2 * S - 300 * MS        # a few seconds before the counter wraps
    for i in range(6):
        rig.edge(start + i * S)                  # crosses the wrap (masked by the fake clock)
    assert rig.counts() == (6, 6, 0, 0)
    rig.edge(start + 5 * S + 400 * MS)           # a glitch on the far side of the wrap
    assert rig.s.pps_rejected == 1


# --- RMC pairing / counters ----------------------------------------------------------------------------------

def test_nmea_pairs_only_with_accepted_edges_and_consumes_them(rig):
    rig.edge(T0)
    rig.edge(T0 + 300 * MS)                       # rejected: must not become pending
    rig.s.feed_nmea(rmc(hhmmss(SOD0)))
    assert rig.s.sync_count == 1
    assert rig.s.ticks_to_gps(T0) == (20721, SOD0, 0)
    rig.s.feed_nmea(rmc(hhmmss(SOD0)))            # the same edge is not paired twice
    assert rig.s.sync_count == 1 and rig.s.no_edge_count == 1


def test_a_sentence_with_only_a_rejected_edge_pending_counts_as_no_edge(rig):
    """After the first sync consumed the pending edge, glitches alone leave nothing to pair with."""
    rig.second(SOD0, T0)
    rig.edge(T0 + 300 * MS)
    rig.edge(T0 + 700 * MS)
    rig.s.feed_nmea(rmc(hhmmss(SOD0 + 1)))
    assert rig.s.sync_count == 1 and rig.s.no_edge_count == 1


def test_raw_edge_count_equals_accepted_plus_rejected(rig):
    rig.second(SOD0, T0)
    for g in (130, 260, 310):
        rig.edge(T0 + g * MS)
    rig.second(SOD0 + 2, T0 + 2 * S)
    rig.edge(T0 + 2 * S + 40 * MS)               # accepted? 40 ms after the last accepted edge: no (k=0)
    s = rig.s
    assert s.pps_count == s.pps_accepted + s.pps_rejected


def test_status_exposes_the_filter_counters(rig):
    rig.second(SOD0, T0)
    rig.edge(T0 + 300 * MS)
    rig.second(SOD0 + 2, T0 + 2 * S)
    st = rig.s.status
    assert st["pps_accepted"] == 2 and st["pps_rejected"] == 1 and st["pps_resync"] == 1
    assert st["pps_reject_interval_us"] == 300 * MS
    assert st["pps_count"] == 3


# --- ISR safety --------------------------------------------------------------------------------------------

def _function(name):
    tree = ast.parse((ROOT / "pps_time_sync.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


@pytest.mark.parametrize("fn", ["_on_pps", "_accept"])
def test_the_isr_path_is_integer_only_and_allocation_free(fn):
    """The pin IRQ handler must not allocate (floats, containers, strings) -- a heap allocation inside
    an interrupt can fail or corrupt the heap."""
    node = _function(fn)
    banned = (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp,
              ast.GeneratorExp, ast.JoinedStr, ast.Lambda, ast.Try, ast.With, ast.Yield)
    for n in ast.walk(node):
        assert not isinstance(n, banned), ast.dump(n)
        if isinstance(n, ast.Constant):
            assert not isinstance(n.value, (float, str, bytes)) or n is node.body[0].value  # docstring/comment only
        assert not (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)), "true division"
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "?")
              for n in ast.walk(node) if isinstance(n, ast.Call)}
    assert called <= {"ticks_us", "ticks_diff", "_accept"}, called


# --- the client's STATUS line -------------------------------------------------------------------------------

def test_the_client_status_line_reports_the_pps_filter_counters_and_its_format_args_line_up():
    src = (ROOT / "wifi_unit_client.py").read_text()
    tree = ast.parse(src)
    found = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format"
                and isinstance(n.func.value, ast.Constant) and isinstance(n.func.value.value, str)
                and n.func.value.value.startswith("# STATUS ")):
            found = n
    assert found is not None
    fmt = found.func.value.value
    for key in ("pps_edges=", "pps_accepted=", "pps_rejected=", "pps_resync=", "sync_count=",
                "sync_rejected=", "no_edge="):
        assert key in fmt, key
    assert fmt.count("{") == len(found.args)                    # every placeholder has exactly one argument
    for key in ("pps_count", "pps_accepted", "pps_rejected", "pps_resync", "sync_count", "rejected_count",
                "no_edge_count"):
        assert f's["{key}"]' in src
