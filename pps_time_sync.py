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
    def __init__(self, pps_pin=15, tolerance_us=PPS_TOLERANCE_US):
        self._tol_us = tolerance_us
        self.pps_count = 0              # every raw rising edge seen, glitches included
        self.pps_accepted = 0           # edges that passed the interval filter
        self.pps_rejected = 0           # glitches: not ~1 s (or a whole number of s) after the last accepted edge
        self.pps_resync = 0             # accepted after a gap (a missed real edge) or by re-anchoring
        self._good_ticks = None         # the last ACCEPTED edge -- the filter's reference
        self._reject_interval_us = None # diagnostic: the interval of the most recently rejected edge
        self.sync_count = 0
        self.rejected_count = 0
        self.no_edge_count = 0  # diagnostic: valid RMC parsed, but no pending PPS edge to pair it with

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

        if self._anchor_ticks is not None:
            ticks_delta_s = time.ticks_diff(edge_ticks, self._anchor_ticks) / 1e6
            utc_delta_s = utc_s - self._anchor_utc_s
            # UTC midnight rollover: fold the delta back into (-12h, 12h]
            if utc_delta_s > _SECONDS_PER_DAY / 2:
                utc_delta_s -= _SECONDS_PER_DAY
            elif utc_delta_s < -_SECONDS_PER_DAY / 2:
                utc_delta_s += _SECONDS_PER_DAY
            if abs(ticks_delta_s - utc_delta_s) > _ANCHOR_SANITY_TOLERANCE_S:
                self.rejected_count += 1
                return

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
            "pps_period_us": self._pps_period_us,
            "anchor_date": self._anchor_date,
        }
