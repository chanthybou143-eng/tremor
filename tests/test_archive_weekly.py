"""scripts/archive_weekly.sh (the unattended weekly run) with every external command stubbed on PATH:
nc (reachability), osascript (mount + notification), ssh (laptop-side check) and the archiver itself.
Nothing real is mounted, contacted or read from the Keychain."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "archive_weekly.sh"

SHA = "a" * 64
OK_OUT = "OK      unit-1 2026-10-17: 86400 rows, 1700000 B, both copies verified\n" \
         "archived this run: 1; verified in both copies: 23; problems: 0\n"
READY = "1 day(s) ready for deletion -> /x/tremor-exports/delete_confirm_2026-10-25.txt\n"

STUBS = {
    "nc": 'exit "${STUB_NC_RC:-0}"',
    "osascript": 'echo "$*" >> "$STUB_DIR/osascript.calls"\n'
                 'case "$*" in *"mount volume"*) exit "${STUB_MOUNT_RC:-0}";; esac\n'
                 'printf "%s\\n" "${@: -2:1} | ${@: -1}" >> "$STUB_DIR/notify"',
    "ssh": 'echo "$*" > "$STUB_DIR/ssh.args"; cat > "$STUB_DIR/ssh.stdin"\n'
           'printf "%s" "${STUB_SSH_OUT-laptop copy: all OK\n}"; exit "${STUB_SSH_RC:-0}"',
    "fake_python": 'echo "$*" > "$STUB_DIR/python.args"\n'
                   '[ -t 0 ] && echo "stdin is a tty" > "$STUB_DIR/python.tty"\n'
                   'printf "%s" "$STUB_ARCHIVER_OUT"; exit "${STUB_ARCHIVER_RC:-0}"',
}


@pytest.fixture
def env(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    for name, body in STUBS.items():
        p = stub / name
        p.write_text("#!/bin/bash\n" + body + "\n")
        p.chmod(0o755)
    dest, second = tmp_path / "tremor-exports", tmp_path / "Volumes" / "tremor-archive" / "tremor-exports"
    dest.mkdir()
    second.mkdir(parents=True)
    (dest / "MANIFEST.tsv").write_text("# unit\tday\tsha256\trows\tsize\tarchived_at_utc\n"
                                       f"unit-1\t2026-10-17\t{SHA}\t86400\t1700000\t2026-10-18T00:00:00Z\n")
    e = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin", HOME=str(tmp_path), STUB_DIR=str(stub),
             TREMOR_ARCHIVE_DEST=str(dest), TREMOR_SECOND_COPY=str(second), TREMOR_PYTHON=str(stub / "fake_python"),
             STUB_ARCHIVER_OUT=OK_OUT)
    return dict(env=e, stub=stub, dest=dest, second=second)


def run(env, **extra):
    e = dict(env["env"], **{k: str(v) for k, v in extra.items()})
    rc = subprocess.run(["/bin/bash", str(SCRIPT)], env=e, stdin=subprocess.DEVNULL, timeout=60).returncode
    notes = (env["stub"] / "notify").read_text().splitlines() if (env["stub"] / "notify").exists() else []
    logs = sorted((env["dest"] / "logs").glob("archive_*.log"))
    return rc, notes, logs[-1].read_text() if logs else ""


def called(env, name):
    return (env["stub"] / name).exists()


def test_all_good_archives_checks_the_laptop_and_notifies_ok(env):
    rc, notes, log = run(env)
    assert rc == 0
    assert notes == ["TREMOR archive OK | archived this run: 1; verified in both copies: 23; problems: 0; laptop check OK."]
    args = (env["stub"] / "python.args").read_text().split()
    assert args == ["scripts/archive_exports.py", "--token-keychain", "tremor-export-token",
                    "--dest", str(env["dest"]), "--second-copy", str(env["second"])]
    assert not called(env, "python.tty")                                  # stdin closed: can never prompt
    assert (env["stub"] / "ssh.stdin").read_text() == f"{SHA}  unit-1/readings_unit-1_2026-10-17.csv.gz\n"
    assert "BatchMode=yes" in (env["stub"] / "ssh.args").read_text()
    assert (env["dest"] / "logs" / "last_success").exists()
    assert "archived this run: 1" in log and "laptop copy: all OK" in log and "exit 0" in log


def test_a_deletion_list_is_reported_as_a_manual_step_never_acted_on(env):
    rc, notes, _ = run(env, STUB_ARCHIVER_OUT=OK_OUT + READY)
    assert rc == 0
    assert notes[-1].endswith("laptop check OK. 1 day(s) ready for deletion: upload delete_confirm_2026-10-25.txt "
                              "(manual step).")


def test_laptop_unreachable_skips_everything_and_says_how_stale_the_archive_is(env):
    rc, notes, log = run(env, STUB_NC_RC=1)
    assert rc == 3 and not called(env, "python.args") and not called(env, "ssh.args")
    assert "mount volume" not in (env["stub"] / "osascript.calls").read_text()   # no Finder error dialog
    assert notes[0].startswith("TREMOR archive skipped | Archive laptop tremor-archive.local unreachable")
    assert "No verified run recorded yet." in notes[0]
    last = env["dest"] / "logs" / "last_success"
    last.touch()
    old = time.time() - 20 * 86400
    os.utime(last, (old, old))
    rc, notes, _ = run(env, STUB_NC_RC=1)
    assert rc == 3 and "No verified archive for 20 days!" in notes[-1]


def test_a_failed_mount_or_a_missing_second_copy_stops_before_the_archiver(env):
    rc, notes, _ = run(env, STUB_MOUNT_RC=1)
    assert rc == 2 and notes[-1].startswith("TREMOR archive: PROBLEM | Could not mount smb://")
    rc, notes, _ = run(env, TREMOR_SECOND_COPY=env["second"].parent / "missing")
    assert rc == 2 and "macOS privacy block on network volumes?" in notes[-1]
    assert not called(env, "python.args")


def test_archiver_problems_are_reported_and_not_recorded_as_success(env):
    rc, notes, _ = run(env, STUB_ARCHIVER_RC=1,
                       STUB_ARCHIVER_OUT="PROBLEM unit-1 2026-10-01: missing from the second copy (x)\n"
                                         "archived this run: 0; verified in both copies: 21; problems: 1\n")
    assert rc == 1 and notes[-1].startswith("TREMOR archive: PROBLEM | archiver exit 1. archived this run: 0;")
    assert not (env["dest"] / "logs" / "last_success").exists()


def test_a_keychain_or_token_failure_is_reported_with_the_archivers_own_message(env):
    msg = "Keychain: no readable item for service 'tremor-export-token' (security exit 44; 44 = no such item)"
    rc, notes, _ = run(env, STUB_ARCHIVER_RC=2, STUB_ARCHIVER_OUT=msg + "\n")
    assert rc == 2 and f"The archiver stopped (exit 2): {msg}" in notes[-1]
    assert not called(env, "ssh.args")


def test_a_failed_laptop_side_check_is_a_problem(env):
    rc, notes, log = run(env, STUB_SSH_RC=1,
                         STUB_SSH_OUT="unit-1/readings_unit-1_2026-10-17.csv.gz: FAILED\n"
                                      "sha256sum: WARNING: 1 computed checksum did NOT match\n")
    assert rc == 1 and "laptop check FAILED (exit 1)" in notes[-1]
    assert "FAILED" in log and not (env["dest"] / "logs" / "last_success").exists()


def test_the_wrapper_never_deletes_and_never_handles_the_token_itself():
    code = "\n".join(ln.split("#", 1)[0] for ln in SCRIPT.read_text().splitlines())     # without comments
    assert not re.search(r"\b(rm|unlink|rmdir|delete-exports|diskutil)\b", code)
    assert not re.search(r"security|getpass|TOKEN\b|X-Tremor-Token", code)
