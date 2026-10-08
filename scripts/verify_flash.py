#!/usr/bin/env python3
"""Verify EVERY file on the Pico's flash against a git commit (run from the Mac, unit in maintenance mode):

    python3 scripts/verify_flash.py <commit>        # e.g. the commit that was bench-tested

On the board, every file's SHA-256 is computed -- except wifi_config.py, which is only checked for
presence: it is never opened, read, hashed or printed. Each hash is compared with `git show
<commit>:<file>`. Reported: OK (matches), MISMATCH, NOT_IN_COMMIT (a file on the board the commit does
not have, e.g. a leftover), MISSING (a file the firmware imports that is not on the board).
Exit status 0 only if every board file matches and nothing the firmware needs is missing.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MPREMOTE = str(ROOT / ".venv" / "bin" / "mpremote")
SECRET = "wifi_config.py"

BOARD_CODE = """
import os, hashlib, binascii
for f in sorted(os.listdir('/')):
    p = '/' + f
    if os.stat(p)[0] & 0x4000:
        print('DIR', f)
    elif f == 'wifi_config.py':
        print('PRESENT', f)
    else:
        h = hashlib.sha256()
        with open(p, 'rb') as fh:
            while True:
                b = fh.read(1024)
                if not b:
                    break
                h.update(b)
        print('FILE', f, os.stat(p)[6], binascii.hexlify(h.digest()).decode())
"""


def board_listing():
    out = subprocess.run([MPREMOTE, "connect", "auto", "exec", BOARD_CODE], capture_output=True, text=True,
                         timeout=120)
    if out.returncode != 0:
        # never echo stderr wholesale: keep to the exit status (a traceback could quote a file)
        raise SystemExit(f"mpremote failed (exit {out.returncode}); is the unit in maintenance mode?")
    return out.stdout.splitlines()


def commit_hash(commit, name):
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{name}"], capture_output=True)
    return None if r.returncode != 0 else hashlib.sha256(r.stdout).hexdigest()


def needed_modules(commit):
    """Repo-root modules main.py imports, transitively, at `commit` (what must be on the board)."""
    seen, todo = set(), ["main"]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        r = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{m}.py"], capture_output=True, text=True)
        if r.returncode != 0:
            continue
        seen.add(m)
        for n in ast.walk(ast.parse(r.stdout)):
            if isinstance(n, ast.Import):
                todo += [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                todo.append(n.module.split(".")[0])
    return {f"{m}.py" for m in seen}


def compare(lines, commit_hash_fn, needed):
    """(rows, ok): rows are (status, name, detail)."""
    rows, on_board, ok = [], set(), True
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "PRESENT" and parts[1] == SECRET:
            on_board.add(SECRET)
            rows.append(("PRESENT", SECRET, "not read (device secret)"))
        elif parts[0] == "DIR":
            rows.append(("DIR", parts[1], ""))
        elif parts[0] == "FILE":
            name, size, h = parts[1], parts[2], parts[3]
            on_board.add(name)
            want = commit_hash_fn(name)
            if want is None:
                rows.append(("NOT_IN_COMMIT", name, f"{size} B"))
                ok = False
            elif want == h:
                rows.append(("OK", name, h[:16]))
            else:
                rows.append(("MISMATCH", name, f"board {h[:16]} != commit {want[:16]}"))
                ok = False
    for name in sorted((needed | {SECRET}) - on_board):
        rows.append(("MISSING", name, "imported by the firmware" if name != SECRET else "device config"))
        ok = False
    return rows, ok


def main(argv):
    if len(argv) != 1:
        print(__doc__)
        return 2
    commit = argv[0]
    rows, ok = compare(board_listing(), lambda n: commit_hash(commit, n), needed_modules(commit))
    for status, name, detail in rows:
        print(f"{status:14} {name:24} {detail}")
    print("ALL FILES VERIFIED" if ok else "VERIFICATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
