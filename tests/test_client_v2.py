"""IngestBuffer v2 (boot_id + per-reading seq + integer GPS time), make_boot_id, and the
client<->server wire contract. Host-only: wifi_ingest.py has no MicroPython imports."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wifi_ingest import IngestBuffer, make_boot_id  # noqa: E402

from helpers import T0, US, v2_gps  # noqa: E402
from tremor.ingest import BOOT_ID_RE, SRC_V2, parse_payload, resolve_time  # noqa: E402

BOOT = "0123456789abcdef"


def _sink(ok=True):
    sent = []

    def post(payload):
        sent.append(json.loads(json.dumps(payload)))         # what actually crosses the wire
        return ok() if callable(ok) else ok
    return sent, post


# --- legacy wire format is untouched ---------------------------------------------------------------

def test_without_a_boot_id_the_payload_is_exactly_the_legacy_format():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post)
    buf.append(50.0, 0.75, 41023.5)
    buf.append(50.01, 0.75, None, gps=(20721, 1, 2))              # gps ignored in legacy mode
    buf.flush()
    assert set(sent[0]) == {"unit_id", "readings"}
    assert all(set(r) == {"frequency_hz", "amplitude_v", "gps_utc_s"} for r in sent[0]["readings"])


# --- v2 payload ---------------------------------------------------------------------------------------

def test_v2_payload_carries_boot_id_seq_and_the_integer_gps_triple():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post, boot_id=BOOT)
    buf.append(50.0, 0.75, 41023.5, gps=(20721, 41023, 500_123))
    buf.append(50.01, 0.75, None, gps=None)                          # not yet synced: no gps
    buf.flush()
    p = sent[0]
    assert p["boot_id"] == BOOT and p["unit_id"] == "unit-1"
    a, b = p["readings"]
    assert a["seq"] == 0 and a["gps"] == [20721, 41023, 500_123]
    assert b["seq"] == 1 and "gps" not in b                       # not yet synced: no time at all -> unlocked
    # the legacy float32 time is NOT sent in v2 by default (SEND_LEGACY_FLOAT = False)
    assert all("gps_utc_s" not in r for r in p["readings"])
    assert set(a) == {"frequency_hz", "amplitude_v", "seq", "gps"}


def test_the_legacy_float_can_be_switched_back_on_for_rollback_compatibility():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post, boot_id=BOOT, send_legacy_float=True)
    buf.append(50.0, 0.75, 41023.5, gps=(20721, 41023, 500_123))
    buf.append(50.01, 0.75, None, gps=None)
    buf.flush()
    a, b = sent[0]["readings"]
    assert a["gps_utc_s"] == 41023.5 and a["gps"] == [20721, 41023, 500_123]
    assert b["gps_utc_s"] is None and "gps" not in b


def test_legacy_v1_always_sends_the_float_regardless_of_the_flag():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post, send_legacy_float=False)            # no boot_id => v1
    buf.append(50.0, 0.75, 41023.5)
    buf.flush()
    assert sent[0]["readings"][0]["gps_utc_s"] == 41023.5


def test_v2_payload_size_with_and_without_the_legacy_float():
    def size(**kw):
        sent = []
        buf = IngestBuffer("unit-1", lambda p: sent.append(p) or True, boot_id=BOOT if kw.pop("v2", True) else None, **kw)
        for i in range(60):
            us = int(T0 * US) + i * 1_000_000 + 123_457
            buf.append(50.0 + 0.0123 * (i % 5), 0.744, float(str(__import__("numpy").float32((us / US) % 86400))),
                       gps=tuple(v2_gps(us)))
        buf.flush()
        return len(json.dumps(sent[0]).encode())
    v1, v2_lean, v2_compat = size(v2=False), size(), size(send_legacy_float=True)
    assert v2_lean < v2_compat and v2_compat - v2_lean >= 60 * 15                 # the float costs >= ~15 B/reading
    assert v2_lean / v1 < 1.30                                                    # lean v2 stays within +30% of v1


def test_seq_is_contiguous_across_failed_posts_merge_back_and_capped_sends():
    results = iter([False, True, True, True])
    sent, post = _sink(ok=lambda: next(results))
    buf = IngestBuffer("unit-1", post, max_readings=100, max_readings_per_post=6, boot_id=BOOT)
    for i in range(10):
        buf.append(50.0 + i * 0.001, 0.7, None)
    assert buf.flush() is False                                      # POST #1 fails: 6 readings put back
    for i in range(10, 15):
        buf.append(50.0 + i * 0.001, 0.7, None)                      # more arrive meanwhile
    buf.flush(); buf.flush(); buf.flush()
    seqs = [[r["seq"] for r in p["readings"]] for p in sent]
    assert seqs[0] == [0, 1, 2, 3, 4, 5]                              # the failed attempt...
    assert seqs[1] == [0, 1, 2, 3, 4, 5]                              # ...is re-sent identically (same seq, same order)
    assert seqs[2] == [6, 7, 8, 9, 10, 11] and seqs[3] == [12, 13, 14]
    flat = [s for grp in seqs[1:] for s in grp]
    assert flat == list(range(15))                                    # every reading exactly once, oldest first


def test_a_full_buffer_drops_the_oldest_and_the_seq_gap_shows_exactly_what_was_lost():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post, max_readings=5, boot_id=BOOT)
    for i in range(8):
        buf.append(50.0 + i * 0.001, 0.7, None)
    assert buf.dropped_count == 3
    buf.flush()
    assert [r["seq"] for r in sent[0]["readings"]] == [3, 4, 5, 6, 7]  # 0, 1, 2 were lost -- and visible as a gap


def test_gps_triple_and_seq_survive_merge_back_intact():
    sent, post = _sink(ok=iter([False, True]).__next__)
    buf = IngestBuffer("unit-1", post, boot_id=BOOT)
    for i in range(4):
        buf.append(50.0, 0.7, None, gps=(20721, 100 + i, 999_000 + i))
    buf.flush()
    buf.append(50.0, 0.7, None, gps=(20721, 104, 5))
    buf.flush()
    assert [r["gps"] for r in sent[1]["readings"]] == [[20721, 100 + i, 999_000 + i] for i in range(4)] + [[20721, 104, 5]]
    assert [r["seq"] for r in sent[1]["readings"]] == [0, 1, 2, 3, 4]


def test_typecodes_hold_realistic_device_values():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post, boot_id=BOOT)
    buf.append(50.0, 0.7, None, gps=(65_535, 86_399, 999_999))       # array 'H' / 'I' / 'I' limits that matter
    buf.flush()
    assert sent[0]["readings"][0]["gps"] == [65_535, 86_399, 999_999]


# --- make_boot_id ----------------------------------------------------------------------------------------------

def test_make_boot_id_is_16_hex_characters_and_matches_the_servers_pattern():
    b = make_boot_id(urandom=lambda n: bytes(range(n)))
    assert b == "0001020304050607" and BOOT_ID_RE.match(b)
    ids = {make_boot_id() for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"[0-9a-f]{16}", i) for i in ids)


def test_make_boot_id_uses_the_fallback_only_when_urandom_fails_and_never_invents_one():
    def bad(_n):
        raise OSError("no entropy")
    assert make_boot_id(urandom=bad, fallback=lambda: b"\xff" * 8) == "ff" * 8
    with pytest.raises(RuntimeError):
        make_boot_id(urandom=bad)                                       # no random source at all -> caller must go legacy


# --- client <-> server contract ---------------------------------------------------------------------------------

def test_a_payload_built_by_the_device_code_parses_and_resolves_exactly_on_the_server():
    """The real compatibility guard: bytes produced by IngestBuffer must be accepted by
    tremor.ingest and resolve to the exact microsecond the device measured."""
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post, boot_id=BOOT)
    unix_us = [int(T0 * US) + i * 1_000_000 + 123_457 for i in range(5)]
    for i, us in enumerate(unix_us):
        buf.append(50.0 + i * 0.01, 0.744, float((us / US) % 86400), gps=tuple(v2_gps(us)))
    buf.append(50.0, 0.744, None, gps=None)                               # one unlocked reading
    buf.flush()
    batch = parse_payload(sent[0])
    assert batch.mode == 2 and batch.boot_id == BOOT and [r.seq for r in batch.readings] == list(range(6))
    resolved = [resolve_time(r, T0 + 10) for r in batch.readings]
    assert [t.gps_utc_us for t in resolved[:5]] == unix_us               # exact, microsecond for microsecond
    assert all(t.time_src == SRC_V2 and t.flags == 0 for t in resolved[:5])
    assert resolved[5].gps_utc_us is None and resolved[5].flags == 1     # unlocked


def test_the_legacy_payload_is_still_accepted_by_the_new_server():
    sent, post = _sink()
    buf = IngestBuffer("unit-1", post)                                     # no boot_id
    buf.append(50.0, 0.744, float(f"{T0 % 86400:.2f}"))
    buf.flush()
    assert parse_payload(sent[0]).mode == 1


# --- wifi_unit_client.py wiring (static: the script runs its main loop at import, so it cannot be
# imported on the host -- see tests/test_http_client.py for the same constraint) ------------------------------

def _client_tree():
    return ast.parse((ROOT / "wifi_unit_client.py").read_text())


def test_client_generates_a_boot_id_and_falls_back_to_legacy_instead_of_a_weak_id():
    src = (ROOT / "wifi_unit_client.py").read_text()
    assert "from wifi_ingest import IngestBuffer, make_boot_id" in src
    tree = _client_tree()
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
             and any(isinstance(x, ast.Assign) and any(getattr(t, "id", "") == "BOOT_ID" for t in x.targets)
                     for x in ast.walk(n))]
    assert tries, "BOOT_ID must be created inside a try/except"
    handler_assigns = [x for h in tries[0].handlers for x in ast.walk(h)
                       if isinstance(x, ast.Assign) and any(getattr(t, "id", "") == "BOOT_ID" for t in x.targets)]
    assert handler_assigns and isinstance(handler_assigns[0].value, ast.Constant) and handler_assigns[0].value.value is None


def test_client_passes_boot_id_to_the_buffer_and_the_integer_gps_to_append():
    tree = _client_tree()
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    ctor = [c for c in calls if getattr(c.func, "id", "") == "IngestBuffer"]
    assert len(ctor) == 1 and any(k.arg == "boot_id" for k in ctor[0].keywords)
    assert any(k.arg == "send_legacy_float" for k in ctor[0].keywords)
    appends = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "append"
               and getattr(c.func.value, "id", "") == "buffer"]
    assert len(appends) == 1 and len(appends[0].args) == 4                # freq, amp, float32 utc, integer gps
    src = (ROOT / "wifi_unit_client.py").read_text()
    assert "sync.ticks_to_gps(" in src and "if BOOT_ID is not None else None" in src


def test_send_legacy_float_is_a_module_constant_defaulting_to_false_and_gates_the_float_work():
    tree = _client_tree()
    consts = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SEND_LEGACY_FLOAT" for t in n.targets)]
    assert len(consts) == 1 and isinstance(consts[0].value, ast.Constant) and consts[0].value.value is False
    src = (ROOT / "wifi_unit_client.py").read_text()
    assert "if (BOOT_ID is None or SEND_LEGACY_FLOAT) else None" in src            # float32 time only computed when it will be sent
