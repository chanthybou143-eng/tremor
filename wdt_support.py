"""Watchdog helpers for the Pico client: a bounded feeding guard for the POST, and a
breadcrumb that survives a watchdog reset.

Both take everything hardware-specific as an injected callable, so they run (and are
tested) on the desktop:  tests/test_wdt_support.py.

Why this exists (overnight freeze of 2026-09-25 15:04 UTC, reproduced on hardware):
  * machine.WDT(8000) is fed by the main loop and at POST stage transitions, but a SINGLE
    blocking C call can hold the CPU past 8 s with nothing to feed it:
      - socket.getaddrinfo has no timeout parameter; with a dead DNS server it blocks 6.5-7 s
        (lwIP's own retry schedule);
      - ssl.wrap_socket's handshake honours sock.settimeout(4) PER SOCKET OPERATION, not for the
        handshake as a whole: with every individual wait at 3.6 s (under the 4 s timeout) the
        handshake still ran 8.18 s -- longer than the watchdog -- and reset the board.
  * A timer callback DOES run while the main thread is stuck inside such a call (soft and hard
    Timers both fed the WDT through the same 8.18 s handshake on the device), so a timer can
    keep the watchdog fed for a bounded window around the POST.
  * A watchdog reset throws away the RAM buffer (up to ~10 minutes of readings after a run of
    failed POSTs); letting a slow POST finish or fail cleanly loses only the ring-buffer samples
    that overflow meanwhile (counted in overflow_count).
"""


class WatchdogGuard:
    """Feeds the watchdog from a periodic timer callback, but ONLY inside a bounded window
    opened with start() and closed with stop().

    The window is a hard cap: once ``window_ms`` has passed since start() the callback stops
    feeding even if stop() was never called, so a genuine hang inside the POST is still reset by
    the watchdog after at most ``window_ms`` + the WDT timeout. Outside a window the callback
    does nothing at all, so a hang in the main loop keeps the normal 8 s watchdog.
    """

    def __init__(self, feed_fn, ticks_ms_fn, ticks_diff_fn, window_ms=25000,
                 wdt_timeout_ms=8000, period_ms=1000, stage_fn=None, log_fn=None):
        self._feed = feed_fn
        self._ticks_ms = ticks_ms_fn
        self._ticks_diff = ticks_diff_fn
        self.window_ms = window_ms
        self._extend_after_ms = wdt_timeout_ms - period_ms   # a stall this long is one the WDT alone would not survive
        self._stage_fn = stage_fn
        self._log_fn = log_fn
        self._started_at = None
        self._last_main_feed = None
        self._ext_logged = False
        self.windows = 0            # POSTs guarded
        self.feeds = 0              # feeds the guard itself performed (i.e. stalls it covered)
        self.expired = 0            # windows that ran out before stop() -- a POST stuck past the cap
        self.extensions = 0         # stalls that outlasted what the bare watchdog tolerates (each logged)
        self.longest_stall_ms = 0   # longest time the main thread went without feeding, inside a window

    def start(self):
        now = self._ticks_ms()
        self._started_at = now
        self._last_main_feed = now
        self._ext_logged = False
        self.windows += 1

    def stop(self):
        self._started_at = None

    def note_main_feed(self):
        """The main thread fed the watchdog itself: the current stall (if any) is over."""
        self._last_main_feed = self._ticks_ms()
        self._ext_logged = False

    def _log(self, kind, stalled_ms, elapsed_ms):
        if self._log_fn is not None:
            try:
                self._log_fn(kind, self._stage_fn() if self._stage_fn is not None else None,
                             stalled_ms, elapsed_ms)
            except Exception:
                pass                # a logging problem must never stop the feed / the cap

    def tick(self, _timer=None):
        """Timer callback. The cap is enforced HERE, from the timer, not by the POST code: whatever
        the main thread is stuck in cannot extend the window."""
        started = self._started_at
        if started is None:
            return
        now = self._ticks_ms()
        elapsed = self._ticks_diff(now, started)
        if elapsed < self.window_ms:
            stalled = self._ticks_diff(now, self._last_main_feed)
            if stalled > self.longest_stall_ms:
                self.longest_stall_ms = stalled
            if stalled >= self._extend_after_ms and not self._ext_logged:
                self._ext_logged = True
                self.extensions += 1
                self._log("extended", stalled, elapsed)     # BEFORE the feed: names the stage even if it dies next
            self._feed()
            self.feeds += 1
        else:
            self._started_at = None
            self.expired += 1
            self._log("expired", self._ticks_diff(now, self._last_main_feed), elapsed)


class Breadcrumb:
    """Three watchdog scratch words (RP2350 WATCHDOG SCRATCH0..2) holding the current POST
    stage. They survive a watchdog reset (verified on the device: values written before an
    un-fed WDT reset read back intact) but not a power cycle, so after a reset the client can
    report WHICH stage was running -- even when no USB host is attached to see the log.

    SCRATCH4..7 are used by the bootrom's reboot API and are left alone.
    Layout: word0 = MAGIC, word1 = stage code | post_no << 8, word2 = ticks_ms at stage start.
    """

    BASE = 0x400D8000 + 0x0C
    MAGIC = 0x7A5C0DE1
    STAGES = ("idle", "dns", "connect", "tls_handshake", "send", "read_response")

    def __init__(self, mem32, base=None):
        self._m = mem32
        self._b = self.BASE if base is None else base

    def mark(self, stage, post_no, ticks_ms):
        code = self.STAGES.index(stage) if stage in self.STAGES else 0
        m, b = self._m, self._b
        m[b + 4] = (code & 0xFF) | ((post_no & 0xFFFFFF) << 8)
        m[b + 8] = ticks_ms & 0xFFFFFFFF
        m[b] = self.MAGIC                     # written last: a half-written record is never "valid"

    def read_and_clear(self):
        """The last recorded (stage, post_no, ticks_ms), or None if nothing valid is stored.
        Clears it, so a later soft reboot cannot report the same freeze twice. Only meaningful
        when the previous reset was a watchdog reset -- the caller checks that."""
        m, b = self._m, self._b
        if (m[b] & 0xFFFFFFFF) != self.MAGIC:      # mem32 may hand back a signed value
            return None
        w1, w2 = m[b + 4] & 0xFFFFFFFF, m[b + 8] & 0xFFFFFFFF
        m[b] = 0
        code = w1 & 0xFF
        stage = self.STAGES[code] if code < len(self.STAGES) else "unknown({})".format(code)
        return {"stage": stage, "post_no": w1 >> 8, "at_ms": w2}


class ResetCounter:
    """Consecutive-watchdog-reset counter in WATCHDOG SCRATCH3 (survives a WDT reset, not a power
    cycle). One word: [31:16] magic | bit 15 "armed" | [14:0] count.

    "armed" is set by main.py just before it starts the client (the client arms the watchdog at import)
    and cleared whenever main.py returns to the REPL without arming it. That is what tells a real
    watchdog reset from a soft reset: machine.reset_cause() keeps reporting WDT_RESET across soft resets
    (Ctrl-D), so on its own it would count every REPL restart after a freeze.

    on_boot(): WDT_RESET + armed -> count + 1; any other hardware cause (power-on, RUN pin, ...) -> 0;
    WDT_RESET but not armed (a soft reset from the REPL) -> count unchanged.
    mark_healthy(): the client calls it after its first successful POST -> count = 0, so "consecutive"
    means "since the last time the unit demonstrably worked".
    """

    ADDR = 0x400D8000 + 0x0C + 12          # SCRATCH3
    MAGIC = 0xC0DE
    ARMED = 0x8000
    MAX = 0x7FFF

    def __init__(self, mem32, addr=None):
        self._m = mem32
        self._a = self.ADDR if addr is None else addr

    def _read(self):
        w = self._m[self._a] & 0xFFFFFFFF          # mem32 reads come back SIGNED on this build (0xC0DE8000 -> -0x3F218000)
        if (w >> 16) != self.MAGIC:
            return 0, False
        return w & self.MAX, bool(w & self.ARMED)

    def _write(self, count, armed):
        self._m[self._a] = (self.MAGIC << 16) | (self.ARMED if armed else 0) | (count if count < self.MAX else self.MAX)

    def on_boot(self, cause_is_wdt):
        count, armed = self._read()
        if cause_is_wdt:
            if armed:
                count += 1
        else:
            count = 0
        self._write(count, False)
        return count

    def arm_started(self):
        count, _ = self._read()
        self._write(count, True)

    def disarmed(self):
        count, _ = self._read()
        self._write(count, False)

    def mark_healthy(self):
        count, armed = self._read()
        if count:
            self._write(0, armed)

    @property
    def count(self):
        return self._read()[0]
