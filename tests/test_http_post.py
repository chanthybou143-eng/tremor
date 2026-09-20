from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_post import build_post_request  # noqa: E402


def test_build_post_request_produces_exact_bytes():
    body = b'{"unit_id": "unit-1"}'
    result = build_post_request("tremorgrid.pythonanywhere.com", "api/ingest", body)
    expected = (
        "POST /api/ingest HTTP/1.0\r\n"
        "Host: tremorgrid.pythonanywhere.com\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).format(len(body)).encode() + body
    assert result == expected


def test_connection_close_is_always_present():
    # The one header PythonAnywhere is known to require explicitly, or it
    # holds the response for ~15s regardless of HTTP version.
    result = build_post_request("example.com", "x", b"{}")
    assert b"Connection: close\r\n" in result


def test_content_length_counts_encoded_bytes_not_characters():
    # A body with a multi-byte UTF-8 character has byte length != string
    # length -- using len(str) instead of len(bytes) would silently send
    # a wrong Content-Length for any non-ASCII payload. This deployment's
    # JSON is always ASCII, but this is the exact bug class Content-Length
    # mistakes fall into, so it's worth pinning down directly.
    body = "café".encode("utf-8")  # 5 bytes, 4 characters
    assert len(body) == 5
    result = build_post_request("example.com", "x", body)
    assert "Content-Length: {}\r\n".format(len(body)).encode() in result
    assert b"Content-Length: 4\r\n" not in result


def test_headers_end_with_blank_line_before_body():
    body = b"payload-bytes"
    result = build_post_request("example.com", "some/path", body)
    header_block, sep, rest = result.partition(b"\r\n\r\n")
    assert sep == b"\r\n\r\n"
    assert rest == body


def test_host_and_path_are_placed_correctly():
    result = build_post_request("tremorgrid.pythonanywhere.com", "api/ingest", b"{}")
    assert result.startswith(b"POST /api/ingest HTTP/1.0\r\n")
    assert b"Host: tremorgrid.pythonanywhere.com\r\n" in result


def test_content_type_defaults_to_json_but_is_overridable():
    default_result = build_post_request("example.com", "x", b"{}")
    assert b"Content-Type: application/json\r\n" in default_result

    custom_result = build_post_request("example.com", "x", b"data", content_type="text/plain")
    assert b"Content-Type: text/plain\r\n" in custom_result
