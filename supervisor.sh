#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  supervisor.sh — launch the Tofu process-supervisor daemon
# ════════════════════════════════════════════════════════════════
#
#  The supervisor is the ALWAYS-ON root of the start/stop chain: it must
#  outlive any single Tofu server AND survive its own crashes. Two launch
#  methods, in order of preference:
#
#    1. systemd USER UNIT with Restart=always  → `./supervisor.sh install`
#    2. setsid session-leader + self-healing watchdog + PID file, for hosts
#       WITHOUT a systemd user session          → `./supervisor.sh daemon`
#
#  The `daemon` path is the durable fallback where `systemctl --user` is
#  unavailable (e.g. a cloud-IDE container with no user D-Bus). Unlike a bare
#  `nohup … &`, it (a) uses `setsid` so the watchdog becomes its OWN session
#  leader — fully detached from the launching terminal, so closing that
#  terminal (SIGHUP to the terminal's process group) can NOT reach it — and
#  (b) wraps supervisor.py in a restart loop so a crash self-heals, the one
#  capability plain nohup lacks. It records the watchdog PID in a PID file so
#  `status` / `stop` / `uninstall` can manage it deterministically.
#
#  Usage:
#    ./supervisor.sh install     # write + enable a systemd --user unit (preferred)
#    ./supervisor.sh daemon      # setsid + watchdog + PID file (no-systemd fallback)
#    ./supervisor.sh run         # run in the foreground (dev / debugging)
#    ./supervisor.sh status      # report both launch paths
#    ./supervisor.sh stop        # stop whichever path is active
#    ./supervisor.sh uninstall   # stop daemon + disable/remove the systemd unit
#    ./supervisor.sh nohup       # DEPRECATED alias → daemon (kept for muscle memory)
#
#  Configuration (env — set these before `install`/`daemon`, they are baked
#  into the systemd unit's Environment= lines). No auth token: Tofu is a
#  personal app and the code-server password already gates the proxy.
#    TOFU_SUPERVISOR_PROJECTS  ':'-separated ABSOLUTE project paths to allow.
#    TOFU_SUPERVISOR_PORT      default 15001
#    TOFU_SUPERVISOR_HOST      default 127.0.0.1
#    TOFU_SUPERVISOR_PYTHON    interpreter used to launch server.py

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${TOFU_SUPERVISOR_PYTHON:-$(command -v python3)}"
UNIT_NAME="tofu-supervisor.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

# setsid/watchdog launch-path state.
PID_FILE="$BASE_DIR/data/supervisor.pid"
LOG_FILE="$BASE_DIR/data/supervisor.log"
WATCHDOG_RESTART_SECS=2          # base backoff after a healthy run dies
WATCHDOG_MAX_BACKOFF_SECS=60     # ceiling for the escalating backoff
WATCHDOG_HEALTHY_SECS=15         # a run lasting >= this is "healthy" → reset backoff

_check_projects() {
    if [[ -z "${TOFU_SUPERVISOR_PROJECTS:-}" ]]; then
        echo "WARNING: TOFU_SUPERVISOR_PROJECTS is empty — no project will be" >&2
        echo "         startable/stoppable until you allow-list one." >&2
    fi
}

# ── setsid/watchdog helpers ─────────────────────────────────────

# True iff the PID file names a live process (the watchdog session leader).
_daemon_alive() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

# The self-healing loop. Runs as the setsid session leader ($$ == session id
# == process-group id), so a group-directed signal reaches BOTH this loop and
# the current supervisor.py child. Restarts supervisor.py on crash; exits
# without restart when asked to stop.
cmd_watchdog() {
    set +e   # the loop manages exit codes itself; do not let `wait` trip errexit
    mkdir -p "$BASE_DIR/data"
    echo "$$" > "$PID_FILE"

    local _stopping=0
    local _child=""
    _on_term() {
        _stopping=1
        [[ -n "$_child" ]] && kill -TERM "$_child" 2>/dev/null
    }
    trap _on_term TERM INT

    # Escalating backoff so a PERSISTENT fast failure — e.g. supervisor.py
    # exiting immediately because port 15001 is already bound (build_server's
    # ThreadingHTTPServer bind raises EADDRINUSE, uncaught → non-zero exit) —
    # can NOT become a 2s CPU-pinning restart storm. A run that dies quickly
    # doubles the wait (capped); a run that stays up past the healthy
    # threshold resets it, so a genuine one-off crash still recovers fast.
    local _backoff="$WATCHDOG_RESTART_SECS"
    echo "[watchdog] started (pid=$$) — supervising supervisor.py, escalating backoff ${WATCHDOG_RESTART_SECS}..${WATCHDOG_MAX_BACKOFF_SECS}s"
    while [[ "$_stopping" -eq 0 ]]; do
        local _t0
        _t0=$(date +%s)
        "$PY" "$BASE_DIR/supervisor.py" &
        _child=$!
        wait "$_child"
        local code=$?
        if [[ "$_stopping" -ne 0 ]]; then
            break
        fi
        local _ran=$(( $(date +%s) - _t0 ))
        if [[ "$_ran" -ge "$WATCHDOG_HEALTHY_SECS" ]]; then
            # Healthy run → treat the death as a fresh incident, reset backoff.
            _backoff="$WATCHDOG_RESTART_SECS"
            echo "[watchdog] supervisor.py exited (code=$code) after ${_ran}s — restarting in ${_backoff}s"
        else
            echo "[watchdog] supervisor.py exited (code=$code) after only ${_ran}s (fast-fail, e.g. port in use) — backing off ${_backoff}s"
        fi
        sleep "$_backoff"
        # Escalate for the NEXT iteration if this one was a fast-fail.
        if [[ "$_ran" -lt "$WATCHDOG_HEALTHY_SECS" ]]; then
            _backoff=$(( _backoff * 2 ))
            [[ "$_backoff" -gt "$WATCHDOG_MAX_BACKOFF_SECS" ]] && _backoff="$WATCHDOG_MAX_BACKOFF_SECS"
        fi
    done
    rm -f "$PID_FILE"
    echo "[watchdog] stopped."
    exit 0
}

cmd_daemon() {
    _check_projects
    mkdir -p "$BASE_DIR/data"
    if _daemon_alive; then
        echo "Supervisor watchdog already running (pid=$(cat "$PID_FILE"))."
        return 0
    fi
    # Stale PID file from an unclean exit — reclaim it.
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"

    if ! command -v setsid >/dev/null 2>&1; then
        echo "WARNING: 'setsid' not found (util-linux). Falling back to nohup — the" >&2
        echo "         watchdog still self-heals, but is NOT fully detached from this" >&2
        echo "         terminal's session. Prefer a host with setsid or systemd." >&2
        nohup bash "$BASE_DIR/supervisor.sh" __watchdog__ >>"$LOG_FILE" 2>&1 < /dev/null &
    else
        # setsid makes the watchdog its own session leader → detached from the
        # launching terminal; a terminal-close SIGHUP can't reach it.
        setsid bash "$BASE_DIR/supervisor.sh" __watchdog__ >>"$LOG_FILE" 2>&1 < /dev/null &
    fi

    # Wait briefly for the watchdog to write its PID file.
    local i
    for i in $(seq 1 25); do
        _daemon_alive && break
        sleep 0.2
    done
    if _daemon_alive; then
        echo "Supervisor watchdog started (pid=$(cat "$PID_FILE")), logging to $LOG_FILE"
        echo "  Stop with: ./supervisor.sh stop"
    else
        echo "Failed to start supervisor watchdog — see $LOG_FILE" >&2
        exit 1
    fi
}

# Stop the setsid/watchdog path by signalling the whole process group (the
# watchdog session leader PID doubles as the PGID), so both the watchdog and
# its current supervisor.py child go down together and no restart fires.
cmd_stop_daemon() {
    set +e
    if ! _daemon_alive; then
        [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
        return 1
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    # Negative PID → signal the process group. Fall back to the bare PID.
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    local i
    for i in $(seq 1 15); do
        if ! _daemon_alive; then
            [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
            echo "Supervisor watchdog stopped."
            return 0
        fi
        sleep 1
    done
    echo "Watchdog still alive after 15s — escalating to SIGKILL." >&2
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    sleep 1
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
    echo "Supervisor watchdog killed."
    return 0
}

# ── foreground / systemd paths ──────────────────────────────────

cmd_run() {
    _check_projects
    exec "$PY" "$BASE_DIR/supervisor.py"
}

cmd_install() {
    _check_projects
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "systemctl not found — this host has no systemd." >&2
        echo "Use './supervisor.sh daemon' instead (setsid + watchdog)." >&2
        exit 1
    fi
    mkdir -p "$UNIT_DIR"
    # Bake the current env into the unit so the daemon starts with the same
    # allow-list after a reboot. Only non-empty vars are emitted.
    {
        echo "[Unit]"
        echo "Description=Tofu process supervisor (remote start/stop)"
        echo "After=network.target"
        echo ""
        echo "[Service]"
        echo "Type=simple"
        echo "WorkingDirectory=$BASE_DIR"
        echo "ExecStart=$PY $BASE_DIR/supervisor.py"
        echo "Restart=always"
        echo "RestartSec=2"
        [[ -n "${TOFU_SUPERVISOR_PROJECTS:-}" ]] && echo "Environment=TOFU_SUPERVISOR_PROJECTS=$TOFU_SUPERVISOR_PROJECTS"
        [[ -n "${TOFU_SUPERVISOR_PORT:-}" ]] && echo "Environment=TOFU_SUPERVISOR_PORT=$TOFU_SUPERVISOR_PORT"
        [[ -n "${TOFU_SUPERVISOR_HOST:-}" ]] && echo "Environment=TOFU_SUPERVISOR_HOST=$TOFU_SUPERVISOR_HOST"
        [[ -n "${TOFU_SUPERVISOR_PYTHON:-}" ]] && echo "Environment=TOFU_SUPERVISOR_PYTHON=$TOFU_SUPERVISOR_PYTHON"
        echo ""
        echo "[Install]"
        echo "WantedBy=default.target"
    } > "$UNIT_PATH"
    echo "Wrote $UNIT_PATH"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    # Keep the unit running after logout (best-effort; needs admin on some hosts).
    loginctl enable-linger "$(whoami)" 2>/dev/null || \
        echo "NOTE: could not enable-linger; the unit stops at logout unless lingering is enabled by an admin."
    echo "Supervisor installed + started. Check: ./supervisor.sh status"
}

cmd_status() {
    if _daemon_alive; then
        echo "setsid watchdog: RUNNING (pid=$(cat "$PID_FILE"), pidfile=$PID_FILE)"
    else
        echo "setsid watchdog: not running (pidfile=$PID_FILE)"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        echo "--- systemd --user unit ---"
        systemctl --user status "$UNIT_NAME" --no-pager 2>/dev/null || \
            echo "(no systemd user session / unit not installed)"
    fi
}

cmd_stop() {
    local acted=0
    if _daemon_alive; then
        cmd_stop_daemon || true
        acted=1
    fi
    if command -v systemctl >/dev/null 2>&1 && \
       systemctl --user is-active "$UNIT_NAME" >/dev/null 2>&1; then
        systemctl --user stop "$UNIT_NAME" && echo "systemd unit stopped." || true
        acted=1
    fi
    [[ "$acted" -eq 0 ]] && \
        echo "Supervisor not running (neither setsid watchdog nor systemd unit active)."
    return 0
}

cmd_uninstall() {
    # Tear down the setsid/watchdog path first (if any), then the systemd unit.
    if _daemon_alive; then
        cmd_stop_daemon || true
    fi
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Removed $UNIT_PATH (and stopped any setsid watchdog)."
}

case "${1:-run}" in
    run)         cmd_run ;;
    daemon)      cmd_daemon ;;
    nohup)       echo "NOTE: 'nohup' is deprecated → running the setsid watchdog path." >&2
                 cmd_daemon ;;
    __watchdog__) cmd_watchdog ;;   # internal: the self-healing loop (do not call directly)
    install)     cmd_install ;;
    status)      cmd_status ;;
    stop)        cmd_stop ;;
    uninstall)   cmd_uninstall ;;
    *) echo "Usage: $0 {install|daemon|run|status|stop|uninstall}" >&2; exit 1 ;;
esac
