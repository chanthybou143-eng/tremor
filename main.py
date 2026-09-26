"""TREMOR unit boot script: runs automatically at power-up / reset on the Pico.

Order matters:
  1. ESCAPE HATCH. Jumper GP22 (physical pin 29) to GND (pin 28, next to it) and power-cycle: this file
     prints a message, arms nothing, starts nothing and ends, leaving the REPL for `mpremote`. That check
     uses only `machine` and `time` and runs before anything else, so no other file can break it.
  2. Config check. wifi_config.py is imported and its required fields validated BEFORE the watchdog is
     armed; on any problem only the exception type, file:line and field NAMES are printed (never values).
  3. Otherwise `import wifi_unit_client`, which arms the watchdog and runs forever. A crash or an exit
     of the client is reported and left to the watchdog to reboot, so the unit keeps retrying.

Status LED: steady on = escape-hatch mode, slow blink (1 Hz) = running normally,
fast blink (5 Hz) = configuration error. See RECOVERY.md.
"""
import time
from machine import Pin

MAINTENANCE_PIN = 22           # GP22 = physical pin 29; free: the client uses GP0/GP1 (GPS UART), GP15 (PPS), GP26 (ADC)
_SAMPLES = 8                   # the jumper must read low on EVERY one of these reads ...
_SPACING_MS = 5                # ... spread over ~40 ms, so a glitch or a floating pin cannot trigger it


def maintenance_requested():
    pin = Pin(MAINTENANCE_PIN, Pin.IN, Pin.PULL_UP)     # internal pull-up: open = 1, jumpered to GND = 0
    time.sleep_ms(20)                                    # let the pull-up settle
    for _ in range(_SAMPLES):
        if pin.value():
            return False
        time.sleep_ms(_SPACING_MS)
    return True


def _steady_led():
    try:
        Pin("LED", Pin.OUT).value(1)
    except Exception:
        pass


_led = None


def _boot():
    import sys
    import machine
    from machine import Timer
    try:
        from io import StringIO
    except ImportError:
        from uio import StringIO
    from boot_support import StatusLed, validate_config, safe_report
    from wdt_support import Breadcrumb, ResetCounter

    def report(exc):
        return safe_report(exc, sys.print_exception, StringIO)

    counter = ResetCounter(machine.mem32)
    cause_is_wdt = machine.reset_cause() == machine.WDT_RESET
    n = counter.on_boot(cause_is_wdt)
    print("# BOOT_COUNTER consecutive_wdt_resets={} last_reset_was_wdt={}".format(n, cause_is_wdt))
    freeze = Breadcrumb(machine.mem32).read_and_clear()
    if freeze is not None and cause_is_wdt:
        print("# PREV_FREEZE last_stage={} post_no={} stage_started_at_uptime_ms={}".format(
            freeze["stage"], freeze["post_no"], freeze["at_ms"]))

    global _led                             # module-level, so the blink timer outlives this function (and stays alive at the REPL)
    _led = led = StatusLed(lambda: Pin("LED", Pin.OUT), Timer, Timer.PERIODIC)

    try:
        import wifi_config
    except Exception as e:
        print("# CONFIG_ERROR wifi_config.py could not be imported {}".format(report(e)))
        print("# Watchdog NOT armed, client NOT started. Fix wifi_config.py (see RECOVERY.md).")
        led.fast()
        return
    missing, invalid = validate_config(wifi_config)
    if missing or invalid:
        print("# CONFIG_ERROR missing={} invalid={}".format(missing, invalid))
        print("# Watchdog NOT armed, client NOT started. Fix wifi_config.py (see RECOVERY.md).")
        led.fast()
        return

    led.slow()
    counter.arm_started()                    # from here a WDT reset counts as one
    try:
        import wifi_unit_client              # arms the watchdog; never returns
        print("# CLIENT_EXITED unexpectedly")
    except Exception as e:
        print("# CLIENT_CRASH {}".format(report(e)))
    # Only reached if the client died. Make sure a watchdog is running (it normally already is), then wait
    # for it: the unit reboots and tries again. The jumper still works, since it is checked first.
    try:
        machine.WDT(timeout=2000)
    except Exception:
        pass
    while True:
        time.sleep_ms(100)


if maintenance_requested():
    _steady_led()
    print("# MAINTENANCE: GP22 (pin 29) is jumpered to GND -- watchdog NOT armed, client NOT started.")
    print("# You are at the REPL. Remove the jumper and power-cycle to run normally.")
    try:                                     # so a later soft reset is not miscounted as a watchdog reset
        import machine
        from wdt_support import ResetCounter
        ResetCounter(machine.mem32).disarmed()
    except Exception:
        pass
else:
    try:
        _boot()
    except Exception as _e:                  # e.g. boot_support.py itself missing: no watchdog yet, so the REPL stays usable
        print("# BOOT_ERROR type={}".format(type(_e).__name__))
