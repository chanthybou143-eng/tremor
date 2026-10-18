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

## 2. Archive run (Mac) -- every Sunday, by hand

Primary copy: `~/tremor-exports` (Mac). Second copy (since 2026-10-18): the old ASUS laptop
**tremor-archive** (Ubuntu, always on, lid closed), Samba share `tremor-archive` mounted on the Mac at
`/Volumes/tremor-archive`; the script writes to `/Volumes/tremor-archive/tremor-exports` (on the laptop:
`/srv/tremor-archive/tremor-exports`). It is a separate disk, so no `--allow-same-disk`. (The 2026-10-12
plan was an iCloud Drive folder; it was never used -- the first real run, 2026-10-18, went to the laptop.)

**The weekly routine -- every Sunday, by hand, in Terminal** (one command; no typing, the token comes from
the Keychain, see 6a):

```bash
~/tremor-merge/scripts/archive_weekly.sh
```

It checks the laptop is reachable, mounts the share silently, runs the archiver
(`--token-keychain tremor-export-token`), re-hashes the laptop copy on the laptop's own disk (2c), prints
everything and saves it to `~/tremor-exports/logs/archive_<date>_<time>.log`, and shows one notification.
**It never deletes anything.** Last line / exit status:

| Exit | Notification | Meaning / what to do |
|---|---|---|
| 0 | `TREMOR archive OK` | All verified in both copies + laptop check OK. If it says `N day(s) ready for deletion: upload delete_confirm_<date>.txt (manual step)`, do section 3. |
| 1 | `TREMOR archive: PROBLEM` | `FAIL`/`PROBLEM`/`WARNING` lines or a failed laptop check: stop, send me the log (it never contains the token). Affected days are never listed for deletion. |
| 2 | `TREMOR archive: PROBLEM` | Setup: Keychain item missing/locked, mount failed, or the share's folder not visible. |
| 3 | `TREMOR archive skipped` | Laptop unreachable: turn it on, run again. Also says how long since the last verified run (and shouts after 14 days). |

Fallback without the wrapper (typed token, nothing else needed) -- the original one-liner:

```bash
osascript -e 'mount volume "smb://chanthy@tremor-archive.local/tremor-archive"' >/dev/null && cd ~/tremor-merge && python3 scripts/archive_exports.py --second-copy /Volumes/tremor-archive/tremor-exports
```

Then 2c. The same, step by step (what the wrapper does):

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

Run step 2 every Sunday (`~/tremor-merge/scripts/archive_weekly.sh`; it only fetches new days and
re-verifies the rest), and step 3 whenever a
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
| Firewall | ufw **enabled** 2026-10-18: default deny incoming / allow outgoing; allow TCP 22 and 445 from 192.168.1.0/24 and from fe80::/10 (IPv6 link-local: `.local` can resolve over IPv6); mDNS (UDP 5353) via Ubuntu's stock `before.rules`. Verified from the Mac: 22 and 445 reachable, 139 (smbd listens) blocked | `sudo ufw status verbose` |

SSH, no-sleep, lid, Wi-Fi power save and the battery limit were proven to survive a reboot with the lid
closed and nobody logged in (Wi-Fi credentials are stored system-wide). Samba (`smbd` enabled) and ufw were
added after those reboots: after the next reboot, remount (2a) and check `sudo ufw status` to confirm. Quick health
check from the Mac:

```bash
ssh tremor-archive 'uptime -p; iw dev wlp2s0 get power_save; cat /sys/class/power_supply/BAT0/charge_control_end_threshold; systemctl is-active smbd; df -h /srv | tail -1'
#   expect: up ..., Power save: off, 80, active, plenty free
```

If the power fails for longer than the battery lasts, the laptop stays off afterwards: press its power button.

## 6. Keychain token (for the weekly command) and the optional launchd schedule (NOT installed)

The weekly command (section 2) reads the export token from the login Keychain inside python
(`archive_exports.py --token-keychain tremor-export-token`); the wrapper itself never sees it. It runs from
`~/tremor-merge`, which stays on `master` (`git pull` it after archiver changes).

**6a. Token into the login Keychain** (done 2026-10-18; redo only if the token changes). `-w` last = hidden prompt
(asked twice); `-T /usr/bin/security` lets the archiver read it without a dialog; `-U` updates an existing item:

```bash
security add-generic-password -s tremor-export-token -a "$USER" -T /usr/bin/security -U -w
security find-generic-password -s tremor-export-token >/dev/null && echo "Keychain item: present"
```

Never add `-w` or `-g` to that `find-generic-password` (they print the token). Prove the item works with
a normal archive run that reads it:

```bash
osascript -e 'mount volume "smb://chanthy@tremor-archive.local/tremor-archive"' >/dev/null && cd ~/tremor-merge \
  && /usr/bin/python3 scripts/archive_exports.py --token-keychain tremor-export-token \
     --second-copy /Volumes/tremor-archive/tremor-exports
#   expect: no prompt; "archived this run: ...; problems: 0"
```

**6b. Optional: a launchd schedule -- NOT installed (decision 2026-10-18: run it by hand instead).**
`deploy/com.tremor.archive-exports.plist` (Sunday 10:00, at the next wake if asleep; `__HOME__` filled in at
install) is kept for reference only. A watched test run on 2026-10-18 showed why it does not work as is:
macOS's privacy control attributes the job's file access to the launchd-started `/bin/bash`, which has no
"Network Volumes" permission and, as a background process, can never be asked -- so reading
`/Volumes/tremor-archive/...` fails with `Operation not permitted` (log: TCC
`kTCCServiceSystemPolicyNetworkVolumes`, responsible `/bin/bash`). From Terminal the same access is
attributed to Terminal, which has it -- hence the manual routine. Making launchd work would need a
dedicated small app (e.g. an `osacompile` applet started with `open -a`) to hold that one permission;
giving `/bin/bash` or python Full Disk Access instead is far too broad. Since that run, an unreadable copy
is reported as `PROBLEM ... unreadable (Operation not permitted)` instead of crashing the archiver.

Check nothing is installed: `ls ~/Library/LaunchAgents | grep -i tremor` (expect nothing) and
`launchctl print gui/$(id -u)/com.tremor.archive-exports` (expect "Bad request" / not found).
