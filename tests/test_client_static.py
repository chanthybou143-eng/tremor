"""Static checks on wifi_unit_client.py (it runs its main loop at import, so it cannot be imported on the
host): every log line's format matches its arguments, and the telemetry it sends is what the server stores."""

from __future__ import annotations

import ast
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tremor.ingest import TELEMETRY_FIELDS  # noqa: E402

SRC = (ROOT / "wifi_unit_client.py").read_text()
TREE = ast.parse(SRC)


def test_every_format_string_has_as_many_fields_as_arguments():
    """A mismatch raises IndexError at runtime -- on the STATUS line, that would crash the client."""
    checked = 0
    for n in ast.walk(TREE):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format"
                and isinstance(n.func.value, ast.Constant) and isinstance(n.func.value.value, str)):
            fields = [f for _, f, _, _ in string.Formatter().parse(n.func.value.value) if f is not None]
            assert len(fields) == len(n.args) + len(n.keywords), f"line {n.lineno}"
            checked += 1
    assert checked > 20


def _telemetry_keys():
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_telemetry")
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys}
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "t":
            keys.add(n.slice.value if isinstance(n.slice, ast.Constant) else n.slice.value.value)
    return keys


def test_the_device_sends_exactly_the_telemetry_fields_the_server_stores():
    assert _telemetry_keys() == set(TELEMETRY_FIELDS)


def test_telemetry_rides_on_every_real_post():
    assert "buffer.flush(telemetry=_telemetry())" in SRC


def test_skipped_chunks_are_counted_not_silently_passed():
    handler = SRC[SRC.index("except ValueError:"):][:200]
    assert "skipped_chunk_count += 1" in handler and "pass" not in handler.split("\n")[1]


def test_power_saving_uses_the_named_constant_and_temperature_uses_core_temp():
    assert 'getattr(network.WLAN, "PM_NONE", None)' in SRC and "0xa11140" not in SRC.lower()
    assert "ADC(ADC.CORE_TEMP)" in SRC and "ADC(4)" not in SRC and "ADC(8)" not in SRC


def test_reconnect_escalation_is_wired_after_two_minutes_with_the_watchdog_fed():
    assert "WIFI_ESCALATE_AFTER_S = 120" in SRC
    assert "escalate_after_ms=WIFI_ESCALATE_AFTER_S * 1000" in SRC and "feed_fn=_feed_wdt" in SRC


def test_the_client_never_prints_or_formats_the_credentials():
    for n in ast.walk(TREE):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
            # allowed: "x is not None" (prints only configured/absent, never the value)
            presence = {c.left.id for c in ast.walk(n) if isinstance(c, ast.Compare) and isinstance(c.left, ast.Name)
                        and all(isinstance(o, (ast.Is, ast.IsNot)) for o in c.ops)
                        and all(isinstance(v, ast.Constant) and v.value is None for v in c.comparators)}
            names = {x.id for x in ast.walk(n) if isinstance(x, ast.Name)} - presence
            assert not names & {"WIFI_PASSWORD", "WIFI_SSID", "INGEST_TOKEN", "_AUTH_HEADERS", "wifi_config"}


# --- hard ADC timer ----------------------------------------------------------------------------------------

def _isr():
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_on_adc_timer")


def test_the_adc_timer_is_hard_and_its_handler_cannot_allocate():
    assert "callback=_on_adc_timer, hard=True)" in SRC
    assert "micropython.alloc_emergency_exception_buf(" in SRC
    isr = _isr()
    banned = (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp,
              ast.JoinedStr, ast.Lambda, ast.Try, ast.With, ast.FunctionDef, ast.BinOp)
    for n in (x for stmt in isr.body for x in ast.walk(stmt)):
        if isinstance(n, ast.BinOp):
            assert isinstance(n.op, (ast.Add, ast.Mod)), "only small-int + and % in the ISR"
            continue
        assert not isinstance(n, banned), type(n).__name__
        if isinstance(n, ast.Constant):
            assert isinstance(n.value, int) or n.value is None
        if isinstance(n, ast.Call):
            assert ast.unparse(n.func) in ("time.ticks_us", "adc.read_u16", "_temp_adc.read_u16"), ast.unparse(n.func)
    # the only counter that grows without bound is capped below 2**30 (a MicroPython small int)
    caps = [n for n in ast.walk(isr) if isinstance(n, ast.Compare) and ast.unparse(n.left) == "overflow_count"]
    assert len(caps) == 1 and isinstance(caps[0].ops[0], ast.Lt) and caps[0].comparators[0].value < 2 ** 30


def test_the_temperature_is_read_only_inside_the_adc_handler_and_no_interrupt_is_ever_disabled():
    # one multiplexed ADC: all access from one context, so nothing can interleave -- and nothing may
    # delay the PPS edge interrupt, whose ticks_us() stamp is the unit's time reference
    assert "disable_irq" not in SRC
    isr_src = ast.unparse(_isr())
    assert "_temp_raw = _temp_adc.read_u16()" in isr_src
    assert isr_src.index("ring_raw[write_idx] = adc.read_u16()") < isr_src.index("_temp_adc.read_u16()")
    assert SRC.count("_temp_adc.read_u16()") == 1
    assert SRC.index("_temp_adc = ADC(ADC.CORE_TEMP)") < SRC.index("adc_timer.init(")


def test_temperature_requests_are_made_only_mid_pps_second():
    fn = SRC[SRC.index("def _maybe_request_temp():"):SRC.index("def _die_temp_c():")]
    assert "sync.last_edge_ticks" in fn and "300000 <= phase_us <= 700000" in fn
    assert "_maybe_request_temp()" in SRC[SRC.index("while True:"):]


def test_status_reports_the_pps_interval_spread():
    assert "pps_iv_min_us={} pps_iv_max_us={}" in SRC and "sync.take_interval_window()" in SRC
