"""Raw-data retention: export, 1-minute aggregates, event protection, pruning.

The database would fill PythonAnywhere's free disk in about a month per unit
(12 MB/day/unit), so raw rows older than ``raw_days`` (default 14) are pruned
automatically -- but only after they are safely preserved elsewhere:

  1. EXPORT   a closed UTC day is written to a gzip CSV (multi-member, appended
              chunk by chunk, resumable) and read back to verify its row count.
  2. AGGREGATE 1-minute rows are computed: mean/min/max/std of frequency, the
              maximum |RoCoF|, and the locked/unlocked counts. (Averages alone
              would erase disturbances, hence min/max/std/max|RoCoF|.)
  3. EVENTS   raw rows within +/-``event_margin_s`` of any |RoCoF| >
              ``rocof_event_hz_s`` or frequency outside [``freq_lo``,
              ``freq_hi``] are recorded as ``events`` intervals; pruning never
              deletes rows inside an event interval, so those stay in the
              database permanently.
  4. VERIFY   before the first delete: exported rows == rows in the database ==
              aggregate counts, and the export file still exists with the same
              SHA-256. Any mismatch marks the day ``attention`` and it is NEVER
              pruned (surfaced on /api/health).
  5. PRUNE    delete in small chunks; an invariant (remaining + deleted ==
              exported) is re-checked every chunk.

Nothing here needs a scheduler: ``RetentionEngine.maybe_step()`` is called after
ingest responses and does at most a few hundred milliseconds of chunked work per
call (PythonAnywhere free accounts created after 2026-01-15 have no scheduled
tasks). ``python -m tremor.retention run`` does the same from a console.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

from .store import DAY_US, US, AggRow, DayState, ReadingStore, Row, StoreError, open_store
from .timeline import rocof_series

log = logging.getLogger("tremor.retention")

EXPORT_HEADER = ["unit_id", "boot_id", "seq", "gps_utc_us", "gps_raw", "time_src", "freq_hz",
                 "amplitude_v", "gps_locked", "flags", "received_at"]


@dataclass
class RetentionConfig:
    export_dir: str
    raw_days: int = 14
    settle_s: float = 3600.0            # a UTC day is processed this long after it ends
    rocof_event_hz_s: float = 0.1
    freq_lo: float = 49.85
    freq_hi: float = 50.15
    event_margin_s: float = 300.0
    export_chunk_rows: int = 5000
    prune_chunk_rows: int = 2000
    ingests_keep_days: int = 30
    step_budget_s: float = 0.25
    min_interval_s: float = 20.0
    max_rocof_gap_s: float = 1.5
    rocof_plausibility_hz_s: float = 5.0

    @classmethod
    def from_env(cls, export_dir: str) -> "RetentionConfig":
        def f(name, default, cast=float):
            v = os.environ.get(name)
            return default if v in (None, "") else cast(v)
        return cls(
            export_dir=os.environ.get("TREMOR_EXPORT_DIR") or export_dir,
            raw_days=f("TREMOR_RAW_DAYS", 14, int),
            rocof_event_hz_s=f("TREMOR_EVENT_ROCOF_HZ_S", 0.1),
            freq_lo=f("TREMOR_EVENT_FREQ_LO", 49.85),
            freq_hi=f("TREMOR_EVENT_FREQ_HI", 50.15),
            event_margin_s=f("TREMOR_EVENT_MARGIN_S", 300.0),
        )


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def day_to_date(day: int) -> date:
    return date(1970, 1, 1) + timedelta(days=day)


class RetentionEngine:
    def __init__(self, store: ReadingStore, cfg: RetentionConfig, clock=time.time):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.Lock()
        self._last_run = 0.0
        self._last_ingest_prune = 0.0

    # -- paths ---------------------------------------------------------------
    def export_path(self, unit_id: str, day: int) -> str:
        u = _safe(unit_id)
        return os.path.join(self.cfg.export_dir, u, f"readings_{u}_{day_to_date(day).isoformat()}.csv.gz")

    # -- scheduling ----------------------------------------------------------
    def maybe_step(self) -> Optional[dict]:
        """Rate-limited, single-flight, exception-proof: safe to call from the
        request path after the response has been sent."""
        now = self.clock()
        if now - self._last_run < self.cfg.min_interval_s:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            self._last_run = now
            return self.run(budget_s=self.cfg.step_budget_s)
        except Exception:                   # never let maintenance affect ingest
            log.exception("retention step failed")
            return None
        finally:
            self._lock.release()

    def run(self, budget_s: float = 0.25, max_steps: int = 10_000) -> dict:
        t0 = time.monotonic()
        done: List[str] = []
        for _ in range(max_steps):
            what = self.step()
            if what is None:
                return {"idle": True, "work": done}
            done.append(what)
            if time.monotonic() - t0 >= budget_s:
                break
        return {"idle": False, "work": done}

    def run_until_idle(self, max_steps: int = 1_000_000) -> List[str]:
        out: List[str] = []
        for _ in range(max_steps):
            what = self.step()
            if what is None:
                return out
            out.append(what)
        return out

    # -- one unit of work ----------------------------------------------------
    def step(self) -> Optional[str]:
        cfg, now = self.cfg, self.clock()
        last_closed = int((now - cfg.settle_s) // 86400) - 1
        prune_cutoff = int((now - cfg.raw_days * 86400) // 86400) - 1
        for u in self.store.unit_states():
            oldest = self.store.oldest_day(u.unit_id)
            states = self.store.day_states(u.unit_id)
            # Start from the oldest day that still has raw rows OR an unfinished
            # state -- a day whose rows are all deleted no longer shows up in
            # oldest_day(), but it must still be allowed to reach pruned_done.
            starts = [d for d, s_ in states.items() if not (s_.pruned_done or s_.attention)]
            if oldest is not None:
                starts.append(oldest)
            days = range(min(starts), last_closed + 1) if starts else range(0)
            for d in days:
                st = states.get(d)
                if st is None:
                    locked, unlocked = self.store.day_row_counts(u.unit_id, d)
                    if locked + unlocked == 0:
                        # empty closed day: record it once so later steps skip it without querying
                        self.store.save_day_state(DayState(
                            u.unit_id, d, export_done=True, agg_done=True, pruned_done=True))
                        continue
                    st = DayState(u.unit_id, d)
                if st.attention or st.pruned_done:
                    continue
                if not st.export_done:
                    return self._export_chunk(st)
                if not st.agg_done:
                    return self._aggregate_hour(st)
                if d <= prune_cutoff:
                    return self._prune_chunk(st)
        if now - self._last_ingest_prune > 3600:
            self._last_ingest_prune = now
            n = self.store.prune_ingests(now - cfg.ingests_keep_days * 86400)
            if n:
                return f"prune_ingests:{n}"
        return None

    # -- 1. export -------------------------------------------------------------
    def _export_chunk(self, st: DayState) -> str:
        path = self.export_path(st.unit_id, st.day)
        partial = path + ".partial"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cursor = json.loads(st.export_cursor) if st.export_cursor else None
        if cursor is not None:
            # resume: drop any bytes written after the last state save
            size = cursor.get("size")
            if size is None or not os.path.exists(partial) or os.path.getsize(partial) < size:
                cursor, st.export_rows = None, 0
                if os.path.exists(partial):
                    os.remove(partial)
            else:
                os.truncate(partial, size)
        elif os.path.exists(partial):
            os.remove(partial)

        rows, nxt = self.store.read_day_page(st.unit_id, st.day, cursor, self.cfg.export_chunk_rows)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        if cursor is None:
            w.writerow(EXPORT_HEADER)
        for r in rows:
            w.writerow([r.unit_id, r.boot_id, r.seq, r.gps_utc_us, r.gps_raw, r.time_src, repr(r.freq_hz),
                        "" if r.amplitude_v is None else repr(r.amplitude_v), r.gps_locked, r.flags,
                        repr(r.received_at)])
        with gzip.open(partial, "ab") as gz:
            gz.write(buf.getvalue().encode())
        st.export_rows += len(rows)
        if nxt is not None:
            nxt = dict(nxt, size=os.path.getsize(partial))
            st.export_cursor = json.dumps(nxt)
            self.store.save_day_state(st)
            return f"export:{st.unit_id}:{st.day}:{st.export_rows}"

        # finished: verify by reading the whole file back
        with gzip.open(partial, "rt", newline="") as gz:
            n = sum(1 for _ in csv.reader(gz)) - 1
        if n != st.export_rows:
            st.attention = f"export verification failed: file has {n} rows, expected {st.export_rows}"
            st.export_cursor = None
            self.store.save_day_state(st)
            return f"export-failed:{st.unit_id}:{st.day}"
        os.replace(partial, path)
        with open(path, "rb") as fh:
            st.export_sha256 = hashlib.sha256(fh.read()).hexdigest()
        st.export_path, st.export_done, st.export_cursor = path, True, None
        self.store.save_day_state(st)
        return f"export-done:{st.unit_id}:{st.day}:{st.export_rows}"

    # -- 2/3. aggregates and events -----------------------------------------------
    def _aggregate_hour(self, st: DayState) -> str:
        cfg = self.cfg
        h = st.agg_next_hour
        start = st.day * DAY_US + h * 3600 * US
        end = start + 3600 * US
        margin = 10 * US
        rows = self.store.read_locked_range(st.unit_id, start - margin, end + margin)
        pts = [(r.gps_utc_us / US, r.freq_hz, r.boot_id) for r in rows]
        series = rocof_series(pts, cfg.max_rocof_gap_s, cfg.rocof_plausibility_hz_s)
        lo_s, hi_s = start / US, end / US
        rocof_by_t = {t: s for t, s in series.points if lo_s <= t < hi_s}

        by_min: Dict[int, List[Row]] = {}
        for r in rows:
            if start <= r.gps_utc_us < end:
                by_min.setdefault(r.gps_utc_us // (60 * US), []).append(r)
        unlocked = self.store.unlocked_minute_counts(st.unit_id, lo_s, hi_s)
        out: List[AggRow] = []
        for m in sorted(set(by_min) | set(unlocked)):
            rs = by_min.get(m, [])
            fs = [r.freq_hz for r in rs]
            amps = [r.amplitude_v for r in rs if r.amplitude_v is not None]
            if fs:
                mean = sum(fs) / len(fs)
                std = (sum((f - mean) ** 2 for f in fs) / len(fs)) ** 0.5
                roc = [abs(rocof_by_t[r.gps_utc_us / US]) for r in rs if r.gps_utc_us / US in rocof_by_t]
                out.append(AggRow(st.unit_id, m, len(fs), unlocked.get(m, 0), mean, min(fs), max(fs), std,
                                  max(roc) if roc else None, sum(amps) / len(amps) if amps else None))
            else:
                out.append(AggRow(st.unit_id, m, 0, unlocked.get(m, 0), None, None, None, None, None, None))
        self.store.save_aggregates(out)
        self._detect_events(st.unit_id, rows, start, end, rocof_by_t)

        st.agg_next_hour = h + 1
        if st.agg_next_hour >= 24:
            st.agg_done = True
        self.store.save_day_state(st)
        return f"aggregate:{st.unit_id}:{st.day}:h{h}"

    def _detect_events(self, unit_id, rows, start_us, end_us, rocof_by_t) -> None:
        cfg = self.cfg
        margin = int(cfg.event_margin_s * US)
        trig = []   # (t_us, reason, |rocof| or None, freq)
        for r in rows:
            if not (start_us <= r.gps_utc_us < end_us):
                continue
            roc = rocof_by_t.get(r.gps_utc_us / US)
            if roc is not None and abs(roc) > cfg.rocof_event_hz_s:
                trig.append((r.gps_utc_us, "rocof", abs(roc), r.freq_hz))
            if not (cfg.freq_lo <= r.freq_hz <= cfg.freq_hi):
                trig.append((r.gps_utc_us, "freq_band", None, r.freq_hz))
        if not trig:
            return
        trig.sort()
        group = [trig[0]]
        groups = []
        for t in trig[1:]:
            if t[0] - group[-1][0] <= 2 * margin:
                group.append(t)
            else:
                groups.append(group)
                group = [t]
        groups.append(group)
        for g in groups:
            rocs = [x[2] for x in g if x[2] is not None]
            self.store.add_event(
                unit_id, g[0][0] - margin, g[-1][0] + margin, "+".join(sorted({x[1] for x in g})),
                max(rocs) if rocs else None, min(x[3] for x in g), max(x[3] for x in g))

    # -- 4/5. verify and prune -------------------------------------------------------
    def _prune_chunk(self, st: DayState) -> str:
        locked, unlocked = self.store.day_row_counts(st.unit_id, st.day)
        if st.verified_at is None:
            agg_locked, agg_unlocked = self.store.aggregate_totals(st.unit_id, st.day)
            problem = None
            if not (st.export_path and os.path.exists(st.export_path)):
                problem = "export file missing"
            else:
                with open(st.export_path, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != st.export_sha256:
                        problem = "export file checksum changed"
            if problem is None and locked + unlocked != st.export_rows:
                problem = f"database has {locked + unlocked} rows but {st.export_rows} were exported"
            if problem is None and (agg_locked != locked or agg_unlocked != unlocked):
                problem = (f"aggregates count {agg_locked}+{agg_unlocked} unlocked, "
                           f"database has {locked}+{unlocked}")
            if problem:
                st.attention = "verification failed: " + problem
                self.store.save_day_state(st)
                return f"verify-failed:{st.unit_id}:{st.day}"
            st.verified_at = self.clock()
            self.store.save_day_state(st)
            return f"verified:{st.unit_id}:{st.day}"
        if locked + unlocked + st.pruned_rows != st.export_rows:
            st.attention = "row count drifted while pruning"
            self.store.save_day_state(st)
            return f"verify-failed:{st.unit_id}:{st.day}"
        deleted = self.store.delete_day_rows(st.unit_id, st.day, self.cfg.prune_chunk_rows)
        st.pruned_rows += deleted
        if deleted == 0 or locked + unlocked - deleted == 0:
            st.pruned_done = True          # nothing left, or what remains is inside kept event intervals
        self.store.save_day_state(st)
        return f"prune:{st.unit_id}:{st.day}:{deleted}"


# --------------------------------------------------------------------------------
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tremor.retention", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("TREMOR_DB_PATH"), required=os.environ.get("TREMOR_DB_PATH") is None)
    ap.add_argument("--export-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--max-seconds", type=float, default=60.0)
    rd = sub.add_parser("reset-day", help="forget a day's retention state (e.g. after fixing an 'attention' problem)")
    rd.add_argument("unit_id"); rd.add_argument("day", type=int)
    pe = sub.add_parser("prune-exports", help="delete old export files (only after you have pulled them off the server)")
    pe.add_argument("--older-than-days", type=int, required=True)
    pe.add_argument("--confirm-downloaded", action="store_true")
    a = ap.parse_args(argv)

    store = open_store(a.db)
    cfg = RetentionConfig.from_env(a.export_dir or os.path.join(os.path.dirname(os.path.abspath(a.db)), "exports"))
    eng = RetentionEngine(store, cfg)
    if a.cmd == "status":
        print(json.dumps(store.health(), indent=1))
        for u in store.unit_states():
            for d, st in sorted(store.day_states(u.unit_id).items()):
                print(u.unit_id, day_to_date(d), "export" if st.export_done else "-", "agg" if st.agg_done else "-",
                      "verified" if st.verified_at else "-", "pruned" if st.pruned_done else "-", st.attention or "")
    elif a.cmd == "run":
        t0 = time.monotonic()
        while time.monotonic() - t0 < a.max_seconds:
            if eng.step() is None:
                print("idle")
                return 0
        print("stopped at the time limit; run again to continue")
    elif a.cmd == "reset-day":
        st = DayState(a.unit_id, a.day)
        store.save_day_state(st)
        print("reset", a.unit_id, day_to_date(a.day))
    elif a.cmd == "prune-exports":
        if not a.confirm_downloaded:
            print("refusing: pass --confirm-downloaded once you have copied the export files elsewhere")
            return 2
        cutoff = time.time() - a.older_than_days * 86400
        n = 0
        for root, _dirs, files in os.walk(cfg.export_dir):
            for fn in files:
                p = os.path.join(root, fn)
                if fn.endswith(".csv.gz") and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
        print("deleted", n, "export file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
