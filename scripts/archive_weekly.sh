#!/bin/bash
# Unattended export archive run (weekly, from launchd: deploy/com.tremor.archive-exports.plist).
#
#   1. is the archive laptop reachable?  no -> log + notify + stop (nothing downloaded, nothing written)
#   2. mount its Samba share silently (password from the Keychain)
#   3. scripts/archive_exports.py --token-keychain ... (token read from the Keychain INSIDE python; this
#      script never touches it, so it cannot reach a log, a command line or the environment)
#   4. re-hash the laptop copy on the laptop's own disk over SSH (BatchMode: never prompts)
#   5. log + one notification with the outcome
#
# It NEVER deletes anything. A deletion list, if the archiver writes one, is only reported: uploading it
# and running delete-exports on PythonAnywhere stays a manual step (deploy/DEPLOY_EXPORT_ARCHIVE.md).
#
# Exit: 0 all verified; 1 archiver or laptop-check problems; 2 setup error (token, mount); 3 laptop unreachable.
# Every setting can be overridden by an environment variable (the tests use that to stub commands).

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${TREMOR_ARCHIVE_DEST:-$HOME/tremor-exports}"
LAPTOP_HOST="${TREMOR_LAPTOP_HOST:-tremor-archive.local}"
SHARE_URL="${TREMOR_SHARE_URL:-smb://chanthy@tremor-archive.local/tremor-archive}"
SECOND="${TREMOR_SECOND_COPY:-/Volumes/tremor-archive/tremor-exports}"
LAPTOP_SSH="${TREMOR_LAPTOP_SSH:-tremor-archive}"
LAPTOP_DIR="${TREMOR_LAPTOP_DIR:-/srv/tremor-archive/tremor-exports}"
KEYCHAIN_SERVICE="${TREMOR_TOKEN_KEYCHAIN:-tremor-export-token}"
PYTHON="${TREMOR_PYTHON:-/usr/bin/python3}"
STALE_DAYS="${TREMOR_STALE_DAYS:-14}"

LOGDIR="$DEST/logs"
mkdir -p "$LOGDIR" || exit 2
LOG="$LOGDIR/archive_$(date +%Y-%m-%d_%H%M%S).log"
LAST_OK="$LOGDIR/last_success"
exec >>"$LOG" 2>&1

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

notify() {  # title, message -- passed as argv, so no quoting/injection issues inside AppleScript
    say "NOTIFY: $1 -- $2"
    osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
        -e 'end run' "$1" "$2" || say "(notification could not be shown)"
}

since_last_ok() {  # " Last fully verified run: N days ago." (or never)
    if [ -f "$LAST_OK" ]; then
        local age=$(( ( $(date +%s) - $(stat -f %m "$LAST_OK" 2>/dev/null || stat -c %Y "$LAST_OK") ) / 86400 ))
        if [ "$age" -ge "$STALE_DAYS" ]; then
            echo " No verified archive for $age days!"
        else
            echo " Last verified run: $age day(s) ago."
        fi
    else
        echo " No verified run recorded yet."
    fi
}

fail() {  # exit code, title, message
    notify "$2" "$3$(since_last_ok) Log: $(basename "$LOG")"
    say "exit $1"
    exit "$1"
}

say "start: repo $REPO ($(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')), dest $DEST, second $SECOND"

# 1. reachable? (SMB port, 5 s) -- checked first so an off laptop never triggers a Finder error dialog
if ! nc -z -G 5 "$LAPTOP_HOST" 445 >/dev/null 2>&1; then
    fail 3 "TREMOR archive skipped" "Archive laptop $LAPTOP_HOST unreachable -- nothing was archived."
fi

# 2. mount (no-op if already mounted)
if ! osascript -e 'on run argv' -e 'mount volume (item 1 of argv)' -e 'end run' "$SHARE_URL" >/dev/null; then
    fail 2 "TREMOR archive: PROBLEM" "Could not mount $SHARE_URL."
fi
if [ ! -d "$SECOND" ]; then
    fail 2 "TREMOR archive: PROBLEM" "$SECOND is not there after mounting (macOS privacy block on network volumes?)."
fi

# 3. archive (stdin closed: nothing can ever wait for input)
say "archiver:"
out="$(cd "$REPO" && "$PYTHON" scripts/archive_exports.py --token-keychain "$KEYCHAIN_SERVICE" \
        --dest "$DEST" --second-copy "$SECOND" </dev/null 2>&1)"
rc=$?
echo "$out"
say "archiver exit $rc"
summary="$(echo "$out" | grep -E '^archived this run:' | tail -1)"
ready="$(echo "$out" | grep -E 'day\(s\) ready for deletion' | tail -1)"
if [ "$rc" -eq 2 ] || [ -z "$summary" ]; then
    fail 2 "TREMOR archive: PROBLEM" "The archiver stopped (exit $rc): $(echo "$out" | tail -1)"
fi

# 4. laptop-side check (independent of the Mac's SMB cache)
say "laptop check:"
check="$(grep -v '^#' "$DEST/MANIFEST.tsv" \
    | awk -F'\t' '{printf "%s  %s/readings_%s_%s.csv.gz\n", $3, $1, $1, $2}' \
    | ssh -o BatchMode=yes -o ConnectTimeout=15 "$LAPTOP_SSH" \
        "cd '$LAPTOP_DIR' && sha256sum -c --strict --quiet - && echo 'laptop copy: all OK'" 2>&1)"
crc=$?
echo "$check"
say "laptop check exit $crc"

# 5. outcome
if [ "$rc" -ne 0 ] || [ "$crc" -ne 0 ] || ! echo "$check" | grep -q '^laptop copy: all OK$'; then
    what="archiver exit $rc"
    [ "$crc" -ne 0 ] && what="$what, laptop check FAILED (exit $crc)"
    fail 1 "TREMOR archive: PROBLEM" "$what. $summary."
fi

touch "$LAST_OK"
msg="${summary#archived this run: }; laptop check OK."
if [ -n "$ready" ]; then
    n="${ready%% day(s)*}"
    msg="$msg $n day(s) ready for deletion: upload $(basename "${ready##*-> }") (manual step)."
fi
notify "TREMOR archive OK" "$msg"
say "exit 0"
exit 0
