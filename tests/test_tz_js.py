"""The history page's time-zone helpers (src/tremor/static/tz.js), run under Node: Adelaide display
with ACST/ACDT, local input -> UTC across both 2026-27 changeovers, and axis ticks. Skipped where
Node is not installed (e.g. on PythonAnywhere; the page itself runs in the browser)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TZ_JS = Path(__file__).resolve().parents[1] / "src" / "tremor" / "static" / "tz.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def js(expr: str):
    """Evaluate ``expr`` with T = tz.js and Z = "Australia/Adelaide"; returns its JSON value."""
    script = f"const T = require({json.dumps(str(TZ_JS))}); const Z = 'Australia/Adelaide';\n" \
             f"process.stdout.write(JSON.stringify(({expr})));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def utc_s(y, mo, d, h=0, mi=0, s=0) -> int:
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def test_display_is_adelaide_local_with_the_zone_abbreviation_and_utc():
    acst = utc_s(2026, 9, 26, 6, 50, 32)
    acdt = utc_s(2026, 10, 10, 6, 50, 32)
    assert js(f"T.fmtLocal(Z, {acst})") == "Sat 26 Sep 2026, 16:20:32 ACST"
    assert js(f"T.fmtLocal(Z, {acdt})") == "Sat 10 Oct 2026, 17:20:32 ACDT"
    assert js(f"T.fmtUtc({acst})") == "2026-09-26 06:50:32 UTC"
    assert js(f"T.fmtUtc({acdt})") == "2026-10-10 06:50:32 UTC"


def test_the_offset_comes_from_the_zone_not_a_fixed_number():
    # the instant clocks go forward (2026-10-03 16:30 UTC = 03:00 ACDT) and back (2027-04-03 16:30 UTC)
    got = js(f"[{utc_s(2026, 10, 3, 16, 29)}, {utc_s(2026, 10, 3, 16, 30)}, {utc_s(2027, 4, 3, 16, 29)}, "
             f"{utc_s(2027, 4, 3, 16, 30)}].map(s => [T.zoneParts(Z, s).offset, T.zoneParts(Z, s).abbr])")
    assert got == [[34200, "ACST"], [37800, "ACDT"], [37800, "ACDT"], [34200, "ACST"]]


@pytest.mark.parametrize("local, utc, kind, shown", [
    # spring forward, Sun 4 Oct 2026: 02:00-02:59 does not exist -> moved on by the gap
    ("2026-10-04T01:59", (2026, 10, 3, 16, 29), "normal", "Sun 4 Oct 2026, 01:59:00 ACST"),
    ("2026-10-04T02:00", (2026, 10, 3, 16, 30), "skipped", "Sun 4 Oct 2026, 03:00:00 ACDT"),
    ("2026-10-04T02:30", (2026, 10, 3, 17, 0), "skipped", "Sun 4 Oct 2026, 03:30:00 ACDT"),
    ("2026-10-04T03:00", (2026, 10, 3, 16, 30), "normal", "Sun 4 Oct 2026, 03:00:00 ACDT"),
    # fall back, Sun 4 Apr 2027: 02:00-02:59 happens twice -> the first (daylight time) one
    ("2027-04-04T01:59", (2027, 4, 3, 15, 29), "normal", "Sun 4 Apr 2027, 01:59:00 ACDT"),
    ("2027-04-04T02:30", (2027, 4, 3, 16, 0), "repeated", "Sun 4 Apr 2027, 02:30:00 ACDT"),
    ("2027-04-04T03:00", (2027, 4, 3, 17, 30), "normal", "Sun 4 Apr 2027, 03:00:00 ACST"),
    # and ordinary times either side
    ("2026-09-26T16:20", (2026, 9, 26, 6, 50), "normal", "Sat 26 Sep 2026, 16:20:00 ACST"),
    ("2026-10-10T17:20", (2026, 10, 10, 6, 50), "normal", "Sat 10 Oct 2026, 17:20:00 ACDT"),
])
def test_local_input_converts_to_utc_across_both_changeovers(local, utc, kind, shown):
    got = js(f"(() => {{ const r = T.parseLocalInput(Z, {json.dumps(local)}); "
             f"return [r.utc, r.kind, T.fmtLocal(Z, r.utc)]; }})()")
    assert got == [utc_s(*utc), kind, shown]


def test_a_preset_range_round_trips_through_the_inputs():
    s = utc_s(2026, 10, 3, 17, 5)
    assert js(f"T.toLocalInput(Z, {s})") == "2026-10-04T03:35"
    assert js(f"T.parseLocalInput(Z, T.toLocalInput(Z, {s})).utc") == s


def test_hourly_ticks_skip_the_missing_hour_and_never_repeat():
    got = js(f"T.ticks(Z, {utc_s(2026, 10, 3, 13, 0)}, {utc_s(2026, 10, 3, 19, 0)}, 10)"
             ".ticks.map(s => T.fmtLocal(Z, s, false).split(', ')[1])")
    # 13:00-19:00 UTC = 22:30 ACST .. 05:30 ACDT; 02:00 local does not exist that night
    assert got == ["23:00 ACST", "00:00 ACST", "01:00 ACST", "03:00 ACDT", "04:00 ACDT", "05:00 ACDT"]
    back = js(f"T.ticks(Z, {utc_s(2027, 4, 3, 13, 0)}, {utc_s(2027, 4, 3, 19, 0)}, 10)"
              ".ticks.map(s => T.fmtLocal(Z, s, false).split(', ')[1])")
    assert back == ["00:00 ACDT", "01:00 ACDT", "02:00 ACDT", "03:00 ACST", "04:00 ACST"]


def test_daily_ticks_stay_on_local_midnight_and_changeover_days_are_23_and_25_hours():
    oct_ = js(f"T.ticks(Z, {utc_s(2026, 10, 1)}, {utc_s(2026, 10, 7)}, 8).ticks")
    assert js(f"[{', '.join(map(str, oct_))}].every(s => T.zoneParts(Z, s).h === 0 && T.zoneParts(Z, s).mi === 0)")
    gaps = [b - a for a, b in zip(oct_, oct_[1:])]
    assert 23 * 3600 in gaps and set(gaps) <= {23 * 3600, 24 * 3600}
    apr = js(f"T.ticks(Z, {utc_s(2027, 4, 1)}, {utc_s(2027, 4, 7)}, 8).ticks")
    gaps = [b - a for a, b in zip(apr, apr[1:])]
    assert 25 * 3600 in gaps and set(gaps) <= {24 * 3600, 25 * 3600}


def test_the_axis_names_the_zone_abbreviations_in_view():
    assert js(f"T.abbrRange(Z, {utc_s(2026, 9, 26)}, {utc_s(2026, 9, 28)})") == "ACST"
    assert js(f"T.abbrRange(Z, {utc_s(2026, 10, 3)}, {utc_s(2026, 10, 5)})") == "ACST → ACDT"
    assert js(f"T.abbrRange(Z, {utc_s(2027, 4, 3)}, {utc_s(2027, 4, 5)})") == "ACDT → ACST"
