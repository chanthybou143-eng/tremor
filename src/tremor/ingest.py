"""Server-side parsing of /api/ingest payloads and resolution of each
reading's GPS time to an exact, absolute UTC instant.

Two payload generations are accepted (see ``parse_payload``):

* **v1 (legacy, what Unit 1 sends today):** ``{"unit_id", "readings": [
  {"frequency_hz", "amplitude_v", "gps_utc_s"}]}``. ``gps_utc_s`` is UTC
  *seconds-of-day* only, as a float32 shortest-round-trip value (~4-8 ms
  resolution), or null before GPS lock. There is no date, so the server has to
  supply one: it picks the calendar day that puts the reading nearest the
  batch's receipt time (see ``reconstruct_v1_us``). Receipt time is used *only*
  to choose which day the device's own seconds-of-day belongs to -- never as the
  reading's time -- and a result that isn't plausibly recent is stored with
  ``gps_utc_us`` NULL and flagged, not silently replaced.
* **v2:** adds a batch-level ``boot_id`` and a per-reading ``seq`` (so the
  server can drop retried duplicates exactly) and a per-reading integer
  ``gps: [days_since_1970, second_of_day, microsecond]`` built on the device
  with integer-only arithmetic from the RMC date and the PPS anchor -- full
  microsecond resolution and the device's own date. ``gps_utc_s`` may still be
  present alongside it during the transition (an older server can then keep
  working); when ``gps`` is present it wins.

Readings without any GPS time (before the first PPS/RMC anchor, or after the
anchor has gone stale) are *unlocked*: stored, flagged, never given a time.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

US = 1_000_000
SECONDS_PER_DAY = 86400

MAX_READINGS_PER_BATCH = 1000
# A reading can't be from the future (beyond a small clock-skew allowance) and
# a retry buffer holds ~10 minutes; anything further than this from the batch's
# receipt time is treated as a bad timestamp, not trusted.
MAX_AGE_S = 3600.0
FUTURE_SKEW_S = 5.0
# days since 1970-01-01: 2001-09-09 .. 2149-06-06 -- sanity range for v2 dates.
DAY_MIN, DAY_MAX = 11_500, 60_000

UNIT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
BOOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

FLAG_UNLOCKED = 1            # no GPS time in the payload
FLAG_TIME_IMPLAUSIBLE = 2    # GPS time present but not plausible; kept raw, gps_utc_us NULL
SRC_NONE, SRC_V1, SRC_V2 = 0, 1, 2


class PayloadError(ValueError):
    """The payload is malformed -- the HTTP layer maps this to a 400."""


@dataclass(frozen=True)
class ParsedReading:
    seq: Optional[int]                        # v2 only
    freq_hz: float
    amplitude_v: Optional[float]
    sod: Optional[float]                      # v1 gps_utc_s exactly as sent
    gps: Optional[Tuple[int, int, int]]       # v2 (days, second_of_day, microsecond)


@dataclass(frozen=True)
class ParsedBatch:
    unit_id: str
    boot_id: Optional[str]                    # None => legacy v1 batch
    readings: Tuple[ParsedReading, ...]

    @property
    def mode(self) -> int:
        return 2 if self.boot_id is not None else 1


@dataclass(frozen=True)
class ResolvedTime:
    gps_utc_us: Optional[int]                 # exact microseconds since the Unix epoch, or None
    gps_raw: Optional[float]                  # what the device claimed (audit): v1 seconds-of-day, v2 unix seconds
    time_src: int
    flags: int
    gps_locked: int


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _finite_float(v, what: str) -> float:
    if isinstance(v, bool):
        raise PayloadError(f"{what} must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise PayloadError(f"{what} must be a number") from None
    if not math.isfinite(f):
        raise PayloadError(f"{what} must be finite")
    return f


def parse_payload(payload) -> ParsedBatch:
    if not isinstance(payload, dict):
        raise PayloadError("expected a JSON object")

    unit_id = payload.get("unit_id")
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise PayloadError(f"invalid unit_id {unit_id!r}")
    if not UNIT_ID_RE.match(unit_id):
        raise PayloadError("unit_id must be 1-64 characters from A-Z a-z 0-9 _ . -")

    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        raise PayloadError("'readings' must be a non-empty list")
    if len(readings) > MAX_READINGS_PER_BATCH:
        raise PayloadError(f"at most {MAX_READINGS_PER_BATCH} readings per batch")

    boot_id = payload.get("boot_id")
    if boot_id is not None and (not isinstance(boot_id, str) or not BOOT_ID_RE.match(boot_id)):
        raise PayloadError("boot_id must be 1-64 characters from A-Z a-z 0-9 _ -")
    v2 = boot_id is not None

    out: List[ParsedReading] = []
    for r in readings:
        if not isinstance(r, dict):
            raise PayloadError("each reading must be an object")
        if "frequency_hz" not in r:
            raise PayloadError("each reading needs a numeric frequency_hz")
        freq = _finite_float(r["frequency_hz"], "frequency_hz")
        amp = None if r.get("amplitude_v") is None else _finite_float(r["amplitude_v"], "amplitude_v")
        sod = None if r.get("gps_utc_s") is None else _finite_float(r["gps_utc_s"], "gps_utc_s")

        seq = r.get("seq")
        if v2:
            if not _is_int(seq) or seq < 0 or seq >= 2**63:
                raise PayloadError("a batch with boot_id needs an integer seq >= 0 on every reading")
        elif seq is not None:
            raise PayloadError("seq requires a batch-level boot_id")

        gps = r.get("gps")
        if gps is not None:
            if not v2:
                raise PayloadError("gps requires a batch-level boot_id")
            if not (isinstance(gps, (list, tuple)) and len(gps) == 3 and all(_is_int(x) for x in gps)):
                raise PayloadError("gps must be [days, second_of_day, microsecond] integers")
            gps = (gps[0], gps[1], gps[2])
        out.append(ParsedReading(seq=seq if v2 else None, freq_hz=freq, amplitude_v=amp, sod=sod, gps=gps))
    return ParsedBatch(unit_id=unit_id, boot_id=boot_id, readings=tuple(out))


def reconstruct_v1_us(sod: float, received_at: float) -> Optional[int]:
    """Absolute UTC microseconds for a v1 seconds-of-day value: the calendar
    day (previous, current or next UTC day of ``received_at``) that lands
    nearest ``received_at``. Returns None if ``sod`` isn't a valid time of day."""
    if not (0.0 <= sod < SECONDS_PER_DAY + 1):      # +1: a leap second reads 86400.x
        return None
    base = math.floor(received_at / SECONDS_PER_DAY)
    best_t, best_diff = None, None
    for d in (base - 1, base, base + 1):
        t = d * SECONDS_PER_DAY + sod
        diff = abs(t - received_at)
        if best_diff is None or diff < best_diff:
            best_t, best_diff = t, diff
    return int(round(best_t * US))


def _plausible(us: int, received_at: float) -> bool:
    lo = (received_at - MAX_AGE_S) * US
    hi = (received_at + FUTURE_SKEW_S) * US
    return lo <= us <= hi


def resolve_time(r: ParsedReading, received_at: float) -> ResolvedTime:
    """Never substitutes receipt time for a reading's time: a reading is either
    given its own GPS time, or stored with ``gps_utc_us`` NULL and a flag."""
    if r.gps is not None:
        days, sec, usec = r.gps
        if not (DAY_MIN <= days <= DAY_MAX and 0 <= sec < SECONDS_PER_DAY and 0 <= usec < US):
            return ResolvedTime(None, None, SRC_V2, FLAG_TIME_IMPLAUSIBLE, 1)
        us = (days * SECONDS_PER_DAY + sec) * US + usec
        if not _plausible(us, received_at):
            return ResolvedTime(None, us / US, SRC_V2, FLAG_TIME_IMPLAUSIBLE, 1)
        return ResolvedTime(us, us / US, SRC_V2, 0, 1)
    if r.sod is not None:
        us = reconstruct_v1_us(r.sod, received_at)
        if us is None or not _plausible(us, received_at):
            return ResolvedTime(None, r.sod, SRC_V1, FLAG_TIME_IMPLAUSIBLE, 1)
        return ResolvedTime(us, r.sod, SRC_V1, 0, 1)
    return ResolvedTime(None, None, SRC_NONE, FLAG_UNLOCKED, 0)
