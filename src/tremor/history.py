"""History overview for the /history page: everything stored, summarised for a browser.

One request returns all the page draws for a unit and a time range, already downsampled
server-side, so the payload stays small however long the range is:

  * frequency: mean with a min-max band per bucket (at most ``points`` buckets)
  * RoCoF: max |RoCoF| per minute (or per bucket, once a bucket is longer than a minute)
  * coverage per hour (coarser for very long ranges): good / excluded / missing
  * a daily table per LOCAL day of ``tz`` (or per UTC day): mean, std, min, max, % coverage

Time zones: everything stored and every timestamp in the response is UTC. ``tz`` (an IANA name,
e.g. "Australia/Adelaide") only decides where buckets start (whole local hours / days) and how the
daily table is cut: local days are found with zoneinfo, so a daylight-saving changeover day is
23 h or 25 h long and its coverage is measured against that real length. Buckets are contiguous
in UTC, so a changeover never shows as a gap or an overlap.

Where the numbers come from, per UTC day:

  * ranges up to ``RAW_MAX_SPAN_S`` (6 h) use the raw readings while they are still stored
    (raw rows are pruned after ``raw_days``; a pruned day falls back to the aggregates);
  * longer ranges use the stored 1-minute aggregates (``readings_1min``), summed in SQL;
  * a day the retention engine has not aggregated yet (today, and yesterday until it settles)
    is computed from its raw readings on the fly, cached briefly per day (``RawDayCache``).

Exclusions (``known_bad_boot``, ``low_amplitude``, ``freq_out_of_band``) never enter the
statistics; they are counted separately and listed as periods. Raw readings are excluded one
by one. A stored aggregate can no longer be split, so a minute whose aggregate shows any
reading outside the band (or a mean amplitude below the threshold) is excluded as a whole.
RoCoF from raw readings is fitted over good readings only, so an unplugged-plugpack reading
never produces a spurious slope; stored aggregates were fitted over every locked reading.

Coverage counts seconds with at least one good, GPS-timed reading (one reading per second is
the project's cadence, so a minute contributes at most 60 s). Unlocked readings have no GPS
time and count as missing.
"""

from __future__ import annotations

import math
import threading
from bisect import bisect_left
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .store import DAY_US, MINUTE_US, US, AggRow, DayState, Exclusion, ReadingStore
from .timeline import rocof_series

EXCLUDE_AMP_BELOW_V = 0.1
# Wider than any real NEM contingency excursion (~47-52 Hz is the whole operating envelope),
# so a genuine event is never excluded; catches the 80.995 Hz glitch and unplugged readings.
EXCLUDE_FREQ_LO_HZ = 47.0
EXCLUDE_FREQ_HI_HZ = 52.0

REASON_BOOT = "known_bad_boot"
REASON_AMP = "low_amplitude"
REASON_FREQ = "freq_out_of_band"

RAW_MAX_SPAN_S = 6 * 3600
HOUR_US = 3600 * US
DEFAULT_POINTS = 1000
MIN_POINTS = 100
MAX_POINTS = 2000
MAX_COVERAGE_BARS = 800
MAX_EXCLUDED_PERIODS = 300
EXCLUDED_MERGE_GAP_S = 60
ROCOF_MARGIN_S = 10                       # same lead-in retention._aggregate_hour reads
NOMINAL_HZ = 50.0                         # sums are taken around nominal (see store.rollup_aggregates)
# Bucket widths, in seconds. Every width divides a day or is a whole number of days, so buckets
# of different charts nest inside each other and inside UTC days.
NICE_WIDTHS_S = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
                 43200, 86400, 172800, 604800)

# Raw results are cached per hour. An hour can still gain readings for ~10 minutes after it ends
# (the device's retry buffer), so it is "open" until RAW_HOUR_SETTLE_S past its end.
RAW_HOUR_SETTLE_S = 15 * 60
RAW_CACHE_TTL_OPEN_S = 30.0
RAW_CACHE_TTL_CLOSED_S = 3600.0
RAW_CACHE_MAX = 1200                      # entries: ~2 days of raw-path hours + 14 days of histogram hours
# Reads of raw readings are split so no single statement covers more than this many hours: under
# the rollback journal a read holds the shared lock an ingest commit must wait for.
RAW_READ_CHUNK_H = 6

# Frequency distribution: 5 mHz bins, bin = floor(f * 200).
HIST_BIN_HZ = 0.005
HIST_BINS_PER_HZ = 200
HIST_CACHE_TTL_S = 6 * 3600.0              # per-hour histograms of aggregated (closed) days
# Uncached raw-histogram work allowed per request. Past it, the rest of the hours are reported as
# pending (and filled in by later requests, since every computed hour is cached) rather than
# holding the single web worker -- and the Pico's next POST -- for seconds.
HIST_RAW_BUDGET_S = 0.5

# Server-wide cache of whole overview responses for long ranges (repeat visitors, several tabs).
OVERVIEW_CACHE_MIN_SPAN_S = 7 * 86400
OVERVIEW_CACHE_TTL_S = 300.0
OVERVIEW_CACHE_QUANTUM_S = 300            # from/to are keyed to 5-minute slots
OVERVIEW_CACHE_MAX = 32


def _utc_us(*a) -> int:
    return int(datetime(*a, tzinfo=timezone.utc).timestamp()) * US


@dataclass(frozen=True)
class KnownBad:
    unit_id: str
    boot_id: str
    start_us: int
    end_us: int
    note: str


# Known-bad data (see CLAUDE.md "Known-bad data"). Excluded by boot_id from raw readings, and by
# time from aggregates. The span comes from the host log of that run (03:47:28Z-03:52:33Z).
KNOWN_BAD: Tuple[KnownBad, ...] = (
    KnownBad("unit-1", "398474c3bef237a1", _utc_us(2026, 9, 26, 3, 47), _utc_us(2026, 9, 26, 3, 53),
             "anchor-lockout bug: every timestamp ~1 s early (RAM test run); rows deleted from the server"),
)


def exclusion_rules() -> dict:
    return dict(amplitude_below_v=EXCLUDE_AMP_BELOW_V, freq_band_hz=[EXCLUDE_FREQ_LO_HZ, EXCLUDE_FREQ_HI_HZ],
                known_bad_boots=[k.boot_id for k in KNOWN_BAD],
                aggregates="a whole minute is excluded if any of its readings was out of band "
                           "or its mean amplitude was low")


def nice_width(target_s: float, floor_s: int) -> int:
    t = max(target_s, floor_s)
    for w in NICE_WIDTHS_S:
        if w >= t:
            return w
    week = NICE_WIDTHS_S[-1]
    return int(math.ceil(t / week)) * week


def _mn(a, b):
    return b if a is None else (a if b is None else min(a, b))


def _mx(a, b):
    return b if a is None else (a if b is None else max(a, b))


class _Acc:
    """Summable statistics of a bucket: good-reading moments around nominal, extremes, coverage."""
    __slots__ = ("n", "n_ex", "secs", "secs_ex", "s1", "s2", "fmin", "fmax", "rmax")

    def __init__(self, n=0, n_ex=0, secs=0, secs_ex=0, s1=0.0, s2=0.0, fmin=None, fmax=None, rmax=None):
        self.n, self.n_ex, self.secs, self.secs_ex = n, n_ex, secs, secs_ex
        self.s1, self.s2, self.fmin, self.fmax, self.rmax = s1, s2, fmin, fmax, rmax

    def add(self, o: "_Acc") -> None:
        self.n += o.n
        self.n_ex += o.n_ex
        self.secs += o.secs
        self.secs_ex += o.secs_ex
        self.s1 += o.s1
        self.s2 += o.s2
        self.fmin = _mn(self.fmin, o.fmin)
        self.fmax = _mx(self.fmax, o.fmax)
        self.rmax = _mx(self.rmax, o.rmax)

    def mean(self) -> float:
        return NOMINAL_HZ + self.s1 / self.n

    def std(self) -> float:
        m = self.s1 / self.n
        return math.sqrt(max(0.0, self.s2 / self.n - m * m))


@dataclass
class _Period:
    start_us: int
    end_us: int
    reasons: Set[str] = field(default_factory=set)
    n: int = 0


Point = Tuple[int, float, Optional[float], Optional[str]]     # store.read_points: (us, freq, amp, boot)


def _classify(freq: float, amp: Optional[float], boot: Optional[str], bad_boots: Set[str]) -> Optional[str]:
    if boot is not None and boot in bad_boots:
        return REASON_BOOT
    if amp is not None and amp < EXCLUDE_AMP_BELOW_V:
        return REASON_AMP
    if not (EXCLUDE_FREQ_LO_HZ <= freq <= EXCLUDE_FREQ_HI_HZ):
        return REASON_FREQ
    return None


@dataclass
class _RawResult:
    minutes: Dict[int, _Acc]
    periods: List[_Period]
    fine: List[Tuple[int, float]]          # good readings inside the range: (gps_utc_us, freq_hz)
    hist: Dict[int, int]                   # good readings inside the range per 5 mHz bin


def _raw_minutes(pts: Sequence[Point], lo_us: int, hi_us: int, bad_boots: Set[str],
                 keep_fine: bool) -> _RawResult:
    """``pts``: locked readings in [lo_us - ROCOF_MARGIN_S, hi_us), in GPS-time order."""
    good: List[Tuple[float, float, Optional[str]]] = []
    good_us: List[int] = []
    excluded: List[Tuple[int, str]] = []
    for us, f, amp, boot in pts:
        why = _classify(f, amp, boot, bad_boots)
        if why is None:
            good.append((us / US, f, boot))
            good_us.append(us)
        elif lo_us <= us < hi_us:
            excluded.append((us, why))
    roc = {t: abs(s) for t, s in rocof_series(good).points}

    minutes: Dict[int, _Acc] = {}
    fine: List[Tuple[int, float]] = []
    hist: Dict[int, int] = {}
    a: Optional[_Acc] = None
    cur_m = None
    for (t, f, _b), us in zip(good, good_us):
        if not (lo_us <= us < hi_us):
            continue
        m = us // MINUTE_US
        if m != cur_m:
            cur_m = m
            a = minutes.get(m)
            if a is None:
                a = minutes[m] = _Acc()
        d = f - NOMINAL_HZ
        a.n += 1
        a.s1 += d
        a.s2 += d * d
        if a.fmin is None or f < a.fmin:
            a.fmin = f
        if a.fmax is None or f > a.fmax:
            a.fmax = f
        r = roc.get(t)
        if r is not None and (a.rmax is None or r > a.rmax):
            a.rmax = r
        k = int(f * HIST_BINS_PER_HZ + 1e-9)          # same rounding as store.raw_histogram
        hist[k] = hist.get(k, 0) + 1
        if keep_fine:
            fine.append((us, f))
    for us, _why in excluded:
        m = us // MINUTE_US
        e = minutes.get(m)
        if e is None:
            e = minutes[m] = _Acc()
        e.n_ex += 1
    for e in minutes.values():
        e.secs = min(e.n, 60)
        e.secs_ex = min(e.n_ex, 60 - e.secs)

    periods: List[_Period] = []
    for us, why in excluded:
        p = periods[-1] if periods else None
        if p is not None and us - p.end_us <= EXCLUDED_MERGE_GAP_S * US:
            p.end_us = max(p.end_us, us + US)
            p.reasons.add(why)
            p.n += 1
        else:
            periods.append(_Period(us, us + US, {why}, 1))
    return _RawResult(minutes, periods, fine, hist)


def _agg_reasons(a: AggRow, ex: Exclusion) -> Set[str]:
    out = set()
    if any(lo <= a.minute <= hi for lo, hi in ex.bad_minutes):
        out.add(REASON_BOOT)
    if a.amp_mean is not None and a.amp_mean < ex.amp_min:
        out.add(REASON_AMP)
    if (a.freq_min is not None and a.freq_min < ex.freq_lo) or (a.freq_max is not None and a.freq_max > ex.freq_hi):
        out.add(REASON_FREQ)
    return out


def _merge_periods(periods: List[_Period]) -> List[_Period]:
    out: List[_Period] = []
    for p in sorted(periods, key=lambda p: p.start_us):
        q = out[-1] if out else None
        if q is not None and p.start_us - q.end_us <= EXCLUDED_MERGE_GAP_S * US:
            q.end_us = max(q.end_us, p.end_us)
            q.reasons |= p.reasons
            q.n += p.n
        else:
            out.append(_Period(p.start_us, p.end_us, set(p.reasons), p.n))
    return out


class RawDayCache:
    """Per-(unit, UTC hour) results computed from raw readings: the raw path for days the
    retention engine has not aggregated yet (so a long-range view does not re-read up to a day
    of raw readings on every request: a warm request recomputes only the current hour), and the
    frequency histogram of aggregated days whose raw readings are still stored. Bounded; the web
    app holds one."""

    def __init__(self, max_entries: int = RAW_CACHE_MAX, monotonic: Callable[[], float] = time.monotonic):
        self._d: Dict[tuple, Tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._max = max_entries
        self._mono = monotonic

    def lookup(self, key: tuple, ttl_s: float):
        with self._lock:
            hit = self._d.get(key)
        if hit is not None and self._mono() - hit[0] < ttl_s:
            return hit[1]
        return None

    def put(self, key: tuple, val) -> None:
        with self._lock:
            self._d.pop(key, None)
            self._d[key] = (self._mono(), val)
            while len(self._d) > self._max:
                self._d.pop(next(iter(self._d)))


def day_state_signature(states: Dict[int, DayState]) -> tuple:
    """Changes whenever retention aggregates, verifies or prunes a day -- the events after which a
    cached long-range overview is out of date. Read from the database, so it also notices work
    done by ``python -m tremor.retention run`` in a console."""
    return tuple(sorted((d, s.agg_done, s.verified_at is not None, s.pruned_done) for d, s in states.items()))


class OverviewCache:
    """Server-wide cache of whole /api/history/overview responses for ranges of at least
    ``OVERVIEW_CACHE_MIN_SPAN_S`` (7 days), so repeat visitors do not recompute a long view and hold
    up the single web worker. Keyed by unit, ``points`` and from/to in 5-minute slots (a preset's
    "now" moves every request); an entry lives ``OVERVIEW_CACHE_TTL_S`` and is dropped as soon as
    the unit's retention day states change (a new day aggregated, a day pruned)."""

    def __init__(self, ttl_s: float = OVERVIEW_CACHE_TTL_S, max_entries: int = OVERVIEW_CACHE_MAX,
                 monotonic: Callable[[], float] = time.monotonic):
        self._d: Dict[tuple, Tuple[float, tuple, dict]] = {}
        self._lock = threading.Lock()
        self._ttl, self._max, self._mono = ttl_s, max_entries, monotonic

    @staticmethod
    def key(unit_id: str, from_us: int, to_us: int, points: int) -> tuple:
        q = OVERVIEW_CACHE_QUANTUM_S * US
        return (unit_id, from_us // q, to_us // q, points)

    def get(self, key: tuple, signature: tuple) -> Optional[Tuple[dict, float]]:
        """(body, age_s) or None."""
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            age = self._mono() - hit[0]
            if age >= self._ttl or hit[1] != signature:
                del self._d[key]
                return None
            return hit[2], age

    def put(self, key: tuple, signature: tuple, body: dict) -> None:
        with self._lock:
            self._d.pop(key, None)
            self._d[key] = (self._mono(), signature, body)
            while len(self._d) > self._max:
                self._d.pop(next(iter(self._d)))


def _hist_json(h: Dict[int, int]) -> Optional[dict]:
    h = {k: v for k, v in h.items() if v}
    if not h:
        return None
    k0, k1 = min(h), max(h)
    return dict(lo_hz=round(k0 / HIST_BINS_PER_HZ, 3), counts=[h.get(k, 0) for k in range(k0, k1 + 1)],
                n=sum(h.values()))


def _local_midnight(tz: tzinfo, d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


def local_days(tz: tzinfo, lo_s: float, hi_s: float) -> List[Tuple[date, int, int]]:
    """(local date, start, end) in UTC seconds for every local day of ``tz`` touching [lo_s, hi_s).
    A daylight-saving changeover day comes out 23 h or 25 h long."""
    out = []
    d = datetime.fromtimestamp(lo_s, tz).date()
    while True:
        start, end = _local_midnight(tz, d), _local_midnight(tz, d + timedelta(days=1))
        if start >= hi_s:
            return out
        out.append((d, start, end))
        d += timedelta(days=1)


def _align_shift(tz: tzinfo, at_s: float, width: int, g: int) -> int:
    """Offset (s) that makes ``width``-second buckets start on whole local hours / local midnight
    (as of ``at_s``). It is a multiple of ``g``, so the base buckets still nest; 0 for a zone whose
    offset is not whole quarter hours."""
    off = int(datetime.fromtimestamp(at_s, tz).utcoffset().total_seconds())
    shift = off % width
    return shift if shift % g == 0 else 0


def _group(base: Dict[int, _Acc], width: int, shift: int = 0) -> Dict[int, _Acc]:
    out: Dict[int, _Acc] = {}
    for k in sorted(base):
        g = (k - shift) // width * width + shift
        a = out.get(g)
        if a is None:
            a = out[g] = _Acc()
        a.add(base[k])
    return out


def _r(x: Optional[float], nd: int) -> Optional[float]:
    return None if x is None else round(x, nd)


def build_overview(store: ReadingStore, unit_id: str, from_us: int, to_us: int, now_s: float,
                   points: int = DEFAULT_POINTS, cache: Optional[RawDayCache] = None,
                   data_start_us: Optional[int] = None,
                   known_bad: Sequence[KnownBad] = KNOWN_BAD,
                   states: Optional[Dict[int, DayState]] = None,
                   hist_budget_s: Optional[float] = None,
                   tz: tzinfo = timezone.utc, local_days_table: bool = True) -> dict:
    """Everything the history page draws for [from_us, to_us). ``data_start_us`` (the unit's
    first data) clips an earlier ``from_us`` so a "30 days" view of a week-old unit does not
    list weeks of empty days. ``states`` (the unit's retention day states) may be passed in when
    the caller already read them. ``tz`` aligns buckets to its local hours and, with
    ``local_days_table``, cuts the daily table at its local midnights (else at UTC midnights)."""
    t_start = time.perf_counter()
    cache = cache if cache is not None else RawDayCache()
    hist_budget_s = HIST_RAW_BUDGET_S if hist_budget_s is None else hist_budget_s
    now_us = int(now_s * US)
    requested_from = from_us
    if data_start_us is not None and from_us < data_start_us:
        from_us = min(data_start_us, to_us)
    span_s = max(1.0, (to_us - from_us) / US)

    mine = [k for k in known_bad if k.unit_id == unit_id]
    bad_boots = {k.boot_id for k in mine}
    exclusion = Exclusion(EXCLUDE_FREQ_LO_HZ, EXCLUDE_FREQ_HI_HZ, EXCLUDE_AMP_BELOW_V,
                          tuple((k.start_us // MINUTE_US, (k.end_us - 1) // MINUTE_US) for k in mine),
                          tuple(sorted(bad_boots)))

    short = span_s <= RAW_MAX_SPAN_S
    states = store.day_states(unit_id) if states is None else states
    segs: List[Tuple[int, int, int, str, bool]] = []    # (day, from_us, to_us, source, raw still stored)
    for d in range(from_us // DAY_US, (max(from_us, to_us - 1)) // DAY_US + 1):
        a, b = max(from_us, d * DAY_US), min(to_us, (d + 1) * DAY_US)
        if a >= b:
            continue
        st = states.get(d)
        agg_done = st is not None and st.agg_done
        raw_intact = st is None or (st.verified_at is None and st.pruned_rows == 0)
        segs.append((d, a, b, "raw" if (not agg_done or (short and raw_intact)) else "1min", raw_intact))

    fine_path = short and all(s[3] == "raw" for s in segs)
    w_freq = nice_width(span_s / points, 1 if fine_path else 60)
    w_rocof = max(60, w_freq)
    w_cov = nice_width(span_s / MAX_COVERAGE_BARS, 3600)
    # base buckets divide 15 min, so they nest inside local hours and local days of any zone
    # whose offset is whole quarter hours (all of them today), DST or not
    g = math.gcd(math.gcd(w_rocof, w_cov), 900)

    base: Dict[int, _Acc] = {}
    periods: List[_Period] = []
    fine: List[Tuple[int, float]] = []

    def add_minutes(minutes: Dict[int, _Acc], a_us: int, b_us: int) -> None:
        lo, hi = a_us // MINUTE_US, (b_us - 1) // MINUTE_US
        for m, acc in minutes.items():
            if lo <= m <= hi:
                k = (m * 60 // g) * g
                cur = base.get(k)
                if cur is None:
                    base[k] = cur = _Acc()
                cur.add(acc)

    def runs(source: str, raw_stored: Optional[bool] = None) -> List[Tuple[int, int]]:
        """Adjacent segments of one source merged, so a long range costs one query per run
        rather than per day (each query opens a connection; slow on a network filesystem)."""
        out: List[List[int]] = []
        for _d, a, b, src, raw_ok in segs:
            if src != source or (raw_stored is not None and raw_ok != raw_stored):
                continue
            if out and out[-1][1] == a:
                out[-1][1] = b
            else:
                out.append([a, b])
        return [(a, b) for a, b in out]

    def add_periods(ps: Sequence[_Period], a_us: int, b_us: int) -> None:
        periods.extend(_Period(max(p.start_us, a_us), min(p.end_us, b_us), set(p.reasons), p.n)
                       for p in ps if p.end_us > a_us and p.start_us < b_us)

    for a, b in runs("1min"):
        lo_m, hi_m = a // MINUTE_US, (b - 1) // MINUTE_US
        for row in store.rollup_aggregates(unit_id, lo_m, hi_m, g, exclusion):
            acc = _Acc(row[1] or 0, row[2] or 0, row[3] or 0, row[4] or 0, row[5] or 0.0, row[6] or 0.0,
                       row[7], row[8], row[9])
            cur = base.get(row[0])
            if cur is None:
                base[row[0]] = cur = _Acc()
            cur.add(acc)
        for ar in store.excluded_aggregate_minutes(unit_id, lo_m, hi_m, exclusion, 20 * MAX_EXCLUDED_PERIODS):
            periods.append(_Period(ar.minute * MINUTE_US, (ar.minute + 1) * MINUTE_US,
                                   _agg_reasons(ar, exclusion), ar.n))

    hist_raw: Dict[int, int] = {}
    hist_means: Dict[int, int] = {}

    def add_hist(dst: Dict[int, int], src: Dict[int, int]) -> None:
        for k, v in src.items():
            dst[k] = dst.get(k, 0) + v

    margin = ROCOF_MARGIN_S * US
    for a, b in runs("raw"):
        if fine_path:
            res = _raw_minutes(store.read_points(unit_id, a - margin, b), a, b, bad_boots, keep_fine=True)
            add_minutes(res.minutes, a, b)
            periods.extend(res.periods)
            fine.extend(res.fine)
            add_hist(hist_raw, res.hist)
            continue
        hours = range(a // HOUR_US, (b - 1) // HOUR_US + 1)
        results: Dict[int, _RawResult] = {}
        missing: List[int] = []
        for h in hours:
            ttl = RAW_CACHE_TTL_OPEN_S if now_us < (h + 1) * HOUR_US + RAW_HOUR_SETTLE_S * US else RAW_CACHE_TTL_CLOSED_S
            hit = cache.lookup((unit_id, h), ttl)
            if hit is None:
                missing.append(h)
            else:
                results[h] = hit
        i = 0
        while i < len(missing):                 # one read per run of consecutive uncached hours (<= 6 h)
            j = i
            while j + 1 < len(missing) and missing[j + 1] == missing[j] + 1 and j + 1 - i < RAW_READ_CHUNK_H:
                j += 1
            h0, h1 = missing[i], missing[j]
            pts = store.read_points(unit_id, h0 * HOUR_US - margin, (h1 + 1) * HOUR_US)
            keys = [p[0] for p in pts]
            for h in range(h0, h1 + 1):
                lo = bisect_left(keys, h * HOUR_US - margin)
                hi = bisect_left(keys, (h + 1) * HOUR_US)
                results[h] = _raw_minutes(pts[lo:hi], h * HOUR_US, (h + 1) * HOUR_US, bad_boots, keep_fine=False)
                cache.put((unit_id, h), results[h])
            i = j + 1
        for h in hours:
            add_minutes(results[h].minutes, a, b)
            add_periods(results[h].periods, a, b)
            add_hist(hist_raw, results[h].hist)       # whole hours: the range's end hours count in full

    # --- frequency distribution where only aggregates were read: per-reading from the raw rows
    # while they are still stored (SQL, per hour, cached, within a time budget), else the
    # distribution of 1-minute means
    pending_hours = 0
    hist_deadline = time.perf_counter() + hist_budget_s
    for a, b in runs("1min", raw_stored=True):
        missing = []
        for h in range(a // HOUR_US, (b - 1) // HOUR_US + 1):
            hit = cache.lookup(("hist", unit_id, h), HIST_CACHE_TTL_S)
            if hit is None:
                missing.append(h)
            else:
                add_hist(hist_raw, hit)
        # consecutive missing hours in chunks of <= RAW_READ_CHUNK_H, one statement each
        chunks: List[List[int]] = []
        for h in missing:
            if chunks and chunks[-1][-1] == h - 1 and len(chunks[-1]) < RAW_READ_CHUNK_H:
                chunks[-1].append(h)
            else:
                chunks.append([h])
        done = store.raw_histogram(unit_id, [(c[0] * HOUR_US, (c[-1] + 1) * HOUR_US) for c in chunks],
                                   HIST_BINS_PER_HZ, exclusion, deadline=hist_deadline) if chunks else []
        for c, rows in zip(chunks, done):
            per_hour: Dict[int, Dict[int, int]] = {h: {} for h in c}
            for h, k, n in rows:
                per_hour[h][k] = n
            for h in c:
                cache.put(("hist", unit_id, h), per_hour[h])
                add_hist(hist_raw, per_hour[h])
        pending_hours += sum(len(c) for c in chunks[len(done):])
    mean_runs = runs("1min", raw_stored=False)
    for a, b in mean_runs:
        for k, c in store.aggregate_mean_histogram(unit_id, a // MINUTE_US, (b - 1) // MINUTE_US,
                                                   HIST_BINS_PER_HZ, exclusion):
            hist_means[k] = hist_means.get(k, 0) + c

    if data_start_us is not None and requested_from <= data_start_us:
        # starts at the unit's first data: begin at the first bucket that really has some,
        # rather than at data_start_us's safety margin
        ks = [k for k, acc in base.items() if acc.n or acc.n_ex]
        if ks:
            from_us = max(from_us, min(min(ks) * US, to_us))

    # --- frequency: mean with a min-max band
    at_s = min(to_us, now_us) / US
    sh_freq = _align_shift(tz, at_s, w_freq, math.gcd(g, w_freq) if fine_path else g)
    sh_rocof = _align_shift(tz, at_s, w_rocof, g)
    sh_cov = _align_shift(tz, at_s, w_cov, g)
    ft, fmean, fmin, fmax, fn = [], [], [], [], []
    if fine_path:
        buckets: Dict[int, _Acc] = {}
        for us, f in fine:
            k = (us // US - sh_freq) // w_freq * w_freq + sh_freq
            acc = buckets.get(k)
            if acc is None:
                acc = buckets[k] = _Acc()
            d = f - NOMINAL_HZ
            acc.n += 1
            acc.s1 += d
            acc.fmin = _mn(acc.fmin, f)
            acc.fmax = _mx(acc.fmax, f)
        freq_groups = buckets
    else:
        freq_groups = _group(base, w_freq, sh_freq)
    for k in sorted(freq_groups):
        acc = freq_groups[k]
        if acc.n:
            ft.append(k)
            fmean.append(round(acc.mean(), 6))
            fmin.append(_r(acc.fmin, 6))
            fmax.append(_r(acc.fmax, 6))
            fn.append(acc.n)

    # --- RoCoF: max |RoCoF| per bucket (per minute unless the range is long)
    rt, rmax = [], []
    for k, acc in sorted(_group(base, w_rocof, sh_rocof).items()):
        if acc.n and acc.rmax is not None:
            rt.append(k)
            rmax.append(round(acc.rmax, 5))

    # --- coverage and daily table, measured against the part of the range that has happened
    end_us = min(to_us, now_us)
    cov = _group(base, w_cov, sh_cov)
    ct, cgood, cex, cexp = [], [], [], []
    if end_us > from_us:
        k = (from_us // US - sh_cov) // w_cov * w_cov + sh_cov
        while k * US < end_us and len(ct) <= MAX_COVERAGE_BARS + 2:
            exp = (min(end_us, (k + w_cov) * US) - max(from_us, k * US)) / US
            if exp > 0:
                acc = cov.get(k) or _Acc()
                ct.append(k)
                cexp.append(round(exp, 3))
                cgood.append(round(min(100.0, 100.0 * acc.secs / exp), 2))
                cex.append(round(min(100.0, 100.0 * acc.secs_ex / exp), 2))
            k += w_cov

    day_tz = tz if local_days_table else timezone.utc
    daily = []
    if end_us > from_us:
        bounds = local_days(day_tz, from_us / US, end_us / US)
        starts = [st for _d, st, _e in bounds]
        per_day: List[_Acc] = [_Acc() for _ in bounds]
        for k, acc in base.items():
            i = bisect_left(starts, k + 1) - 1          # the day whose start is <= k
            if 0 <= i < len(bounds) and k < bounds[i][2]:
                per_day[i].add(acc)
        for (d, st, en), acc in zip(bounds, per_day):
            exp = (min(end_us, en * US) - max(from_us, st * US)) / US
            if exp <= 0:
                continue
            srcs = sorted({src for _d, a, b, src, _r in segs if a < en * US and b > st * US})
            names = []
            for t in (st, en - 1):
                n = datetime.fromtimestamp(t, day_tz).tzname()
                if n not in names:
                    names.append(n)
            has = acc.n > 0
            daily.append(dict(
                day=d.isoformat(), start_us=st * US, end_us=en * US, length_h=round((en - st) / 3600, 2),
                tz_abbr="/".join(names), source="+".join(srcs) or None, n=acc.n, n_excluded=acc.n_ex,
                mean=round(acc.mean(), 6) if has else None, std=round(acc.std(), 6) if has else None,
                min=_r(acc.fmin, 6), max=_r(acc.fmax, 6), rocof_max_abs=_r(acc.rmax, 5),
                coverage_pct=round(min(100.0, 100.0 * acc.secs / exp), 2),
                excluded_pct=round(min(100.0, 100.0 * acc.secs_ex / exp), 2), expected_s=round(exp, 3)))

    # --- excluded periods (known-bad entries are listed even when their rows are long gone)
    kb = [k for k in mine if k.start_us < to_us and k.end_us > from_us]
    for k in kb:
        periods.append(_Period(k.start_us, k.end_us, {REASON_BOOT}, 0))
    merged = _merge_periods(periods)

    sources = []
    for _d, a, b, src, _raw_ok in segs:
        if b <= from_us:
            continue
        a = max(a, from_us)
        if sources and sources[-1]["source"] == src and sources[-1]["to_us"] == a:
            sources[-1]["to_us"] = b
        else:
            sources.append(dict(from_us=a, to_us=b, source=src))

    return dict(
        unit=unit_id, from_us=from_us, to_us=to_us, requested_from_us=requested_from, now_us=now_us,
        points=points, sources=sources, tz=str(tz) if tz is not timezone.utc else "UTC",
        daily_tz="UTC" if day_tz is timezone.utc else str(day_tz),
        freq=dict(bucket_s=w_freq, source="raw" if fine_path else "1min", t=ft, mean=fmean, min=fmin, max=fmax, n=fn),
        rocof=dict(bucket_s=w_rocof, t=rt, max_abs=rmax),
        coverage=dict(bucket_s=w_cov, t=ct, good_pct=cgood, excluded_pct=cex, expected_s=cexp),
        histogram=dict(bin_hz=HIST_BIN_HZ, raw=_hist_json(hist_raw), minute_means=_hist_json(hist_means),
                       minute_means_ranges=[dict(from_us=a, to_us=b) for a, b in mean_runs],
                       complete=pending_hours == 0, pending_hours=pending_hours,
                       whole_hours=not fine_path),
        daily=daily,
        excluded=dict(rules=exclusion_rules(), truncated=len(merged) > MAX_EXCLUDED_PERIODS,
                      periods=[dict(start_us=p.start_us, end_us=p.end_us, reasons=sorted(p.reasons), n=p.n)
                               for p in merged[:MAX_EXCLUDED_PERIODS]]),
        known_bad=[dict(boot_id=k.boot_id, start_us=k.start_us, end_us=k.end_us, note=k.note) for k in kb],
        elapsed_ms=round((time.perf_counter() - t_start) * 1000, 1),
    )
