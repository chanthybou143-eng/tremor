"""Host-side tests for http_client.py's timeout_post(): every network
touch point (getaddrinfo/socket/ssl.wrap_socket) is injected as a fake,
so these never open a real connection. Covers the specific failure
shapes Trial 3's freeze motivated: a stall at each individual stage
(each should raise PostStageError tagged with that stage), and the
overall deadline tripping even when no single stage's fake call ever
raises a timeout itself -- the scenario a per-operation timeout alone
cannot catch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from http_client import (  # noqa: E402
    PostStageError,
    POST_DEADLINE_S,
    parse_https_url,
    timeout_post,
)


def test_parse_https_url_splits_host_port_path():
    assert parse_https_url("https://tremorgrid.pythonanywhere.com/api/ingest") == (
        "tremorgrid.pythonanywhere.com", 443, "/api/ingest")


def test_parse_https_url_with_explicit_port():
    assert parse_https_url("https://example.invalid:8443/x") == ("example.invalid", 8443, "/x")


def test_parse_https_url_rejects_non_https():
    with pytest.raises(ValueError):
        parse_https_url("http://example.invalid/x")


def _fake_getaddrinfo(host, port, *args):
    # Real getaddrinfo is called as getaddrinfo(host, port, 0,
    # socket.SOCK_STREAM) -- accepts and ignores the extra filter args,
    # same as it would if called with none.
    return [(2, 1, 6, "", ("203.0.113.1", port))]


def _fake_ticks_diff(a, b):
    return a - b


class FakeSocket:
    def __init__(self, connect_exc=None):
        self._connect_exc = connect_exc
        self.timeout = None
        self.connected = False
        self.closed = False
        self.family = None
        self.type = None
        self.proto = None

    def settimeout(self, s):
        self.timeout = s

    def connect(self, addr):
        if self._connect_exc is not None:
            raise self._connect_exc
        self.connected = True

    def close(self):
        self.closed = True


def _fake_socket_factory(sock):
    """Returns a socket_factory callable matching timeout_post()'s real
    call shape (family, type, proto), recording what it was called with
    onto `sock` and returning it regardless -- lets most tests ignore the
    args while test_socket_factory_receives_resolved_family_type_proto
    checks them specifically."""
    def _factory(family, type, proto):
        sock.family, sock.type, sock.proto = family, type, proto
        return sock
    return _factory


class FakeSSLSocket:
    """Duck-typed stand-in for what ssl.wrap_socket() returns: .write(),
    .read(n), .close(). `chunks` is a list of byte-strings (or OSError
    instances, raised in place) returned/raised one at a time per call
    to .read(), regardless of the requested n -- enough control to
    simulate a slow trickle or a stall at an exact point in the response.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    def read(self, n):
        if not self._chunks:
            return b""
        item = self._chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def _ok_response_chunks(body=b'{"ok":true}'):
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n\r\n"
    ).format(len(body)).encode("utf-8")
    return [head + body]


class FakeClock:
    """Advances by `step` on every call -- stands in for time.ticks_ms()
    (production's now_fn default), so `step`'s magnitude is in the same
    units timeout_post() compares against POST_DEADLINE_S*1000, NOT
    seconds. Used to simulate cumulative elapsed ticks across several
    stages, each of which "completes" instantly from the fake socket's
    point of view. Pair with _fake_ticks_diff as ticks_diff_fn when a
    test cares about that being exercised explicitly (the host fallback
    in http_client.py works too, but doesn't demonstrate injection)."""

    def __init__(self, step):
        self.t = 0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_successful_post_returns_status_headers_body():
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    status, headers, body = timeout_post(
        "example.invalid", "/api/ingest", b'{"a":1}',
        socket_factory=_fake_socket_factory(sock),
        ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
        getaddrinfo_fn=_fake_getaddrinfo,
    )
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert body == b'{"ok":true}'
    assert sock.timeout == pytest.approx(4.0)
    assert ssl_sock.closed is True


def test_connect_stall_raises_stage_connect():
    sock = FakeSocket(connect_exc=OSError("timed out"))
    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=lambda s, server_hostname=None: FakeSSLSocket([]),
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert exc_info.value.stage == "connect"


def test_stage_error_carries_timing_fields_even_for_a_plain_oserror():
    """Trial 4 needed to manually cross-reference a POST_FAIL line against
    a separately-measured duration to notice the deadline clock itself
    was misbehaving -- elapsed_s/stage_duration_s/deadline_s put that
    directly on every PostStageError, not just the deadline-exceeded
    ones, so this shouldn't only work for _check_deadline's own raises."""
    sock = FakeSocket(connect_exc=OSError("connection refused"))
    clock = FakeClock(step=100)  # 100 "ms" per call -- tiny relative to POST_DEADLINE_S*1000
    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=lambda s, server_hostname=None: FakeSSLSocket([]),
            getaddrinfo_fn=_fake_getaddrinfo,
            now_fn=clock,
            ticks_diff_fn=_fake_ticks_diff,
        )
    assert exc_info.value.stage == "connect"
    assert exc_info.value.reason == "connection refused"
    assert exc_info.value.elapsed_s is not None
    assert exc_info.value.stage_duration_s is not None
    assert exc_info.value.deadline_s == POST_DEADLINE_S


def test_handshake_stall_raises_stage_tls_handshake():
    sock = FakeSocket()

    def _stalling_wrap(s, server_hostname=None):
        raise OSError("timed out")

    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=_stalling_wrap,
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert exc_info.value.stage == "tls_handshake"


def test_mid_response_stall_raises_stage_read_response():
    sock = FakeSocket()
    # First read returns a partial header (no terminator yet), second
    # read raises -- simulating a connection that accepted the request
    # but then stalls partway through sending the response.
    ssl_sock = FakeSSLSocket([b"HTTP/1.1 200 OK\r\n", OSError("timed out")])
    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert exc_info.value.stage == "read_response"


def test_bad_status_is_returned_not_raised():
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks_for_status(500, b"boom"))
    status, _headers, body = timeout_post(
        "example.invalid", "/api/ingest", b"{}",
        socket_factory=_fake_socket_factory(sock),
        ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
        getaddrinfo_fn=_fake_getaddrinfo,
    )
    assert status == 500
    assert body == b"boom"


def _ok_response_chunks_for_status(status, body):
    head = (
        "HTTP/1.1 {} Error\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n\r\n"
    ).format(status, len(body)).encode("utf-8")
    return [head + body]


def test_chunked_response_raises_stage_read_response():
    sock = FakeSocket()
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Transfer-Encoding: chunked\r\n\r\n"
    ).encode("utf-8")
    ssl_sock = FakeSSLSocket([head])
    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert exc_info.value.stage == "read_response"
    assert "chunked" in exc_info.value.reason


def test_overall_deadline_enforced_even_when_each_stage_is_individually_fast():
    """The scenario a per-operation timeout alone cannot catch: connect,
    handshake and send each "complete" instantly (no real delay, no
    per-call timeout tripped), but the injected clock advances enough
    between checks that the cumulative elapsed time crosses
    POST_DEADLINE_S before the read stage starts."""
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    # In fake-ticks-ms units (matching production, which compares against
    # POST_DEADLINE_S*1000): large enough that a handful of now_fn() calls
    # (timeout_post()/_read_response_with_deadline() each call it several
    # times per stage -- once to mark the stage's own start, once per
    # deadline check) cross the 10000ms deadline, while each fake
    # connect/handshake/send call itself still "completes" instantly, no
    # real delay. Deliberately not pinned to an exact call count, since
    # that's an implementation detail that can shift.
    clock = FakeClock(step=2000)

    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
            getaddrinfo_fn=_fake_getaddrinfo,
            now_fn=clock,
            ticks_diff_fn=_fake_ticks_diff,
        )
    assert exc_info.value.reason == "overall_deadline_exceeded"
    assert exc_info.value.elapsed_s >= POST_DEADLINE_S


def test_post_not_aborted_even_if_real_time_time_jumps_wildly(monkeypatch):
    """The actual Trial 4 bug, made unrepeatable: this module used to
    default to time.time() for its deadline, and real time.time() was
    observed jumping by >=10s in well under 150ms of real elapsed time on
    real hardware. Confirms the fix holds even in the worst case: with
    the REAL time.time() mocked to jump around unpredictably (exactly
    that misbehaviour), timeout_post() -- now driven entirely by an
    injected ticks-style clock, never time.time() -- is completely
    unaffected and the POST still succeeds."""
    call_count = [0]

    def _chaotic_time():
        call_count[0] += 1
        # A huge, inconsistent jump every call -- if timeout_post() (or
        # anything it calls) still secretly depended on this, any
        # deadline check would immediately see it and abort.
        return call_count[0] * 1000.0

    monkeypatch.setattr(time, "time", _chaotic_time)

    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    ticks_clock = FakeClock(step=5)  # advances slowly and normally, unlike the mocked time.time()

    status, _headers, _body = timeout_post(
        "example.invalid", "/api/ingest", b"{}",
        socket_factory=_fake_socket_factory(sock),
        ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
        getaddrinfo_fn=_fake_getaddrinfo,
        now_fn=ticks_clock,
        ticks_diff_fn=_fake_ticks_diff,
    )
    assert status == 200
    # Confirms this isn't passing by coincidence -- timeout_post() never
    # actually called the chaotic time.time() at all.
    assert call_count[0] == 0


def test_socket_factory_receives_resolved_family_type_proto():
    """Aligns with urequests.py's own call shape: getaddrinfo(host, port,
    0, socket.SOCK_STREAM) then socket.socket(family, type, proto) from
    the resolved tuple, rather than a bare socket.socket() relying on
    this platform's default family/type/proto."""
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    timeout_post(
        "example.invalid", "/api/ingest", b"{}",
        socket_factory=_fake_socket_factory(sock),
        ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
        getaddrinfo_fn=_fake_getaddrinfo,
    )
    assert (sock.family, sock.type, sock.proto) == (2, 1, 6)


def test_feed_fn_is_called_at_each_stage_and_each_read():
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    feed_calls = []
    timeout_post(
        "example.invalid", "/api/ingest", b"{}",
        socket_factory=_fake_socket_factory(sock),
        ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
        getaddrinfo_fn=_fake_getaddrinfo,
        feed_fn=lambda: feed_calls.append(True),
    )
    # connect, tls_handshake, send stages + at least one read -- exact
    # count isn't the point, just that it's genuinely wired in.
    assert len(feed_calls) >= 4


def test_socket_is_always_closed_on_failure():
    sock = FakeSocket()

    def _stalling_wrap(s, server_hostname=None):
        raise OSError("timed out")

    with pytest.raises(PostStageError):
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=_fake_socket_factory(sock),
            ssl_wrap_fn=_stalling_wrap,
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert sock.closed is True
