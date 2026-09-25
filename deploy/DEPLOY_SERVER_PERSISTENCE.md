# Deploying the persistent server to PythonAnywhere

**Server-only.** Unit 1 keeps sending today's (v1) payload throughout, with no token, and nothing on the Pico
changes. The new server accepts that unchanged. The v2 client (boot_id / seq / integer time / token) is a
separate, later flash. Replace `<username>` (from `https://<username>.pythonanywhere.com`) and `<project>` (the
directory the app is cloned into) below.

Do the steps in order. Every step has a check; stop at the first one that doesn't match.

## 0. Start the Unit 1 soak, so live legacy data is flowing

You want real legacy traffic hitting the server while you deploy, so the smoke test in step 7 proves the new
server works with the *actual* unit and not just with `curl`.

**Run it from a checkout of `master` (f7cd144), NOT from `server-persistence`.** `mpremote run` executes the
local `wifi_unit_client.py`, and the branch contains the v2 client. (It would fail at start anyway, because
the Pico's flash still holds the old `wifi_ingest.py` and cannot import `make_boot_id`; but do not rely on that.)
```bash
git -C /Users/thy/tremor-merge worktree add /tmp/tremor-legacy f7cd144        # a read-only master checkout
cd /tmp/tremor-legacy && ln -s /Users/thy/tremor-merge/.venv .venv
# then, from an interactive Terminal (so Ctrl-C / kill -INT behave normally):
cd /tmp/tremor-legacy && nohup caffeinate -dims .venv/bin/python3 overnight_wifi_log.py > /dev/null 2>&1 &
echo "soak PGID: $(ps -o pgid= -p $!)"          # stop later with:  kill -INT -<PGID>
```
Checks (a minute or two later):
```bash
tail -n 3 /tmp/tremor-legacy/logs/wifi_soak_*.log | cut -c1-200
```
`wifi=True synced=True`, `post_successes` rising by 1 every ~30 s, `dropped=0`. Keep the Mac on AC power with the
lid open (`pmset -g batt`), and leave the soak running through the whole deployment.

## 1. Before you start (5 minutes)

1. **Check your account type** (Account page → creation date). Accounts created on or after 2026-01-15 have *no
   scheduled tasks and no MySQL*. That is fine: retention runs inside the web app in small chunks.
2. **Renew the free web app.** Free web apps expire after 1 month unless you click *Run until 1 month from today*
   on the Web tab. Do it now, and add the **monthly renewal** to your calendar (see step 8): an expired app stops
   accepting Unit 1's POSTs.
3. **Note the state you are replacing** (needed to roll back). In a Bash console:
   ```bash
   cd ~/<project> && git rev-parse HEAD && git status --short | head
   df -h ~ | tail -1; du -sh ~ 2>/dev/null
   ```
   Write the commit hash down. `du -sh ~` is your current disk use out of 512 MiB.
4. **Check the Python and SQLite versions the web app runs on** (Web tab shows the Python version; use the same in
   the console):
   ```bash
   python3 -c "import sqlite3, sys; print(sys.version.split()[0], 'sqlite', sqlite3.sqlite_version)"
   ```
   Need Python ≥ 3.9 and SQLite ≥ 3.24 (upserts). Anything current is fine.

## 2. Back up the current app

```bash
mkdir -p ~/backups
cp -a ~/<project> ~/backups/tremor-before-persistence-$(date +%Y%m%d)
cp /var/www/<username>_pythonanywhere_com_wsgi.py ~/backups/wsgi-before-persistence-$(date +%Y%m%d).py
ls ~/backups
```
Check: both exist. (The copy costs disk; delete it after a week of good running.)

## 3. Get the new code onto the server

**Option A – git (after you have pushed the branch yourself; it is already on origin as a backup):**
```bash
cd ~/<project>
git fetch origin
git checkout server-persistence
git log --oneline -3          # top commit should be the security / SEND_LEGACY_FLOAT commit from this branch
```
**Option B – no git needed:** on your Mac, in `tremor-merge`:
`git archive --format=zip -o /tmp/tremor-server-persistence.zip server-persistence`, upload it with the
PythonAnywhere **Files** page, then in a console: `cd ~ && unzip -o tremor-server-persistence.zip -d ~/<project>`.

**No new dependencies:** SQLite is part of Python, Flask and numpy are already installed. (Sanity check:
`cd ~/<project> && python -c "import sys; sys.path.insert(0,'src'); import tremor.webapp"` prints nothing.)

## 4. Measure the database on *this* disk (do not skip)

PythonAnywhere's disk is a network filesystem. The device gives every request **4 s** to answer, so a slow commit
would turn into failed POSTs. This writes to a scratch file and deletes it; it never touches real data:
```bash
cd ~/<project> && python scripts/measure_db_latency.py --dir ~/tremor_data
```
Expect `VERDICT: OK` (p99 under 1000 ms; on a laptop it is about 1 ms). If it says **WARN** or **FAIL**, stop and
tell me: options are `TREMOR_SQLITE_SYNCHRONOUS=NORMAL` or a different backend (`store.py` is behind an interface
for exactly this reason).

## 5. Configure: storage, tokens, access control

**a) Generate two tokens on your Mac** (one for Unit 1's ingest, one for exports). Keep them in a password
manager; never paste them into chat, a screenshot, or the repo:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # run twice
```
Unit 1's ingest token will later go into the Pico's `wifi_config.py` as `INGEST_TOKEN` (flash session).

**b) Edit the WSGI file** (Web tab → *WSGI configuration file*; reference copy with placeholders in
`deploy/pythonanywhere_wsgi.py`). Add, **above** the line `from tremor.webapp import create_app`:
```python
import os
os.environ.setdefault("TREMOR_DB_PATH", "/home/<username>/tremor_data/readings.db")
os.environ.setdefault("TREMOR_QUOTA_ROOT", "/home/<username>")
os.environ.setdefault("TREMOR_QUOTA_MB", "512")
# access control -- REAL VALUES ONLY IN THIS FILE ON PYTHONANYWHERE
os.environ["TREMOR_INGEST_TOKENS"] = "unit-1=<ingest token>"
os.environ["TREMOR_INGEST_AUTH"] = "optional"      # phase 1: legacy Unit 1 cannot send a token yet (see step 9)
os.environ["TREMOR_EXPORT_TOKEN"] = "<export token>"
```
`optional` means: a request **without** a token is accepted (and counted), a request with a **wrong** token is
rejected with 401. `/api/export` is unusable without the export token, `/api/history` is rate limited to
30 requests/minute per client, and `/api/health` stays public. A bad setting (token under 16 characters, an
unknown mode) stops the app from starting, so you will see it in the error log at step 6, not silently later.

**c)** Then: `mkdir -p ~/tremor_data && chmod 700 ~/tremor_data`. The database and `exports/` folder are created
automatically on the first request.

## 6. Reload and read the error log

Web tab → **Reload**. Open the **error log** link and look at the newest lines.
Check: no traceback. (`TREMOR_DB_PATH not set` means step 5b was not saved; a `ConfigError` names the bad setting.)

## 7. Smoke test with the live legacy Unit 1 (step 0's soak still running)

From your Mac. Replace the host with your app's:
```bash
H=https://<username>.pythonanywhere.com
curl -s $H/api/health | python3 -m json.tool | head -60
```
Check: `"status": "ok"`, `store.backend` = `sqlite`, `db_bytes` > 0, `ingest_auth.mode` = `optional`,
`export.enabled` = `true`.

Wait about 90 seconds for two or three of Unit 1's POSTs, then:
```bash
curl -s $H/api/units | python3 -c "import sys,json; u=json.load(sys.stdin)[0]; print({k:u[k] for k in ('id','status','freq_hz','gps_locked','gps_utc','unlocked_count','duplicates_ignored','samples_per_minute','completeness_pct')}); print('history points:', len(u['history']))"
curl -s "$H/api/history?unit=unit-1&limit=3" | python3 -m json.tool | head -40
curl -s $H/api/health | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)['ingest_auth'], indent=1))"
```
Check all of:
- `status` is `live`; `freq_hz` ≈ 50.0; `gps_locked` is `true`.
- `gps_utc` is within a minute or two of the current UTC time as Unix seconds (`date -u +%s`).
- `history points` is about 55–60 and `completeness_pct` is about 95% (median 95%, range 85–100% in the local
  replay: about 5% of seconds legitimately have no reading at 0.944/s; what changes is that the 30–60 s dips
  after a failed POST are gone).
- `/api/history` rows show `time_src: 1` (legacy float32 time) and `boot_id: null`.
- `ingest_auth`: `missing_accepted` rises by about 2 per minute (that is legacy Unit 1 posting without a token),
  `authenticated` is 0, `rejected_wrong` is 0, and `units_seen_without_token` is `["unit-1"]`.
- The dashboard in the browser draws the chart and the frequency number.
- In the **access log** (Web tab), `POST /api/ingest` shows `202`, not 4xx/5xx.
- The soak from step 0 still shows `post_successes` rising and `dropped=0`.

**Access-control probes** (none of these store anything; do NOT probe with a missing token, since `optional`
mode would accept it and store a reading):
```bash
# a wrong token is refused -> 401
curl -s -o /dev/null -w "wrong ingest token -> %{http_code}\n" -X POST $H/api/ingest -H 'Content-Type: application/json' \
     -H 'X-Tremor-Token: wrong-token-0123456789' -d '{"unit_id":"unit-1","readings":[{"frequency_hz":50}]}'
# export needs its own token -> 401 without, 401 with the ingest token, otherwise 404 (no such day yet) or 200
curl -s -o /dev/null -w "export, no token     -> %{http_code}\n" $H/api/export/unit-1/2026-09-01
curl -s -o /dev/null -w "export, export token -> %{http_code}\n" -H "X-Tremor-Token: <export token>" $H/api/export/unit-1/2026-09-01
# /api/history is public but rate limited: expect the last few of 35 rapid requests to be 429
for i in $(seq 1 35); do curl -s -o /dev/null -w "%{http_code} " "$H/api/history?unit=unit-1&limit=1"; done; echo
```
**Rate limiting is per client address, so check the server sees YOUR address, not the proxy's:**
```bash
curl -s $H/api/health | python3 -c "import sys,json; print(json.load(sys.stdin)['history_rate_limit'])"; curl -s ifconfig.me; echo
```
`your_address_as_seen` should equal your public IP (the second line). If it shows a private/constant address, every
visitor shares one bucket (the limit still protects the worker, but it is then a global limit). Set
`os.environ["TREMOR_CLIENT_IP_HEADER"] = "X-Real-IP"` in the WSGI file, reload, and re-check (try
`X-Forwarded-For` if that is not it). I have not verified which header PythonAnywhere uses.

Then leave it 10 minutes and re-run the health check: `store.raw_rows` should have grown by about 570 per 10
minutes (0.944 readings/s) and `days_needing_attention` should be empty.

## 8. What to watch over the next days

- **Monthly: renew the web app.** Web tab → *Run until 1 month from today*. PythonAnywhere emails a reminder, but
  put it in your calendar too (say every 3 weeks). An expired free app returns errors and Unit 1's POSTs fail.
- `curl -s $H/api/health` — `storage.used_fraction` (warning at 0.8), `store.days_needing_attention`, and
  `ingest_auth.rejected_wrong` (should stay 0; a rise means something is posting with a bad token).
- **Day 1–2:** yesterday's export should appear: `ls -l ~/tremor_data/exports/unit-1/`. Pull it to the Mac with the
  export token: `curl -H "X-Tremor-Token: <export token>" -O $H/api/export/unit-1/2026-09-25` (date = the UTC
  day). Do this daily; the server keeps exports until you remove them
  (`PYTHONPATH=~/<project>/src python -m tremor.retention --db ~/tremor_data/readings.db prune-exports --older-than-days N --confirm-downloaded`).
- **Day 14+:** raw rows older than 14 days are pruned automatically, only after export + aggregates verify.
  `PYTHONPATH=~/<project>/src python -m tremor.retention --db ~/tremor_data/readings.db status` lists each day's state;
  a day marked `attention` is never deleted — send me its message.
- Disk use should settle near 14 days × ≈12 MB ≈ 170 MB for one unit.

## 9. Phase 2 — require tokens (only AFTER the v2 client is flashed with its token)

Do not do this early: a unit that cannot send a token would be locked out (its readings stay buffered on the
Pico, up to about 10 minutes, then start dropping). The safe order:
1. Flash the v2 client with `INGEST_TOKEN` in the Pico's `wifi_config.py` (see the flash-session checklist in
   `CLAUDE.md`).
2. Watch `curl -s $H/api/health`: `ingest_auth.authenticated` rises; `missing_accepted` **stops rising**;
   `units_seen_without_token` no longer updates for `unit-1`.
3. Only then change the WSGI file to `os.environ["TREMOR_INGEST_AUTH"] = "required"` and reload.
4. Probe: a request with no token now returns 401 (it stores nothing); Unit 1 keeps authenticating.

## 10. Roll back (any time)

The database is only ever *added to* by the new code, so rolling back loses nothing already stored:
```bash
cd ~/<project> && git checkout <the commit hash from step 1>      # or: rsync -a ~/backups/tremor-before-persistence-*/ ~/<project>/
cp ~/backups/wsgi-before-persistence-*.py /var/www/<username>_pythonanywhere_com_wsgi.py
```
Web tab → **Reload**. The old app ignores the new database (leave `~/tremor_data` in place; it can be rolled forward
again later). While rolled back, readings are not persisted (the old behaviour) and the dashboard has the old
30–60 s dips.

**Before the reflash**, Unit 1 is unaffected either way: it sends the same legacy payload.
**After the reflash**, a pre-v2 server ignores the new fields (it accepts the batch with 202) but reads a
reading's time *only* from the float `gps_utc_s`, which the v2 client no longer sends by default
(`SEND_LEGACY_FLOAT = False`). A rolled-back old server would therefore see every reading as "no GPS". If you
want rollback to stay fully usable after the reflash, flash with `SEND_LEGACY_FLOAT = True` (costs about 24 bytes
per reading: a 60-reading POST is about 8.4 KB instead of 6.8 KB).

## 11. Later (separate session, needs your approval)

Flash the v2 client (boot_id, per-reading seq, integer GPS time, token). Order matters: **server first (this
document), client second.** With v2 the server dedupes retried batches exactly and stores times exact to the
microsecond with the device's own date. Run the hardware checks in `CLAUDE.md` → *Flash-session checklist*.

## Known limits to be aware of

- **Public data:** `/api/history` and `/api/health` are readable by anyone with the URL, like the dashboard already
  is (`/api/history` is capped at 10,000 rows per request and rate limited). Ingest is protected by tokens (phase 2
  makes that mandatory), and exports need their own token.
- **SQLite on NFS:** PythonAnywhere itself recommends MySQL/Postgres for high-volume use. One unit posting every
  30 s is a light load and the measurement in step 4 is the test. A lock error answers 503 and the device simply
  retries (no data loss).
- The free plan's single web worker means one request at a time; retention work happens after a response is sent
  and is limited to ≈0.25 s per ingest. Request bodies over 512 KB are refused before parsing.
- The auth counters and rate-limit buckets live in the web worker's memory: they reset on every reload.
