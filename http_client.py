"""One-shot HTTPS POST with a bounded per-operation timeout AND a bounded
overall deadline -- replaces `urequests.post()` in wifi_unit_client.py,
which has neither (see that module's docstring: "no default socket
timeout in most urequests forks... a blocking POST can stall the main
loop for longer than the ring buffer's headroom"). Trial 3 hit exactly
that: a ~2 minute WiFi outage produced a ~7 minute full main-loop freeze,
confirmed by elapsed_s staying flat while wall-clock time advanced and
the ADC ring buffer's overflow counter jumping by ~400,000 during the
stall -- one stuck socket call, unbounded.

Deliberately NOT a persistent/keep-alive connection: always opens a
fresh connection and sends "Connection: close", matching urequests' own
behaviour. The keepalive-single-core branch investigated connection
reuse against this specific PythonAnywhere deployment and found it
consistently ~6-7x SLOWER (~15s vs ~2.1-2.6s) plus the connection didn't
survive to be reused anyway -- see that branch's commit 2e365d9. This
module reuses that branch's http_keepalive.py request-building and
Content-Length-based response-reading code (both already written to be
host-testable -- see _read_response_with_deadline's docstring) but drops
the reuse machinery entirely, since reuse is a dead end here, and adds
the one thing that branch never needed: a timeout.

No `machine`/`network` imports -- `socket` and `ssl` both exist in the
CPython stdlib too (just without ever exercising a real connection off
a Pico), so this module imports and its logic is testable on the host
with fake socket-like objects (see tests/test_http_client.py). All
network-touching calls are reached through injectable factory
parameters (socket_factory/ssl_wrap_fn/getaddrinfo_fn) for exactly that
reason -- production code never has to pass them (they default to the
real socket/ssl module functions), only tests do.
"""

import socket
import ssl
import time


class PostStageError(Exception):
    """Raised by timeout_post() for any failure, identifying which stage
    of the request was in progress. `stage` is one of "dns", "connect",
    "tls_handshake", "send", "read_response". `reason` is a short string
    -- either the underlying OSError's message, or
    "overall_deadline_exceeded" for the one timeout case this module
    itself detects as opposed to wrapping a lower-level exception.
    Callers (wifi_unit_client.py's _post_batch) catch this via the
    existing broad `except Exception`, exactly like any other POST
    failure -- it's a normal Exception subclass, not a special control
    path -- but can read .stage/.reason to log which part of the request
    actually got stuck, which a bare `except Exception as exc: str(exc)`
    couldn't distinguish before this module existed.

    elapsed_s/stage_duration_s/deadline_s (all via now_fn, the SAME clock
    timeout_post() itself uses for POST_DEADLINE_S -- see that constant's
    comment) exist specifically to make a now_fn misbehaving visible in
    the log, not just in a debugger: elapsed_s is time since this
    attempt started (any stage), stage_duration_s is time since the
    CURRENT stage started, deadline_s is the configured POST_DEADLINE_S
    for reference. Trial 4 needed manual cross-referencing against
    wifi_unit_client.py's separately-measured (ticks_us-based)
    longest_post_duration_s to notice that a "connect:
    overall_deadline_exceeded" failure's REAL wall-clock duration was
    ~0.147s, not >=10s -- these fields put that same signal directly on
    every POST_FAIL line instead.
    """

    def __init__(self, stage, reason, elapsed_s=None, stage_duration_s=None, deadline_s=None):
        self.stage = stage
        self.reason = reason
        self.elapsed_s = elapsed_s
        self.stage_duration_s = stage_duration_s
        self.deadline_s = deadline_s
        super().__init__(
            "stage={} reason={} elapsed_s={} stage_duration_s={} deadline_s={}".format(
                stage, reason, elapsed_s, stage_duration_s, deadline_s))


# Applied via sock.settimeout() once, immediately after the socket is
# created and before connect() -- MicroPython's usocket, like CPython's
# socket, applies one timeout value to every blocking call made on that
# socket from then on (connect, the TLS handshake's internal reads/
# writes, send, and each individual recv/read), not just to whichever
# call was active at the moment settimeout() was called. This bounds any
# SINGLE blocking syscall to at most this long.
#
# Value justified against measured normal latency (see
# wifi_unit_client.py's own docstring): a full round trip (DNS+TCP+TLS+
# transfer) measures ~0.5-2.6s total, and the TLS handshake alone
# measures ~1.4s. 4s gives close to 3x headroom over the slowest normal
# SINGLE stage (the handshake) and ~1.5-8x headroom over the normal
# total -- generous enough that ordinary network jitter should not trip
# it, while being nowhere near the multi-minute stall Trial 3 actually
# saw.
SOCKET_OP_TIMEOUT_S = 4.0

# A per-operation timeout alone is NOT enough: four stages (connect,
# handshake, send, read) can each individually complete in just under
# SOCKET_OP_TIMEOUT_S and still sum to ~4x that -- up to ~16s in the
# worst case, 4x the ADC ring buffer's ~4s headroom
# (RING_CAPACITY=4096 samples at ADC_SAMPLE_HZ=1030 in
# wifi_unit_client.py) -- while every individual per-operation timeout
# check technically "passed". A slow trickle of small reads inside the
# read_response stage specifically could stall even longer this way,
# since each individual recv() can legitimately take just under
# SOCKET_OP_TIMEOUT_S without ever tripping it.
#
# POST_DEADLINE_S is a second, coarser wall-clock budget for the WHOLE
# attempt (connect through the last response byte), checked between
# stages AND, inside read_response, between each individual read (see
# _read_response_with_deadline) -- so the attempt is aborted the moment
# TOTAL elapsed time crosses this budget, regardless of which individual
# stage is in progress or how close to its own timeout it was.
#
# 10s is roughly 4-20x the measured normal total (0.5-2.6s) -- generous
# enough that normal jitter never trips it -- while still being a small,
# bounded multiple (~2.5x) of the ring buffer's ~4s headroom, not the
# effectively unbounded stall Trial 3 saw. Some sample loss is still
# plausible in the worst case, since 10s > 4s headroom -- this is an
# accepted, bounded trade-off, not a full fix. Shrinking the deadline
# further to fit inside 4s would risk false-tripping on legitimately
# slow (but working) POSTs; the more complete fix -- moving the POST off
# the main loop via the Pico 2's second core -- is flagged as an open
# design question in wifi_unit_client.py's own docstring and is out of
# scope here.
POST_DEADLINE_S = 10.0


def parse_https_url(url):
    """Splits a URL of the form "https://host[:port]/path" into
    (host, port, path). Only supports https (port defaults to 443), and
    only a bare host[:port]/path shape -- no query string/auth/etc. --
    since this project has exactly one POST endpoint (wifi_config.py's
    INGEST_URL), not a general HTTP client. Intentionally minimal rather
    than a full RFC 3986 parser, and rather than depending on
    urequests.post()'s own (removed) URL-parsing for this.
    """
    if not url.startswith("https://"):
        raise ValueError("only https:// URLs are supported: {!r}".format(url))
    rest = url[len("https://"):]
    host_port, _, path = rest.partition("/")
    path = "/" + path
    host, _, port_str = host_port.partition(":")
    port = int(port_str) if port_str else 443
    return host, port, path


def _stage_error(stage, reason, now_fn, start_at, stage_start_at):
    """Builds a PostStageError with elapsed_s/stage_duration_s/deadline_s
    filled in from the SAME now_fn timeout_post() itself uses -- one
    helper so every raise site (deadline checks and wrapped OSErrors
    alike) reports this consistently, rather than only the deadline
    checks carrying timing information."""
    now = now_fn()
    return PostStageError(
        stage, reason,
        elapsed_s=now - start_at,
        stage_duration_s=now - stage_start_at,
        deadline_s=POST_DEADLINE_S,
    )


def _check_deadline(deadline_at, stage, now_fn, start_at, stage_start_at):
    if now_fn() >= deadline_at:
        raise _stage_error(stage, "overall_deadline_exceeded", now_fn, start_at, stage_start_at)


def _build_request(method, host, path, body_bytes, extra_headers=None):
    headers = {
        "Host": host,
        "Content-Type": "application/json",
        "Content-Length": str(len(body_bytes)),
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    header_lines = "".join("{}: {}\r\n".format(k, v) for k, v in headers.items())
    return "{} {} HTTP/1.1\r\n{}\r\n".format(method, path, header_lines).encode("utf-8") + body_bytes


def _read_response_with_deadline(sock_like, deadline_at, now_fn, feed_fn, start_at,
                                  max_header_bytes=4096):
    """Reads one full HTTP/1.1 response from sock_like (anything with a
    .read(n) method returning bytes, or b""/None at EOF -- adapted from
    the keepalive-single-core branch's http_keepalive.py, which was
    already written this way specifically to be testable under desktop
    Python with a fake in place of a real ssl socket).

    Unlike the original, this also re-checks the overall deadline (and
    feeds the watchdog, if a real feed_fn was passed) before EVERY read,
    not just once at the start of this stage -- a slow trickle of small
    reads could otherwise stay under SOCKET_OP_TIMEOUT_S on every single
    call while the response as a whole takes far longer than
    POST_DEADLINE_S.

    Raises PostStageError(stage="read_response", ...) if the connection
    closes before a full response is read (headers or body), the headers
    are malformed/implausibly large, or the response uses
    Transfer-Encoding: chunked (not supported -- fails loudly rather than
    silently mis-parsing a chunked body, same convention as
    freq_estimator.estimate_frequency's docstring elsewhere in this
    repo). Returns (status_code, headers_dict, body_bytes) on success.
    """
    stage_start_at = now_fn()  # covers the WHOLE read_response stage, not reset per read --
                               # a failure anywhere in this function reports duration since
                               # read_response itself began, not since the last individual read
    buf = b""
    while b"\r\n\r\n" not in buf:
        _check_deadline(deadline_at, "read_response", now_fn, start_at, stage_start_at)
        feed_fn()
        try:
            chunk = sock_like.read(256)
        except OSError as exc:
            raise _stage_error("read_response", str(exc), now_fn, start_at, stage_start_at)
        if not chunk:
            raise _stage_error("read_response", "connection closed while reading response headers",
                                now_fn, start_at, stage_start_at)
        buf += chunk
        if len(buf) > max_header_bytes:
            raise _stage_error("read_response", "response headers too large or malformed",
                                now_fn, start_at, stage_start_at)

    header_bytes, _, body_start = buf.partition(b"\r\n\r\n")
    lines = header_bytes.split(b"\r\n")
    status_parts = lines[0].decode("utf-8", "replace").split(" ", 2)
    if len(status_parts) < 2:
        raise _stage_error("read_response", "malformed status line: {!r}".format(lines[0]),
                            now_fn, start_at, stage_start_at)
    status_code = int(status_parts[1])

    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower().decode("utf-8", "replace")] = v.strip().decode("utf-8", "replace")

    if headers.get("transfer-encoding", "").lower() == "chunked":
        raise _stage_error("read_response", "chunked response body not supported",
                            now_fn, start_at, stage_start_at)

    content_length = int(headers.get("content-length", "0"))
    body = body_start
    while len(body) < content_length:
        _check_deadline(deadline_at, "read_response", now_fn, start_at, stage_start_at)
        feed_fn()
        try:
            chunk = sock_like.read(content_length - len(body))
        except OSError as exc:
            raise _stage_error("read_response", str(exc), now_fn, start_at, stage_start_at)
        if not chunk:
            raise _stage_error("read_response", "connection closed while reading response body",
                                now_fn, start_at, stage_start_at)
        body += chunk

    return status_code, headers, body


def timeout_post(host, path, payload_bytes, port=443, extra_headers=None,
                  socket_factory=None, ssl_wrap_fn=None, getaddrinfo_fn=None,
                  now_fn=None, feed_fn=None):
    """One-shot HTTPS POST with SOCKET_OP_TIMEOUT_S applied to every
    individual blocking call and POST_DEADLINE_S enforced across the
    whole attempt (see both constants' comments above for why both are
    needed). Always closes whatever socket it opened, success or
    failure -- callers must not assume otherwise.

    Raises PostStageError(stage, reason) on any failure. Returns
    (status_code, headers_dict, body_bytes) on success -- callers decide
    what counts as success (wifi_unit_client.py checks 200 <= status <
    300, same as it did with urequests' response.status_code).

    feed_fn, if given, is called at every stage transition and before
    every individual read -- wifi_unit_client.py passes its watchdog's
    .feed as this, so the watchdog only goes unfed while a SINGLE
    blocking call is actually stuck, not for the whole POST_DEADLINE_S
    duration of a legitimately slow but working POST. Defaults to a
    no-op so tests and any other caller don't need to pass one.

    socket_factory/ssl_wrap_fn/getaddrinfo_fn default to the real
    socket.socket/ssl.wrap_socket/socket.getaddrinfo -- tests inject
    fakes instead of touching a real network (see
    tests/test_http_client.py).
    """
    socket_factory = socket_factory or socket.socket
    ssl_wrap_fn = ssl_wrap_fn or ssl.wrap_socket
    getaddrinfo_fn = getaddrinfo_fn or socket.getaddrinfo
    now_fn = now_fn or time.time
    feed_fn = feed_fn or (lambda: None)

    start_at = now_fn()
    deadline_at = start_at + POST_DEADLINE_S
    sock = None
    ssl_sock = None
    try:
        stage_start_at = now_fn()
        try:
            addr_info = getaddrinfo_fn(host, port)
        except OSError as exc:
            raise _stage_error("dns", str(exc), now_fn, start_at, stage_start_at)
        addr = addr_info[0][-1]

        stage_start_at = now_fn()
        _check_deadline(deadline_at, "connect", now_fn, start_at, stage_start_at)
        feed_fn()
        try:
            sock = socket_factory()
            sock.settimeout(SOCKET_OP_TIMEOUT_S)
            sock.connect(addr)
        except OSError as exc:
            raise _stage_error("connect", str(exc), now_fn, start_at, stage_start_at)

        stage_start_at = now_fn()
        _check_deadline(deadline_at, "tls_handshake", now_fn, start_at, stage_start_at)
        feed_fn()
        try:
            ssl_sock = ssl_wrap_fn(sock, server_hostname=host)
        except OSError as exc:
            raise _stage_error("tls_handshake", str(exc), now_fn, start_at, stage_start_at)

        stage_start_at = now_fn()
        _check_deadline(deadline_at, "send", now_fn, start_at, stage_start_at)
        feed_fn()
        request = _build_request("POST", host, path, payload_bytes, extra_headers)
        try:
            ssl_sock.write(request)
        except OSError as exc:
            raise _stage_error("send", str(exc), now_fn, start_at, stage_start_at)

        stage_start_at = now_fn()
        _check_deadline(deadline_at, "read_response", now_fn, start_at, stage_start_at)
        return _read_response_with_deadline(ssl_sock, deadline_at, now_fn, feed_fn, start_at)
    finally:
        if ssl_sock is not None:
            try:
                ssl_sock.close()
            except OSError:
                pass
        elif sock is not None:
            try:
                sock.close()
            except OSError:
                pass
