# Export archive: pull daily exports off PythonAnywhere, verified, then free the space

Why: retention exports each closed UTC day to a gzip CSV (~1.6 MB/day) and keeps it forever on the server.
With the database capped by pruning, those files alone would take the account past the 80 % quota warning
in roughly late November / early December 2026. Design (approved 2026-10-12): N = 30 days, second copy
required, deletion only on the server and only after a verified off-server copy exists.

Pieces (branch `export-archive`):
- **Server:** `GET /api/export/manifest` (export token): per day its stored SHA-256, row count, size, prune
  state, attention, deletion time. `python3.9 -m tremor.retention delete-exports --confirm FILE [--yes]`:
  deletes a server copy only if the day is pruned with no attention, ended >= 30 days ago, and the file's
  current SHA-256 equals both the Mac's verified hash and the hash stored at export time (dry run without
  `--yes`). A deleted day's `/api/export/...` answers **410 "archived off-server on <date>"**. The old
  `prune-exports` (deleted by file age, no checks) now refuses.
- **Mac:** `scripts/archive_exports.py --second-copy <dir>`: downloads every exported day, verifies size,
  SHA-256, gzip, CSV header and row count, writes it to `~/tremor-exports` AND the second copy (each
  verified), re-hashes everything already archived on every run, and writes
  `~/tremor-exports/delete_confirm_<date>.txt` listing only the days that are safe to delete. It never
  deletes anything on the server. Stock macOS `python3` is enough (stdlib only).

What is NOT affected by deleting a server export: the History page (aggregates + raw rows, never exports),
retention (it never reads a pruned day's export again), event rows (kept in the database permanently).
What IS: that day's full-resolution raw data then exists ONLY in your two archive copies.

## 1. Deploy the server part (after `export-archive` is merged to master)

Rollback point: the commit PythonAnywhere runs before the deploy (`git rev-parse HEAD` in step 1a).

```bash
cd ~/tremor && git status --short; git rev-parse HEAD        # 1a: clean; note the hash (rollback)
git fetch origin && git merge --ff-only origin/master        # 1b
git diff --stat <rollback hash> HEAD -- src/                 # 1c: retention.py, store.py, webapp.py only
python3.9 -c "import sys; sys.path.insert(0,'src'); import tremor.webapp"      # 1d: prints nothing
```

Web tab → **Reload**; error log clean. Then:

```bash
curl -s https://tremorgrid.pythonanywhere.com/api/health | python3.9 -c "import sys,json; s=json.load(sys.stdin)['store']; print(s['schema_version'], s['days_export_archived_off_server'], s['days_needing_attention'])"
#   expect: 1 0 []     (the new column was added at startup; schema version unchanged)
```

Rollback: `git checkout <rollback hash>`, Reload. The added column is ignored by older code.

## 2. First archive run (Mac)

Primary copy: `~/tremor-exports`. Second copy (chosen 2026-10-12): the iCloud Drive folder
`~/Library/Mobile Documents/com~apple~CloudDocs/TREMOR-archive`, which is on the same disk, so the script
needs `--allow-same-disk` (it relies on iCloud syncing it off the Mac).

**Keep it downloaded.** In Finder, open iCloud Drive, right-click **TREMOR-archive** and choose
**Keep Downloaded**. Otherwise macOS may evict the files under storage pressure: an evicted file is
only a placeholder on this Mac, its content in the cloud. The script checks every second-copy file for that
*before* hashing it. A placeholder is reported as `WARNING ... iCloud placeholder (evicted)` (or
`(.icloud stub)` on older macOS); that day is not verified and not listed for deletion, and the run exits 1.
Fix it with Keep Downloaded and run again.

```bash
mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/TREMOR-archive        # once
cd ~/tremor-merge && python3 scripts/archive_exports.py --allow-same-disk \
    --second-copy ~/Library/Mobile\ Documents/com~apple~CloudDocs/TREMOR-archive
#   Export token (hidden): <type it; nothing is echoed>
```

Expect one `OK` line per day (~6.5 s each: the export rate limit), then
`archived this run: N; verified in both copies: N; problems: 0`. The first run takes a few minutes.
Any `FAIL`/`PROBLEM`/`WARNING` line: stop and send me the output (it never contains the token).

## 3. Deleting server copies (from ~26 Oct: the oldest day, 25 Sep, turns 30 days old)

When a run says `N day(s) ready for deletion -> ~/tremor-exports/delete_confirm_<date>.txt`:
1. Upload that file to `~/tremor/` on PythonAnywhere (Files tab). It holds day names and hashes only.
2. Dry run, then delete:

```bash
cd ~/tremor && python3.9 -m tremor.retention delete-exports --confirm delete_confirm_<date>.txt
#   expect: WOULD DELETE lines, "would delete N, skipped 0"
python3.9 -m tremor.retention delete-exports --confirm delete_confirm_<date>.txt --yes
#   expect: DELETED lines, "deleted N, skipped 0"
```

Any `SKIP` line explains itself (not pruned yet, too young, hash mismatch, ...); nothing is deleted for it.
Check `/api/health`: `exports_bytes` and `used_fraction` drop; `days_needing_attention` stays `[]`.

## 4. Ongoing

Run step 2 every week or two (it only fetches new days and re-verifies the rest), and step 3 whenever a
list is produced. Keep the second copy current (plug the disk in for each run). If a day ever has to go
back on the server: copy the archived file to `~/tremor_data/exports/<unit>/` (same name), and clear its
`export_deleted_at` (ask me -- not needed for anything the server does today).
