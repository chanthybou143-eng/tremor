"""History overview for the /history page: everything stored, summarised for a browser.

One request returns all the page draws for a unit and a time range, already downsampled
server-side, so the payload stays small however long the range is:

  * frequency: mean with a min-max band per bucket (at most ``points`` buckets)
  * RoCoF: max |RoCoF| per minute (or per bucket, once a bucket is longer than a minute)
  * coverage per hour (coarser for very long ranges): good / excluded / missing
  * a daily table per UTC day: mean, std, min, max, % coverage

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
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .retention import day_to_date
from .store import DAY_US, MINUTE_US, US, AggExclusion, AggRow, ReadingStore
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
RAW_CACHE_MAX = 96                        # hours; two not-yet-aggregated days fit with room to spare


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
    return _RawResult(minutes, periods, fine)


def _agg_reasons(a: AggRow, ex: AggExclusion) -> Set[str]:
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
    """Per-(unit, UTC hour) results of the raw path for days the retention engine has not
    aggregated yet, so a long-range view does not re-read up to a day of raw readings on every
    request: a warm request recomputes only the current hour. Bounded; the web app holds one."""

    def __init__(self, max_entries: int = RAW_CACHE_MAX, monotonic: Callable[[], float] = time.monotonic):
        self._d: Dict[Tuple[str, int], Tuple[float, _RawResult]] = {}
        self._lock = threading.Lock()
        self._max = max_entries
        self._mono = monotonic

    def lookup(self, key: Tuple[str, int], ttl_s: float) -> Optional[_RawResult]:
        with self._lock:
            hit = self._d.get(key)
        if hit is not None and self._mono() - hit[0] < ttl_s:
            return hit[1]
        return None

    def put(self, key: Tuple[str, int], val: _RawResult) -> None:
        with self._lock:
            self._d.pop(key, None)
            self._d[key] = (self._mono(), val)
            while len(self._d) > self._max:
                self._d.pop(next(iter(self._d)))


def _group(base: Dict[int, _Acc], width: int) -> Dict[int, _Acc]:
    out: Dict[int, _Acc] = {}
    for k in sorted(base):
        g = (k // width) * width
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
                   known_bad: Sequence[KnownBad] = KNOWN_BAD) -> dict:
    """Everything the history page draws for [from_us, to_us). ``data_start_us`` (the unit's
    first data) clips an earlier ``from_us`` so a "30 days" view of a week-old unit does not
    list weeks of empty days."""
    t_start = time.perf_counter()
    cache = cache if cache is not None else RawDayCache()
    now_us = int(now_s * US)
    requested_from = from_us
    if data_start_us is not None and from_us < data_start_us:
        from_us = min(data_start_us, to_us)
    span_s = max(1.0, (to_us - from_us) / US)

    mine = [k for k in known_bad if k.unit_id == unit_id]
    bad_boots = {k.boot_id for k in mine}
    exclusion = AggExclusion(EXCLUDE_FREQ_LO_HZ, EXCLUDE_FREQ_HI_HZ, EXCLUDE_AMP_BELOW_V,
                             tuple((k.start_us // MINUTE_US, (k.end_us - 1) // MINUTE_US) for k in mine))

    short = span_s <= RAW_MAX_SPAN_S
    states = store.day_states(unit_id)
    segs: List[Tuple[int, int, int, str]] = []          # (day, from_us, to_us, source)
    for d in range(from_us // DAY_US, (max(from_us, to_us - 1)) // DAY_US + 1):
        a, b = max(from_us, d * DAY_US), min(to_us, (d + 1) * DAY_US)
        if a >= b:
            continue
        st = states.get(d)
        agg_done = st is not None and st.agg_done
        raw_intact = st is None or (st.verified_at is None and st.pruned_rows == 0)
        segs.append((d, a, b, "raw" if (not agg_done or (short and raw_intact)) else "1min"))

    fine_path = short and all(s[3] == "raw" for s in segs)
    w_freq = nice_width(span_s / points, 1 if fine_path else 60)
    w_rocof = max(60, w_freq)
    w_cov = nice_width(span_s / MAX_COVERAGE_BARS, 3600)
    g = math.gcd(math.gcd(w_rocof, w_cov), 86400)

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

    def runs(source: str) -> List[Tuple[int, int]]:
        """Adjacent segments of one source merged, so a long range costs one query per run
        rather than per day (each query opens a connection; slow on a network filesystem)."""
        out: List[List[int]] = []
        for _d, a, b, src in segs:
            if src != source:
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

    margin = ROCOF_MARGIN_S * US
    for a, b in runs("raw"):
        if fine_path:
            res = _raw_minutes(store.read_points(unit_id, a - margin, b), a, b, bad_boots, keep_fine=True)
            add_minutes(res.minutes, a, b)
            periods.extend(res.periods)
            fine.extend(res.fine)
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
        while i < len(missing):                 # one read per run of consecutive uncached hours
            j = i
            while j + 1 < len(missing) and missing[j + 1] == missing[j] + 1:
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

    if data_start_us is not None and requested_from <= data_start_us:
        # starts at the unit's first data: begin at the first bucket that really has some,
        # rather than at data_start_us's safety margin
        ks = [k for k, acc in base.items() if acc.n or acc.n_ex]
        if ks:
            from_us = max(from_us, min(min(ks) * US, to_us))

    # --- frequency: mean with a min-max band
    ft, fmean, fmin, fmax, fn = [], [], [], [], []
    if fine_path:
        buckets: Dict[int, _Acc] = {}
        for us, f in fine:
            k = (us // US // w_freq) * w_freq
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
        freq_groups = _group(base, w_freq)
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
    for k, acc in sorted(_group(base, w_rocof).items()):
        if acc.n and acc.rmax is not None:
            rt.append(k)
            rmax.append(round(acc.rmax, 5))

    # --- coverage and daily table, measured against the part of the range that has happened
    end_us = min(to_us, now_us)
    cov = _group(base, w_cov)
    ct, cgood, cex, cexp = [], [], [], []
    if end_us > from_us:
        k = (from_us // US // w_cov) * w_cov
        while k * US < end_us and len(ct) <= MAX_COVERAGE_BARS + 2:
            exp = (min(end_us, (k + w_cov) * US) - max(from_us, k * US)) / US
            if exp > 0:
                acc = cov.get(k) or _Acc()
                ct.append(k)
                cexp.append(round(exp, 3))
                cgood.append(round(min(100.0, 100.0 * acc.secs / exp), 2))
                cex.append(round(min(100.0, 100.0 * acc.secs_ex / exp), 2))
            k += w_cov

    seg_src = {d: src for d, _a, _b, src in segs}
    days = _group(base, 86400)
    daily = []
    if end_us > from_us:
        for d in range(from_us // DAY_US, (end_us - 1) // DAY_US + 1):
            exp = (min(end_us, (d + 1) * DAY_US) - max(from_us, d * DAY_US)) / US
            if exp <= 0:
                continue
            acc = days.get(d * 86400) or _Acc()
            has = acc.n > 0
            daily.append(dict(
                day=day_to_date(d).isoformat(), source=seg_src.get(d), n=acc.n, n_excluded=acc.n_ex,
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
    for _d, a, b, src in segs:
        if b <= from_us:
            continue
        a = max(a, from_us)
        if sources and sources[-1]["source"] == src and sources[-1]["to_us"] == a:
            sources[-1]["to_us"] = b
        else:
            sources.append(dict(from_us=a, to_us=b, source=src))

    return dict(
        unit=unit_id, from_us=from_us, to_us=to_us, requested_from_us=requested_from, now_us=now_us,
        points=points, sources=sources,
        freq=dict(bucket_s=w_freq, source="raw" if fine_path else "1min", t=ft, mean=fmean, min=fmin, max=fmax, n=fn),
        rocof=dict(bucket_s=w_rocof, t=rt, max_abs=rmax),
        coverage=dict(bucket_s=w_cov, t=ct, good_pct=cgood, excluded_pct=cex, expected_s=cexp),
        daily=daily,
        excluded=dict(rules=exclusion_rules(), truncated=len(merged) > MAX_EXCLUDED_PERIODS,
                      periods=[dict(start_us=p.start_us, end_us=p.end_us, reasons=sorted(p.reasons), n=p.n)
                               for p in merged[:MAX_EXCLUDED_PERIODS]]),
        known_bad=[dict(boot_id=k.boot_id, start_us=k.start_us, end_us=k.end_us, note=k.note) for k in kb],
        elapsed_ms=round((time.perf_counter() - t_start) * 1000, 1),
    )
