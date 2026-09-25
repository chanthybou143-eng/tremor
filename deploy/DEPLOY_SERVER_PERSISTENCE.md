# Deploying the persistent server to PythonAnywhere

**Server-only.** Unit 1 keeps sending today's (v1) payload throughout, and nothing on the Pico changes.
The new server accepts the v1 payload unchanged; the v2 client (boot_id / seq / integer time) is a
separate, later flash. Replace `<username>` (from `https://<username>.pythonanywhere.com`) and
`<project>` (the directory the app is cloned into) below.

Do the steps in order. Every step has a check; stop at the first one that doesn't match.

## 0. Before you start (5 minutes)

1. **Check your account type** (Account page → creation date). Accounts created on or after 2026-01-15
   have *no scheduled tasks and no MySQL*. That is fine: retention runs inside the web app in small chunks and
   needs neither.
2. **Free web apps expire after 1 month** unless you click *Run until 1 month from today* on the Web tab. Do it
   now, and set a monthly reminder: an expired app stops accepting Unit 1's POSTs.
3. **Note the state you are replacing** (you will need it to roll back). In a Bash console:
   ```bash
   cd ~/<project> && git rev-parse HEAD && git status --short | head
   df -h ~ | tail -1; du -sh ~ 2>/dev/null
   ```
   Write the commit hash down. `du -sh ~` is your current disk use out of 512 MiB.
4. **Check the Python and SQLite versions the web app runs on** (Web tab shows the Python version; use the same
   in the console):
   ```bash
   python3 -c "import sqlite3, sys; print(sys.version.split()[0], 'sqlite', sqlite3.sqlite_version)"
   ```
   Need Python ≥ 3.9 and SQLite ≥ 3.24 (upserts). Anything current is fine.

## 1. Back up the current app

```bash
mkdir -p ~/backups
cp -a ~/<project> ~/backups/tremor-before-persistence-$(date +%Y%m%d)
cp /var/www/<username>_pythonanywhere_com_wsgi.py ~/backups/wsgi-before-persistence-$(date +%Y%m%d).py
ls ~/backups
```
Check: both exist. (The copy costs disk; delete it after a week of good running.)

## 2. Get the new code onto the server

**Option A – git (after you have pushed the branch yourself):**
```bash
cd ~/<project>
git fetch origin
git checkout server-persistence
git log --oneline -3          # top commit should be the client/CLAUDE.md commit from this branch
```
**Option B – no push needed:** on your Mac, in `tremor-merge`:
`git archive --format=zip -o /tmp/tremor-server-persistence.zip server-persistence`, upload it with the
PythonAnywhere **Files** page, then in a console: `cd ~ && unzip -o tremor-server-persistence.zip -d ~/<project>`.

**No new dependencies:** SQLite is part of Python, Flask and numpy are already installed. (If you want to be
sure: `cd ~/<project> && python -c "import sys; sys.path.insert(0,'src'); import tremor.webapp"` prints nothing.)

## 3. Measure the database on *this* disk (do not skip)

PythonAnywhere's disk is a network filesystem. The device gives every request **4 s** to answer, so a slow
commit would turn into failed POSTs. This writes to a scratch file and deletes it; it never touches real data:
```bash
cd ~/<project> && python scripts/measure_db_latency.py --dir ~/tremor_data
```
Expect `VERDICT: OK` (p99 under 1000 ms; on a laptop it is about 1 ms). If it says **WARN** or **FAIL**, stop and
tell me: options are `TREMOR_SQLITE_SYNCHRONOUS=NORMAL` or a different backend (`store.py` is behind an interface
for exactly this reason).

## 4. Configure

Edit the WSGI file (Web tab → *WSGI configuration file*; reference copy in `deploy/pythonanywhere_wsgi.py`).
Add, **above** the line `from tremor.webapp import create_app`:
```python
import os
os.environ.setdefault("TREMOR_DB_PATH", "/home/<username>/tremor_data/readings.db")
os.environ.setdefault("TREMOR_QUOTA_ROOT", "/home/<username>")
os.environ.setdefault("TREMOR_QUOTA_MB", "512")
```
The database file and the `exports/` folder are created automatically on the first request. Then:
```bash
mkdir -p ~/tremor_data && chmod 700 ~/tremor_data
```

## 5. Reload and read the error log

Web tab → **Reload**. Open the **error log** link and look at the newest lines.
Check: no traceback. (`TREMOR_DB_PATH not set` warnings mean step 4 was not saved.)

## 6. Smoke test with the live Unit 1 still sending v1 payloads

From your Mac (or the browser). Replace the host with your app's:
```bash
H=https://<username>.pythonanywhere.com
curl -s $H/api/health | python3 -m json.tool | head -30
```
Check: `"status": "ok"`, `storage.used_fraction` small, `store.backend` = `sqlite`, and `db_bytes` > 0.

Wait about 90 seconds for two or three of Unit 1's POSTs, then:
```bash
curl -s $H/api/units | python3 -c "import sys,json; u=json.load(sys.stdin)[0]; print({k:u[k] for k in ('id','status','freq_hz','gps_locked','gps_utc','unlocked_count','duplicates_ignored','samples_per_minute','completeness_pct')}); print('history points:', len(u['history']))"
curl -s "$H/api/history?unit=unit-1&limit=3" | python3 -m json.tool | head -40
```
Check all of:
- `status` is `live`; `freq_hz` ≈ 50.0; `gps_locked` is `true`.
- `gps_utc` is within a minute or two of the current UTC time as Unix seconds (`date -u +%s`).
- `history points` is about 55–60, and `completeness_pct` is about 95% (median 95%, range 85–100% in the local
  replay). It looks like the old ~93% because about 5% of seconds legitimately have no reading at 0.944/s;
  what changes is that the 30–60 s dips after a failed POST are gone.
- `/api/history` rows show `time_src: 1` (legacy float32 time) and `boot_id: null`.
- The dashboard in the browser draws the chart and the frequency number.
- In the **access log** (Web tab), `POST /api/ingest` shows `202`, not 4xx/5xx.

Then leave it 10 minutes and re-run the health check: `raw_rows` should have grown by about 570 per 10 minutes
(0.944 readings/s), `days_needing_attention` empty.

## 7. What to watch over the next days

- `curl -s $H/api/health` — `storage.used_fraction` (a warning appears at 0.8), `store.days_needing_attention`.
- **Day 1–2:** yesterday's export should appear: `ls -l ~/tremor_data/exports/unit-1/`. Pull it to the Mac:
  `curl -O $H/api/export/unit-1/2026-09-25` (date = the UTC day). Do this daily; the server keeps exports until
  you remove them (`PYTHONPATH=~/<project>/src python -m tremor.retention --db ~/tremor_data/readings.db prune-exports --older-than-days N --confirm-downloaded`).
- **Day 14+:** raw rows older than 14 days are pruned automatically, only after export + aggregates verify.
  `PYTHONPATH=~/<project>/src python -m tremor.retention --db ~/tremor_data/readings.db status` lists each day's state; a day marked
  `attention` is never deleted — send me its message.
- Disk use should settle near 14 days × ≈12 MB ≈ 170 MB for one unit.

## 8. Roll back (any time)

The database is only ever *added to* by the new code, so rolling back loses nothing already stored:
```bash
cd ~/<project> && git checkout <the commit hash from step 0>      # or: rsync -a ~/backups/tremor-before-persistence-*/ ~/<project>/
cp ~/backups/wsgi-before-persistence-*.py /var/www/<username>_pythonanywhere_com_wsgi.py
```
Web tab → **Reload**. The old app ignores the new database (leave `~/tremor_data` in place; it can be rolled
forward again later). Unit 1 is unaffected either way: the old server accepts the same payloads, including v2
ones (unknown fields are ignored and the v2 client also sends the legacy float time).
While rolled back, readings are not persisted (the old behaviour).

## 9. Later (separate session, needs your approval)

Flash the v2 client (boot_id, per-reading seq, integer GPS time) to the Pico. Order matters: **server first
(this document), client second.** With v2 the server dedupes retried batches exactly and stores times exact to
the microsecond with the device's own date.

## Known limits to be aware of

- **Public data:** `/api/history`, `/api/export` and `/api/health` are readable by anyone with the URL, like the
  dashboard already is. `/api/history` is capped at 10,000 rows per request. If that matters, ask for an
  optional shared-secret check.
- **SQLite on NFS:** PythonAnywhere itself recommends MySQL/Postgres for high-volume use. One unit posting every
  30 s is a light load and the measurement in step 3 is the test. A lock error answers 503 and the device simply
  retries (no data loss).
- The free plan's single web worker means one request at a time; retention work happens after a response is
  sent and is limited to ≈0.25 s per ingest.
