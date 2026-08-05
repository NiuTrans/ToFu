#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  stop.sh — Stop the Tofu server running from THIS project dir
# ════════════════════════════════════════════════════════════════
#
#  Reads data/.server.lock (written by server.py at startup) to find
#  the running server's PID and hostname, then sends SIGTERM. Falls
#  back to SIGKILL only if the process is still alive after the
#  graceful shutdown window.
#
#  Exit codes:
#    0 — stopped cleanly, or nothing was running
#    1 — refused to act (lock from another host, malformed lock, etc.)
#    2 — had to escalate to SIGKILL
#
#  This script is intentionally narrow: it only kills the server
#  registered in THIS project's lock file. It will not kill an
#  unrelated server.py running elsewhere on the host.
#
#  WATCHDOG INTERLOCK (2026-08-03): deploy/tofu_guard.sh relaunches a
#  dead :15000 within ~15s. Measured: it won the race 9s after our
#  SIGKILL and the fresh instance took the instance lock, so the
#  operator's very next `python server.py` refused to start. We touch
#  data/.tofu_guard_disabled BEFORE the kill so the server STAYS down;
#  re-enable with `bash deploy/tofu_guard.sh --start`.

set -u

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="$BASE_DIR/data/.server.lock"
GRACEFUL_SECS=12   # > server.py hconfig.graceful_timeout (10s)

if [[ ! -f "$LOCK" ]]; then
    echo "No lock file at $LOCK — server is not running from this directory."
    exit 0
fi

entry="$(head -n 1 "$LOCK" 2>/dev/null || true)"
if [[ -z "$entry" ]]; then
    echo "Lock file is empty — server may have crashed mid-startup. Removing stale lock."
    rm -f "$LOCK"
    exit 0
fi

pid="${entry%@*}"
host="${entry#*@}"

if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "Lock file malformed: '$entry' (expected '<pid>@<host>'). Refusing to act." >&2
    exit 1
fi

if [[ "$host" != "$(hostname)" ]]; then
    echo "Lock owned by host '$host' (we are '$(hostname)'). Refusing to kill — wrong machine." >&2
    echo "If you are sure this lock is stale, delete it manually: rm $LOCK" >&2
    exit 1
fi

# Defensive: confirm the PID actually points at our server.py.
# Without this we could kill an unrelated process if the PID was reused.
if ! kill -0 "$pid" 2>/dev/null; then
    echo "PID $pid is not running (stale lock). Removing $LOCK."
    rm -f "$LOCK"
    exit 0
fi

cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
if ! echo "$cmdline" | grep -q 'server\.py'; then
    echo "PID $pid does not look like server.py:" >&2
    echo "  $cmdline" >&2
    echo "Refusing to kill — likely a stale lock with PID reuse." >&2
    exit 1
fi

# Disable the watchdog's auto-relaunch BEFORE the kill, or it
# relaunches between our SIGKILL and the operator's next manual start.
GUARD_FLAG="$BASE_DIR/data/.tofu_guard_disabled"
touch "$GUARD_FLAG"
echo "Watchdog auto-relaunch DISABLED ($GUARD_FLAG) — the server will stay down."
echo "  Re-enable it after your next start: bash deploy/tofu_guard.sh --start"

# A zombie is already dead (its flock is released with the process) —
# only a LIVE process should keep us waiting. `kill -0` alone reports a
# zombie as alive forever when the parent never reaps it (the exact
# 'Failed to kill even with SIGKILL' false alarm); same lesson as
# restart_15000.sh's [2b/5].
_pid_gone() {
    kill -0 "$1" 2>/dev/null || return 0
    local st
    st="$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')"
    case "${st}" in Z*) return 0 ;; esac
    return 1
}

echo "Sending SIGTERM to server.py (PID=$pid)…"
kill "$pid"

for _ in $(seq 1 "$GRACEFUL_SECS"); do
    if _pid_gone "$pid"; then
        echo "Server stopped cleanly."
        exit 0
    fi
    sleep 1
done

echo "Still alive after ${GRACEFUL_SECS}s — escalating to SIGKILL." >&2
kill -9 "$pid" 2>/dev/null || true
sleep 1
if ! _pid_gone "$pid"; then
    echo "Failed to kill PID $pid even with SIGKILL." >&2
    exit 1
fi
echo "Server killed (ungraceful)."
exit 2
