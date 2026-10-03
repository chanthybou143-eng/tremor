# Deploying the history page (`history-page` branch)

Adds `/history` and `/api/history/overview`. Read-only: no schema change, no new environment variables, no
change to ingest, retention, the live dashboard's data or the Pico. The only server-side code paths touched are
two new read-only store queries and the new endpoint. Replace `<username>` (from
`https://<username>.pythonanywhere.com`).

Before you start: the server is on `master` at `f8c6e77`, and the branch has been merged into `master` and pushed
(the deploy pulls `master`, as the server tracks it since 2026-10-03).

**Reloading resets the in-memory ingest-auth counters and rate-limit buckets** (`/api/health` →
`ingest_auth`). If you are in the middle of the >= 24 h observation before `TREMOR_INGEST_AUTH=required`,
either finish it first or restart that observation after the reload.

## 1. Benchmark on PythonAnywhere first (no change to the live app)

Uses a separate checkout so `~/tremor` is untouched until the numbers are known. Bash console:

```bash
cd ~/tremor && git fetch origin
git worktree add ~/tremor-bench origin/master          # detached checkout of the new code
cd ~/tremor-bench && python scripts/bench_history.py --dir ~/tremor_data
```

It builds a scratch `history_probe.db` (~17 MB, deleted afterwards) next to the real database -- same network
filesystem, never the real file -- and times each preset range cold and warm. Expect a table like the local run:

```
range       cold ms  warm ms     KB
last 24 h       257        6     51
30 days         352       82     80     <- local Mac; PythonAnywhere will be several times slower
VERDICT: OK
```

- **OK** (every cold request < 1 s): go on.
- **WARN** (< 2 s): acceptable but note it; a history view can then hold up one ingest POST for that long.
- **FAIL**: stop and send me the output -- do not deploy.

Then remove the bench checkout: `cd ~ && git -C ~/tremor worktree remove ~/tremor-bench`.

(The benchmark uses CPU seconds from the free account's daily console allowance; one run is small.)

## 2. Update `~/tremor`

```bash
cd ~/tremor
git status --short                   # expect: clean
git rev-parse HEAD                   # expect: f8c6e778d8ae44c97877712c4b655af8b9db190b (write it down for rollback)
git merge --ff-only origin/master
git rev-parse HEAD                   # expect: the new master commit
git diff --stat f8c6e77 HEAD -- src/ deploy/   # expect only: history.py, store.py, webapp.py,
                                               # templates/history.html, templates/index.html, this file
python -c "import sys; sys.path.insert(0,'src'); import tremor.webapp"   # prints nothing
```

## 3. Reload and check

Web tab → **Reload**. Open the **error log**: no new tracebacks.

```bash
S=https://<username>.pythonanywhere.com
curl -s -o /dev/null -w "%{http_code}\n" $S/history                                       # 200
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" "$S/api/history/overview?unit=unit-1"   # 200, all data
curl -s "$S/api/history/overview?unit=unit-1&from=$(( $(date +%s) - 86400 ))" | python -c \
  "import sys,json; j=json.load(sys.stdin); print(j['sources'], j['elapsed_ms'], 'ms', len(j['excluded']['periods']), 'excluded')"
```

- `elapsed_ms` is the server's own time for that request; it should be in line with the benchmark.
- In a browser: open `/`, click **history →**, try each preset. Expect the 2026-09-26 known-bad boot listed under
  *Excluded data* (its rows are gone, so 0 readings), and any plugpack-unplugged readings that reached the server
  (the 2026-09-26 toggle tests) as excluded periods -- low amplitude or an impossible frequency. If an unplugged
  stretch is NOT excluded, note its time: its readings had amplitude >= 0.1 V and an in-band frequency, and the
  rules need another look.
- **The Pico is unaffected:** `curl -s $S/api/health | python -m json.tool | grep -A3 '"units"'` --
  `seconds_since_last_reading` stays under ~60 s, and the readings count keeps rising over a few minutes.

## Rollback

```bash
cd ~/tremor && git checkout f8c6e77
```

Web tab → **Reload**. Nothing to undo in the database or the WSGI file. (`git checkout master` returns to the new
code later.)
