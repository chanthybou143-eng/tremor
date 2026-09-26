"""Helpers for main.py's boot sequence (config validation, safe error reports, status LED).

Everything hardware-specific is injected, so it runs (and is tested) on the desktop:
tests/test_boot_main.py.

Nothing here ever returns or prints a config VALUE. Error reports carry field names, exception type
names and file/line numbers only -- never a message and never file contents: wifi_config.py holds the
Wi-Fi password and the ingest token, and a traceback's message can echo them.
"""

# Fields the standalone client needs. INGEST_TOKEN is required here (the server is meant to move to
# TREMOR_INGEST_AUTH=required); the client itself would tolerate its absence in "optional" mode.
REQUIRED_FIELDS = ("WIFI_SSID", "WIFI_PASSWORD", "UNIT_ID", "INGEST_URL", "INGEST_TOKEN")


def validate_config(cfg, required=REQUIRED_FIELDS):
    """(missing, invalid): field NAMES only. missing = not defined; invalid = defined but not a
    non-empty string (or, for INGEST_URL, not an http(s) URL)."""
    missing = []
    invalid = []
    for name in required:
        if not hasattr(cfg, name):
            missing.append(name)
            continue
        v = getattr(cfg, name)
        if not isinstance(v, str) or not v.strip():
            invalid.append(name)
        elif name == "INGEST_URL" and not (v.startswith("https://") or v.startswith("http://")):
            invalid.append(name)
    return missing, invalid


def trace_locations(exc, print_exception_fn, buffer_factory):
    """Where an exception happened, without what it said: ['wifi_config.py:15', ...]. The traceback is
    rendered into a buffer and only its 'File "x", line N' parts are kept (MicroPython tracebacks carry
    no source text, but the exception message can hold anything, so it is dropped)."""
    buf = buffer_factory()
    print_exception_fn(exc, buf)
    out = []
    for line in buf.getvalue().split("\n"):
        line = line.strip()
        if line.startswith('File "'):
            try:
                name = line.split('"')[1]
                num = line.split(", line ")[1].split(",")[0].strip()
                out.append("{}:{}".format(name, num))
            except IndexError:
                pass
    return out


def safe_report(exc, print_exception_fn, buffer_factory):
    """'type=SyntaxError at=wifi_config.py:15' -- the whole of what may be printed about an exception."""
    return "type={} at={}".format(type(exc).__name__,
                                  ",".join(trace_locations(exc, print_exception_fn, buffer_factory)) or "?")


class StatusLed:
    """Onboard LED: off / steady on / blinking, all driven by ONE periodic timer callback (no blocking,
    no allocation in the callback). Any LED or timer failure is swallowed -- the LED is a courtesy and
    must never be why the unit does not start.

      steady()  escape-hatch (maintenance) mode
      slow()    running normally        (toggle every SLOW_MS)
      fast()    configuration error     (toggle every FAST_MS)
    """

    SLOW_MS = 500     # 1 Hz blink
    FAST_MS = 100     # 5 Hz blink

    def __init__(self, led_factory, timer_factory, periodic_mode=1):
        self._periodic = periodic_mode     # machine.Timer.PERIODIC
        self._timer = None
        self._led = None
        self._state = 0
        self.toggles = 0
        try:
            self._led = led_factory()
            self._timer = timer_factory()
        except Exception:
            self._led = None

    def _stop(self):
        if self._timer is not None:
            try:
                self._timer.deinit()
            except Exception:
                pass

    def _set(self, v):
        self._state = v
        try:
            self._led.value(v)
        except Exception:
            pass

    def _tick(self, _t=None):
        self._state ^= 1
        self.toggles += 1
        try:
            self._led.value(self._state)
        except Exception:
            pass

    def _blink(self, period_ms):
        if self._led is None or self._timer is None:
            return
        self._stop()
        self._set(1)
        try:
            self._timer.init(period=period_ms, mode=self._periodic, callback=self._tick)
        except Exception:
            pass

    def steady(self):
        if self._led is not None:
            self._stop()
            self._set(1)

    def slow(self):
        self._blink(self.SLOW_MS)

    def fast(self):
        self._blink(self.FAST_MS)

    def off(self):
        if self._led is not None:
            self._stop()
            self._set(0)
