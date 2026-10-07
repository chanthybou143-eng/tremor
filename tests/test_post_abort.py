"""Worst-case POST duration with http_client.SocketAborter: a slow uplink must never reset the board.

The real timeout_post() and WatchdogGuard run against a millisecond-resolution fake world that models
the MicroPython facts the bound rests on (http_client.POST_ABORT_MS has the derivation):
  * every lwIP wait loop (connect / recv / send) re-reads the socket's CURRENT timeout on each pass;
  * the guard is a soft timer: its callback runs at those poll points (and in mp_hal_delay_ms), but
    NOT during CPU-bound code such as TLS crypto -- there it is deferred to the next poll point;
  * lwIP's tcp_write ERR_MEM retry loop delays up to 200 x 50 ms without looking at the timeout;
  * getaddrinfo has no timeout (<= 7 s measured).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_client  # noqa: E402
from http_client import (ABORTED_REASON, ABORT_SOCKET_TIMEOUT_S, POST_ABORT_MS, SOCKET_OP_TIMEOUT_S,  # noqa: E402
                         PostStageError, SocketAborter, timeout_post)
from wdt_support import WatchdogGuard  # noqa: E402

GUARD_WINDOW_MS = 25_000          # wifi_unit_client.POST_WDT_GUARD_MS
WDT_MS = 8_000
REQUIRED_MARGIN_MS = 3_500        # every scenario must end at least this far inside the guard window
OK = b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\nok"


class World:
    def __init__(self, abort=True, phase_ms=0):
        self.t = phase_ms                             # where in the guard timer's 1 s period the POST starts
        self.pending_tick = False
        self.reset_at = None
        self.last_feed = phase_ms
        self.aborter = SocketAborter() if abort else None
        self.guard = WatchdogGuard(self.feed, lambda: self.t, lambda a, b: a - b, window_ms=GUARD_WINDOW_MS,
                                   wdt_timeout_ms=WDT_MS, abort_after_ms=POST_ABORT_MS if abort else None,
                                   abort_fn=self.aborter.fire if abort else None)

    # the hardware watchdog + the guard's 1 s soft timer
    def feed(self):
        self.last_feed = self.t

    def main_feed(self):
        self.feed()
        self.guard.note_main_feed()

    def _ms(self, callbacks_run):
        self.t += 1
        if self.t % 1000 == 0:
            if callbacks_run:
                self.guard.tick()
            else:
                self.pending_tick = True            # soft IRQ scheduled, runs at the next poll point
        if self.reset_at is None and self.t - self.last_feed > WDT_MS:
            self.reset_at = self.t

    def poll_point(self):
        if self.pending_tick:
            self.pending_tick = False
            self.guard.tick()

    # primitives a fake socket / TLS layer is built from
    def wait(self, sock, ms):
        """An lwIP wait loop: done after ms, unless the socket's (current) timeout runs out first."""
        waited = 0
        while waited < ms:
            self.poll_point()
            self._ms(True)
            waited += 1
            if waited > sock.timeout_ms:
                raise OSError(110, "ETIMEDOUT")

    def cpu(self, ms):
        for _ in range(ms):
            self._ms(False)

    def errmem(self, ms):
        for _ in range(ms):                          # mp_hal_delay_ms: callbacks run, timeout ignored
            self.poll_point()
            self._ms(True)


class Sock:
    def __init__(self, world, connect_ms):
        self.w, self.connect_ms, self.timeout_ms = world, connect_ms, None

    def settimeout(self, s):
        self.timeout_ms = s * 1000

    def connect(self, addr):
        self.w.wait(self, self.connect_ms)

    def close(self):
        pass


def run_ops(world, sock, ops):
    for kind, ms in ops:
        if kind == "wait":
            world.wait(sock, ms)
        elif kind == "cpu":
            world.cpu(ms)
        elif kind == "errmem":
            world.errmem(ms)


class SSock:
    def __init__(self, world, raw, send_ops, read_ops):
        self.w, self.raw, self.send_ops, self.read_ops = world, raw, list(send_ops), list(read_ops)
        self.body = OK

    def write(self, data):
        run_ops(self.w, self.raw, self.send_ops)

    def read(self, n):
        if self.read_ops:
            run_ops(self.w, self.raw, [self.read_ops.pop(0)])
            if self.read_ops:
                return b"H" if not self.body.startswith(b"H") else self._trickle()
        out, self.body = self.body, b""
        return out

    def _trickle(self):
        out, self.body = self.body[:1], self.body[1:]
        return out

    def close(self):
        pass


def post(world, dns_ms=0, connect_ms=150, handshake=(("wait", 300), ("cpu", 600), ("wait", 300)),
         send=(("wait", 200),), read=(("wait", 400),)):
    """One timeout_post() inside a guard window, exactly as wifi_unit_client._post_batch runs it.
    Returns (duration_ms, result_or_exception)."""
    def gai(host, port, *a):
        world.cpu(0)
        for _ in range(dns_ms):                      # lwIP DNS wait: callbacks run, no timeout
            world.poll_point()
            world._ms(True)
        return [(2, 1, 6, "", ("203.0.113.1", port))]

    holder = {}

    def factory(f, t, p):
        holder["sock"] = Sock(world, connect_ms)
        return holder["sock"]

    def wrap(sock, server_hostname=None):
        run_ops(world, sock, handshake)
        return SSock(world, sock, send, read)

    if world.aborter is not None:
        world.aborter.arm()
    world.main_feed()
    start = world.t
    world.guard.start()
    try:
        result = timeout_post("example.invalid", "/api/ingest", b"{}", socket_factory=factory, ssl_wrap_fn=wrap,
                              getaddrinfo_fn=gai, now_fn=lambda: world.t, ticks_diff_fn=lambda a, b: a - b,
                              feed_fn=world.main_feed, aborter=world.aborter)
    except PostStageError as exc:
        result = exc
    finally:
        world.guard.stop()
        if world.aborter is not None:
            world.aborter.disarm()
    world.main_feed()                                # the main loop resumes and feeds
    return world.t - start, result


TRICKLE = 3_900                                       # each wait just under SOCKET_OP_TIMEOUT_S

SCENARIOS = {
    "tls handshake trickles (8.18 s seen on device, here unbounded)": dict(handshake=[("wait", TRICKLE)] * 12),
    "crypto straddles the abort (timer deferred)": dict(handshake=[("wait", TRICKLE), ("wait", TRICKLE),
                                                                   ("wait", 1_800), ("cpu", 2_000),
                                                                   ("wait", TRICKLE)] + [("wait", TRICKLE)] * 5),
    "request body trickles out": dict(send=[("wait", TRICKLE)] * 12),
    "lwIP ERR_MEM retry loop at the abort": dict(send=[("wait", TRICKLE), ("wait", TRICKLE), ("wait", 800),
                                                       ("errmem", 10_000), ("wait", TRICKLE), ("wait", TRICKLE)]),
    "response trickles in": dict(read=[("wait", TRICKLE)] * 12),
    "dns miss (7 s) then a trickling handshake": dict(dns_ms=7_000, handshake=[("wait", TRICKLE)] * 6),
    "slow connect then trickling handshake": dict(connect_ms=TRICKLE, handshake=[("wait", TRICKLE)] * 6),
}


def test_the_abort_fires_well_inside_the_guard_window():
    assert POST_ABORT_MS + 1000 + 10_000 + REQUIRED_MARGIN_MS <= GUARD_WINDOW_MS    # abort + tick + ERR_MEM tail
    assert ABORT_SOCKET_TIMEOUT_S * 1000 < 50 and SOCKET_OP_TIMEOUT_S == 4.0


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_without_the_abort_these_uplinks_reset_the_board_or_come_close(name):
    w = World(abort=False)
    duration, _ = post(w, **SCENARIOS[name])
    assert duration > 15_000 or w.reset_at is not None or "response" in name   # the read loop has its own 10 s check


PHASES = (0, 1, 437, 999)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_with_the_abort_every_slow_uplink_ends_inside_the_guard_window_and_never_resets(name, phase):
    w = World(abort=True, phase_ms=phase)
    duration, result = post(w, **SCENARIOS[name])
    assert w.reset_at is None and w.guard.expired == 0
    assert duration <= GUARD_WINDOW_MS - REQUIRED_MARGIN_MS, duration
    assert isinstance(result, PostStageError) and result.reason.startswith(ABORTED_REASON)
    if "ERR_MEM" not in name and "crypto" not in name:
        assert duration <= POST_ABORT_MS + 1_000 + 10                 # within a tick (+ the 5 ms timeout) of the abort


def worst_cases():
    return {name: max(post(World(abort=True, phase_ms=ph), **kw)[0] for ph in PHASES) for name, kw in SCENARIOS.items()}


def test_worst_cases_by_mechanism():
    """The table in http_client.POST_ABORT_MS, measured over timer phases."""
    got = worst_cases()
    assert max(got.values()) == got["lwIP ERR_MEM retry loop at the abort"]
    assert 19_000 <= got["lwIP ERR_MEM retry loop at the abort"] <= 21_100          # ~ abort time + the 10 s loop
    assert got["crypto straddles the abort (timer deferred)"] <= POST_ABORT_MS + 1_000 + 2_000 + 10
    for name in SCENARIOS:
        if "ERR_MEM" not in name and "crypto" not in name:
            assert POST_ABORT_MS <= got[name] <= POST_ABORT_MS + 1_000 + 10


def test_aborted_posts_are_reported_as_such_with_their_stage():
    w = World()
    _, exc = post(w, **SCENARIOS["tls handshake trickles (8.18 s seen on device, here unbounded)"])
    assert exc.stage == "tls_handshake" and exc.reason.startswith(ABORTED_REASON)
    _, exc = post(w, **SCENARIOS["request body trickles out"])
    assert exc.stage == "send" and exc.reason.startswith(ABORTED_REASON)
    assert w.aborter.aborts == 2 and w.guard.aborts == 2


def test_a_normal_post_is_untouched():
    w = World()
    duration, result = post(w)
    assert result[0] == 202 and duration < 3_000 and w.aborter.aborts == 0 and w.guard.aborts == 0


def test_a_slow_but_working_post_under_the_abort_time_still_succeeds():
    w = World()
    duration, result = post(w, handshake=[("wait", 2_500), ("cpu", 1_000), ("wait", 2_500)], send=[("wait", 2_000)])
    assert result[0] == 202 and 8_000 < duration < POST_ABORT_MS and w.aborter.aborts == 0


def test_the_abort_resets_for_the_next_post():
    w = World()
    post(w, **SCENARIOS["request body trickles out"])
    duration, result = post(w)
    assert result[0] == 202 and not w.aborter.fired


def test_a_fire_before_the_socket_exists_is_applied_when_it_is_attached():
    class S:
        timeout = None

        def settimeout(self, s):
            self.timeout = s
    a = SocketAborter()
    a.arm()
    a.fire()
    s = S()
    s.settimeout(SOCKET_OP_TIMEOUT_S)
    a.attach(s)
    assert s.timeout == ABORT_SOCKET_TIMEOUT_S and a.aborts == 1
    a.fire()
    assert a.aborts == 1                                              # once per POST


def test_a_broken_socket_or_abort_fn_never_stops_the_guard_feeding():
    class Bad:
        def settimeout(self, s):
            raise OSError("closed")
    a = SocketAborter()
    a.arm()
    a.attach(Bad())
    a.fire()                                                          # swallowed
    fed = []

    def boom():
        raise RuntimeError("x")
    t = [0]
    g = WatchdogGuard(lambda: fed.append(t[0]), lambda: t[0], lambda x, y: x - y, abort_after_ms=1000, abort_fn=boom)
    g.start()
    for _ in range(3):
        t[0] += 1000
        g.tick()
    assert len(fed) == 3 and g.aborts == 1


def test_the_client_wires_the_aborter_into_the_guard_and_the_post():
    src = (ROOT / "wifi_unit_client.py").read_text()
    assert "abort_after_ms=POST_ABORT_MS, abort_fn=_post_aborter.fire" in src
    assert "aborter=_post_aborter)" in src
    arm, start, post_call, disarm = (src.index("_post_aborter.arm()"), src.index("_wdt_guard.start()"),
                                     src.index("timeout_post(\n"), src.index("_post_aborter.disarm()"))
    assert arm < start < post_call < disarm
    assert "SLOW_POST_LOG_S = 15.0" in src and "# SLOW_POST duration_ms=" in src
    assert http_client.POST_ABORT_MS == 10_000
