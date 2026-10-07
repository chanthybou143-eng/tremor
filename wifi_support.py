"""Wi-Fi supervision for the Pico client: power saving off, non-blocking reconnects, and escalation
when a plain reconnect does not work. Hardware-free (the WLAN object, clock and logger are injected),
so it runs and is tested on the desktop: tests/test_wifi_support.py.

Never sees or logs the SSID or password: the client passes a connect_fn closure that holds them.

cyw43 facts this relies on (MicroPython v1.27, extmod/network_cyw43.c):
  * WLAN.PM_NONE / PM_PERFORMANCE / PM_POWERSAVE exist; config(pm=...) sets the mode and config("pm")
    reads it back. The driver's default is PM_PERFORMANCE (PM2: the radio sleeps between beacons,
    200 ms). PM_NONE keeps it awake: a little more power, no added latency on a weak uplink.
  * status() with no argument is cyw43_tcpip_link_status(): see LINK_NAMES.
  * disconnect() leaves the network; active(False) additionally takes the interface down
    (cyw43_wifi_set_up(..., false)), active(True) brings it back up -- a full reset of the link
    state that a stuck association can need. Power management must be applied again after it.
"""

LINK_NAMES = {0: "DOWN", 1: "JOIN", 2: "NOIP", 3: "UP", -1: "FAIL", -2: "NONET", -3: "BADAUTH"}
_JOINING = (1, 2)                  # an attempt is in progress: don't restart it every retry_ms


def link_name(code):
    return "{}({})".format(LINK_NAMES.get(code, "?"), code)


def _hex(v):
    return "0x{:x}".format(v) if isinstance(v, int) else str(v)


class WifiSupervisor:
    """Call service() on every main-loop pass; it never blocks except during an escalation
    (disconnect + active(False) + active(True): measured on the bench, see the fw-resilience report).

    While disconnected: a connect is started at most every retry_ms -- but not while the driver is
    still joining / waiting for an address, unless join_grace_ms has passed since the last attempt.
    After escalate_after_ms without a connection (and every escalate_after_ms after that), the link
    is reset: disconnect(), active(False), active(True), power management re-applied, connect.
    """

    def __init__(self, wlan, connect_fn, ticks_ms, ticks_diff, pm_value=None, retry_ms=5000,
                 join_grace_ms=20000, escalate_after_ms=120000, log_fn=None, feed_fn=None):
        self._wlan = wlan
        self._connect = connect_fn
        self._now = ticks_ms
        self._diff = ticks_diff
        self._pm = pm_value
        self.retry_ms = retry_ms
        self.join_grace_ms = join_grace_ms
        self.escalate_after_ms = escalate_after_ms
        self._log = log_fn or (lambda line: None)
        self._feed = feed_fn or (lambda: None)
        self._ever_up = False
        self._down_since = None
        self._last_attempt = None
        self._last_escalation = None
        self._last_status = None
        self.reconnects = 0          # down -> up transitions after the first connection of this boot
        self.escalations = 0
        self.connect_calls = 0
        self.longest_down_ms = 0
        self.pm_readback = None

    def apply_pm(self):
        """Set the power-management mode (if one was given) and read it back for the log."""
        if self._pm is None:
            return None
        try:
            before = self._wlan.config("pm")
        except Exception:
            before = None
        try:
            self._wlan.config(pm=self._pm)
            self.pm_readback = self._wlan.config("pm")
        except Exception as exc:
            self._log("# WIFI_PM_FAILED type={}".format(type(exc).__name__))
            return None
        self._log("# WIFI_PM requested={} before={} after={}".format(
            _hex(self._pm), "n/a" if before is None else _hex(before), _hex(self.pm_readback)))
        return self.pm_readback

    def _status(self):
        try:
            return self._wlan.status()
        except Exception:
            return None

    def status_code(self):
        """wlan.status() for a log line; None (never an exception) if the driver cannot say."""
        return self._status()

    def _start_connect(self, now):
        self._last_attempt = now
        self.connect_calls += 1
        try:
            self._connect()
        except OSError:
            pass                     # e.g. "already connecting" -- the next retry catches a real failure

    def _escalate(self, now, down_ms):
        self.escalations += 1
        self._last_escalation = now
        self._log("# WIFI_ESCALATE n={} down_ms={} status={}".format(
            self.escalations, down_ms, link_name(self._status())))
        for step in ("disconnect", "inactive", "active"):
            self._feed()
            try:
                if step == "disconnect":
                    self._wlan.disconnect()
                elif step == "inactive":
                    self._wlan.active(False)
                else:
                    self._wlan.active(True)
            except Exception as exc:
                self._log("# WIFI_ESCALATE_STEP_FAILED step={} type={}".format(step, type(exc).__name__))
        self._feed()
        self.apply_pm()
        self._start_connect(self._now())

    def service(self):
        now = self._now()
        if self._wlan.isconnected():
            if self._down_since is not None:
                down_ms = self._diff(now, self._down_since)
                if down_ms > self.longest_down_ms:
                    self.longest_down_ms = down_ms
                if self._ever_up:
                    self.reconnects += 1
                self._log("# WIFI_UP after_ms={} reconnects={} escalations={}".format(
                    down_ms, self.reconnects, self.escalations))
                self._down_since = None
                self._last_escalation = None
            self._ever_up = True
            self._last_status = 3
            return True

        status = self._status()
        if self._down_since is None:
            self._down_since = now
            self._log("# WIFI_DOWN status={}".format(link_name(status)))
        elif status != self._last_status:
            self._log("# WIFI_STATUS status={} down_ms={}".format(link_name(status), self._diff(now, self._down_since)))
        self._last_status = status

        down_ms = self._diff(now, self._down_since)
        since_esc = None if self._last_escalation is None else self._diff(now, self._last_escalation)
        if down_ms >= self.escalate_after_ms and (since_esc is None or since_esc >= self.escalate_after_ms):
            self._escalate(now, down_ms)
            return False

        if self._last_attempt is None:
            self._start_connect(now)
            return False
        since = self._diff(now, self._last_attempt)
        if since >= self.retry_ms and (status not in _JOINING or since >= self.join_grace_ms):
            self._start_connect(now)
        return False
