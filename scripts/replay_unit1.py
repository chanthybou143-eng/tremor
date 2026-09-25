#!/usr/bin/env python3
"""End-to-end replay of Unit 1-shaped traffic against a local Flask server.

    PYTHONPATH=src python scripts/replay_unit1.py [--hours 2] [--mode v2|v1] [--seed 1]

What it does
  * starts the real web app (tremor.webapp.create_app) under werkzeug's server, on
    localhost, so every POST is real HTTP;
  * simulates a Unit 1 with a virtual clock: ~0.944 readings/s, the REAL
    wifi_ingest.IngestBuffer (oldest-60-per-POST, merge-back on failure), a 30 s POST
    interval that doubles after a failure (cap 120 s), and reboots that lose the RAM buffer;
  * injects the failure modes seen in the soak:
      - tls/connect failure: the server never sees the request;
      - read_response timeout AFTER the server stored the batch (device thinks it failed,
        so it re-sends the same readings -- the duplicates this work must remove);
      - a burst of consecutive failures; a window where the server returns 503;
  * verifies the database against the device's own ground truth: exactly one copy of every
    reading the server received, at the exact time the device measured it, nothing invented.

Exit status 0 = every check passed.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import logging
import os
import random
import statistics
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from wifi_ingest import IngestBuffer, make_boot_id  # noqa: E402
from tremor.retention import RetentionConfig  # noqa: E402
from tremor.security import IngestAuth  # noqa: E402
from tremor.store import StoreError  # noqa: E402
from tremor.timeline import rocof_series  # noqa: E402
from tremor.webapp import create_app  # noqa: E402

US = 1_000_000
POST_INTERVAL_S = 30.0
BACKOFF_CAP_S = 120.0
MAX_BUFFERED = 600
MAX_PER_POST = 60


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def f32_text(x):
    """What the device sends for a float32: its shortest round-trip text, re-parsed."""
    return float(str(np.float32(x)))


class Device:
    def __init__(self, url, clock, start_unix, rng, mode, reboots, fail_bursts, fault_windows, log, token=None):
        self.url, self.clock, self.rng, self.mode, self.log = url, clock, rng, mode, log
        self.token = token
        self.t0 = start_unix
        self.reboots = sorted(reboots)
        self.fail_bursts = fail_bursts            # [(start_s, n_attempts)]
        self.fault_windows = fault_windows        # [(start_s, end_s)] server answers 503
        self.now = 0.0                            # virtual seconds since start
        self.next_second = 1                      # next reading is produced at this virtual second
        self.boot = 0
        self.interval = POST_INTERVAL_S
        self.produced = {}                        # (boot, seq) -> (unix_us, freq, amp, locked)
        self.lookup = {}                          # (float32 freq, float32 gps sod) -> (boot, seq), for seq-less v1 payloads
        self.server_saw = set()                   # readings that were in a POST the server processed
        self.sent_total = 0
        self.processed_total = 0                  # readings in POSTs the server answered 202 to
        self.attempts = []                        # (t, mode, n, result)
        self.lost_on_reboot = 0
        self.boot_ids = []
        self.readings_since_boot = 0
        self.down_until = 0.0
        self._burst_left = 0
        self._new_boot()

    def _new_boot(self):
        self.boot_id = make_boot_id(urandom=lambda n: bytes(self.rng.getrandbits(8) for _ in range(n)))
        self.boot_ids.append(self.boot_id)
        self.buf = IngestBuffer("unit-1", self._post, max_readings=MAX_BUFFERED,
                                max_readings_per_post=MAX_PER_POST,
                                boot_id=self.boot_id if self.mode == "v2" else None)
        self.seq_of_boot = 0
        self.readings_since_boot = 0

    # -- reading production ---------------------------------------------------------------------
    def produce_until(self, t_end):
        while self.next_second <= t_end:
            s = self.next_second
            self.next_second += 1
            if s < self.down_until or self.rng.random() < 0.056:      # ~0.944 readings/s, like the real unit
                continue
            unix = self.t0 + s + self.rng.uniform(0.0, 0.02)
            us = int(round(unix * US))
            freq = 50.0 + 0.015 * np.sin(s / 610.0) + self.rng.gauss(0, 0.012)
            locked = self.readings_since_boot >= 3                        # the first readings after boot have no anchor yet
            seq = self.seq_of_boot
            self.seq_of_boot += 1
            self.readings_since_boot += 1
            self.produced[(self.boot, seq)] = (us, float(freq), 0.744, locked)
            gps = None
            if locked:
                d, rem = divmod(us, 86400 * US)
                sec, usec = divmod(rem, US)
                gps = (d, sec, usec)
            sod32 = f32_text((us / US) % 86400) if locked else None
            self.lookup[(float(np.float32(freq)), None if sod32 is None else float(np.float32(sod32)))] = (self.boot, seq)
            self.buf.append(float(freq), 0.744, sod32, gps)

    # -- one POST attempt --------------------------------------------------------------------------
    def _post(self, payload):
        t = self.now
        n = len(payload["readings"])
        self.sent_total += n
        keys = [(self.boot, r["seq"]) if "seq" in r else self.lookup.get((r["frequency_hz"], r["gps_utc_s"]))
                for r in payload["readings"]]
        mode = "ok"
        if self._burst_left > 0:
            self._burst_left -= 1
            mode = "tls"
        else:
            for start, cnt in self.fail_bursts:
                if abs(t - start) < POST_INTERVAL_S / 2 and (start, cnt) not in getattr(self, "_burst_done", set()):
                    self._burst_done = getattr(self, "_burst_done", set()) | {(start, cnt)}
                    self._burst_left = cnt - 1
                    mode = "tls"
                    break
            else:
                x = self.rng.random()
                mode = "tls" if x < 0.006 else "read_response_stored" if x < 0.014 else "read_response_lost" if x < 0.018 else "ok"
        dur = 2.5 if mode == "ok" else self.rng.uniform(5.0, 9.0)
        self.clock.t = self.t0 + t + dur * 0.5                      # the server's clock at receipt
        result = "n/a"
        stored_by_server = False
        if mode in ("ok", "read_response_stored"):
            hdrs = {"Content-Type": "application/json"}
            if self.token:
                hdrs["X-Tremor-Token"] = self.token
            req = urllib.request.Request(self.url + "/api/ingest", data=json.dumps(payload).encode(), headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = json.loads(r.read())
                    status = r.status
            except urllib.error.HTTPError as e:
                status, body = e.code, {}
            if status == 202:
                stored_by_server = True
                self.processed_total += n
                result = f"inserted {body['inserted']}, duplicates {body['duplicates']}"
            else:
                result = f"HTTP {status}"
        else:
            result = "request never reached the server" if mode == "tls" else "request lost"
        if stored_by_server:
            for k in keys:
                self.server_saw.add(k if k is not None else None)
        self.attempts.append((t, mode, n, result, stored_by_server))
        self.produce_until(int(t + dur))                              # readings keep arriving while the POST is in flight
        self.now = t + dur
        device_ok = mode == "ok" and stored_by_server
        if not device_ok:
            raise TimeoutError(mode)                                  # IngestBuffer treats any exception as a failed POST
        return True

    # -- main loop -----------------------------------------------------------------------------------
    def run(self, duration_s):
        next_flush = POST_INTERVAL_S
        reboots = list(self.reboots)
        while self.now < duration_s:
            self.produce_until(int(next_flush))
            self.now = max(self.now, next_flush)
            if reboots and self.now >= reboots[0]:
                reboots.pop(0)
                self.lost_on_reboot += len(self.buf)
                self.log(f"t={self.now:7.0f}s  REBOOT: {len(self.buf)} buffered readings lost with the RAM")
                self.boot += 1
                self.down_until = self.now + 20
                self.next_second = max(self.next_second, int(self.now))
                self._new_boot()
                self.interval = POST_INTERVAL_S
                next_flush = self.now + 20 + POST_INTERVAL_S
                continue
            before = len(self.buf)
            ok = self.buf.flush()
            self.interval = POST_INTERVAL_S if ok else min(self.interval * 2, BACKOFF_CAP_S)
            t, mode, n, result, stored = self.attempts[-1] if self.attempts else (0, "-", 0, "", False)
            if mode != "ok" or "HTTP" in result or ("duplicates" in result and "duplicates 0" not in result):
                self.log(f"t={t:7.0f}s  POST {mode:20s} n={n:3d} buffered={before:3d} -> {result}")
            next_flush = self.now + self.interval


TOKEN = "replay-token-unit1-0123456789abcdef"
EXPORT_TOKEN = "replay-export-token-0123456789abcdef"


def start_server(db_path, clock, export_dir, auth_mode="off"):
    auth = IngestAuth({"unit-1": TOKEN}, auth_mode, clock=clock) if auth_mode != "off" else IngestAuth({}, "off")
    app = create_app(simulated_units=[], db_path=db_path, clock=clock, ingest_auth=auth, export_token=EXPORT_TOKEN,
                     retention_config=RetentionConfig(export_dir=export_dir, raw_days=14))
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return app, srv, f"http://127.0.0.1:{srv.server_port}"


def post_probe(url, token):
    """POST a tiny valid v2 batch with the given token (or none) and return the HTTP status."""
    body = json.dumps({"unit_id": "unit-1", "boot_id": "probe0000probe00", "readings": [
        {"seq": 0, "frequency_hz": 50.0, "amplitude_v": 0.7}]}).encode()
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["X-Tremor-Token"] = token
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/api/ingest", data=body, headers=hdrs)) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--mode", choices=("v1", "v2"), default="v2")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--start", default="2026-09-24T23:00:00Z",
                    help="virtual UTC start (default crosses UTC midnight = 09:30 ACST)")
    ap.add_argument("--auth", choices=("off", "optional", "required"), default="off",
                    help="server ingest-auth mode; the simulated device sends its token unless --no-token")
    ap.add_argument("--no-token", action="store_true", help="simulate a legacy unit that cannot send a token")
    ap.add_argument("--keep-db", metavar="PATH")
    a = ap.parse_args()

    logging.getLogger("werkzeug").setLevel(logging.ERROR)          # one line per POST would drown the report
    rng = random.Random(a.seed)
    start = datetime.fromisoformat(a.start.replace("Z", "+00:00")).timestamp()
    duration = a.hours * 3600
    tmp = tempfile.mkdtemp(prefix="tremor-replay-")
    db_path = a.keep_db or os.path.join(tmp, "readings.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    clock = Clock(start)
    app, srv, url = start_server(db_path, clock, os.path.join(tmp, "exports"), a.auth)
    store = app.config["TREMOR_STORE"]

    # the server answers 503 for a window (storage unavailable), via the real StoreError path
    fault_windows = [(duration * 0.55, duration * 0.55 + 200)]
    real_ingest = store.ingest

    def flaky_ingest(batch, received_at):
        t = received_at - start
        if any(lo <= t < hi for lo, hi in fault_windows):
            raise StoreError("database is locked (injected)")
        return real_ingest(batch, received_at)
    store.ingest = flaky_ingest

    print(f"replaying {a.hours:g} h of virtual Unit 1 traffic ({a.mode} payloads) -> {url}\n  db: {db_path}\n")
    events = []
    dev = Device(url, clock, start, rng, a.mode, reboots=[duration * 0.35, duration * 0.8],
                 fail_bursts=[(duration * 0.2, 4), (duration * 0.7, 2)], fault_windows=fault_windows,
                 log=lambda s: (events.append(s), print("  " + s)),
                 token=None if (a.auth == "off" or a.no_token) else TOKEN)
    dev.run(duration)
    # drain: keep flushing until the buffer is empty (as the real device would)
    for _ in range(60):
        if len(dev.buf) == 0:
            break
        dev.now += POST_INTERVAL_S
        dev.buf.flush()
    print()

    # ------------------------------------------------------------------ verification
    failures = []

    def check(ok, msg):
        print(("  PASS  " if ok else "  FAIL  ") + msg)
        if not ok:
            failures.append(msg)

    if a.auth == "required" and a.no_token:
        h = get(f"{url}/api/health")
        print("Access control (mode: required; the simulated device sent no token)")
        check(dev.processed_total == 0 and h["store"]["raw_rows"] == 0,
              f"a legacy device with no token stored nothing: {h['store']['raw_rows']} rows, {len(dev.produced)} readings still on the device")
        check(h["ingest_auth"]["rejected_missing"] > 0 and h["ingest_auth"]["authenticated"] == 0,
              f"every POST was refused with 401 and counted ({h['ingest_auth']['rejected_missing']} rejected)")
        print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED"))
        srv.shutdown()
        return 1 if failures else 0

    lo_s = min(v[0] for v in dev.produced.values()) / US - 10           # ground truth's own time range
    hi_s = max(v[0] for v in dev.produced.values()) / US + 10
    page = get(f"{url}/api/history?unit=unit-1&from={lo_s}&to={hi_s}&limit=10000&include_unlocked=1")
    rows, cursor = page["readings"], page
    while cursor["truncated"]:
        cursor = get(f"{url}/api/history?unit=unit-1&from={cursor['next_from_us'] / US}&after_id={cursor['next_after_id']}"
                     f"&to={hi_s}&limit=10000")
        rows += cursor["readings"]
    unlocked_rows = page.get("unlocked", [])
    db_keys = [(r["boot_id"], r["seq"]) for r in rows]

    expected_locked = {k for k in dev.server_saw if k in dev.produced and dev.produced[k][3]}
    expected_unlocked = {k for k in dev.server_saw if k in dev.produced and not dev.produced[k][3]}
    boot_index = {b: i for i, b in enumerate(dev.boot_ids)}

    print("Database vs the device's own ground truth")
    if a.mode == "v2":
        got = {(boot_index[b], s) for b, s in db_keys}
        check(len(db_keys) == len(set(db_keys)), f"no reading stored twice ({len(db_keys)} GPS-timed rows, {len(set(db_keys))} distinct keys)")
        check(got == expected_locked, f"exactly the readings the server received are present: {len(expected_locked)} expected, {len(got)} stored, "
                                      f"{len(expected_locked - got)} missing, {len(got - expected_locked)} unexpected")
        exact = all(dev.produced[(boot_index[r['boot_id']], r['seq'])][0] == r["gps_utc_us"] for r in rows)
        check(exact, "every stored time equals the device's measured GPS time to the microsecond")
        ul_keys = {(boot_index[r["boot_id"]], r["seq"]) for r in unlocked_rows}
        check(ul_keys == expected_unlocked and len(unlocked_rows) == len(ul_keys),
              f"unlocked (pre-anchor) readings stored once and flagged: {len(ul_keys)} of {len(expected_unlocked)}")
        # seq gaps == readings the device never delivered
        lost = 0
        for bi, bid in enumerate(dev.boot_ids):
            seqs = sorted(s for (b, s) in got | ul_keys if b == bi)
            if seqs:
                lost += (seqs[-1] - seqs[0] + 1) - len(seqs)
        undelivered = sum(1 for k in dev.produced if k not in dev.server_saw)
        print(f"        (info) readings produced {len(dev.produced)}; delivered {len(dev.server_saw)}; never delivered {undelivered} "
              f"(lost on reboot {dev.lost_on_reboot}, dropped by the full buffer {dev.buf.dropped_count}; "
              f"{lost} visible as seq gaps between delivered readings)")
    else:
        # legacy: no per-reading ids; dedupe is by GPS instant (float32 seconds-of-day, ~8 ms resolution)
        times = [r["gps_utc_us"] for r in rows]
        check(len(times) == len(set(times)), f"no GPS instant stored twice ({len(times)} rows)")
        check(len(rows) == len(expected_locked),
              f"one row per delivered locked reading: {len(expected_locked)} expected, {len(rows)} stored")
        truth_us = sorted(dev.produced[k][0] for k in expected_locked)
        worst = 0
        for r in rows:
            i = bisect.bisect_left(truth_us, r["gps_utc_us"])
            cands = truth_us[max(0, i - 1):i + 1]
            worst = max(worst, min(abs(r["gps_utc_us"] - c) for c in cands))
        check(worst <= 8000, f"stored times are within float32 resolution of the measured time (worst {worst} us)")
        check(len(unlocked_rows) >= len(expected_unlocked),
              f"every unlocked reading is stored and flagged ({len(unlocked_rows)} rows for {len(expected_unlocked)} readings)")
        print(f"        (info) legacy unlocked readings cannot be deduplicated, so a retried one can be stored twice "
              f"({len(unlocked_rows) - len(expected_unlocked)} extra unlocked rows this run)")

    stored = len(rows) + len(unlocked_rows)
    dup_expected = dev.processed_total - stored
    print(f"        (info) readings the device POSTed {dev.sent_total}; in requests the server processed {dev.processed_total}; "
          f"rows stored {stored}; duplicates the server discarded {dup_expected}")

    # time series shape: contiguous except where the device really lost data, and no impossible RoCoF
    ts = [r["t"] for r in rows]
    holes = [(b - a) for a, b in zip(ts, ts[1:]) if b - a > 20]
    print(f"\nSeries shape: {len(ts)} points over {ts[-1] - ts[0]:.0f} s; holes > 20 s: {len(holes)} "
          f"(largest {max(holes) if holes else 0:.0f} s)")
    series = rocof_series([(r["t"], r["freq_hz"], r["boot_id"]) for r in rows])
    worst_rocof = max((abs(s) for _t, s in series.points), default=0.0)
    check(worst_rocof < 0.5, f"no impossible RoCoF anywhere: worst |RoCoF| {worst_rocof:.3f} Hz/s over {len(series.points)} points, "
                             f"{series.skipped_boundary} suppressed at boot boundaries")

    units = get(f"{url}/api/units")
    u = next(x for x in units if x["id"] == "unit-1")
    print(f"\n/api/units: status={u['status']} gps_locked={u['gps_locked']} unlocked_count={u['unlocked_count']} "
          f"duplicates_ignored={u['duplicates_ignored']}")
    check(u["duplicates_ignored"] == dup_expected and dup_expected >= 1,
          f"duplicates the server counted ({u['duplicates_ignored']}) == readings processed minus rows stored ({dup_expected})")

    h = get(f"{url}/api/health")
    print(f"/api/health: {h['status']}  db={h['storage']['db_bytes'] / 1e6:.2f} MB  rows={h['store']['raw_rows']}")

    # restart: a new app on the same DB, and a stale retry of readings it already has
    srv.shutdown()
    app2, srv2, url2 = start_server(db_path, clock, os.path.join(tmp, "exports"), a.auth)
    try:
        c2 = get(f"{url2}/api/history?unit=unit-1&from={lo_s}&to={hi_s}&limit=10000")
        check(c2["count"] == min(len(rows), 10000), f"after a server restart the same DB serves the same history ({c2['count']} rows)")
        last = rows[-5:]
        if a.mode == "v2":
            hdr2 = {"Content-Type": "application/json"}
            if a.auth != "off" and not a.no_token:
                hdr2["X-Tremor-Token"] = TOKEN
            req = urllib.request.Request(url2 + "/api/ingest", data=json.dumps({
                "unit_id": "unit-1", "boot_id": last[0]["boot_id"], "readings": [
                    {"seq": r["seq"], "frequency_hz": r["freq_hz"], "amplitude_v": r["amplitude_v"],
                     "gps": [r["gps_utc_us"] // (86400 * US), (r["gps_utc_us"] % (86400 * US)) // US, r["gps_utc_us"] % US]}
                    for r in last]}).encode(), headers=hdr2)
            clock.t = start + duration + 5
            with urllib.request.urlopen(req) as r:
                body = json.loads(r.read())
            check(body["inserted"] == 0 and body["duplicates"] == 5, "a stale retry after the restart is recognised as duplicates")
    finally:
        srv2.shutdown()

    if a.auth != "off":
        print(f"\nAccess control (mode: {a.auth}; the simulated device {'sent no token' if a.no_token else 'sent its token'})")
        srv_p = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=srv_p.serve_forever, daemon=True).start()
        url_probe = f"http://127.0.0.1:{srv_p.server_port}"
        try:
            clock.t = start + duration + 100
            codes = {name: post_probe(url_probe, tok) for name, tok in
                     (("no token", None), ("wrong token", "w" * 30), ("right token", TOKEN))}
            expect = {"optional": {"no token": 202, "wrong token": 401, "right token": 202},
                      "required": {"no token": 401, "wrong token": 401, "right token": 202}}[a.auth]
            check(codes == expect, f"POST with no / wrong / right token -> {codes['no token']} / {codes['wrong token']} / {codes['right token']} (expected {expect['no token']} / {expect['wrong token']} / {expect['right token']})")
            if codes["right token"] == 202:
                stored += 1                                   # the probe itself stored one (unlocked) reading
            summary = get(f"{url_probe}/api/health")["ingest_auth"]
            print(f"        /api/health ingest_auth: {json.dumps(summary)}")
            if a.no_token and a.auth == "required":
                check(dev.processed_total == 0, "a legacy device without a token delivered nothing in 'required' mode (all its POSTs got 401)")
            elif a.no_token:
                check(summary["missing_accepted"] > 0, f"in 'optional' mode the token-less legacy device was accepted and counted ({summary['missing_accepted']} requests)")
            else:
                probe_rejects = 1 if a.auth == "required" else 0          # the deliberate no-token probe above
                check(summary["authenticated"] > 0 and summary["rejected_missing"] == probe_rejects,
                      f"the token-bearing device authenticated ({summary['authenticated']} requests); the only missing-token rejection is my probe")
            exp_day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
            for label, hdr, want in (("no token", {}, 401), ("ingest token", {"X-Tremor-Token": TOKEN}, 401)):
                try:
                    urllib.request.urlopen(urllib.request.Request(f"{url_probe}/api/export/unit-1/{exp_day}", headers=hdr))
                    got = 200
                except urllib.error.HTTPError as e:
                    got = e.code
                check(got == want, f"/api/export with {label} -> {got}")
        finally:
            srv_p.shutdown()

    # retention: age the clock 20 days, let the guarded pruner run, and prove nothing was lost
    print("\nRetention (clock advanced 20 days, raw_days=14)")
    clock.t = start + duration + 20 * 86400
    eng = app.config["TREMOR_RETENTION"]
    app.config["TREMOR_STORE"].ingest = real_ingest
    work = eng.run_until_idle()
    st = app.config["TREMOR_STORE"]
    h2 = st.health()
    days = st.day_states("unit-1")
    exported = sum(d.export_rows for d in days.values())
    agg_locked = sum(st.aggregate_totals("unit-1", d)[0] for d in days)
    agg_unl = sum(st.aggregate_totals("unit-1", d)[1] for d in days)
    print(f"        {len(work)} maintenance steps; days: " + ", ".join(
        f"{datetime.fromtimestamp(d * 86400, timezone.utc).date()} exported={s_.export_rows} pruned={s_.pruned_rows}"
        for d, s_ in sorted(days.items())))
    check(not h2["days_needing_attention"], "every day verified (export == database == aggregates); none needs attention")
    check(exported == stored, f"the exports hold every stored row: {exported} exported, {stored} were stored")
    check(agg_locked + agg_unl == stored, f"the 1-minute aggregates account for every row: {agg_locked} locked + {agg_unl} unlocked")
    check(h2["raw_rows"] == 0 and all(d.pruned_done for d in days.values()),
          f"raw rows pruned only after verification: {h2['raw_rows']} left, {sum(d.pruned_rows for d in days.values())} pruned")
    total_n = 0
    for d in sorted(days):
        p = eng.export_path("unit-1", d)
        with gzip.open(p, "rt") as fh:
            total_n += sum(1 for _ in fh) - 1
    check(total_n == stored, f"re-reading the gzip files from disk finds all {total_n} rows")
    srv3 = make_server("127.0.0.1", 0, app, threaded=True)          # serve the aged database over real HTTP
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    url3 = f"http://127.0.0.1:{srv3.server_port}"
    try:
        old = get(f"{url3}/api/history?unit=unit-1&from={lo_s}&to={hi_s}&limit=10000")
        check(old["resolution"] == "1min" and sum(x["n"] for x in old["aggregates"]) == agg_locked,
              f"history for data older than raw_days is served as 1-minute aggregates ({len(old['aggregates'])} minutes, "
              f"{sum(x['n'] for x in old['aggregates'])} readings)")
        a0 = old["aggregates"][len(old["aggregates"]) // 2]
        check(all(k in a0 for k in ("freq_mean", "freq_min", "freq_max", "freq_std", "rocof_max_abs", "n_unlocked")),
              "aggregates keep mean/min/max/std of frequency, max |RoCoF| and the unlocked count")
        d0 = sorted(days)[0]
        day_iso = datetime.fromtimestamp(d0 * 86400, timezone.utc).date().isoformat()
        with urllib.request.urlopen(urllib.request.Request(f"{url3}/api/export/unit-1/{day_iso}",
                                                            headers={"X-Tremor-Token": EXPORT_TOKEN})) as r:
            n_dl = len(gzip.decompress(r.read()).decode().splitlines()) - 1
        check(n_dl == days[d0].export_rows, f"the export downloads over HTTP intact: {n_dl} rows for {day_iso}")
    finally:
        srv3.shutdown()

    print("\nEvent log above = what the device saw. Every 'read_response_stored' line is a batch the server DID keep while the device")
    print("timed out waiting for the answer; its retry re-sent those readings and they were discarded, not stored twice.")
    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
