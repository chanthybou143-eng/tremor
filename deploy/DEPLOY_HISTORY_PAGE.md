# Deploying the history page

Adds `/history` and `/api/history/overview` (see CLAUDE.md, "History page"). Read-only: no schema change, no
new environment variables, no change to ingest, retention, the live dashboard's data or the Pico. Server-side
code touched: new read-only store queries, an optional read-only mode of the store (used only by the
benchmark), and the new endpoint. Replace `<username>` (from `https://<username>.pythonanywhere.com`).

Starting point: `~/tremor` is on `master` at `f8c6e77`; the history page (including the Adelaide time-zone
work) is merged into `master` and pushed.

Reloading is safe: the server has run `TREMOR_INGEST_AUTH=required` since 2026-09-27, so the in-memory
auth counters and rate-limit buckets a reload resets no longer gate anything.

Use the same Python version as the web app (Web tab) for every command below, e.g. `python3.10`.

The page needs the IANA time-zone database on the server (Python's `zoneinfo`). Check once:

```bash
python3.10 -c "from zoneinfo import ZoneInfo; from datetime import datetime, timezone; \
print(datetime(2026,10,10,6,50,32,tzinfo=timezone.utc).astimezone(ZoneInfo('Australia/Adelaide')))"
# expect: 2026-10-10 17:20:32+10:30   -- a ZoneInfoNotFoundError means: stop and tell me
```

## 1. Benchmark against the real data (the live app is untouched)

A separate checkout, so `~/tremor` does not change until the numbers are known. Bash console:

```bash
cd ~/tremor && git fetch origin
git worktree add ~/tremor-bench origin/master          # detached checkout of the new code
cd ~/tremor-bench && python3.10 scripts/bench_history.py --db ~/tremor_data/readings.db
```

What it does, in order:

1. Prints the database size and the account's disk use (every file under your home directory, against
   512 MB), and what the use would be with a copy.
2. **If the copy would take use above 75%, it does not copy** and exits telling you to use `--live` (go to 1b).
3. Otherwise copies the database next to the real one (`~/tremor_data/history_bench_copy.db`, same network
   filesystem) with SQLite's online backup in ~1 MB steps: ingest keeps committing meanwhile, and a step that
   races a commit is redone (`restarts` in the output). It then runs an integrity check on the copy.
4. Times every preset range (1 h, 6 h, 24 h, 7 days, 30 days, all) for each unit, cold and warm, on the copy;
   deletes the copy at the end, even on Ctrl-C. If the console is killed instead:
   `rm -f ~/tremor_data/history_bench_copy.db ~/tremor_data/history_bench_copy.db-journal`.

**1b. Only if it refused to copy:** benchmark the live file read-only instead (`mode=ro`: it cannot write).
Each raw read covers at most 6 hours so no statement holds the lock long, but run it at a quiet moment:

```bash
cd ~/tremor-bench && python3.10 scripts/bench_history.py --db ~/tremor_data/readings.db --live
```

Reading the output:

```
range       cold ms  warm ms    KB  longest stmt ms  stmts  sources / histogram
30 days         594       78    74               65    124  1min+raw / raw
...
slowest cold view: 603 ms (the Pico's whole-request deadline is 4000 ms)
VERDICT: OK                        <- local Mac, 141 MB database; PythonAnywhere will be slower
```

- **VERDICT OK** (every cold view < 1 s): go on. **WARN** (< 2 s): acceptable, but tell me the numbers.
  **FAIL**: stop and send me the whole output -- do not deploy.
- **longest stmt ms** is the longest single SQL statement: how long an ingest COMMIT could have to wait behind
  a history view. Anything near 1000 ms: stop and send me the output.
- `(N h pending: budget reached)` on long ranges is expected on a cold cache: the histogram's raw-reading work
  is capped at 0.5 s per request and later requests fill in the rest.

Then remove the bench checkout: `cd ~ && git -C ~/tremor worktree remove ~/tremor-bench`.

(A run uses some of the free account's daily console CPU allowance; one run is small.)

## 2. Update `~/tremor`

```bash
cd ~/tremor
git status --short                   # expect: clean
git rev-parse HEAD                   # expect: f8c6e778d8ae44c97877712c4b655af8b9db190b (rollback point)
git merge --ff-only origin/master
git rev-parse HEAD                   # expect: the master commit you were given
git diff --stat f8c6e77 HEAD -- src/ deploy/   # expect only: history.py, store.py, webapp.py,
                                               # templates/history.html, templates/index.html, this file
python3.10 -c "import sys; sys.path.insert(0,'src'); import tremor.webapp"   # prints nothing
```

## 3. Reload and check

Web tab → **Reload**. Open the **error log**: no new tracebacks.

```bash
S=https://<username>.pythonanywhere.com
curl -s -o /dev/null -w "%{http_code}\n" $S/history                                    # 200
F=$(( $(date +%s) - 8 * 86400 ))
for i in 1 2; do curl -s "$S/api/history/overview?unit=unit-1&from=$F" | python3.10 -c \
  "import sys,json; j=json.load(sys.stdin); h=j['histogram']; print(j['elapsed_ms'], 'ms', j['cache'], \
   'hist complete' if h['complete'] else f\"{h['pending_hours']} h pending\")"; done
```

- The first line: server time in line with the benchmark. The second: `{'hit': True, ...}` (served from the
  server-wide cache) -- unless the histogram was still pending, in which case run the loop again.
- **In a browser** (this is the only check of the page's JavaScript): open `/`, click **history →**, try each
  preset. All five cards must draw (frequency, distribution, RoCoF, coverage, daily table). Expect:
  - every time in Adelaide local time, with ACST (until Sun 4 Oct 2026, 02:00) / ACDT (after) on the axis
    title and in tooltips, and the UTC time in every tooltip; the daily table in Adelaide days by default,
    with a **UTC days** toggle;
  - after this Sunday's changeover, the "Last 24 h" axis goes 01:00 → 03:00 with no gap or overlap in the
    data, and the daily table marks 2026-10-04 as `ACST/ACDT · 23 h day`;
  - the 2026-09-26 dip (06:44-06:58 UTC, ~49.88-49.90 Hz) in the 7-day frequency chart and in the
    distribution's lowest bins, not shaded red;
  - the known-bad boot 398474c3bef237a1 (2026-09-26 03:47-03:53 UTC) under *Excluded data*, 0 readings;
  - plugpack-unplugged readings (the 2026-09-26 toggle tests) as excluded periods. If an unplugged stretch is
    NOT excluded, note its time: its readings had amplitude >= 0.1 V and an in-band frequency.
- **The Pico is unaffected:** `curl -s $S/api/health | python3.10 -m json.tool | grep -A3 '"units"'` --
  `seconds_since_last_reading` stays under ~60 s and `readings_total` keeps rising over a few minutes.

## Rollback

```bash
cd ~/tremor && git checkout f8c6e77
```

Web tab → **Reload**. Nothing to undo in the database or the WSGI file. (`git checkout master` returns to the new
code later.)
