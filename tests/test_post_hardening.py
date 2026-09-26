"""Freeze hardening (2026-09-26): stage logging, DNS cache, watchdog guard, breadcrumb.

Background, measured on the device: getaddrinfo blocks 6.5-7 s with a dead DNS server and has no
timeout; ssl.wrap_socket's handshake honours the socket timeout PER OPERATION, so a handshake of
several 3.6 s waits ran 8.18 s and reset the board through the 8 s watchdog.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http_client import DnsCache, PostStageError, timeout_post  # noqa: E402
from wdt_support import Breadcrumb, WatchdogGuard  # noqa: E402

TICKS_MASK = (1 << 30) - 1


def wrap_diff(a, b):
    """time.ticks_diff: signed, wrap-aware (ticks_ms wraps at 2**30 on MicroPython)."""
    return ((a - b + (1 << 29)) & TICKS_MASK) - (1 << 29)


class Clock:
    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t & TICKS_MASK

    def advance(self, ms):
        self.t += ms


# --- fakes that record the ORDER of everything that happens ----------------------------------------------------

class Rec:
    def __init__(self):
        self.events = []

    def __call__(self, *e):
        self.events.append(e[0] if len(e) == 1 else e)


class Sock:
    def __init__(self, rec, connect_exc=None):
        self.rec, self.connect_exc, self.closed = rec, connect_exc, False

    def settimeout(self, s):
        pass

    def connect(self, addr):
        self.rec("connect()", addr)
        if self.connect_exc:
            raise self.connect_exc

    def close(self):
        self.closed = True


class SSock:
    def __init__(self, rec, write_exc=None, chunks=None):
        self.rec, self.write_exc = rec, write_exc
        self.chunks = list(chunks if chunks is not None else [
            b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\nok"])

    def write(self, d):
        self.rec("write()")
        if self.write_exc:
            raise self.write_exc

    def read(self, n):
        self.rec("read()")
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        pass


def harness(rec, ips=("203.0.113.1",), connect_exc=None, wrap_exc=None, write_exc=None, cache=None, clock=None):
    ip_iter = iter(ips)
    last = {"ip": ips[0]}

    def getaddrinfo(host, port, *a):
        rec("getaddrinfo()")
        try:
            last["ip"] = next(ip_iter)
        except StopIteration:
            pass
        return [(2, 1, 6, "", (last["ip"], port))]

    def wrap(sock, server_hostname=None):
        rec("wrap_socket()")
        if wrap_exc:
            raise wrap_exc
        return SSock(rec, write_exc)
    clk = clock or Clock()
    kw = dict(socket_factory=lambda f, t, p: Sock(rec, connect_exc), ssl_wrap_fn=wrap, getaddrinfo_fn=getaddrinfo,
              now_fn=clk, ticks_diff_fn=wrap_diff, feed_fn=lambda: rec("feed"),
              stage_log_fn=lambda stage, note=None: rec("LOG:" + stage + (":" + note if note else "")))
    if cache is not None:
        kw["dns_cache"] = cache
    return kw


# --- stage logging ----------------------------------------------------------------------------------------------

def test_every_stage_is_logged_before_its_blocking_call_and_done_is_last():
    rec = Rec()
    status, _h, _b = timeout_post("example.invalid", "/x", b"{}", **harness(rec))
    assert status == 202
    ev = [e if isinstance(e, str) else e[0] for e in rec.events]
    order = [e for e in ev if e.startswith("LOG:") or e in ("getaddrinfo()", "connect()", "wrap_socket()", "write()", "read()")]
    first_read = order.index("read()")
    assert order[:first_read] == ["LOG:dns:lookup", "getaddrinfo()", "LOG:connect", "connect()", "LOG:tls_handshake",
                                  "wrap_socket()", "LOG:send", "write()", "LOG:read_response"]
    assert order[-1] == "LOG:done"
    for stage, call in (("LOG:dns:lookup", "getaddrinfo()"), ("LOG:connect", "connect()"),
                        ("LOG:tls_handshake", "wrap_socket()"), ("LOG:send", "write()")):
        assert order.index(stage) < order.index(call)                       # the log line precedes the blocking call


def test_the_last_logged_stage_names_where_a_freeze_happened():
    rec = Rec()
    with pytest.raises(PostStageError) as e:
        timeout_post("example.invalid", "/x", b"{}", **harness(rec, wrap_exc=OSError(110, "ETIMEDOUT")))
    logs = [x for x in rec.events if isinstance(x, str) and x.startswith("LOG:")]
    assert logs[-1] == "LOG:tls_handshake" and "LOG:done" not in logs and e.value.stage == "tls_handshake"


def test_the_watchdog_is_fed_before_the_dns_lookup():
    """The lookup used to be the one blocking call with no feed ahead of it."""
    rec = Rec()
    timeout_post("example.invalid", "/x", b"{}", **harness(rec))
    plain = [e if isinstance(e, str) else e[0] for e in rec.events]
    assert plain.index("feed") < plain.index("getaddrinfo()")


def test_logging_is_optional_and_off_by_default():
    rec = Rec()
    kw = harness(rec)
    kw.pop("stage_log_fn")
    assert timeout_post("example.invalid", "/x", b"{}", **kw)[0] == 202


# --- DNS cache ---------------------------------------------------------------------------------------------------

def make_cache(ips=("203.0.113.1", "203.0.113.2"), fail=None, **kw):
    clock = Clock(1000)
    calls = []
    it = iter(ips)

    def gai(host, port, *a):
        calls.append(host)
        if fail and fail():
            raise OSError(-2, "gaierror")
        return [(2, 1, 6, "", (next(it), port))]
    return DnsCache(gai, clock, wrap_diff, **kw), clock, calls


def test_the_first_resolve_looks_up_and_later_ones_reuse_the_address():
    c, clock, calls = make_cache()
    a, src = c.resolve("h", 443)
    b, src2 = c.resolve("h", 443)
    assert (src, src2) == ("lookup", "cache") and a is b and calls == ["h"]
    assert (c.lookups, c.hits) == (1, 1)


def test_an_entry_expires_after_the_maximum_age_and_is_looked_up_again():
    c, clock, calls = make_cache(max_age_s=60)
    c.resolve("h", 443)
    clock.advance(59_000)
    assert c.resolve("h", 443)[1] == "cache"
    clock.advance(2_000)
    addr, src = c.resolve("h", 443)
    assert src == "lookup" and addr[4][0] == "203.0.113.2" and len(calls) == 2         # picked up the new address


def test_invalidate_forces_a_fresh_lookup_for_one_key_or_everything():
    c, clock, calls = make_cache(ips=("1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"))
    c.resolve("a", 1)
    c.resolve("b", 1)
    assert c.invalidate("a", 1) == 1 and c.invalidate("nope", 9) == 0
    assert c.resolve("a", 1)[1] == "lookup" and c.resolve("b", 1)[1] == "cache"
    assert c.invalidate() == 2 and c.resolve("b", 1)[1] == "lookup" and c.invalidations == 3


def test_ticks_wraparound_does_not_make_a_fresh_entry_look_ancient_or_immortal():
    c, clock, calls = make_cache(max_age_s=3600)
    clock.t = (1 << 30) - 500                                   # 0.5 s before ticks_ms wraps
    c.resolve("h", 443)
    clock.advance(1_000)                                        # now just past the wrap
    assert clock() < 1000 and c.resolve("h", 443)[1] == "cache"
    clock.advance(3_700_000)
    assert c.resolve("h", 443)[1] == "lookup"


def test_a_failed_lookup_falls_back_to_the_last_known_address_but_only_for_a_bounded_time():
    state = {"down": False}
    c, clock, calls = make_cache(ips=("1.1.1.1", "2.2.2.2"), fail=lambda: state["down"], max_age_s=60, stale_ok_s=3600)
    c.resolve("h", 443)
    clock.advance(120_000)
    state["down"] = True                                        # DNS is down, the entry is stale
    addr, src = c.resolve("h", 443)
    assert src == "stale" and addr[4][0] == "1.1.1.1" and c.stale_uses == 1
    clock.advance(4_000_000)                                    # beyond stale_ok_s: better to fail than use a very old address
    with pytest.raises(OSError):
        c.resolve("h", 443)


def test_a_failed_lookup_with_nothing_cached_raises_and_an_invalidated_address_is_not_resurrected():
    state = {"down": True}
    c, clock, calls = make_cache(ips=("1.1.1.1",), fail=lambda: state["down"])
    with pytest.raises(OSError):
        c.resolve("h", 443)
    state["down"] = False
    c.resolve("h", 443)
    c.invalidate("h", 443)                                      # that address just failed to connect
    state["down"] = True
    with pytest.raises(OSError):
        c.resolve("h", 443)


def test_a_zero_max_age_disables_reuse():
    c, clock, calls = make_cache(ips=("1.1.1.1", "2.2.2.2"), max_age_s=0)
    c.resolve("h", 1)
    assert c.resolve("h", 1)[1] == "lookup" and len(calls) == 2 and c.hits == 0


# --- DNS cache inside timeout_post ------------------------------------------------------------------------------------

def test_two_posts_share_one_lookup_and_the_second_logs_a_cache_hit():
    rec = Rec()
    cache = DnsCache(now_fn=Clock(), ticks_diff_fn=wrap_diff)
    kw = harness(rec, cache=cache)
    cache._getaddrinfo = kw["getaddrinfo_fn"]
    timeout_post("example.invalid", "/x", b"{}", **kw)
    timeout_post("example.invalid", "/x", b"{}", **kw)
    assert [e for e in rec.events if e == "getaddrinfo()"] == ["getaddrinfo()"]
    assert "LOG:dns:lookup" in rec.events and "LOG:dns:cache" in rec.events


def test_without_a_cache_every_post_still_looks_up_as_before():
    rec = Rec()
    kw = harness(rec)
    timeout_post("example.invalid", "/x", b"{}", **kw)
    timeout_post("example.invalid", "/x", b"{}", **kw)
    assert rec.events.count("getaddrinfo()") == 2


@pytest.mark.parametrize("kwargs,stage", [
    (dict(connect_exc=OSError(110, "ETIMEDOUT")), "connect"),
    (dict(wrap_exc=OSError(110, "ETIMEDOUT")), "tls_handshake"),
])
def test_a_connect_or_tls_failure_invalidates_the_cached_address(kwargs, stage):
    rec = Rec()
    clock = Clock()
    cache = DnsCache(now_fn=clock, ticks_diff_fn=wrap_diff)
    ok = harness(rec, ips=("203.0.113.1", "203.0.113.2"), cache=cache, clock=clock)
    cache._getaddrinfo = ok["getaddrinfo_fn"]
    timeout_post("example.invalid", "/x", b"{}", **ok)                       # resolves .1 and works
    bad = harness(rec, cache=cache, clock=clock, **kwargs)
    with pytest.raises(PostStageError) as e:
        timeout_post("example.invalid", "/x", b"{}", **bad)                  # cached .1 now fails
    assert e.value.stage == stage and cache.invalidations == 1
    assert cache.resolve("example.invalid", 443)[1] == "lookup"              # the next POST resolves afresh


@pytest.mark.parametrize("kwargs,stage", [
    (dict(write_exc=OSError(104, "ECONNRESET")), "send"),
])
def test_other_failures_keep_the_cached_address(kwargs, stage):
    rec = Rec()
    clock = Clock()
    cache = DnsCache(now_fn=clock, ticks_diff_fn=wrap_diff)
    ok = harness(rec, cache=cache, clock=clock)
    cache._getaddrinfo = ok["getaddrinfo_fn"]
    timeout_post("example.invalid", "/x", b"{}", **ok)
    with pytest.raises(PostStageError) as e:
        timeout_post("example.invalid", "/x", b"{}", **harness(rec, cache=cache, clock=clock, **kwargs))
    assert e.value.stage == stage and cache.invalidations == 0


def test_the_server_moves_to_a_new_ip_and_the_client_recovers_after_one_failed_post():
    """Cached .1 is retired; connect fails; the address is dropped; the next POST looks up .2 and connects to it."""
    rec = Rec()
    clock = Clock()
    cache = DnsCache(now_fn=clock, ticks_diff_fn=wrap_diff)
    world = {"server": "203.0.113.1"}
    lookups = []

    def gai(host, port, *a):
        lookups.append(world["server"])
        return [(2, 1, 6, "", (world["server"], port))]
    cache._getaddrinfo = gai

    def post():
        def factory(f, t, p):
            return SockToIP(rec, world)
        return timeout_post("example.invalid", "/x", b"{}", socket_factory=factory,
                            ssl_wrap_fn=lambda s, server_hostname=None: SSock(rec), getaddrinfo_fn=gai,
                            now_fn=clock, ticks_diff_fn=wrap_diff, dns_cache=cache)
    assert post()[0] == 202
    world["server"] = "203.0.113.2"                                          # the old address stops answering
    with pytest.raises(PostStageError):
        post()
    assert post()[0] == 202
    assert lookups == ["203.0.113.1", "203.0.113.2"]


class SockToIP(Sock):
    """A socket that only connects if the address it is given is the server's CURRENT one."""
    def __init__(self, rec, world):
        super().__init__(rec)
        self.world = world

    def connect(self, addr):
        if addr[0] != self.world["server"]:
            raise OSError(110, "ETIMEDOUT")


# --- watchdog guard -------------------------------------------------------------------------------------------------------

class FakeWdt:
    """A watchdog on a virtual clock: expires if not fed within timeout_ms."""
    def __init__(self, clock, timeout_ms=8000):
        self.clock, self.timeout, self.last, self.reset_at = clock, timeout_ms, clock.t, None

    def feed(self):
        self.last = self.clock.t

    def check(self):
        if self.reset_at is None and self.clock.t - self.last > self.timeout:
            self.reset_at = self.last + self.timeout


def simulate(block_ms, guard=True, window_ms=25000, feed_at_start=True):
    """A POST that blocks the main thread for block_ms while a 1 s timer runs; returns the WDT."""
    clock = Clock()
    wdt = FakeWdt(clock)
    g = WatchdogGuard(wdt.feed, clock, wrap_diff, window_ms=window_ms)
    if feed_at_start:
        wdt.feed()
    if guard:
        g.start()
    for _ in range(block_ms // 1000):
        clock.advance(1000)
        g.tick()
        wdt.check()
    g.stop()
    return wdt, g


def test_without_the_guard_a_12_second_stall_resets_the_board_like_the_overnight_freeze():
    wdt, _ = simulate(12_000, guard=False)
    assert wdt.reset_at == 8000


def test_with_the_guard_the_same_stall_survives():
    wdt, g = simulate(12_000, guard=True)
    assert wdt.reset_at is None and g.feeds == 12 and g.windows == 1 and g.expired == 0


def test_a_genuine_hang_is_still_reset_once_the_window_is_used_up():
    wdt, g = simulate(60_000, guard=True, window_ms=25_000)
    assert wdt.reset_at is not None and 25_000 <= wdt.reset_at <= 25_000 + 8_000 and g.expired == 1


def test_the_guard_does_nothing_outside_a_post_so_a_main_loop_hang_keeps_the_normal_8_seconds():
    clock = Clock()
    wdt = FakeWdt(clock)
    g = WatchdogGuard(wdt.feed, clock, wrap_diff)
    for _ in range(20):
        clock.advance(1000)
        g.tick()
        wdt.check()
    assert wdt.reset_at == 8000 and g.feeds == 0


def test_stop_closes_the_window_immediately():
    clock = Clock()
    wdt = FakeWdt(clock)
    g = WatchdogGuard(wdt.feed, clock, wrap_diff)
    g.start(); clock.advance(1000); g.tick(); g.stop(); clock.advance(1000); g.tick()
    assert g.feeds == 1


def test_the_window_is_measured_across_a_ticks_wrap():
    clock = Clock((1 << 30) - 3000)
    wdt = FakeWdt(clock)
    g = WatchdogGuard(wdt.feed, clock, wrap_diff, window_ms=10_000)
    g.start()
    for _ in range(9):
        clock.advance(1000)
        g.tick()
    assert g.feeds == 9                                              # still inside the window after the wrap
    clock.advance(2000); g.tick()
    assert g.expired == 1


# --- breadcrumb -------------------------------------------------------------------------------------------------------------

class Mem(dict):
    def __missing__(self, k):
        return 0


def test_the_breadcrumb_round_trips_and_is_cleared_after_being_read():
    m = Mem()
    b = Breadcrumb(m)
    b.mark("tls_handshake", 1234, 987_654)
    assert b.read_and_clear() == {"stage": "tls_handshake", "post_no": 1234, "at_ms": 987_654}
    assert b.read_and_clear() is None                                # cannot be reported twice


def test_every_stage_has_a_distinct_code():
    m = Mem()
    b = Breadcrumb(m)
    seen = []
    for st in Breadcrumb.STAGES:
        b.mark(st, 1, 5)
        seen.append(b.read_and_clear()["stage"])
    assert seen == list(Breadcrumb.STAGES)


def test_only_the_watchdog_scratch_words_0_to_2_are_touched():
    m = Mem()
    Breadcrumb(m).mark("dns", 7, 9)
    base = Breadcrumb.BASE
    assert sorted(m) == [base, base + 4, base + 8] and base == 0x400D800C     # SCRATCH4..7 (bootrom reboot API) untouched


def test_memory_without_the_magic_number_reports_nothing():
    m = Mem({Breadcrumb.BASE: 0xDEADBEEF, Breadcrumb.BASE + 4: 0x01020304})
    assert Breadcrumb(m).read_and_clear() is None
    assert Breadcrumb(Mem()).read_and_clear() is None                   # a fresh power-on: registers are zero


def test_a_half_written_record_is_never_valid_because_the_magic_is_written_last():
    m = Mem()

    class Interrupted(Mem):
        def __setitem__(self, k, v):
            if k == Breadcrumb.BASE:                                      # the reset hits before the magic lands
                return
            super().__setitem__(k, v)
    m = Interrupted()
    Breadcrumb(m).mark("connect", 1, 1)
    assert Breadcrumb(m).read_and_clear() is None


def test_large_values_are_packed_without_corrupting_neighbouring_fields():
    m = Mem()
    b = Breadcrumb(m)
    b.mark("read_response", 0xFFFFFF, 0xFFFFFFFF)
    assert b.read_and_clear() == {"stage": "read_response", "post_no": 0xFFFFFF, "at_ms": 0xFFFFFFFF}
    b.mark("send", 0x1FFFFFF, 2**32 + 5)                                  # overflowing inputs are truncated, never spill
    r = b.read_and_clear()
    assert r["stage"] == "send" and r["post_no"] == 0xFFFFFF and r["at_ms"] == 5


# --- wiring in wifi_unit_client.py (static: it cannot be imported on the host) -----------------------------------------------

SRC = (ROOT / "wifi_unit_client.py").read_text()


def test_client_passes_the_cache_and_stage_log_and_wraps_the_post_in_the_guard():
    tree = ast.parse(SRC)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "timeout_post"]
    assert len(calls) == 1
    kws = {k.arg for k in calls[0].keywords}
    assert {"dns_cache", "stage_log_fn", "feed_fn", "extra_headers"} <= kws
    start, stop = SRC.index("_wdt_guard.start()"), SRC.index("_wdt_guard.stop()")
    post = SRC.index("timeout_post(\n")
    assert start < post < stop                                             # opened before the POST, closed in its finally
    finally_pos = SRC.rindex("finally:", 0, stop)
    assert post < finally_pos < stop


def test_client_reads_the_breadcrumb_at_boot_before_the_hardware_starts_and_reports_a_previous_freeze():
    assert SRC.index("_breadcrumb.read_and_clear()") < SRC.index("adc = ADC(26)")
    assert "# PREV_FREEZE last_stage=" in SRC and 'BOOT_RESET_CAUSE_NAME == "WDT_RESET"' in SRC


def test_client_guard_is_a_bounded_timer_and_can_be_disabled():
    assert "POST_WDT_GUARD_MS = 25000" in SRC and "if wdt is not None and POST_WDT_GUARD_MS:" in SRC
    assert "callback=_wdt_guard.tick" in SRC and "period=1000" in SRC


def test_client_invalidates_the_dns_cache_on_a_wrong_answer_and_on_every_third_failure():
    assert SRC.count("_dns_cache.invalidate()") == 2
    assert "if _consecutive_failures % 3 == 0:" in SRC


def test_status_line_reports_the_new_counters():
    for key in ("dns_lookups=", "dns_hits=", "dns_stale=", "dns_inval=", "guard_windows=", "guard_feeds=", "guard_expired="):
        assert key in SRC
