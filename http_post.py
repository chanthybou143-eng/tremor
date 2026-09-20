"""Pure HTTP/1.0 request assembly -- no sockets, no MicroPython-only
imports, so this is host-testable (see tests/test_http_post.py) even
though wifi_unit_client.py itself isn't (it imports MicroPython-only
network/machine modules and can't be run off-device).

Split out specifically so the exact bytes sent over the wire for a POST
can be checked byte-for-byte against PythonAnywhere's known requirement:
a response is held for ~15s on any connection not explicitly told to
close (see the keep-alive investigation, commit history) -- a header
block assembled by hand is worth testing directly, not just eyeballing.
"""


def build_post_request(host, path, body_bytes, content_type="application/json"):
    """Return a complete HTTP/1.0 POST request (request line + headers +
    a blank line + body) as bytes, ready to write to a socket in one
    call.

    "Connection: close" is sent explicitly even though HTTP/1.0 already
    defaults to closing the connection -- PythonAnywhere is known to hold
    a response for ~15s on any connection it doesn't see explicitly told
    to close, regardless of protocol version.

    Content-Length is len(body_bytes) -- body_bytes must already be
    encoded (the caller's job, e.g. json.dumps(payload).encode()), so
    this is always a byte count, never a character count. Those two only
    diverge for non-ASCII content, which this deployment's JSON payloads
    (unit_id, floats) never contain, but getting Content-Length wrong for
    even one byte breaks the request, so it's worth being exact about
    which count this is -- see the test for a case where they'd differ.
    """
    headers = (
        "POST /{path} HTTP/1.0\r\n"
        "Host: {host}\r\n"
        "Content-Type: {content_type}\r\n"
        "Content-Length: {length}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).format(path=path, host=host, content_type=content_type, length=len(body_bytes))
    return headers.encode() + body_bytes
