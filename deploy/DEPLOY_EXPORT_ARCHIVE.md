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

## 2. Archive run (Mac) -- every week or two

Primary copy: `~/tremor-exports` (Mac). Second copy (since 2026-10-18): the old ASUS laptop
**tremor-archive** (Ubuntu, always on, lid closed), Samba share `tremor-archive` mounted on the Mac at
`/Volumes/tremor-archive`; the script writes to `/Volumes/tremor-archive/tremor-exports` (on the laptop:
`/srv/tremor-archive/tremor-exports`). It is a separate disk, so no `--allow-same-disk`. (The 2026-10-12
plan was an iCloud Drive folder; it was never used -- the first real run, 2026-10-18, went to the laptop.)

**The routine in one line** (mount, then archive; the archiver only starts if the mount succeeded):

```bash
osascript -e 'mount volume "smb://chanthy@tremor-archive.local/tremor-archive"' >/dev/null && cd ~/tremor-merge && python3 scripts/archive_exports.py --second-copy /Volumes/tremor-archive/tremor-exports
```

Then 2c. The same, step by step:

**2a. Mount the share** -- silent, password from the Keychain (tested 2026-10-18 after a fresh unmount: no
dialog; already mounted: exit 0, no duplicate mount). Laptop unreachable: expected to exit non-zero
(not yet tested -- try it once with the laptop off):

```bash
osascript -e 'mount volume "smb://chanthy@tremor-archive.local/tremor-archive"' >/dev/null \
  && ls -d /Volumes/tremor-archive/tremor-exports || echo "NOT MOUNTED -- is the laptop on?"
```

**2b. Run the archiver:**

```bash
cd ~/tremor-merge && python3 scripts/archive_exports.py --second-copy /Volumes/tremor-archive/tremor-exports
#   Export token (hidden): <type it; nothing is echoed>
```

Expect one `OK` line per new day (~6.5 s each: the export rate limit), then
`archived this run: N; verified in both copies: M; problems: 0`, where M is every day archived so far
(both copies are re-hashed on every run). Any `FAIL`/`PROBLEM`/`WARNING` line: stop and send me the output
(it never contains the token).

**2c. Check the laptop copy on the laptop's own disk** (independent of the Mac's SMB cache; SSH key login):

```bash
grep -v '^#' ~/tremor-exports/MANIFEST.tsv \
  | awk -F'\t' '{printf "%s  %s/readings_%s_%s.csv.gz\n", $3, $1, $1, $2}' \
  | ssh tremor-archive 'cd /srv/tremor-archive/tremor-exports && sha256sum -c --strict --quiet - && echo "laptop copy: all OK"'
```

Prints only mismatches, then `laptop copy: all OK`. First verified 2026-10-18: 22/22 days (25 Sep - 16 Oct).

**If the laptop is off or unreachable** -- always safe, nothing is deleted anywhere:
- not mounted: 2a prints `NOT MOUNTED` (the one-liner stops before the token prompt); if run anyway, 2b refuses (`second copy ... is not an existing
  directory`, exit 2) before downloading anything, and writes no deletion list. Turn the laptop on, rerun.
- drops out mid-run (or a stale mount): the run hangs up to ~1 min on the SMB timeout, then errors out. The
  day in flight is not recorded in `MANIFEST.tsv`, so the next run fetches and verifies it again; a stray
  `.partial` file is overwritten. Unmount a stale share with `diskutil unmount force /Volumes/tremor-archive`.
- the deletion list only ever contains days verified in BOTH copies in that same run.

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
list is produced. Keep the second copy current (laptop on and the share mounted for each run). If a day
ever has to go back on the server: copy the archived file to `~/tremor_data/exports/<unit>/` (same name),
and clear its `export_deleted_at` (ask me -- not needed for anything the server does today).

## 5. The archive laptop (tremor-archive) -- set up 2026-10-18

Old ASUS, Ubuntu 26.04.1 LTS desktop, user `chanthy`, 1 TB HDD, Wi-Fi only (DHCP; reached by name
`tremor-archive.local` via mDNS -- a DHCP reservation for 192.168.1.113 on the router is optional backup).
Mac `~/.ssh/config` host `tremor-archive` (HostName `tremor-archive.local`, `HostKeyAlias tremor-archive`,
key `~/.ssh/id_ed25519`, passphrase in the Keychain). Host key ED25519
`SHA256:yQxZT/d8a1dLfKZxRrikJLuF3Ob36bJa0GTlecay+w0` (checked at the laptop console).

| What | How | File / unit |
|---|---|---|
| SSH key-only | password + keyboard-interactive off, no root login | `/etc/ssh/sshd_config.d/00-tremor-keys-only.conf` (`00-`: sshd takes the FIRST value) |
| Never sleeps | sleep/suspend/hibernate/hybrid/suspend-then-hibernate targets masked | `systemctl mask ...` |
| Lid closed OK | `HandleLidSwitch*=ignore`, `IdleAction=ignore` | `/etc/systemd/logind.conf.d/50-tremor-lid.conf` |
| Wi-Fi power save off | `wifi.powersave = 2` | `/etc/NetworkManager/conf.d/zz-tremor-wifi-powersave-off.conf` (`zz-`: NM takes the LAST file, must sort after Ubuntu's `default-wifi-powersave-on.conf`) |
| Security updates | unattended-upgrades (stock), auto-reboot 04:00 when needed | `/etc/apt/apt.conf.d/52tremor-unattended` |
| Battery limit 80 % | re-applied every boot (ASUS `charge_control_end_threshold`) | `tremor-battery-limit.service` |
| Samba share | SMB3 only, encryption required, user `chanthy` only, NetBIOS off (`nmbd` disabled) | `/etc/samba/smb.conf` (stock kept as `smb.conf.orig`) |
| Firewall | **ufw is OFF** (checked 2026-10-18: `Status: inactive`; Ubuntu desktop default). A rule for TCP 445 from 192.168.1.0/24 is stored but not enforced. To turn it on: allow SSH from 192.168.1.0/24, default deny incoming, `ufw enable` -- keep an SSH session open while doing it | `sudo ufw status verbose` |

SSH, no-sleep, lid, Wi-Fi power save and the battery limit were proven to survive a reboot with the lid
closed and nobody logged in (Wi-Fi credentials are stored system-wide). Samba (`smbd` enabled) was added
after those reboots: after the next reboot, remount (2a) to confirm. Quick health
check from the Mac:

```bash
ssh tremor-archive 'uptime -p; iw dev wlp2s0 get power_save; cat /sys/class/power_supply/BAT0/charge_control_end_threshold; systemctl is-active smbd; df -h /srv | tail -1'
#   expect: up ..., Power save: off, 80, active, plenty free
```

If the power fails for longer than the battery lasts, the laptop stays off afterwards: press its power button.
