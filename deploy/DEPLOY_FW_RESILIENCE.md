# Deploying fw-resilience (server first, then Unit 1)

Branch `fw-resilience`. Each step below needs your go-ahead. Do them in this order: the server must accept
6-hour-late readings **before** a unit can hold a 60-minute backlog.

| Commit | What |
|---|---|
| `6f261e7` | Server: `MAX_AGE_S` 6 h, retention `settle_s` 6.5 h, raw-hour settle 75 min, telemetry table + `/api/health` |
| `11fd04e` | Firmware: 60-min 16-byte-record ring, 10 s catch-up |
| `66db40b` | Firmware: hard POST abort at 10 s, `# SLOW_POST` over 15 s |
| `fccf4e0` | Firmware: Wi-Fi PM_NONE, reconnect escalation, telemetry, skipped-chunk counter |
| `5fa2501` | Firmware: hard ADC timer (separate, for the A/B below) |
| branch head | Firmware: STATUS reads the Wi-Fi status exception-safely |

Device files that change: `wifi_unit_client.py`, `wifi_ingest.py`, `http_client.py`, `wdt_support.py`, and the
new `wifi_support.py`. `main.py`, `boot_support.py`, `pps_time_sync.py`, `nmea_parser.py`, `chunk_summary.py`,
`freq_estimator.py` and `wifi_config.py` are untouched.

## 0. Push the branch (Mac)

```bash
cd ~/tremor-merge && git push -u origin fw-resilience
```

## 1. Server (PythonAnywhere)

Python 3.9, as before. Reloading is safe (ingest auth is `required`, so nothing depends on the counters a
reload resets). The old firmware keeps working unchanged: it sends no `telemetry`, and its readings are never
more than ~10 min late.

```bash
cd ~/tremor
git status --short                 # expect: clean
git rev-parse HEAD                 # expect: e58ef14ce0213774b97b1594ba5aeeac5b2c93fd  (rollback point)
git fetch origin
git checkout --detach origin/fw-resilience
git diff --stat e58ef14 HEAD -- src/ deploy/
#   expect only: src/tremor/{history,ingest,retention,store,webapp}.py and deploy/DEPLOY_FW_RESILIENCE.md
python3.9 -c "import sys; sys.path.insert(0,'src'); import tremor.webapp"      # prints nothing
```

Web tab → **Reload**, then the **error log**: no new tracebacks.

```bash
S=https://tremorgrid.pythonanywhere.com
curl -s $S/api/health | python3.9 -c "import sys,json; h=json.load(sys.stdin); print(h['status'], \
  h['store']['schema_version'], [(u['unit_id'], round(u['seconds_since_last_reading']), u['telemetry']) for u in h['units']])"
#   expect: ok 1 [('unit-1', <60, None)]   -- telemetry stays None until the new firmware runs
python3.9 -c "import sqlite3,os; db=sqlite3.connect(os.path.expanduser('~/tremor_data/readings.db')); \
  print(db.execute(\"select count(*) from sqlite_master where name='telemetry'\").fetchone(), \
  db.execute('pragma user_version').fetchone())"
#   expect: (1,) (1,)   -- the new table exists, the schema version did not change
```

Watch `seconds_since_last_reading` for a few minutes: it stays under ~60 s and `readings_total` keeps rising.

Note: retention now processes a UTC day 6.5 h after it ends (was 1 h), i.e. at 17:00 ACDT the next day. The
history page computes not-yet-aggregated days from raw readings, as it already does for today.

**Rollback:** `cd ~/tremor && git checkout e58ef14` (or `git checkout master`), Web tab → Reload. The
`telemetry` table can stay: older code ignores it and the schema version is unchanged.

## 2. Bench tests on the Pico (nothing written to flash)

Unit 1 keeps posting during these runs, under new `boot_id`s. Each run is the new code served from the Mac
via `mpremote mount` and run from RAM; the device's own `wifi_config.py` is imported from its flash, never
printed. Stop a run with Ctrl-C: the watchdog resets the board ~8 s later and, with the jumper still fitted,
it comes back in maintenance mode (LED steady on).

**2.0 Maintenance mode:** unplug, fit the GP22 jumper (pin 29 to pin 28), plug in. LED steady on
(RECOVERY.md).

**2.1 Hard-ADC-timer A/B (30 min each).** Soft timer = commit `fccf4e0`; hard timer = branch head.

```bash
cd ~/tremor-merge
git worktree add ../tremor-fw-soft fccf4e0
mpremote connect auto mount ../tremor-fw-soft run scripts/bench_normal.py | tee ~/bench_soft.log    # 30 min, Ctrl-C
mpremote connect auto mount .                 run scripts/bench_normal.py | tee ~/bench_hard.log    # 30 min, Ctrl-C
grep -m1 '# BOOT_ID' ~/bench_soft.log ~/bench_hard.log
python3 scripts/bench_gap_report.py --boot <soft boot_id> --compare-boot <hard boot_id>
git worktree remove ../tremor-fw-soft
```

Pass:
- **Hard:** `time_gaps` about 0, and `gaps_locked_to_post_cadence` no longer close to all of them.
  `readings_per_10min` ≥ ~595 (600 minus `skipped_chunks`). STATUS `overflow=0`.
- **Soft** (for reference; it matches production): about 44 gaps/h, nearly all locked to the 30 s cadence,
  and ~567 readings/10 min.
- **Frequency unchanged:** `freq_step_median_mHz` (per-reading noise, comparable across runs) within ±15% of
  the soft run, and `freq_glitches_gt_20mHz` no higher. The means differ by whatever the grid did between the
  two half-hours. For an absolute check, compare each run with AEMO FPP SA1 (4 s) as on 2026-09-27: the offset
  should stay ≈ −1.9 mHz.
- No `# BOOT` line in the middle of a log (= no reset).

**2.2 Outage test (≥ 25 min).** Normal for 3 min, then every POST refused for 10 min, then normal again.

```bash
mpremote connect auto mount . run scripts/bench_outage.py | tee ~/bench_outage.log     # ≥ 25 min, Ctrl-C
grep -E '# (BOOT_ID|BUFFER|POST_GUARD|WIFI_PM|SLOW_POST|WDT_GUARD)' ~/bench_outage.log
grep -c '# BENCH_OUTAGE refused_post' ~/bench_outage.log                               # ~6-7 (60/120 s backoff)
python3 scripts/bench_gap_report.py --boot <its boot_id>                               # exit status 0
```

Pass:
- The report shows `seq_missing 0` and `seq_duplicates 0`.
- Latest telemetry shows `dropped_total 0`, and `backlog` back under 60 within ~5 min of the outage ending.
- After the outage, POSTs come ~10 s apart with 60 readings each until the backlog is gone.
- No reset.

**2.3 Real Wi-Fi loss (optional, recommended; ~8 min).** During a `bench_normal.py` run, switch the access
point off for 3 min, then back on.

Expect, in order:
- `# WIFI_DOWN`
- `# WIFI_ESCALATE n=1 down_ms=120…` after 2 min. Note how long the `active(False)`/`active(True)` step
  blocks: compare the `t_ms` of the lines around it.
- `# WIFI_UP after_ms=… reconnects=1`
- no seq gaps (gap report), and `wifi_reconnects_total` 1 in telemetry.

**2.4 RAM and sensors** (from any run's log):
- `# BUFFER capacity=3600 storage_bytes=57600 heap_free=…` and the STATUS `heap_free=` (taken after
  `gc.collect()`). **Pass: ≥ 150 KB free at steady state.** Otherwise set `MAX_BUFFERED_READINGS = 2700`
  (45 min) and rerun 2.2.
- `# WIFI_PM requested=0xa11140 before=… after=0xa11140`. `before` is the driver default; PM_PERFORMANCE
  is `0xa11142`.
- STATUS `die_temp_c=` plausible (ambient + a few °C). If it reads nonsense, `ADC.CORE_TEMP` is not channel 4
  on this board: report it. Telemetry tolerates a missing value, so it is not a blocker.
- `# POST_GUARD window_ms=25000 abort_ms=10000` and `# WDT_ARMED requested_ms=8000`.

## 3. Flash Unit 1

From maintenance mode (2.0). Back up exactly the files being replaced, then copy the new ones.

```bash
B=~/tremor-flash-backup-$(date +%Y%m%d)-pre-fw-resilience && mkdir -p $B && cd $B
for f in wifi_unit_client.py wifi_ingest.py http_client.py wdt_support.py; do mpremote fs cp :$f ./$f; done
shasum -a 256 *.py > MANIFEST.txt
cd ~/tremor-merge
mpremote fs cp wifi_unit_client.py wifi_ingest.py http_client.py wdt_support.py wifi_support.py :
mpremote exec "import hashlib,binascii
for f in ('wifi_unit_client.py','wifi_ingest.py','http_client.py','wdt_support.py','wifi_support.py'):
    print(binascii.hexlify(hashlib.sha256(open(f,'rb').read()).digest()).decode(), f)"
shasum -a 256 wifi_unit_client.py wifi_ingest.py http_client.py wdt_support.py wifi_support.py   # must match
```

Unplug, **remove the jumper**, power up (on its normal supply). LED slow blink. Within ~1 min:

```bash
curl -s https://tremorgrid.pythonanywhere.com/api/health | python3 -m json.tool | grep -A25 '"unit-1"'
#   new boot_id in telemetry; rssi_dbm, die_temp_c, backlog (< 60), dropped_total 0, heap_free (>= 150000)
```

The next afternoon, `python3 scripts/bench_gap_report.py --boot <new boot_id> --from <ISO> --to <ISO>`:
`seq_missing 0` through the slow-uplink period, and in telemetry `post_aborts_total` / `slow_posts_total`
(how often the abort was needed) and `dropped_total` (should stay 0 unless an outage lasted > ~58 min).

**Rollback (firmware):** jumper → maintenance; `mpremote fs cp $B/*.py :`; `mpremote fs rm :wifi_support.py`;
remove the jumper; power-cycle. To drop only the hard ADC timer, flash `wifi_unit_client.py` from `fccf4e0`
(`git show fccf4e0:wifi_unit_client.py > /tmp/wuc.py`, then copy it to the board as `wifi_unit_client.py`).
