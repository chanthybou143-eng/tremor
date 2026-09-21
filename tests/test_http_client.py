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


def _fake_getaddrinfo(host, port):
    return [(2, 1, 6, "", ("203.0.113.1", port))]


class FakeSocket:
    def __init__(self, connect_exc=None):
        self._connect_exc = connect_exc
        self.timeout = None
        self.connected = False
        self.closed = False

    def settimeout(self, s):
        self.timeout = s

    def connect(self, addr):
        if self._connect_exc is not None:
            raise self._connect_exc
        self.connected = True

    def close(self):
        self.closed = True


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
    """Advances by `step` seconds on every call -- used to simulate
    cumulative elapsed time across several stages, each of which
    "completes" instantly from the fake socket's point of view."""

    def __init__(self, step):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_successful_post_returns_status_headers_body():
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    status, headers, body = timeout_post(
        "example.invalid", "/api/ingest", b'{"a":1}',
        socket_factory=lambda: sock,
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
            socket_factory=lambda: sock,
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
    clock = FakeClock(step=0.1)
    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=lambda: sock,
            ssl_wrap_fn=lambda s, server_hostname=None: FakeSSLSocket([]),
            getaddrinfo_fn=_fake_getaddrinfo,
            now_fn=clock,
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
            socket_factory=lambda: sock,
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
            socket_factory=lambda: sock,
            ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert exc_info.value.stage == "read_response"


def test_bad_status_is_returned_not_raised():
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks_for_status(500, b"boom"))
    status, _headers, body = timeout_post(
        "example.invalid", "/api/ingest", b"{}",
        socket_factory=lambda: sock,
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
            socket_factory=lambda: sock,
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
    # Small enough that the fake connect/handshake/send calls themselves
    # (which succeed instantly, no real delay) never look individually
    # slow, but timeout_post() and _read_response_with_deadline() between
    # them call now_fn() several times per stage (once to mark the stage's
    # own start, once per deadline check) -- deliberately not pinned to an
    # exact call count, since that's an implementation detail that can
    # shift; this just needs enough steps that the cumulative total
    # crosses POST_DEADLINE_S well before any real network operation
    # could plausibly take that long.
    clock = FakeClock(step=1.5)

    with pytest.raises(PostStageError) as exc_info:
        timeout_post(
            "example.invalid", "/api/ingest", b"{}",
            socket_factory=lambda: sock,
            ssl_wrap_fn=lambda s, server_hostname=None: ssl_sock,
            getaddrinfo_fn=_fake_getaddrinfo,
            now_fn=clock,
        )
    assert exc_info.value.reason == "overall_deadline_exceeded"
    assert exc_info.value.elapsed_s >= POST_DEADLINE_S


def test_feed_fn_is_called_at_each_stage_and_each_read():
    sock = FakeSocket()
    ssl_sock = FakeSSLSocket(_ok_response_chunks())
    feed_calls = []
    timeout_post(
        "example.invalid", "/api/ingest", b"{}",
        socket_factory=lambda: sock,
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
            socket_factory=lambda: sock,
            ssl_wrap_fn=_stalling_wrap,
            getaddrinfo_fn=_fake_getaddrinfo,
        )
    assert sock.closed is True
