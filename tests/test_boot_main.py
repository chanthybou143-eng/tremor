"""main.py (boot script), boot_support.py and wdt_support.ResetCounter, run on the desktop behind a fake
`machine`. The properties that matter:
  * the GP22 jumper check comes first and, when jumpered, nothing else happens (no config import, no WDT,
    no client);
  * wifi_config.py is validated BEFORE the watchdog is armed, and no error report ever contains a value
    from it -- the tests plant sentinel "secrets" everywhere and assert they never reach stdout;
  * consecutive WDT resets are counted, printed together with the previous freeze, and never stop the retry.
"""

from __future__ import annotations

import ast
import sys
import textwrap
import time
import traceback
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import boot_support  # noqa: E402
from boot_support import StatusLed, safe_report, trace_locations, validate_config  # noqa: E402
from wdt_support import Breadcrumb, ResetCounter  # noqa: E402

MAIN_SRC = (ROOT / "main.py").read_text()
SECRET_PW = "SECRET-WIFI-PASSWORD-7f3a"
SECRET_TOKEN = "SECRET-INGEST-TOKEN-9c1d"
WDT_RESET, PWRON = 3, 1


class Halt(BaseException):
    """Raised by the fake sleep_ms to break main.py's deliberate 'wait for the watchdog' loop."""


class Mem(dict):
    """Scratch registers: unwritten ones read as 0."""
    def __missing__(self, k):
        return 0


class Hw:
    """The fake board. `mem` persists across simulated boots (scratch registers survive a WDT reset)."""

    def __init__(self, jumper=None, cause=PWRON, mem=None):
        self.jumper = [1] * 64 if jumper is None else jumper    # successive reads of GP22
        self.cause = cause
        self.mem = {} if mem is None else mem
        self.log = []                                            # ordered hardware actions
        self.wdts = []
        self.timers = []
        self.leds = []
        self.sleeps = 0


def install(monkeypatch, hw, cfg=None, client=None, cfg_file_dir=None):
    class Pin:
        IN, OUT, PULL_UP = 0, 1, 2

        def __init__(self, ident, mode=None, pull=None):
            self.ident = ident
            hw.log.append(("Pin", ident, mode, pull))
            if ident == "LED":
                hw.leds.append(self)
                self.v = 0

        def value(self, v=None):
            if v is not None:
                self.v = v
                return None
            if self.ident == 22:
                return hw.jumper.pop(0) if hw.jumper else 1
            return 0

    class Timer:
        PERIODIC = 1

        def __init__(self, *a, **k):
            hw.log.append(("Timer",))
            hw.timers.append(self)
            self.period = None
            self.active = False

        def init(self, period=None, mode=None, callback=None):
            self.period, self.active, self.cb = period, True, callback

        def deinit(self):
            self.active = False

    class WDT:
        def __init__(self, timeout=None):
            hw.log.append(("WDT", timeout))
            hw.wdts.append(timeout)

    machine = types.ModuleType("machine")
    machine.Pin, machine.Timer, machine.WDT = Pin, Timer, WDT
    machine.mem32 = hw.mem
    machine.WDT_RESET = WDT_RESET
    machine.reset_cause = lambda: hw.cause
    monkeypatch.setitem(sys.modules, "machine", machine)

    machine.mem32 = hw.mem = Mem(hw.mem)

    def sleep_ms(ms):
        hw.sleeps += 1
        if hw.sleeps > 2000:
            raise Halt()
    monkeypatch.setattr(time, "sleep_ms", sleep_ms, raising=False)
    monkeypatch.setattr(sys, "print_exception",
                        lambda e, f=None: traceback.print_exception(type(e), e, e.__traceback__, file=f),
                        raising=False)
    for name in ("wifi_config", "wifi_unit_client"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    imports = []
    if cfg is not None:
        m = types.ModuleType("wifi_config")
        for k, v in cfg.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, "wifi_config", m)
    if client is not None:
        c = types.ModuleType("wifi_unit_client")
        monkeypatch.setitem(sys.modules, "wifi_unit_client", c)
        c.__dict__["_run"] = client
    hw.imports = imports
    return machine


GOOD = {"WIFI_SSID": "home-net", "WIFI_PASSWORD": SECRET_PW, "UNIT_ID": "unit-1",
        "INGEST_URL": "https://example.invalid/api/ingest", "INGEST_TOKEN": SECRET_TOKEN}


def run_main():
    ns = {"__name__": "__main__"}
    exec(compile(MAIN_SRC, "main.py", "exec"), ns)
    return ns


def boot(monkeypatch, capsys, cfg=None, client_ok=True, **hwkw):
    """Run main.py once. `client_ok`: True -> the client 'runs forever' (raises Halt); a callable -> its behaviour."""
    hw = Hw(**hwkw)

    def client_body():
        if client_ok is True:
            hw.log.append(("client_started",))
            raise Halt()
        client_ok(hw)

    install(monkeypatch, hw, cfg=cfg, client=client_body)
    # importing wifi_unit_client must run client_body: a module whose import executes it
    import importlib.abc
    import importlib.util

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "wifi_unit_client":
                return importlib.util.spec_from_loader(name, Loader())

    class Loader(importlib.abc.Loader):
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            hw.log.append(("client_imported",))
            client_body()
    monkeypatch.delitem(sys.modules, "wifi_unit_client", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Finder()] + sys.meta_path)
    try:
        run_main()
    except Halt:
        pass
    out = capsys.readouterr().out
    return hw, out


def assert_no_secrets(out):
    assert SECRET_PW not in out and SECRET_TOKEN not in out


# --- (a) the jumper check ---------------------------------------------------------------------------------------------

def test_jumper_to_gnd_prints_a_message_arms_nothing_and_starts_nothing(monkeypatch, capsys):
    hw, out = boot(monkeypatch, capsys, cfg=GOOD, jumper=[0] * 64)
    assert "# MAINTENANCE: GP22 (pin 29) is jumpered to GND" in out and "REPL" in out
    assert hw.wdts == []                                              # watchdog never armed
    assert not any(e[0] == "client_imported" for e in hw.log)
    assert hw.timers == []                                            # no blinker: the LED is just steady on
    assert hw.leds and hw.leds[0].v == 1
    assert "CONFIG_ERROR" not in out and "BOOT_COUNTER" not in out


def test_the_jumper_check_is_the_very_first_hardware_action_and_uses_the_internal_pull_up(monkeypatch, capsys):
    hw, _ = boot(monkeypatch, capsys, cfg=GOOD, jumper=[0] * 64)
    assert hw.log[0] == ("Pin", 22, 0, 2)                             # Pin(22, Pin.IN, Pin.PULL_UP)


def test_the_maintenance_path_never_touches_wifi_config(monkeypatch, capsys):
    touched = []
    hw = Hw(jumper=[0] * 64)
    install(monkeypatch, hw)

    class Poison(types.ModuleType):
        def __getattr__(self, name):
            touched.append(name)
            raise AttributeError(name)
    monkeypatch.setitem(sys.modules, "wifi_config", Poison("wifi_config"))
    run_main()
    capsys.readouterr()
    assert touched == []


def test_a_single_high_read_means_no_jumper(monkeypatch, capsys):
    reads = [0, 0, 0, 1, 0, 0, 0, 0] + [1] * 60                       # a glitch is not a jumper
    hw, out = boot(monkeypatch, capsys, cfg=GOOD, jumper=reads)
    assert "MAINTENANCE" not in out and any(e[0] == "client_imported" for e in hw.log)


def test_a_floating_pin_is_pulled_up_and_runs_normally(monkeypatch, capsys):
    hw, out = boot(monkeypatch, capsys, cfg=GOOD)
    assert "MAINTENANCE" not in out and any(e[0] == "client_imported" for e in hw.log)


def test_main_py_top_level_imports_only_time_and_machine_and_checks_the_jumper_before_anything_else():
    tree = ast.parse(MAIN_SRC)
    top_imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {(n.module if isinstance(n, ast.ImportFrom) else n.names[0].name) for n in top_imports}
    assert names == {"time", "machine"}
    first_call = next(n for n in tree.body if isinstance(n, ast.If))
    assert "maintenance_requested" in ast.dump(first_call.test)
    # nothing but definitions and constants precede the check
    for n in tree.body[: tree.body.index(first_call)]:
        assert isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.Expr))
        if isinstance(n, ast.Assign):
            assert isinstance(n.value, (ast.Constant, ast.Name)), ast.dump(n)


# --- (b) config validation before the watchdog ----------------------------------------------------------------------

def test_missing_fields_are_reported_by_name_and_the_watchdog_is_never_armed(monkeypatch, capsys):
    cfg = {k: v for k, v in GOOD.items() if k not in ("INGEST_TOKEN", "UNIT_ID")}
    hw, out = boot(monkeypatch, capsys, cfg=cfg)
    assert "# CONFIG_ERROR missing=['UNIT_ID', 'INGEST_TOKEN'] invalid=[]" in out
    assert hw.wdts == [] and not any(e[0] == "client_imported" for e in hw.log)
    assert_no_secrets(out)
    assert "home-net" not in out and "example.invalid" not in out    # no values at all


def test_invalid_fields_are_reported_by_name_only(monkeypatch, capsys):
    cfg = dict(GOOD, WIFI_SSID="", UNIT_ID=7, INGEST_URL="ftp://" + SECRET_TOKEN, INGEST_TOKEN="   ")
    hw, out = boot(monkeypatch, capsys, cfg=cfg)
    assert "missing=[] invalid=['WIFI_SSID', 'UNIT_ID', 'INGEST_URL', 'INGEST_TOKEN']" in out
    assert hw.wdts == []
    assert_no_secrets(out)


def test_an_unimportable_config_prints_only_the_exception_type_and_line_and_never_the_contents(
        monkeypatch, capsys, tmp_path):
    (tmp_path / "wifi_config.py").write_text(textwrap.dedent(f'''\
        WIFI_SSID = "home-net"
        WIFI_PASSWORD = "{SECRET_PW}"
          INGEST_TOKEN = "{SECRET_TOKEN}"
        '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    hw, out = boot(monkeypatch, capsys, cfg=None)
    assert "# CONFIG_ERROR wifi_config.py could not be imported type=IndentationError" in out
    assert "wifi_config.py:3" in out
    assert hw.wdts == [] and not any(e[0] == "client_imported" for e in hw.log)
    assert_no_secrets(out)
    assert "unexpected indent" not in out                             # not even the message


def test_a_missing_config_file_is_reported_without_arming_the_watchdog(monkeypatch, capsys):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if not (Path(p) / "wifi_config.py").exists()])
    hw, out = boot(monkeypatch, capsys, cfg=None)
    assert "CONFIG_ERROR" in out and "type=ModuleNotFoundError" in out
    assert hw.wdts == []


# --- (c) status LED ------------------------------------------------------------------------------------------------------

def test_led_slow_blink_when_running_normally(monkeypatch, capsys):
    hw, _ = boot(monkeypatch, capsys, cfg=GOOD)
    t = [t for t in hw.timers if t.active]
    assert len(t) == 1 and t[0].period == StatusLed.SLOW_MS == 500


def test_led_fast_blink_on_a_config_error(monkeypatch, capsys):
    hw, _ = boot(monkeypatch, capsys, cfg={})
    t = [t for t in hw.timers if t.active]
    assert len(t) == 1 and t[0].period == StatusLed.FAST_MS == 100
    assert StatusLed.FAST_MS < StatusLed.SLOW_MS


def test_led_timer_is_kept_alive_in_a_module_global_so_it_outlives_the_boot_function(monkeypatch, capsys):
    hw = Hw()
    install(monkeypatch, hw, cfg={})
    ns = run_main()
    capsys.readouterr()
    assert ns["_led"] is not None and ns["_led"]._timer.active


def test_status_led_states_and_toggling():
    log = []

    class Led:
        def value(self, v):
            log.append(v)

    class Tm:
        period = None
        active = False

        def init(self, period, mode, callback):
            self.period, self.active, self.cb = period, True, callback

        def deinit(self):
            self.active = False
    tm = Tm()
    led = StatusLed(Led, lambda: tm)
    led.steady()
    assert log == [1] and not tm.active                               # steady: no timer at all
    led.slow()
    assert tm.active and tm.period == 500 and log[-1] == 1
    for _ in range(4):
        tm.cb(tm)
    assert log[-4:] == [0, 1, 0, 1] and led.toggles == 4
    led.fast()
    assert tm.period == 100
    led.steady()
    assert not tm.active and log[-1] == 1
    led.off()
    assert log[-1] == 0


def test_a_broken_led_or_timer_never_raises():
    def boom():
        raise OSError("no cyw43")
    led = StatusLed(boom, boom)
    led.steady(); led.slow(); led.fast(); led.off()

    class BadLed:
        def value(self, v):
            raise OSError("bus")

    class Tm:
        def init(self, **k):
            pass

        def deinit(self):
            pass
    led = StatusLed(BadLed, Tm)
    led.slow(); led._tick(); led.steady()


def test_the_led_callback_allocates_nothing_and_never_blocks():
    src = (ROOT / "boot_support.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_tick")
    banned = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.JoinedStr, ast.Lambda)
    assert not any(isinstance(n, banned) for n in ast.walk(fn))
    calls = {getattr(n.func, "attr", getattr(n.func, "id", "?")) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert calls <= {"value"}


# --- (d) consecutive WDT resets ---------------------------------------------------------------------------------------

def boots(monkeypatch, capsys, causes, mem=None, client_ok=True, cfg=GOOD, breadcrumb=None):
    """Simulate successive boots sharing the scratch registers; returns the printed counts."""
    counts, outs = [], []
    mem = Mem() if mem is None else mem
    for cause in causes:
        hw, out = boot(monkeypatch, capsys, cfg=cfg, cause=cause, mem=mem, client_ok=client_ok)
        mem = dict(hw.mem)
        outs.append(out)
        import re
        counts.append(int(re.search(r"consecutive_wdt_resets=(\d+)", out)[1]))
    return counts, outs, mem


def test_consecutive_watchdog_resets_are_counted_and_the_unit_keeps_retrying(monkeypatch, capsys):
    counts, outs, _ = boots(monkeypatch, capsys, [PWRON, WDT_RESET, WDT_RESET, WDT_RESET])
    assert counts == [0, 1, 2, 3]
    assert all("last_reset_was_wdt" in o for o in outs)
    for o in outs:                                                    # every boot went on to start the client
        assert "CONFIG_ERROR" not in o and "MAINTENANCE" not in o


def test_a_power_cycle_resets_the_count(monkeypatch, capsys):
    counts, _, _ = boots(monkeypatch, capsys, [PWRON, WDT_RESET, WDT_RESET, PWRON, WDT_RESET])
    assert counts == [0, 1, 2, 0, 1]


def test_a_soft_reset_after_the_jumper_path_is_not_miscounted_as_a_watchdog_reset(monkeypatch, capsys):
    """reset_cause() keeps saying WDT_RESET across soft resets. The 'armed' flag is what tells them apart."""
    counts, _, mem = boots(monkeypatch, capsys, [PWRON, WDT_RESET])
    assert counts == [0, 1]
    hw, out = boot(monkeypatch, capsys, cfg=GOOD, cause=WDT_RESET, mem=mem, jumper=[0] * 64)   # boot in maintenance mode
    assert "MAINTENANCE" in out
    counts, _, _ = boots(monkeypatch, capsys, [WDT_RESET], mem=dict(hw.mem))                    # Ctrl-D from the REPL
    assert counts == [1]                                              # unchanged, not 2


def test_a_config_error_boot_does_not_count_as_a_watchdog_reset(monkeypatch, capsys):
    counts, _, mem = boots(monkeypatch, capsys, [PWRON, WDT_RESET], cfg={})
    assert counts == [0, 0]


def test_the_count_and_the_previous_freeze_are_printed_at_boot(monkeypatch, capsys):
    m = Mem()
    Breadcrumb(m).mark("tls_handshake", 42, 123456)                   # what the client leaves behind
    counts, outs, _ = boots(monkeypatch, capsys, [WDT_RESET], mem=m)
    assert "# PREV_FREEZE last_stage=tls_handshake post_no=42 stage_started_at_uptime_ms=123456" in outs[0]
    assert "# BOOT_COUNTER consecutive_wdt_resets=" in outs[0]


def test_no_previous_freeze_is_reported_after_a_power_cycle(monkeypatch, capsys):
    m = Mem()
    Breadcrumb(m).mark("dns", 1, 5)
    _, outs, _ = boots(monkeypatch, capsys, [PWRON], mem=m)
    assert "PREV_FREEZE" not in outs[0]


def test_a_client_crash_is_reported_without_its_message_and_left_to_the_watchdog(monkeypatch, capsys):
    def crash(hw):
        raise RuntimeError("boom " + SECRET_TOKEN)
    hw, out = boot(monkeypatch, capsys, cfg=GOOD, client_ok=crash)
    assert "# CLIENT_CRASH type=RuntimeError at=" in out
    assert_no_secrets(out)
    assert hw.wdts == [2000]                                          # forces a reboot; jumper still works next time
    assert hw.sleeps > 100                                            # and waits for it, never returns to the REPL on its own


def test_a_client_that_returns_is_treated_like_a_crash(monkeypatch, capsys):
    hw, out = boot(monkeypatch, capsys, cfg=GOOD, client_ok=lambda hw: None)
    assert "# CLIENT_EXITED" in out and hw.wdts == [2000]


def test_a_broken_boot_support_module_leaves_the_repl_usable_and_the_watchdog_unarmed(monkeypatch, capsys):
    hw = Hw()
    install(monkeypatch, hw, cfg=GOOD)
    monkeypatch.setitem(sys.modules, "boot_support", None)            # import raises ImportError
    run_main()
    out = capsys.readouterr().out
    assert "# BOOT_ERROR type=" in out and hw.wdts == []


def test_the_jumper_still_wins_after_any_number_of_watchdog_resets(monkeypatch, capsys):
    counts, _, mem = boots(monkeypatch, capsys, [PWRON] + [WDT_RESET] * 5)
    hw, out = boot(monkeypatch, capsys, cfg=GOOD, cause=WDT_RESET, mem=mem, jumper=[0] * 64)
    assert "MAINTENANCE" in out and hw.wdts == []


def test_reset_counter_unit_behaviour():
    m = Mem()
    c = ResetCounter(m)
    assert c.on_boot(True) == 0                                       # no magic yet: nothing is 'armed'
    c.arm_started()
    assert c.on_boot(True) == 1
    c.arm_started()
    assert c.on_boot(True) == 2
    c.arm_started(); c.mark_healthy()
    assert c.count == 0
    assert c.on_boot(True) == 1                                       # armed survived mark_healthy
    c.arm_started()
    for _ in range(0x9000):
        c.on_boot(True); c.arm_started()
    assert c.count == ResetCounter.MAX                                # saturates instead of overflowing into the flags
    assert c.on_boot(False) == 0


def test_reset_counter_uses_only_scratch3():
    m = Mem()
    ResetCounter(m).arm_started()
    assert list(m) == [0x400D8000 + 0x0C + 12]
    Breadcrumb(m).mark("dns", 1, 1)
    assert 0x400D8000 + 0x0C + 12 in m and len(m) == 4               # the breadcrumb owns scratch0..2 only


def test_the_client_clears_the_count_after_its_first_successful_post():
    src = (ROOT / "wifi_unit_client.py").read_text()
    a = src.index("post_successes += 1\n")
    assert "_reset_counter.mark_healthy()" in src[a:a + 300]


# --- helpers ------------------------------------------------------------------------------------------------------------

def test_validate_config_reports_names_only():
    class C:
        WIFI_SSID = "x"
        WIFI_PASSWORD = ""
        UNIT_ID = 5
        INGEST_URL = "https://h/x"
    missing, invalid = validate_config(C)
    assert missing == ["INGEST_TOKEN"] and invalid == ["WIFI_PASSWORD", "UNIT_ID"]
    assert validate_config(types.SimpleNamespace(**GOOD)) == ([], [])


def test_trace_locations_keeps_file_and_line_but_not_source_or_message():
    try:
        exec(compile("x = 1\n\nraise ValueError('" + SECRET_TOKEN + "')", "cfg.py", "exec"))
    except ValueError as e:
        rep = safe_report(e, lambda ex, f: traceback.print_exception(type(ex), ex, ex.__traceback__, file=f),
                          __import__("io").StringIO)
    assert rep.startswith("type=ValueError at=") and "cfg.py:3" in rep and SECRET_TOKEN not in rep


class SignedMem(Mem):
    """What the device really does: mem32 reads return a SIGNED 32-bit value (found on hardware: the
    counter word 0xC0DE8000 read back as -0x3F218000, the magic check failed, and the count never rose)."""
    def __getitem__(self, k):
        v = dict.get(self, k, 0) & 0xFFFFFFFF
        return v - (1 << 32) if v >= (1 << 31) else v


def test_counter_and_breadcrumb_survive_mem32_returning_signed_values():
    m = SignedMem()
    c = ResetCounter(m)
    c.on_boot(False)
    c.arm_started()
    assert (dict.get(m, ResetCounter.ADDR) >> 16) == 0xC0DE and m[ResetCounter.ADDR] < 0      # high bit set -> negative read
    assert c.on_boot(True) == 1
    c.arm_started()
    assert c.on_boot(True) == 2
    bc = Breadcrumb(m)
    bc.mark("send", 7, 0xF0000000)                                    # ticks_ms above 2**31 reads back negative too
    assert bc.read_and_clear() == {"stage": "send", "post_no": 7, "at_ms": 0xF0000000}
