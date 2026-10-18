#!/usr/bin/env python3
"""Archive TREMOR's daily export files off PythonAnywhere, verified, before any server copy is deleted.

    python3 scripts/archive_exports.py --second-copy /Volumes/Backup/tremor-exports
    python3 scripts/archive_exports.py --second-copy ~/Library/Mobile\\ Documents/.../tremor-exports --allow-same-disk

For every exported day on the server (GET /api/export/manifest):
  1. download it (GET /api/export/<unit>/<day>) to a .partial file, paced under the export rate limit;
  2. verify: size and SHA-256 equal the manifest, the gzip reads to the end, the CSV header is right and the
     row count equals the manifest -- only then is it renamed into place (--dest, default ~/tremor-exports);
  3. mirror it into --second-copy and verify that copy's SHA-256 too;
  4. record it in <dest>/MANIFEST.tsv.
Every run also re-hashes every archived file in BOTH copies; a changed or missing copy is reported and that
day is never offered for deletion. Finally it writes <dest>/delete_confirm_<date>.txt listing the days whose
server copy may go: verified in both copies, pruned on the server (retention never reads its export again),
no 'attention', ended at least --keep-days (30) ago. Deleting is a separate, deliberate step ON THE SERVER:
    python3.9 -m tremor.retention delete-exports --confirm delete_confirm_<date>.txt          (dry run)
    python3.9 -m tremor.retention delete-exports --confirm delete_confirm_<date>.txt --yes
This script never deletes anything on the server.

The export token is typed at a hidden prompt (or, for the unattended weekly run, read from the macOS login
Keychain with --token-keychain SERVICE: see scripts/archive_weekly.sh), sent only as the X-Tremor-Token header
over HTTPS, and never printed, logged, written to disk, put on a command line or in the environment; errors
report the HTTP status only.
Once a server copy is deleted and its day pruned, these two copies are the only full-resolution record of
that day (the server keeps 1-minute aggregates and event rows) -- hence the required second copy.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tremor.retention import EXPORT_HEADER, EXPORT_KEEP_DAYS  # noqa: E402

DEFAULT_BASE = "https://tremorgrid.pythonanywhere.com"
PACE_S = 6.5                     # the export endpoints allow 10 requests per 60 s per client


class FetchError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def https_fetcher(base, token, pace_s=PACE_S):
    """GET base+path with the token header; returns bytes. Paces requests and retries a 429."""
    last = [0.0]

    def fetch(path):
        for _attempt in range(5):
            wait = last[0] + pace_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            req = urllib.request.Request(base + path, headers={"X-Tremor-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(float(e.headers.get("Retry-After") or 30))
                    continue
                raise FetchError(e.code) from None          # status only: never the request (it has the token)
            except urllib.error.URLError as e:
                raise FetchError(f"network error ({type(e.reason).__name__})") from None
        raise FetchError(429)
    return fetch


class TokenError(Exception):
    """The token could not be obtained. The message never contains the token."""


def keychain_token(service, run=None):
    """The export token from the macOS login Keychain (a generic password with this service name), read by
    /usr/bin/security straight into memory: its stdout is captured, never printed, and the token is never put
    on a command line or in the environment. Raises TokenError (exit status only, no output) on failure."""
    run = run or subprocess.run
    try:
        r = run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TokenError(f"Keychain: could not run /usr/bin/security ({type(e).__name__})") from None
    if r.returncode != 0:
        raise TokenError(f"Keychain: no readable item for service '{service}' (security exit {r.returncode}; "
                         "44 = no such item, otherwise the Keychain is locked or access was denied)")
    token = r.stdout.strip()
    if not token:
        raise TokenError(f"Keychain: the item for service '{service}' is empty")
    return token


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def check_export_bytes(data, want):
    """Problems with a downloaded export vs its manifest entry ([] = good)."""
    problems = []
    if want.get("size") is not None and len(data) != want["size"]:
        problems.append(f"size {len(data)} != {want['size']}")
    if hashlib.sha256(data).hexdigest() != want["sha256"]:
        problems.append("SHA-256 differs from the manifest")
        return problems
    try:
        text = gzip.decompress(data).decode()
    except (OSError, EOFError, UnicodeDecodeError) as e:
        return problems + [f"gzip unreadable ({type(e).__name__})"]
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0] != EXPORT_HEADER:
        problems.append("CSV header is not the export header")
    elif len(rows) - 1 != want["rows"]:
        problems.append(f"{len(rows) - 1} rows != {want['rows']}")
    return problems


SF_DATALESS = 0x40000000          # macOS: file content not on disk (iCloud Drive "evicted" / not downloaded)


def icloud_placeholder(p):
    """None if the file's content is really on this disk; otherwise a description. An evicted iCloud Drive
    file is either 'dataless' (same name, content in the cloud only -- reading it would trigger a download,
    or hang offline) or, on older macOS, replaced by a hidden '.<name>.icloud' stub."""
    p = Path(p)
    stub = p.with_name("." + p.name + ".icloud")
    if not p.exists() and stub.exists():
        return "iCloud placeholder (.icloud stub): not downloaded on this Mac"
    if p.exists() and getattr(os.stat(p), "st_flags", 0) & SF_DATALESS:
        return "iCloud placeholder (evicted): not downloaded on this Mac"
    return None


def file_name(unit, day):
    return f"readings_{unit}_{day}.csv.gz"


def load_local_manifest(dest):
    p = dest / "MANIFEST.tsv"
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if line and not line.startswith("#"):
                unit, day, sha, rows, size, when = line.split("\t")
                out[(unit, day)] = dict(sha256=sha, rows=int(rows), size=int(size), archived_at=when)
    return out


def save_local_manifest(dest, man):
    p = dest / "MANIFEST.tsv"
    tmp = p.with_suffix(".tsv.partial")
    with open(tmp, "w") as fh:
        fh.write("# unit\tday\tsha256\trows\tsize\tarchived_at_utc\n")
        for (unit, day), m in sorted(man.items()):
            fh.write(f"{unit}\t{day}\t{m['sha256']}\t{m['rows']}\t{m['size']}\t{m['archived_at']}\n")
    os.replace(tmp, p)


def write_verified(data, target, sha):
    """Write bytes to target via a .partial file, re-read and hash it, then rename into place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".partial")
    with open(part, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if sha256_of(part) != sha:
        part.unlink()
        return False
    os.replace(part, target)
    return True


def archive(fetch, dest, second, keep_days=EXPORT_KEEP_DAYS, allow_same_disk=False, out=print, today=None):
    """One archive run. Returns (exit status, path of the deletion list or None)."""
    dest, second = Path(dest).expanduser(), Path(second).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    if not second.is_dir():
        out(f"second copy {second} is not an existing directory (mount/create it first): refusing to run")
        return 2, None
    if dest.resolve() == second.resolve() or second.resolve().is_relative_to(dest.resolve()) \
            or dest.resolve().is_relative_to(second.resolve()):
        out("--second-copy must be a separate location from --dest: refusing to run")
        return 2, None
    same_disk = os.stat(dest).st_dev == os.stat(second).st_dev
    if same_disk and not allow_same_disk:
        out(f"{second} is on the same disk as {dest}: not a real second copy. Use another disk, or pass "
            "--allow-same-disk if it is a cloud-synced folder (iCloud Drive, Dropbox).")
        return 2, None

    try:
        manifest = json.loads(fetch("/api/export/manifest"))
    except FetchError as e:
        out(f"manifest: {e} (401 = wrong token, 403 = exports disabled on the server)")
        return 2, None
    server_now = manifest.get("server_time") or time.time()
    local = load_local_manifest(dest)
    problems = 0
    fetched = 0

    for unit, days in sorted(manifest["units"].items()):
        for d in days:
            day, key = d["day"], (unit, d["day"])
            p1, p2 = dest / unit / file_name(unit, day), second / unit / file_name(unit, day)
            have = key in local and p1.exists() and (p2.exists() or icloud_placeholder(p2) is not None)
            if have and local[key]["sha256"] != d["sha256"]:
                out(f"WARNING {unit} {day}: archived hash differs from the server's stored hash -- re-archiving")
                have = False
            if have:
                continue
            if not d["file_present"]:
                if key not in local:
                    out(f"WARNING {unit} {day}: server copy already deleted and NOT in this archive")
                    problems += 1
                continue
            try:
                data = fetch(f"/api/export/{unit}/{day}")
            except FetchError as e:
                out(f"FAIL    {unit} {day}: download {e}")
                problems += 1
                continue
            bad = check_export_bytes(data, d)
            if bad:
                out(f"FAIL    {unit} {day}: " + "; ".join(bad))
                problems += 1
                continue
            if not write_verified(data, p1, d["sha256"]) or not write_verified(data, p2, d["sha256"]):
                out(f"FAIL    {unit} {day}: local write did not verify")
                problems += 1
                continue
            local[key] = dict(sha256=d["sha256"], rows=d["rows"], size=len(data),
                              archived_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            save_local_manifest(dest, local)
            fetched += 1
            out(f"OK      {unit} {day}: {d['rows']} rows, {len(data)} B, both copies verified")

    # re-verify EVERY archived file in both copies, every run
    verified = set()
    for (unit, day), m in sorted(local.items()):
        ok = True
        for base, name in ((dest, "archive"), (second, "second copy")):
            p = base / unit / file_name(unit, day)
            ph = icloud_placeholder(p)
            if ph:                                    # never hash it: that would silently download, or hang
                out(f"WARNING {unit} {day}: {name} is an {ph} ({p}) -- not verified, not listed for deletion. "
                    "In Finder: right-click the folder > Keep Downloaded.")
                ok = False
            elif not p.exists():
                out(f"PROBLEM {unit} {day}: missing from the {name} ({p})")
                ok = False
            elif sha256_of(p) != m["sha256"]:
                out(f"PROBLEM {unit} {day}: {name} no longer matches its recorded hash ({p})")
                ok = False
        if ok:
            verified.add((unit, day))
        else:
            problems += 1

    # the deletion list
    lines = []
    for unit, days in sorted(manifest["units"].items()):
        for d in days:
            key = (unit, d["day"])
            day_end = (date.fromisoformat(d["day"]).toordinal() - date(1970, 1, 1).toordinal() + 1) * 86400
            if (key in verified and local[key]["sha256"] == d["sha256"] and d["file_present"]
                    and d["pruned_done"] and not d["attention"] and d["export_deleted_at"] is None
                    and day_end <= server_now - keep_days * 86400):
                lines.append(f"{unit} {d['day']} {d['sha256']}")
    out(f"archived this run: {fetched}; verified in both copies: {len(verified)}; problems: {problems}")
    if not lines:
        out("nothing is ready for deletion from the server")
        return (1 if problems else 0), None
    stamp = (today or date.today()).isoformat()
    conf = dest / f"delete_confirm_{stamp}.txt"
    conf.write_text("# server copies that may be deleted: verified in both archive copies, pruned on the server,\n"
                    f"# no attention, older than {keep_days} days. Upload, then on PythonAnywhere:\n"
                    f"#   python3.9 -m tremor.retention delete-exports --confirm {conf.name}        (dry run)\n"
                    f"#   python3.9 -m tremor.retention delete-exports --confirm {conf.name} --yes\n"
                    + "\n".join(lines) + "\n")
    out(f"{len(lines)} day(s) ready for deletion -> {conf}")
    return (1 if problems else 0), conf


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--second-copy", required=True, help="a second directory, ideally on another disk")
    ap.add_argument("--dest", default="~/tremor-exports")
    ap.add_argument("--keep-days", type=int, default=EXPORT_KEEP_DAYS)
    ap.add_argument("--allow-same-disk", action="store_true",
                    help="accept a second copy on the same disk (only for a cloud-synced folder)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token-keychain", metavar="SERVICE",
                    help="read the token from the macOS Keychain item with this service name instead of prompting "
                         "(unattended runs; never prompts)")
    a = ap.parse_args(argv)
    if a.token_keychain:
        try:
            token = keychain_token(a.token_keychain)
        except TokenError as e:
            print(e)
            return 2
    else:
        token = getpass.getpass("Export token (hidden): ").strip()
        if not token:
            print("no token entered")
            return 2
    rc, _conf = archive(https_fetcher(a.base, token), a.dest, a.second_copy, a.keep_days, a.allow_same_disk)
    return rc


if __name__ == "__main__":
    sys.exit(main())
