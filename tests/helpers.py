"""Shared builders for the server-persistence tests: a frozen clock and payloads
shaped like real Unit 1 traffic (v1: float32 seconds-of-day; v2: boot_id + seq +
integer GPS triple)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

US = 1_000_000

# 2026-09-25 02:00:00 UTC == 11:30 ACST. A fixed, realistic instant: every test
# that needs "now" uses this rather than the real clock.
T0 = datetime(2026, 9, 25, 2, 0, 0, tzinfo=timezone.utc).timestamp()


def utc(y, mo, d, h=0, mi=0, s=0.0) -> float:
    return datetime(y, mo, d, h, mi, 0, tzinfo=timezone.utc).timestamp() + s


class FakeClock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def float32_text(x: float) -> float:
    """What the device really sends for a float32 value: its shortest
    round-trip decimal text, parsed back to a double by the server."""
    return float(str(np.float32(x)))


def v1_reading(freq: float, unix_s: float | None, amp: float | None = 0.744) -> dict:
    """Legacy reading: gps_utc_s = UTC seconds-of-day as float32 text, or null
    (unlocked) when unix_s is None."""
    sod = None if unix_s is None else float32_text(unix_s % 86400)
    return {"frequency_hz": freq, "amplitude_v": amp, "gps_utc_s": sod}


def v2_gps(unix_us: int) -> list:
    """The device's integer (days_since_1970, second_of_day, microsecond)."""
    days, rem = divmod(unix_us, 86400 * US)
    sec, us = divmod(rem, US)
    return [days, sec, us]


def v2_reading(freq: float, seq: int, unix_us: int | None, amp: float | None = 0.744,
               with_legacy_float: bool = False) -> dict:
    r = {"seq": seq, "frequency_hz": freq, "amplitude_v": amp}
    if unix_us is not None:
        r["gps"] = v2_gps(unix_us)
        if with_legacy_float:
            r["gps_utc_s"] = float32_text((unix_us / US) % 86400)
    else:
        r["gps_utc_s"] = None
    return r


def v1_batch(unit: str, readings: list) -> dict:
    return {"unit_id": unit, "readings": readings}


def v2_batch(unit: str, boot: str, readings: list) -> dict:
    return {"unit_id": unit, "boot_id": boot, "readings": readings}


def v2_series(unit: str, boot: str, start_unix: float, n: int, seq0: int = 0, step: float = 1.0,
              freq=lambda i: 50.0, amp: float | None = 0.744) -> dict:
    """n readings, `step` seconds apart, starting at start_unix (exact integer µs)."""
    base = int(round(start_unix * US))
    return v2_batch(unit, boot, [
        v2_reading(freq(i), seq0 + i, base + int(round(i * step * US)), amp) for i in range(n)])


def v1_series(unit: str, start_unix: float, n: int, step: float = 1.0,
              freq=lambda i: 50.0, amp: float | None = 0.744) -> dict:
    return v1_batch(unit, [v1_reading(freq(i), start_unix + i * step, amp) for i in range(n)])
