"""Static checks on wifi_unit_client.py (it runs its main loop at import, so it cannot be imported on the
host): every log line's format matches its arguments, and the telemetry it sends is what the server stores."""

from __future__ import annotations

import ast
import string

import pytest
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

def _isr_factory():
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_make_adc_isr")


def _isr():
    return next(n for n in _isr_factory().body if isinstance(n, ast.FunctionDef) and n.name == "_on_adc_timer")


def test_the_adc_timer_is_hard_and_its_handler_cannot_allocate():
    assert "callback=_on_adc_timer, hard=True)" in SRC
    assert "micropython.alloc_emergency_exception_buf(" in SRC
    isr = _isr()
    banned = (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp,
              ast.JoinedStr, ast.Lambda, ast.Try, ast.With, ast.FunctionDef, ast.Attribute, ast.Global, ast.Nonlocal)
    for n in (x for stmt in isr.body for x in ast.walk(stmt)):
        if isinstance(n, ast.BinOp):
            assert isinstance(n.op, (ast.Add, ast.Mod)), "only small-int + and % in the ISR"
        assert not isinstance(n, banned), type(n).__name__
        if isinstance(n, ast.Constant):
            assert isinstance(n.value, int) or n.value is None
        if isinstance(n, ast.Call):
            assert isinstance(n.func, ast.Name) and n.func.id in ("ticks_us", "read_mains", "read_temp"), ast.unparse(n)
    caps = [n for n in ast.walk(isr) if isinstance(n, ast.Compare) and ast.unparse(n.left) == "state[2]"
            and isinstance(n.ops[0], ast.Lt)]
    assert len(caps) == 1 and caps[0].comparators[0].value < 2 ** 30


def test_the_adc_handler_never_looks_anything_up_in_a_globals_table():
    """A hard handler that resolves a name in the module's globals table can miss it while the main thread
    is adding a global (seen on the bench: NameError for 'write_idx', timer disabled, no readings). Every
    name the handler uses must be one of its own locals or a closure variable of _make_adc_isr."""
    factory, isr = _isr_factory(), _isr()
    closure = {a.arg for a in factory.args.args}
    local = {a.arg for a in isr.args.args} | {t.id for n in ast.walk(isr) if isinstance(n, ast.Assign)
                                              for t in n.targets if isinstance(t, ast.Name)}
    used = {n.id for n in ast.walk(isr) if isinstance(n, ast.Name)}
    assert used <= closure | local, sorted(used - closure - local)
    assert "_on_adc_timer = _make_adc_isr(ring_ticks, ring_raw, _adc_state, RING_CAPACITY, time.ticks_us, adc.read_u16," in SRC
    for old in ("global write_idx", "\nwrite_idx = 0", "\nread_idx = 0", "\noverflow_count = 0", "\n_temp_req = 0"):
        assert old not in SRC


def test_a_stalled_adc_timer_is_detected_and_restarted():
    loop = SRC[SRC.index("\nwhile True:"):]
    assert "_alive = (_adc_state[_W], _adc_state[_OVF])" in loop           # overflow counts as alive (ring full)
    stall = loop[loop.index("ADC_STALL_MS:"):][:400]
    assert "# ADC_STALLED" in stall and "adc_timer.init(freq=ADC_SAMPLE_HZ, mode=Timer.PERIODIC, callback=_on_adc_timer, hard=True)" in stall
    assert "adc_restarts={}" in SRC


def test_the_temperature_is_read_only_inside_the_adc_handler_and_no_interrupt_is_ever_disabled():
    assert "disable_irq" not in SRC
    isr_src = ast.unparse(_isr())
    assert "state[4] = read_temp()" in isr_src
    assert isr_src.index("ring_raw[w] = read_mains()") < isr_src.index("read_temp()")
    assert SRC.count("_temp_adc.read_u16") == 1                            # handed to the handler, never called elsewhere
    assert SRC.index("_temp_adc = ADC(ADC.CORE_TEMP)") < SRC.index("adc_timer.init(")


def test_the_pps_handler_only_uses_attributes_created_before_its_interrupt_is_enabled():
    """Same hazard for the PPS hard handler: it reads/writes instance attributes, so the instance's attribute
    table must never grow once the interrupt is on -- every attribute any method sets must already exist."""
    tree = ast.parse((ROOT / "pps_time_sync.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PPSTimeSync")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    irq_line = next(n.lineno for n in ast.walk(init) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "irq")

    def assigned(fn, before=None):
        out = set()
        for n in ast.walk(fn):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AugAssign) else []
            for t in targets:
                for x in ast.walk(t):
                    if (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self"
                            and (before is None or n.lineno < before)):
                        out.add(x.attr)
        return out
    created = assigned(init, before=irq_line)
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef) and fn.name != "__init__":
            assert assigned(fn) <= created, (fn.name, sorted(assigned(fn) - created))
    pps = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_on_pps")
    read = {x.attr for x in ast.walk(pps) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self"}
    methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert read <= created | methods, sorted(read - created - methods)


def test_temperature_requests_are_made_only_mid_pps_second():
    fn = SRC[SRC.index("def _maybe_request_temp():"):SRC.index("def _die_temp_c():")]
    assert "sync.last_edge_ticks" in fn and "300000 <= phase_us <= 700000" in fn
    assert "_maybe_request_temp()" in SRC[SRC.index("while True:"):]


def test_status_reports_the_pps_interval_spread():
    assert "pps_iv_min_us={} pps_iv_max_us={}" in SRC and "sync.take_interval_window()" in SRC


def _repo_imports(name, seen=None):
    """Repo-root modules imported (transitively) by `name` -- what must be on the Pico's flash."""
    seen = set() if seen is None else seen
    path = ROOT / f"{name}.py"
    if name in seen or not path.exists():
        return seen
    seen.add(name)
    for n in ast.walk(ast.parse(path.read_text())):
        mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module] if isinstance(n, ast.ImportFrom) and n.module else []
        for m in mods:
            _repo_imports(m.split(".")[0], seen)
    return seen


def test_the_flash_runbook_names_every_module_the_firmware_imports():
    doc = (ROOT / "deploy" / "DEPLOY_FW_RESILIENCE.md").read_text()
    cp_line = next(line for line in doc.splitlines() if "mpremote fs cp " in line and line.endswith(" :"))
    flashed = set(cp_line[cp_line.index("mpremote fs cp ") + len("mpremote fs cp "):-2].split())
    untouched = doc[doc.index("are untouched") - 200:doc.index("are untouched")]
    needed = _repo_imports("main") | _repo_imports("wifi_unit_client")
    needed.discard("wifi_config")                       # the device's own, never in the repo
    for m in sorted(needed):
        assert f"{m}.py" in flashed or f"`{m}.py`" in untouched, f"{m}.py is neither flashed nor listed as untouched"
    assert {"adc_chunker.py", "wifi_support.py", "pps_time_sync.py"} <= flashed


def test_files_the_runbook_calls_untouched_really_are_unchanged_since_master():
    import re
    import subprocess
    doc = (ROOT / "deploy" / "DEPLOY_FW_RESILIENCE.md").read_text()
    seg = doc[doc.index("Device files that change:"):doc.index("are untouched")]
    untouched = re.findall(r"`(\w+\.py)`", seg[seg.index("`main.py`"):])
    assert "main.py" in untouched and "wifi_config.py" in untouched
    try:
        ok = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "master"], capture_output=True).returncode == 0
    except OSError:
        ok = False
    if not ok:
        pytest.skip("no git / no master branch here")
    for f in untouched:
        if f == "wifi_config.py":
            continue
        r = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "master", "--", f])
        assert r.returncode == 0, f"{f} is listed as untouched but differs from master"


def test_pps_spread_telemetry_is_the_max_since_the_last_successful_post():
    status = SRC[SRC.index("_pps_iv = sync.take_interval_window()"):][:400]
    assert "_pps_spread_since_post = _pps_spread" in status
    flush = SRC[SRC.index("if buffer.flush(telemetry=_telemetry()):"):][:250]
    assert "_pps_spread_since_post = None" in flush                 # reset only after a delivered POST


def test_pps_time_sync_never_adds_module_globals_after_import():
    """The PPS hard handler reads a few pps_time_sync module globals (time, PPS_* constants); that table is
    complete once the module has imported -- before the interrupt is enabled -- as long as nothing adds to it."""
    src = (ROOT / "pps_time_sync.py").read_text()
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Global)]
    assert "setattr(" not in src and "globals()" not in src
