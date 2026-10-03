// Time-zone helpers for the history page (also loaded by tests/test_tz_js.py under Node).
//
// Every instant is UTC: seconds since the Unix epoch, exactly as the API sends it, and the charts
// plot those numbers directly -- so a daylight-saving changeover can never make a gap or an
// overlap. Only LABELS and the from/to INPUTS are local, in one IANA zone (Australia/Adelaide for
// TREMOR), always through Intl.DateTimeFormat -- never a fixed +9:30 -- so ACST <-> ACDT switches
// by itself.
//
// "Wall time" below is a local date-time expressed as seconds as if it were UTC
// (Date.UTC(y, m, d, h, mi, s) / 1000): convenient for arithmetic on local calendar values.
//
// Local input -> UTC uses the "compatible" rule (as JavaScript Temporal and RFC 5545 do):
//   * a SKIPPED time (spring forward; e.g. 02:00-02:59 on Sun 4 Oct 2026 in Adelaide) is moved
//     forward by the length of the gap: 02:30 -> 03:30 ACDT;
//   * a REPEATED time (fall back; 02:00-02:59 on Sun 4 Apr 2027 in Adelaide) means the EARLIER of
//     the two instants, i.e. the one still on daylight time: 02:30 -> 02:30 ACDT (16:00 UTC).
(function (root) {
  'use strict';

  const formatters = {};
  function formatter(tz) {
    // en-AU gives the zone's short name ("ACST"/"ACDT") rather than "GMT+9:30"
    return formatters[tz] || (formatters[tz] = new Intl.DateTimeFormat('en-AU', {
      timeZone: tz, hourCycle: 'h23', weekday: 'short', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
    }));
  }

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const p2 = (n) => String(n).padStart(2, '0');

  // Local calendar fields, zone abbreviation and UTC offset (s) of a UTC instant (s).
  function zoneParts(tz, s) {
    const whole = Math.floor(s);
    const o = {};
    for (const p of formatter(tz).formatToParts(new Date(whole * 1000))) o[p.type] = p.value;
    const y = +o.year, mo = +o.month, d = +o.day, h = +o.hour % 24, mi = +o.minute, sec = +o.second;
    const wall = Date.UTC(y, mo - 1, d, h, mi, sec) / 1000;
    return { y, mo, d, h, mi, s: sec, weekday: o.weekday, abbr: o.timeZoneName, wall, offset: wall - whole };
  }

  // Local wall time (s) -> { utc, kind: 'normal' | 'skipped' | 'repeated' } -- see the rule above.
  function wallToUtc(tz, wall) {
    const offs = [...new Set([-86400, 0, 86400].map((dt) => zoneParts(tz, wall + dt).offset))];
    const hits = offs.map((o) => wall - o).filter((u) => zoneParts(tz, u).wall === wall).sort((a, b) => a - b);
    if (hits.length) return { utc: hits[0], kind: hits.length > 1 ? 'repeated' : 'normal' };
    // in the gap: use the offset in force before it (the smaller one -- clocks went forward)
    return { utc: wall - Math.min(...offs), kind: 'skipped' };
  }

  // "2026-10-04T02:30" (an <input type=datetime-local> value, local to tz) -> as wallToUtc.
  function parseLocalInput(tz, value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value || '');
    if (!m) return null;
    return wallToUtc(tz, Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) / 1000);
  }

  function toLocalInput(tz, s) {
    const z = zoneParts(tz, s);
    return `${z.y}-${p2(z.mo)}-${p2(z.d)}T${p2(z.h)}:${p2(z.mi)}`;
  }

  function fmtLocal(tz, s, seconds = true) {        // "Sat 26 Sep 2026, 16:20:32 ACST"
    const z = zoneParts(tz, s);
    return `${z.weekday} ${z.d} ${MONTHS[z.mo - 1]} ${z.y}, ${p2(z.h)}:${p2(z.mi)}` +
      (seconds ? `:${p2(z.s)}` : '') + ` ${z.abbr}`;
  }

  function fmtUtc(s, seconds = true) {              // "2026-09-26 06:50:32 UTC"
    const d = new Date(Math.floor(s) * 1000);
    return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())} ` +
      `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}` + (seconds ? `:${p2(d.getUTCSeconds())}` : '') + ' UTC';
  }

  // Axis ticks on round LOCAL times (whole hours, local midnight, ...), as UTC instants. Stepping is
  // done in wall time, so days stay on local midnight across a changeover (23 h / 25 h apart).
  const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  function ticks(tz, min, max, target) {
    const step = TICK_STEPS.find((st) => (max - min) / st <= target) ||
      604800 * Math.ceil((max - min) / 604800 / target);
    const out = [];
    let w = Math.ceil(zoneParts(tz, min).wall / step) * step;
    for (let i = 0; i < 2000; i++, w += step) {
      const u = wallToUtc(tz, w).utc;
      if (u > max) break;
      if (u >= min && (!out.length || u > out[out.length - 1])) out.push(u);
    }
    return { step, ticks: out };
  }

  function tickLabel(tz, s, step, span) {
    const z = zoneParts(tz, s);
    const date = `${z.d} ${MONTHS[z.mo - 1]}`;
    if (step >= 86400) return date;
    const hm = `${p2(z.h)}:${p2(z.mi)}`;
    return span <= 86400 && z.h !== 0 ? hm : [date, hm];
  }

  // The zone abbreviation(s) in force over [min, max]: "ACST", or "ACST → ACDT" across a change.
  function abbrRange(tz, min, max) {
    const a = zoneParts(tz, min).abbr, b = zoneParts(tz, max).abbr;
    return a === b ? a : `${a} → ${b}`;
  }

  const api = { zoneParts, wallToUtc, parseLocalInput, toLocalInput, fmtLocal, fmtUtc, ticks, tickLabel, abbrRange };
  root.TremorTZ = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
