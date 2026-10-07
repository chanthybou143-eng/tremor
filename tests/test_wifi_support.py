"""wifi_support.WifiSupervisor: power saving off, reconnect pacing, escalation after ~2 min, counters."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wifi_support import LINK_NAMES, WifiSupervisor, link_name  # noqa: E402

PM_NONE = 0xA11140                  # CYW43_PM_VALUE(CYW43_NO_POWERSAVE_MODE, 10, 0, 0, 0) -- informational only
PM_PERFORMANCE = 0xA11142


class FakeWlan:
    def __init__(self):
        self.connected = False
        self.link = 0
        self.pm = PM_PERFORMANCE
        self.calls = []

    def isconnected(self):
        return self.connected

    def status(self):
        return self.link

    def config(self, *a, **kw):
        if a == ("pm",):
            return self.pm
        self.calls.append(("config", kw))
        self.pm = kw["pm"]

    def disconnect(self):
        self.calls.append("disconnect")

    def active(self, v):
        self.calls.append(("active", v))


class Rig:
    def __init__(self, **kw):
        self.t = 0
        self.wlan = FakeWlan()
        self.log = []
        self.connects = []
        self.feeds = 0
        self.sup = WifiSupervisor(self.wlan, lambda: self.connects.append(self.t), lambda: self.t, lambda a, b: a - b,
                                  pm_value=PM_NONE, log_fn=self.log.append, feed_fn=self.feed, **kw)

    def feed(self):
        self.feeds += 1

    def run(self, ms, step=100):
        for _ in range(ms // step):
            self.t += step
            self.sup.service()


def test_power_saving_is_switched_off_and_read_back():
    r = Rig()
    assert r.sup.apply_pm() == PM_NONE and r.wlan.pm == PM_NONE
    assert r.log == ["# WIFI_PM requested=0xa11140 before=0xa11142 after=0xa11140"]


def test_a_driver_without_pm_support_is_logged_by_exception_type_only():
    r = Rig()

    def boom(*a, **kw):
        raise ValueError("unknown config param")
    r.wlan.config = boom
    assert r.sup.apply_pm() is None and r.log == ["# WIFI_PM_FAILED type=ValueError"]


def test_connects_immediately_then_paces_retries_and_does_not_restart_a_join_in_progress():
    r = Rig()
    r.wlan.link = -2                                         # NONET: retry every 5 s
    r.run(16_000)
    assert r.connects == [100, 5100, 10100, 15100]
    r.connects.clear()
    r.wlan.link = 1                                          # JOIN in progress: leave it alone until the grace
    r.run(20_000)
    assert r.connects == [35_100]


def test_escalates_after_two_minutes_down_and_then_every_two_minutes():
    r = Rig()
    r.wlan.link = -1
    r.run(119_900)
    assert r.sup.escalations == 0
    r.run(200)
    assert r.sup.escalations == 1
    assert r.wlan.calls[-4:] == ["disconnect", ("active", False), ("active", True), ("config", {"pm": PM_NONE})]
    assert r.feeds >= 4                                       # the watchdog is fed around every blocking step
    assert any(line.startswith("# WIFI_ESCALATE n=1 down_ms=120000 status=FAIL(-1)") for line in r.log)
    r.run(120_000)
    assert r.sup.escalations == 2


def test_reconnects_are_counted_after_the_first_connection_only():
    r = Rig()
    r.run(1_000)
    r.wlan.connected = True
    r.run(1_000)
    assert r.sup.reconnects == 0                              # the boot connection is not a reconnect
    for _ in range(3):
        r.wlan.connected = False
        r.wlan.link = 0
        r.run(30_000)
        r.wlan.connected = True
        r.run(1_000)
    assert r.sup.reconnects == 3 and r.sup.longest_down_ms == 30_000
    assert sum(line.startswith("# WIFI_UP") for line in r.log) == 4


def test_status_changes_are_logged_with_their_name_and_never_any_credentials():
    r = Rig()
    r.wlan.link = 1
    r.run(500)
    r.wlan.link = -3
    r.run(500)
    joined = "\n".join(r.log)
    assert "# WIFI_DOWN status=JOIN(1)" in joined and "# WIFI_STATUS status=BADAUTH(-3)" in joined
    assert link_name(7) == "?(7)" and set(LINK_NAMES) == {0, 1, 2, 3, -1, -2, -3}


def test_an_escalation_step_failing_is_logged_and_the_rest_still_runs():
    r = Rig()

    def bad_disconnect():
        raise OSError("not connected")
    r.wlan.disconnect = bad_disconnect
    r.wlan.link = 0
    r.run(120_100)
    assert r.sup.escalations == 1 and ("active", True) in r.wlan.calls
    assert "# WIFI_ESCALATE_STEP_FAILED step=disconnect type=OSError" in r.log


def test_a_connect_raising_oserror_never_escapes():
    r = Rig()

    def raising():
        raise OSError("already connecting")
    r.sup._connect = raising
    r.run(11_000)
    assert r.sup.connect_calls == 3
