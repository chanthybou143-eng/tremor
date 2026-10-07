"""Bench run of the client over USB with a simulated uplink outage -- runs ON THE PICO, from the Mac:

    # unit in maintenance mode first (GP22 jumper, LED steady on -- see RECOVERY.md), then:
    mpremote connect auto mount . run scripts/bench_outage.py | tee bench_outage.log

`mount .` serves this checkout to the Pico as /remote, so the NEW firmware runs from the Mac's files
and NOTHING is written to the Pico's flash. The device's own wifi_config.py is imported from flash
(never the Mac's, never printed). Then POSTs are refused for OUTAGE_S seconds starting OUTAGE_START_S
after start -- as if the uplink were down -- and the client runs normally otherwise.

The client arms the 8 s watchdog as usual. To stop: Ctrl-C; the watchdog resets the board ~8 s later,
and with the jumper still fitted it comes back in maintenance mode.

scripts/bench_normal.py is this file with OUTAGE_S = 0 (a plain run, e.g. for the hard-timer A/B);
tests/test_bench_scripts.py keeps the two identical otherwise.
"""
import sys
import time

OUTAGE_START_S = 180          # post normally for 3 minutes first
OUTAGE_S = 600                # then 10 minutes with every POST refused

_saved_path = list(sys.path)
sys.path[:] = ["/"]           # the Pico's flash root: its own wifi_config.py
try:
    import wifi_config        # noqa: F401  (cached in sys.modules for the client)
    _config_ok = True
except Exception as _exc:     # type only -- never the message or a traceback (they can quote the file)
    _config_ok = False
    print("# BENCH wifi_config import failed type={}".format(type(_exc).__name__))
finally:
    sys.path[:] = _saved_path

if _config_ok:
    import http_client

    _real_post = http_client.timeout_post
    _t0 = time.ticks_ms()
    _blocked = [0]

    def _bench_post(*args, **kwargs):
        t_s = time.ticks_diff(time.ticks_ms(), _t0) // 1000
        if OUTAGE_S and OUTAGE_START_S <= t_s < OUTAGE_START_S + OUTAGE_S:
            _blocked[0] += 1
            print("# BENCH_OUTAGE refused_post n={} t_s={}".format(_blocked[0], t_s))
            raise http_client.PostStageError("connect", "bench_outage")
        return _real_post(*args, **kwargs)

    http_client.timeout_post = _bench_post      # before the client's `from http_client import timeout_post`
    print("# BENCH start outage_start_s={} outage_s={} t_ms={}".format(OUTAGE_START_S, OUTAGE_S, _t0))
    import wifi_unit_client                       # noqa: F401  arms the watchdog and runs until Ctrl-C
