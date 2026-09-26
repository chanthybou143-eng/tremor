"""PPS-to-UTC time sync for TREMOR (see nmea_parser.py for the RMC parser
this pairs against). Pico-only -- imports machine.Pin and relies on
time.ticks_us()/ticks_diff(), neither available under desktop Python.

The MAX-M10S emits its NMEA burst for second N right after the PPS edge
marking the start of second N, so PPSTimeSync pairs each valid RMC fix with
the most recent PPS edge that hasn't been paired yet. Once at least one
pairing (an "anchor") is accepted, any later time.ticks_us() reading can be
converted to UTC by measuring its ticks offset from the anchor.
"""

from machine import Pin
import time

from nmea_parser import parse_rmc_both

_ANCHOR_SANITY_TOLERANCE_S = 0.250  # see feed_nmea: reject an anchor whose
                                    # ticks/UTC deltas disagree by more than this
_ANCHOR_MAX_AGE_S = 300  # see ticks_to_utc/_is_anchor_fresh: an anchor normally
                          # refreshes every ~1s under continuous GPS sync, but if
                          # the module loses lock for an extended stretch it stops
                          # refreshing -- time.ticks_diff() against it is only
                          # *guaranteed* correct while the true elapsed time is
                          # under roughly half of MicroPython's ticks wrap period
                          # (spec guarantees that period is at least 2**31 ticks,
                          # i.e. correctness only guaranteed under ~2**30us =~
                          # 17.9 minutes). 300s is comfortably inside that floor,
                          # and since this gets checked on every sample (~1030Hz),
                          # a real outage gets caught and reported as "not synced"
                          # long before staleness could reach the truly-unsafe
                          # range -- rather than silently producing a wrapped,
                          # wrong UTC value from a diff against a months-old
                          # anchor with no indication anything's wrong.
# PPS interval filter. An edge is accepted only if its distance from the last ACCEPTED edge is within
# +/-PPS_TOLERANCE_US of a whole number of seconds (1..PPS_MAX_MULTIPLE -- a real edge that was missed
# arrives at 2 s, 3 s, ...). Everything else is a glitch: counted, and never used. If no edge has been
# accepted for PPS_REANCHOR_US the next one is taken unconditionally, so a bad first reference (or a long
# outage) can never lock the filter out. Integers only: this runs in the pin interrupt.
PPS_TOLERANCE_US = 50000
PPS_MAX_MULTIPLE = 5
PPS_REANCHOR_US = 6000000
# Anchor recovery. The 250 ms sanity check compares every new candidate anchor with the CURRENT anchor, so
# a wrong first anchor (e.g. an RMC read after the NEXT edge had already arrived, which pairs it a whole
# second off) made every later, correct candidate look wrong and the unit stayed ~1 s off until it
# rebooted (seen on the device, 2026-09-26). If PPS_REANCHOR_STREAK rejected candidates in a row agree
# with EACH OTHER, the old anchor is the outlier and the newest candidate replaces it. Mispairs during a
# POST disagree with each other (and are interleaved with good anchors, which reset the streak), so they
# never trigger this.
PPS_REANCHOR_STREAK = 5
# ...and only candidates that were NOT possibly stale. While the main loop is blocked (a POST, up to 25 s;
# the boot-time Wi-Fi connect) nobody reads the GPS UART, so the first sentence read afterwards can be
# several seconds old yet gets paired with the newest PPS edge: a mispair whose size is the blocked time.
# Slow POSTs of similar length (a long outage: every POST times out after the same 4 s) would make those
# mispairs agree with each other. So a candidate that arrives within PPS_BLOCK_SHADOW_US of the end of a
# blocking window is IGNORED -- it neither extends nor resets the streak -- and, before the first anchor
# exists, is not accepted as one either.
PPS_BLOCK_SHADOW_US = 2000000
_ANCHOR_MAX_AGE_US = _ANCHOR_MAX_AGE_S * 1000000  # 3e8 -- inside MicroPython's 31-bit small-int range (< 1.07e9)
_SECONDS_PER_DAY = 86400


def days_from_civil(year, month, day):
    """Days since 1970-01-01 for a proleptic-Gregorian date, integer-only
    (Howard Hinnant's algorithm). No float, no datetime/calendar module -- so it
    runs identically on CPython (where it is tested against datetime.date) and
    on MicroPython."""
    if month <= 2:
        year -= 1
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    mp = month - 3 if month > 2 else month + 9
    doy = (153 * mp + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


class PPSTimeSync:
    def __init__(self, pps_pin=15, tolerance_us=PPS_TOLERANCE_US, on_reanchor=None):
        self._tol_us = tolerance_us
        self._on_reanchor = on_reanchor     # called with (old_utc, new_utc, correction_us) -- see feed_nmea
        self._blocked = False               # the main loop is inside a long blocking call (a POST)
        self._block_end_ticks = None        # when the last blocking window ended
        self.shadow_ignored = 0             # candidates ignored because they may have been stale (see PPS_BLOCK_SHADOW_US)
        self.pps_count = 0              # every raw rising edge seen, glitches included
        self.pps_accepted = 0           # edges that passed the interval filter
        self.pps_rejected = 0           # glitches: not ~1 s (or a whole number of s) after the last accepted edge
        self.pps_resync = 0             # accepted after a gap (a missed real edge) or by re-anchoring
        self._good_ticks = None         # the last ACCEPTED edge -- the filter's reference
        self._reject_interval_us = None # diagnostic: the interval of the most recently rejected edge
        self.sync_count = 0
        self.rejected_count = 0
        self.no_edge_count = 0  # diagnostic: valid RMC parsed, but no pending PPS edge to pair it with
        self.reanchor_last_correction_us = None  # how far the replaced anchor was off, at the moment it was replaced
        self.reanchor_count = 0         # times a run of mutually consistent rejected candidates replaced the anchor
        self._cand_ticks = None         # the previous REJECTED candidate anchor (edge ticks, UTC seconds) ...
        self._cand_utc = None
        self._cand_streak = 0           # ... and how many rejected candidates in a row have agreed with each other

        self._pps_period_us = None
        self._pending_edge_ticks = None  # most recent PPS edge not yet paired to a sentence

        self._anchor_ticks = None       # ticks_us() at the PPS edge that starts _anchor_utc_s
        self._anchor_utc_s = None       # UTC seconds-of-day at _anchor_ticks
        self._anchor_date = None        # (year, month, day) at _anchor_ticks
        # Integer twin of the anchor above, for ticks_to_gps(): the RMC time/date
        # parsed WITHOUT any float (see nmea_parser.parse_rmc_int).
        self._anchor_days = None        # days since 1970-01-01 at the anchor's PPS edge
        self._anchor_sod = None         # whole UTC second-of-day at the anchor
        self._anchor_usec = 0           # microsecond within that second

        self._pin = Pin(pps_pin, Pin.IN)
        # hard=True: the handler runs in the real interrupt, so ticks_us() is read within microseconds of
        # the edge. A soft (scheduled) handler -- the default -- only runs when the VM next gets to run
        # scheduled callbacks, which does not happen while the main thread sits in a blocking network
        # call (each ~2.3 s POST): edges were then stamped late, batched, or dropped (see CLAUDE.md,
        # 2026-09-26 RAM run). _on_pps is integer-only and allocation-free for exactly this reason.
        self._pin.irq(trigger=Pin.IRQ_RISING, handler=self._on_pps, hard=True)

    def _on_pps(self, pin):
        # ISR: clock read + integer bookkeeping only -- no parsing, no allocation beyond plain ints,
        # no printing. Only an ACCEPTED edge becomes the pending edge that feed_nmea() pairs an RMC
        # sentence with (the previous version overwrote it with every edge, so a glitch between the
        # real PPS and its sentence stole the pairing).
        now = time.ticks_us()
        self.pps_count += 1
        ref = self._good_ticks
        if ref is None:
            self._accept(now, False)                # nothing to compare with yet: take the first edge
            return
        interval = time.ticks_diff(now, ref)
        k = (interval + 500000) // 1000000         # nearest whole number of seconds
        if 1 <= k <= PPS_MAX_MULTIPLE:
            err = interval - k * 1000000
            if -self._tol_us <= err <= self._tol_us:
                self._accept(now, k > 1)
                if k == 1:
                    self._pps_period_us = interval
                return
        elif interval >= PPS_REANCHOR_US:
            self._accept(now, True)                 # nothing accepted for a long time: start over from this edge
            return
        self.pps_rejected += 1
        self._reject_interval_us = interval

    def _accept(self, now, resync):
        self._good_ticks = now
        self._pending_edge_ticks = now
        self.pps_accepted += 1
        if resync:
            self.pps_resync += 1

    def blocking_started(self):
        """The main loop is about to block for a long time (a POST): UART data will pile up unread."""
        self._blocked = True

    def blocking_ended(self):
        """The main loop is back (call this once before its first pass, too: boot-time Wi-Fi connect blocks).
        Sentences read for the next PPS_BLOCK_SHADOW_US may be stale and are not trusted as evidence."""
        self._blocked = False
        self._block_end_ticks = time.ticks_us()

    def _in_shadow(self):
        if self._blocked:
            return True
        end = self._block_end_ticks
        return end is not None and time.ticks_diff(time.ticks_us(), end) < PPS_BLOCK_SHADOW_US

    @staticmethod
    def _agrees(edge_ticks, utc_s, ref_ticks, ref_utc_s):
        """Do (edge_ticks, utc_s) and the reference (ref_ticks, ref_utc_s) describe the same clock?
        ticks elapsed vs UTC elapsed, within _ANCHOR_SANITY_TOLERANCE_S, folded across UTC midnight."""
        ticks_delta_s = time.ticks_diff(edge_ticks, ref_ticks) / 1e6
        utc_delta_s = utc_s - ref_utc_s
        # UTC midnight rollover: fold the delta back into (-12h, 12h]
        if utc_delta_s > _SECONDS_PER_DAY / 2:
            utc_delta_s -= _SECONDS_PER_DAY
        elif utc_delta_s < -_SECONDS_PER_DAY / 2:
            utc_delta_s += _SECONDS_PER_DAY
        return abs(ticks_delta_s - utc_delta_s) <= _ANCHOR_SANITY_TOLERANCE_S

    def _report_reanchor(self, edge_ticks, new_sod, new_usec):
        """Old and new anchor, both expressed as UTC at the NEW edge (integers only): the old anchor's
        projection forward and the sentence's own time. correction_us is what the old anchor was off by."""
        rel = self._anchor_usec + time.ticks_diff(edge_ticks, self._anchor_ticks)
        old_sod = self._anchor_sod + rel // 1000000
        old_usec = rel % 1000000
        d_sod = (new_sod - old_sod + 43200) % 86400 - 43200          # fold across UTC midnight
        self.reanchor_last_correction_us = d_sod * 1000000 + (new_usec - old_usec)   # < 0: the old anchor was FAST
        if self._on_reanchor is not None:
            self._on_reanchor((old_sod % 86400, old_usec), (new_sod, new_usec), self.reanchor_last_correction_us)

    def feed_nmea(self, line):
        """Call from the main loop with each raw GPS UART line (whatever
        sentence type -- non-RMC lines and void fixes are simply ignored).
        Non-blocking, does no I/O itself."""
        both = parse_rmc_both(line)
        if both is None:
            return
        (utc_s, date), (int_sod, int_usec, _date) = both

        edge_ticks = self._pending_edge_ticks
        if edge_ticks is None:
            self.no_edge_count += 1
            return  # no PPS edge seen yet to pair this sentence with
        self._pending_edge_ticks = None  # consume it -- don't pair it again

        if self._anchor_ticks is None:
            if self._in_shadow():
                self.shadow_ignored += 1
                return                          # possibly stale (e.g. read after the boot-time Wi-Fi connect): wait for a fresh one
        else:
            if not self._agrees(edge_ticks, utc_s, self._anchor_ticks, self._anchor_utc_s):
                self.rejected_count += 1
                if self._in_shadow():
                    self.shadow_ignored += 1
                    return                      # never evidence for a re-anchor: neither extends nor resets the streak
                if self._cand_ticks is not None and self._agrees(edge_ticks, utc_s, self._cand_ticks, self._cand_utc):
                    self._cand_streak += 1
                else:
                    self._cand_streak = 1
                self._cand_ticks = edge_ticks
                self._cand_utc = utc_s
                if self._cand_streak < PPS_REANCHOR_STREAK:
                    return
                self.reanchor_count += 1        # the candidates agree with each other, not with the anchor: replace it
                self._report_reanchor(edge_ticks, int_sod, int_usec)
        self._cand_ticks = None
        self._cand_streak = 0

        self._anchor_ticks = edge_ticks
        self._anchor_utc_s = utc_s
        self._anchor_date = date
        self._anchor_days = days_from_civil(date[0], date[1], date[2])
        self._anchor_sod = int_sod
        self._anchor_usec = int_usec
        self.sync_count += 1

    def ticks_to_utc(self, ticks_us_value):
        """Convert a time.ticks_us() reading to UTC seconds-of-day, or
        None if not yet synced -- or no longer confidently synced, if the
        anchor has gone stale (see _ANCHOR_MAX_AGE_S)."""
        if self._anchor_ticks is None:
            return None
        delta_s = time.ticks_diff(ticks_us_value, self._anchor_ticks) / 1e6
        if abs(delta_s) > _ANCHOR_MAX_AGE_S:
            return None
        utc_s = self._anchor_utc_s + delta_s
        if utc_s >= _SECONDS_PER_DAY:
            utc_s -= _SECONDS_PER_DAY
        elif utc_s < 0:
            utc_s += _SECONDS_PER_DAY
        return utc_s

    def ticks_to_gps(self, ticks_us_value):
        """Full-precision GPS UTC for a time.ticks_us() reading, as three small
        ints ``(days_since_1970, second_of_day, microsecond)`` -- or None if not
        synced / the anchor is stale (same 300 s rule as ticks_to_utc).

        Integer-only on purpose: ticks_to_utc()'s float result is single
        precision on a stock MicroPython build (~4-8 ms at these magnitudes),
        far too coarse for TREMOR's inter-unit arrival-time comparisons. Every
        intermediate here fits MicroPython's 31-bit small ints (|delta| <= 3e8 us,
        anchor microseconds < 1e6, days ~2e4, seconds < 86400 + 300), so no
        long-int allocation and no rounding; // and % are floor operations, so
        a reading slightly BEFORE the anchor (negative delta) and a rollover
        past 24:00:00 both normalise correctly."""
        if self._anchor_sod is None:
            return None
        delta_us = time.ticks_diff(ticks_us_value, self._anchor_ticks)
        if delta_us > _ANCHOR_MAX_AGE_US or delta_us < -_ANCHOR_MAX_AGE_US:
            return None
        us = self._anchor_usec + delta_us
        sec = self._anchor_sod + us // 1000000
        us = us % 1000000
        days = self._anchor_days + sec // _SECONDS_PER_DAY
        sec = sec % _SECONDS_PER_DAY
        return days, sec, us

    def _is_anchor_fresh(self):
        if self._anchor_ticks is None:
            return False
        age_s = time.ticks_diff(time.ticks_us(), self._anchor_ticks) / 1e6
        return abs(age_s) <= _ANCHOR_MAX_AGE_S

    @property
    def status(self):
        return {
            "synced": self._is_anchor_fresh(),
            "pps_count": self.pps_count,
            "pps_accepted": self.pps_accepted,
            "pps_rejected": self.pps_rejected,
            "pps_resync": self.pps_resync,
            "pps_reject_interval_us": self._reject_interval_us,
            "sync_count": self.sync_count,
            "rejected_count": self.rejected_count,
            "no_edge_count": self.no_edge_count,
            "reanchor_count": self.reanchor_count,
            "reanchor_last_correction_us": self.reanchor_last_correction_us,
            "shadow_ignored": self.shadow_ignored,
            "pps_period_us": self._pps_period_us,
            "anchor_date": self._anchor_date,
        }
