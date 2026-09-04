#!/usr/bin/env bash
#
# restart_15000.sh — reliably reload the Tofu server on :15000.
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │ RUN THIS FROM A TERMINAL THAT IS **NOT** A CHILD OF THE :15000 SERVER.    │
# │ A plain VS Code terminal is fine. Do NOT run it from inside a Tofu agent  │
# │ shell — that shell is a child of the :15000 process, so killing the       │
# │ server would also kill the shell running this script (self-plug-pull).    │
# └─────────────────────────────────────────────────────────────────────────┘
#
# WHY THIS SCRIPT WAS REWRITTEN (2026-07-10):
#   The live server is launched as `python server.py` with NO `--port` argument
#   (the port defaults to $PORT / 15000 inside server.py). The previous version
#   matched `pkill -f "server.py --port 15000"`, which matched NOTHING, so the
#   old process was never killed; the relaunch then either shifted to :15001 via
#   server.py's _find_free_port fallback OR aborted on the instance lock
#   ("Another server instance is already running"). Root fix: kill the EXACT PID
#   that is actually listening on :15000 (from `ss -ltnp`), escalate SIGTERM →
#   SIGKILL if the port doesn't free, and only then relaunch — with the SAME
#   command the process really uses (`python server.py`, no --port).
#
# Safe to re-run (idempotent): if nothing is on :15000 it just launches one.
# No `set -e` on the whole script so "nothing to kill" is not fatal.
#
# DETACH (2026-07-16): the relaunch uses `setsid nohup` so the server starts in
# its own session with NO controlling terminal — a code-server terminal/session
# reap can no longer SIGTERM it. Output still goes to ${LOG} (the shell does the
# redirect, not nohup). Step [4b/5] asserts the live listener really left this
# terminal (tty=?, no "+" in STAT, sid≠this shell). For a server that must also
# survive OOM/crash, prefer the rendered supervisord program instead — see
# deploy/supervisor/tofu.conf.template (autostart/autorestart=true).
#
# MUTEX (2026-07-16): this script and the supervisord program are TWO owners of
# :15000 and must never both drive it. Step [pre/5] detects when supervisord
# already manages tofu and REFUSES to kill/relaunch (pointing you at
# `supervisorctl restart tofu`), so the two mechanisms can never fight. Once the
# supervisord program is installed, use supervisorctl — not this script.
#
# SERIALIZE (2026-07-16): step [pre/5b] takes an flock on data/.restart.lock so
# concurrent sibling restarts on a shared-HEAD box queue instead of both killing
# the listener at once (the paired-SIGTERM signature). After acquiring the lock
# it skips a redundant restart iff a sibling ALREADY brought up a healthy
# instance that STARTED AFTER this restart began (start-time proof, not just
# "port is occupied"), so a stale pre-existing process is still reloaded.

_restart_usage() {
  cat <<'EOF'
usage: bash restart_15000.sh

Linux-only legacy restart and deployment-verification compatibility entrypoint.
For normal source installations use `python serverctl.py restart`; it owns the
single managed worker, readiness checks, diagnostics, and human approval flow.

  -h, --help  show this help without inspecting or changing live processes
EOF
}

case "$#:${1:-}" in
  0:) ;;
  1:-h|1:--help) _restart_usage; exit 0 ;;
  *)
    echo "restart_15000.sh: unsupported arguments: $*" >&2
    _restart_usage >&2
    exit 2
    ;;
esac

umask 077  # inherited server stdout can contain private diagnostic evidence

[ "$(uname -s)" = "Linux" ] || {
  echo "restart_15000.sh is Linux-only; use python serverctl.py restart." >&2
  exit 1
}

SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"
SCRIPT_PROJECT="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"
PROJ="${TOFU_PROJECT_ROOT:-$SCRIPT_PROJECT}"
PROJ="$(cd -- "$PROJ" 2>/dev/null && pwd -P)" \
  || { echo "FATAL: cannot resolve project directory: ${PROJ}"; exit 1; }

# Explicit override wins; otherwise honor the installer marker, then the
# project-local uv environment, then a Python 3 already on PATH. A stale marker
# after a directory/host move fails closed with a concrete repair instead of
# launching the wrong interpreter.
PY="${TOFU_RUNTIME_PYTHON:-}"
_resolve_runtime_python() {
  local selected="$PY"
  local marker="${PROJ}/.tofu_env.json"
  local bootstrap="" candidate=""
  if [ -z "$selected" ] && [ -f "$marker" ]; then
    for candidate in "${PROJ}/.venv/bin/python" "$(command -v python3 2>/dev/null)"; do
      if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        bootstrap="$candidate"
        break
      fi
    done
    [ -n "$bootstrap" ] || {
      echo "FATAL: cannot parse ${marker}; set TOFU_RUNTIME_PYTHON." >&2
      return 1
    }
    selected="$("$bootstrap" - "$marker" <<'PYEOF'
import json
import sys
try:
    with open(sys.argv[1], encoding='utf-8') as stream:
        value = json.load(stream).get('python')
except (OSError, TypeError, ValueError):
    raise SystemExit(2)
if not isinstance(value, str) or not value:
    raise SystemExit(2)
sys.stdout.write(value)
PYEOF
)" || {
      echo "FATAL: invalid ${marker}; rerun install.sh or set TOFU_RUNTIME_PYTHON." >&2
      return 1
    }
    [ -x "$selected" ] || {
      echo "FATAL: ${marker} points to missing Python: ${selected}" >&2
      echo "       Rerun install.sh after moving this checkout." >&2
      return 1
    }
  fi
  if [ -z "$selected" ] && [ -x "${PROJ}/.venv/bin/python" ]; then
    selected="${PROJ}/.venv/bin/python"
  fi
  if [ -z "$selected" ]; then
    selected="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"
  fi
  [ -n "$selected" ] && [ -x "$selected" ] || {
    echo "FATAL: no runtime Python found; run install.sh or set TOFU_RUNTIME_PYTHON." >&2
    return 1
  }
  printf '%s' "$selected"
}
PY="$(_resolve_runtime_python)" || exit 1
"${PY}" - <<'PYEOF' >/dev/null 2>&1 || {
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)
PYEOF
  echo "FATAL: runtime interpreter must be Python 3.12+: ${PY}" >&2
  exit 1
}

REQUESTED_PORT="${PORT:-}"
PORT=15000
[ -z "$REQUESTED_PORT" ] || PORT="$REQUESTED_PORT"
case "$PORT" in
  ''|*[!0-9]*) echo "FATAL: PORT must be an integer in 1..65535" >&2; exit 2 ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || { echo "FATAL: PORT must be an integer in 1..65535" >&2; exit 2; }
LOG="server_${PORT}.log"

# A test process must never turn this production lifecycle entrypoint into an
# integration fixture.  ``skipif(port_listening)`` is vulnerable to TOCTOU: a
# server present at collection can disappear before execution, making the
# script take its intentional dead-server recovery path and launch a real Tofu
# with pytest's temporary DB/data environment.  Retargeted, defanged test
# COPIES may opt in only with a complete, mechanically checked isolation
# declaration. A boolean escape hatch alone is not authority: that exact gap
# once let an inherited production PORT override a retargeted test copy.
_lifecycle_test_refuse() {
  echo "[lifecycle-gate] REFUSING restart_15000.sh from an unisolated test process."
  echo "                 Test copies require a private pytest root, project/data paths"
  echo "                 beneath it, and an explicit non-production port."
  exit 3
}

_LIFECYCLE_TEST_MODE=0
if [ -n "${PYTEST_CURRENT_TEST:-}" ] \
   || [ "${TOFU_TESTING:-}" = "1" ] \
   || [ "${TOFU_ALLOW_LIFECYCLE_TEST:-}" = "1" ]; then
  _LIFECYCLE_TEST_MODE=1
  [ "${TOFU_ALLOW_LIFECYCLE_TEST:-}" = "1" ] || _lifecycle_test_refuse
  [ -n "${TOFU_PYTEST_RUN_ROOT:-}" ] || _lifecycle_test_refuse
  [ -n "${TOFU_LIFECYCLE_TEST_ROOT:-}" ] || _lifecycle_test_refuse
  [ -n "${TOFU_LIFECYCLE_TEST_PORT:-}" ] || _lifecycle_test_refuse
  [ -n "${TOFU_LIFECYCLE_TEST_TARGET_PID:-}" ] || _lifecycle_test_refuse
  [ -n "${TOFU_DATA_DIR:-}" ] || _lifecycle_test_refuse

  _TEST_RUN_ROOT="$(cd -- "${TOFU_PYTEST_RUN_ROOT}" 2>/dev/null && pwd -P)" \
    || _lifecycle_test_refuse
  _TEST_ROOT="$(cd -- "${TOFU_LIFECYCLE_TEST_ROOT}" 2>/dev/null && pwd -P)" \
    || _lifecycle_test_refuse
  _TEST_DATA_ROOT="$(cd -- "${TOFU_DATA_DIR}" 2>/dev/null && pwd -P)" \
    || _lifecycle_test_refuse
  [ "${_TEST_RUN_ROOT}" != "/" ] || _lifecycle_test_refuse
  case "${_TEST_ROOT}/" in
    "${_TEST_RUN_ROOT}/"*) ;;
    *) _lifecycle_test_refuse ;;
  esac
  case "${SCRIPT_PATH}/" in
    "${_TEST_ROOT}/"*) ;;
    *) _lifecycle_test_refuse ;;
  esac
  case "${PROJ}/" in
    "${_TEST_ROOT}/"*) ;;
    *) _lifecycle_test_refuse ;;
  esac
  case "${_TEST_DATA_ROOT}/" in
    "${_TEST_ROOT}/"*) ;;
    *) _lifecycle_test_refuse ;;
  esac
  [ "${TOFU_LIFECYCLE_TEST_PORT}" = "${PORT}" ] \
    || _lifecycle_test_refuse
  [ "${PORT}" != "15000" ] || _lifecycle_test_refuse
fi

# ── Headless-Chromium libs from LOCAL disk, never the FUSE conda env. ──
# The conda env lives on beegfs-fuse, which intermittently fails .so reads
# under pressure — measured 2026-08-03: 'libatk-1.0.so.0: cannot open shared
# object file' launch storms alternating with successful launches under a
# CONSTANT process env (the libs were never missing; FUSE weather decides).
# chromium_env.chromium_lib_dirs() honors CHROMIUM_EXTRA_LIB_DIRS FIRST and
# unfiltered; tofu_search's standalone fallback reads the same variable.
# The fontconfig half is exported too — env/etc/fonts is on the same FUSE
# mount, and a bad window there renders every glyph as nothing.
BROWSER_LIBS_DIR="${TOFU_BROWSER_LIBS_DIR:-${HOME}/tofu-browser-libs}"
if [ -d "${BROWSER_LIBS_DIR}/lib" ]; then
  export CHROMIUM_EXTRA_LIB_DIRS="${BROWSER_LIBS_DIR}/lib"
  if [ -f "${BROWSER_LIBS_DIR}/etc/fonts/fonts.conf" ]; then
    export FONTCONFIG_PATH="${BROWSER_LIBS_DIR}/etc/fonts"
    export FONTCONFIG_FILE="${BROWSER_LIBS_DIR}/etc/fonts/fonts.conf"
  fi
  echo "      chromium libs: CHROMIUM_EXTRA_LIB_DIRS=${CHROMIUM_EXTRA_LIB_DIRS}"
else
  echo "      NOTE: ${BROWSER_LIBS_DIR}/lib absent — Chromium libs resolve from the conda env (FUSE-flaky on this host)"
fi

echo "════════════════════════════════════════════════════════════════"
echo "[0/5] restart_15000.sh — reloading Tofu server on :${PORT}"
echo "      project: ${PROJ}"
cd "${PROJ}" || { echo "FATAL: cannot cd into project dir"; exit 1; }

# ── Helper: PIDs currently LISTENING on :PORT (the authoritative kill target). ──
# `ss -ltnp` Local Address:Port column ($4) looks like 127.0.0.1:15000 or
# *:15000 — match a literal ":PORT" at end of field, then pull pid=NNN.
listener_pids() {
  ss -ltnp 2>/dev/null \
    | awk -v pat=":${PORT}\$" '$4 ~ pat {print}' \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

_test_target_identity_matches() {
  [ "${_LIFECYCLE_TEST_MODE}" = "1" ] || return 0
  [ "${TOFU_LIFECYCLE_TEST_TARGET_PID}" != "none" ] || return 1
  local target_cwd target_start
  target_cwd="$(readlink -f "/proc/${TOFU_LIFECYCLE_TEST_TARGET_PID}/cwd" 2>/dev/null)" \
    || return 1
  case "${target_cwd}/" in
    "${_TEST_ROOT}/"*) ;;
    *) return 1 ;;
  esac
  target_start="$(awk '{print $22}' \
    "/proc/${TOFU_LIFECYCLE_TEST_TARGET_PID}/stat" 2>/dev/null)"
  [ -n "${target_start}" ] \
    && [ "${target_start}" = "${_LIFECYCLE_TEST_TARGET_START:-}" ]
}

# ── [pre/5] MUTEX GUARD — refuse to run if supervisord already OWNS tofu. ──
# The durable fix (rendered from deploy/supervisor/tofu.conf.template) hands
# :PORT to the host
# supervisord with autorestart=true. If BOTH mechanisms are live they FIGHT:
# this script's [1/5] kill → supervisord instantly relaunches (grabbing the
# port) → this script's [3/5] relaunch then aborts on the single-instance lock
# (or races a second instance). So once supervisord owns tofu, the ONLY correct
# restart entrypoint is `supervisorctl restart tofu`; this script must stand
# down. Detection is layered so a partial install can't slip through:
#   (1) the program is defined AND not STOPPED/absent (RUNNING/STARTING/BACKOFF
#       — i.e. supervisord is actively managing/relaunching it), via whichever
#       supervisorctl invocation works (plain, or sudo -n if the socket is
#       root-only). A definitive RUNNING/STARTING/BACKOFF is authoritative.
#   (2) fallback when supervisorctl is unreachable (no sudo, socket perms): the
#       conf is installed under /etc/supervisor/conf.d AND a :PORT listener's
#       process tree traces back to the supervisord daemon (ppid chain hits a
#       `supervisord`) — that proves supervisord, not a terminal, spawned it.
# A clean STOPPED/absent program, or conf present but the listener is NOT a
# supervisord child, means the script may proceed (manual mode).
SUPERVISOR_PROG="tofu"

_supervisor_conf_present() {
  local candidate
  if [ -n "${TOFU_SUPERVISOR_CONF:-}" ]; then
    [ -f "${TOFU_SUPERVISOR_CONF}" ]
    return
  fi
  for candidate in \
      /etc/supervisor/conf.d/tofu.conf \
      /etc/supervisord.d/tofu.ini; do
    [ -f "$candidate" ] && return 0
  done
  return 1
}

_supervisorctl() {
  # Emit the program's status line ONLY on a genuine query success. Try an
  # unprivileged call first, then non-interactive sudo (never prompts). Critical
  # subtlety: `supervisorctl status` prints a socket "Permission denied" error
  # to STDOUT and exits non-zero when the sock is root-only. We must NOT surface
  # that error text as a status — otherwise the caller's `[ -n SV_STATUS ]`
  # branch wins on garbage and the conf-installed fallback never runs. So we
  # gate on exit code AND require the output to actually name the program.
  command -v supervisorctl >/dev/null 2>&1 || return 1
  local out
  out="$(supervisorctl status "${SUPERVISOR_PROG}" 2>/dev/null)"
  if [ $? -eq 0 ] && printf '%s' "${out}" | grep -q "^${SUPERVISOR_PROG}[[:space:]]"; then
    printf '%s' "${out}"; return 0
  fi
  out="$(sudo -n supervisorctl status "${SUPERVISOR_PROG}" 2>/dev/null)"
  if [ $? -eq 0 ] && printf '%s' "${out}" | grep -q "^${SUPERVISOR_PROG}[[:space:]]"; then
    printf '%s' "${out}"; return 0
  fi
  return 1
}

_listener_is_supervisord_child() {
  # Walk the ppid chain of each :PORT listener; return 0 if any ancestor's comm
  # is 'supervisord'. Bounded to 12 hops (pid 1 terminates it anyway).
  local pids p comm hops
  pids="$(listener_pids)"
  [ -z "${pids}" ] && return 1
  for p in ${pids}; do
    hops=0
    while [ -n "${p}" ] && [ "${p}" != "1" ] && [ "${hops}" -lt 12 ]; do
      comm="$(ps -o comm= -p "${p}" 2>/dev/null | tr -d ' ')"
      case "${comm}" in *supervisord*) return 0 ;; esac
      p="$(ps -o ppid= -p "${p}" 2>/dev/null | tr -d ' ')"
      hops=$((hops + 1))
    done
  done
  return 1
}

supervisord_owns=0
SV_STATUS="$(_supervisorctl)"
if [ -n "${SV_STATUS}" ]; then
  # supervisorctl answered authoritatively. RUNNING/STARTING/BACKOFF = owned.
  case "${SV_STATUS}" in
    *RUNNING*|*STARTING*|*BACKOFF*) supervisord_owns=1 ;;
  esac
elif _supervisor_conf_present && _listener_is_supervisord_child; then
  # supervisorctl unreachable, but the conf is installed and the live listener
  # is genuinely a supervisord child — treat as owned.
  supervisord_owns=1
fi

if [ "${supervisord_owns}" = "1" ]; then
  echo "════════════════════════════════════════════════════════════════"
  echo "[pre/5] REFUSING to run: tofu on :${PORT} is MANAGED BY supervisord."
  echo "        This script kills + relaunches the port process; with"
  echo "        autorestart=true that FIGHTS supervisord (double instance /"
  echo "        instance-lock abort). The correct restart entrypoint is:"
  echo ""
  echo "            sudo supervisorctl restart ${SUPERVISOR_PROG}"
  echo ""
  echo "        (status:  sudo supervisorctl status ${SUPERVISOR_PROG}"
  echo "         logs:    tail -f ${PROJ}/logs/supervisor_tofu.log )"
  [ -n "${SV_STATUS}" ] && echo "        current: ${SV_STATUS}"
  echo "        To hand control back, remove the rendered tofu.conf/tofu.ini from"
  echo "        the supervisord include directory, then run supervisorctl update."
  echo "════════════════════════════════════════════════════════════════"
  exit 0
fi

# ── Guard: refuse to run if THIS shell is a descendant of a :PORT listener. ──
# Killing that PID would terminate this very shell (self-plug-pull).
LPIDS_INIT="$(listener_pids)"
if [ "${_LIFECYCLE_TEST_MODE}" = "1" ]; then
  case "${TOFU_LIFECYCLE_TEST_TARGET_PID}" in
    none)
      [ -z "${LPIDS_INIT}" ] || _lifecycle_test_refuse
      ;;
    ''|*[!0-9]*) _lifecycle_test_refuse ;;
    *)
      [ "${LPIDS_INIT}" = "${TOFU_LIFECYCLE_TEST_TARGET_PID}" ] \
        || _lifecycle_test_refuse
      _LIFECYCLE_TEST_TARGET_START="$(awk '{print $22}' \
        "/proc/${TOFU_LIFECYCLE_TEST_TARGET_PID}/stat" 2>/dev/null)"
      [ -n "${_LIFECYCLE_TEST_TARGET_START}" ] || _lifecycle_test_refuse
      _test_target_identity_matches || _lifecycle_test_refuse
      ;;
  esac
fi
if [ -n "${LPIDS_INIT}" ]; then
  up=$$
  for _ in 1 2 3 4 5 6 7 8; do
    { [ -z "${up}" ] || [ "${up}" = "1" ]; } && break
    for lp in ${LPIDS_INIT}; do
      if [ "${up}" = "${lp}" ]; then
        echo "FATAL: this shell (pid $$) is a DESCENDANT of the :${PORT} server"
        echo "       (pid ${lp}). Killing it would terminate this shell."
        echo "       Re-run from a plain VS Code terminal, not a Tofu agent shell."
        exit 2
      fi
    done
    up="$(ps -o ppid= -p "${up}" 2>/dev/null | tr -d ' ')"
  done
fi

# ── [pre/5c] HUMAN APPROVAL GATE (pt_40d00fd526e5479a, 2026-07-28) ──────────
# A restart of a RUNNING server is a high-risk action and requires explicit
# HUMAN approval (owner ruling after an autopilot conv curl'ed the HTTP
# restart endpoint twice in 3 minutes, killing 23 in-flight tasks). The HTTP
# endpoint is gated server-side; this gate stops the same bypass through the
# shell script (the 2026-07-27 watcher incident ran this script detached).
# Three ways through:
#   (i)  NO live listener on :PORT → this is a recovery/relaunch of a DEAD
#        server, not a restart of a live one — the gate does not apply.
#   (ii) interactive TTY → the human types RESTART at the prompt (a real
#        person at a terminal IS the approval).
#   (iii) non-interactive (agent shell / watcher) → a server-minted,
#        human-approved, unexpired, unconsumed token in
#        data/lifecycle_approvals.json (approved in the UI beforehand).
LPIDS_GATE="$(listener_pids)"
if [ -n "${LPIDS_GATE}" ]; then
  if [ -t 0 ]; then
    echo "[pre/5c] LIVE server on :${PORT} (pid(s): ${LPIDS_GATE})."
    echo "        Restarting it interrupts every in-flight task. This action"
    echo "        requires a HUMAN decision — confirm below."
    printf '        Type RESTART to proceed: '
    read -r _LC_ANSWER
    if [ "${_LC_ANSWER}" != "RESTART" ]; then
      echo "[pre/5c] Not confirmed — aborted (no process was touched)."
      exit 3
    fi
    echo "[pre/5c] Confirmed interactively."
  else
    echo "[pre/5c] Non-interactive run with a LIVE server on :${PORT} —"
    echo "        checking for a human-approved restart token ..."
    if "${PY}" -m lib.lifecycle_approval --script-gate restart; then
      echo "[pre/5c] Human-approved token consumed — proceeding."
    else
      echo "[pre/5c] Aborted (no process was touched)."
      exit 3
    fi
  fi
fi

# The approval above is the authoritative human gate for this compatibility
# entrypoint. Once admitted, hand the operation to the single lifecycle owner.
# Isolated legacy tests copy only this script and therefore exercise the old,
# self-contained implementation below without touching a real manager.
if [ -f "${PROJ}/serverctl.py" ]; then
  export TOFU_RESTART_GATE_PASSED=1
  exec "${PY}" "${PROJ}/serverctl.py" restart -y --source legacy-restart_15000.sh
fi

# ── [pre/5b] RESTART SERIALIZATION LOCK — one restart at a time on this box. ──
# On a shared-HEAD box, multiple sibling agents may each run this script to load
# a commit, unaware of each other. Without a lock they BOTH reach [1/5] and kill
# the same listener at the same second (the observed paired SIGTERMs), then race
# two relaunches — one aborts on the single-instance lock. This flock serializes
# restarts: the 2nd caller BLOCKS on fd 9 until the 1st fully finishes (the lock
# is held for the whole kill→relaunch→verify span because fd 9 stays open until
# this script exits). Placed AFTER the [pre/5] supervisord guard and BEFORE the
# [1/5] kill, so the kill phase itself is always inside the lock.
#
# SECOND-PROBE (skip-if-already-done): after we finally GET the lock, a sibling
# that held it first may have JUST relaunched a healthy new instance loading the
# same HEAD — killing it and relaunching again is pure waste. Skip only when the
# listener PID was NOT present in the pre-lock snapshot and the endpoint is a
# real Tofu health document. PID identity avoids the one-second timestamp race
# where an already-present listener and RESTART_EPOCH round to the same second;
# ``curl -f`` + bootId avoids treating an arbitrary HTTP 404 listener as Tofu.
RESTART_LOCK="${PROJ}/data/.restart.lock"
if command -v flock >/dev/null 2>&1 \
   && mkdir -p "${PROJ}/data" 2>/dev/null \
   && exec 9>"${RESTART_LOCK}" 2>/dev/null; then
  echo "[pre/5b] Acquiring restart lock (${RESTART_LOCK}) — serialize concurrent restarts ..."
  if flock -w 60 9; then
    echo "      Restart lock acquired (held until this script exits)."
    RL_PID="$(listener_pids | head -n1)"
    RL_WAS_INITIAL=0
    for _rl_initial in ${LPIDS_INIT}; do
      [ "${RL_PID}" = "${_rl_initial}" ] && RL_WAS_INITIAL=1
    done
    RL_HEALTH=""
    if [ -n "${RL_PID}" ] && [ "${RL_WAS_INITIAL}" = "0" ]; then
      RL_HEALTH="$(curl -fsS --max-time 2 \
        "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
    fi
    if [ -n "${RL_PID}" ] && [ "${RL_WAS_INITIAL}" = "0" ] \
       && printf '%s' "${RL_HEALTH}" | grep -q '"bootId"'; then
      RL_AGE="$(ps -o etimes= -p "${RL_PID}" 2>/dev/null | tr -d ' ')"
      echo "[pre/5b] A concurrent restart already brought up a HEALTHY new"
      echo "        Tofu instance (pid ${RL_PID}, age ${RL_AGE:-?}s — absent from"
      echo "        the pre-lock listener snapshot) — skipping redundant reload. Done."
      exit 0
    elif [ -n "${RL_PID}" ]; then
      echo "      Listener pid ${RL_PID} is pre-existing or not a Tofu health"
      echo "      endpoint — proceeding with the requested reload."
    fi
  else
    echo "[pre/5b] Another restart has held the lock for >60s — aborting to avoid a"
    echo "        double kill+relaunch. Re-run once it settles."
    exit 0
  fi
else
  echo "[pre/5b] flock unavailable / ${PROJ}/data not writable — proceeding WITHOUT"
  echo "        restart serialization (concurrent restarts on this box may collide)."
fi

# ── [1/5] Stop whatever is listening on :PORT (by exact PID). ──
echo "[1/5] Stopping current server on :${PORT} ..."
LPIDS="$(listener_pids)"
if [ "${_LIFECYCLE_TEST_MODE}" = "1" ]; then
  if [ "${TOFU_LIFECYCLE_TEST_TARGET_PID}" = "none" ]; then
    [ -z "${LPIDS}" ] || _lifecycle_test_refuse
    LPIDS=""
  else
    [ "${LPIDS}" = "${TOFU_LIFECYCLE_TEST_TARGET_PID}" ] \
      && _test_target_identity_matches \
      || _lifecycle_test_refuse
  fi
elif [ -z "${LPIDS}" ]; then
  # Fallback: no listener socket found (e.g. mid-crash) — match the real
  # launch command. NOTE: matches `python server.py`, NOT a --port substring.
  LPIDS="$(pgrep -f 'server\.py' 2>/dev/null | tr '\n' ' ')"
fi
if [ -n "${LPIDS}" ]; then
  echo "      Target PID(s): ${LPIDS}"
  for lp in ${LPIDS}; do kill "${lp}" 2>/dev/null && echo "      SIGTERM -> ${lp}"; done
else
  echo "      No process found listening on :${PORT} — nothing to stop."
fi

# ── [2/5] Wait for the port to free (up to ~20s); escalate to SIGKILL. ──
echo "[2/5] Waiting for :${PORT} to free ..."
freed=0
for i in $(seq 1 20); do
  if [ -z "$(listener_pids)" ] && ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    freed=1
    echo "      Port :${PORT} is free (after ${i}s)."
    break
  fi
  sleep 1
done
if [ "${freed}" != "1" ]; then
  echo "      WARNING: :${PORT} still bound after 20s — escalating to SIGKILL."
  KPIDS="$(listener_pids)"
  if [ "${_LIFECYCLE_TEST_MODE}" = "1" ]; then
    if [ -n "${KPIDS}" ]; then
      [ "${KPIDS}" = "${TOFU_LIFECYCLE_TEST_TARGET_PID}" ] \
        && _test_target_identity_matches \
        || _lifecycle_test_refuse
    fi
  elif [ -z "${KPIDS}" ]; then
    KPIDS="$(pgrep -f 'server\.py' 2>/dev/null | tr '\n' ' ')"
  fi
  for lp in ${KPIDS}; do kill -9 "${lp}" 2>/dev/null && echo "      SIGKILL -> ${lp}"; done
  sleep 2
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "      FATAL: :${PORT} STILL bound after SIGKILL. Aborting to avoid a"
    echo "             stray second instance / port shift. Investigate manually."
    exit 3
  fi
  echo "      Port :${PORT} freed after SIGKILL."
fi

# ── [2b/5] Wait for the OLD server PROCESS to actually exit — port-free is
#   NOT process-dead (pt_0c1d75f7eb824467, measured 2026-07-31 18:10): the old
#   server's graceful shutdown (~285 threads) kept the single-instance flock
#   on data/.server.lock alive ~10s AFTER the listener disappeared, so a
#   relaunch gated only on the port died on the instance lock
#   ('[Lock] instance lock held by a LIVE local server', new pid 3243972
#   exited at 18:10:19) — and the script did not retry. A ZOMBIE counts as
#   exited: the flock dies with the process, only a LIVE holder blocks us.
if [ -n "${LPIDS}${KPIDS:-}" ]; then
  echo "[2b/5] Waiting for old server process(es) to exit (flock release) ..."
  all_gone=0
  for i in $(seq 1 30); do
    alive=0
    for lp in ${LPIDS} ${KPIDS:-}; do
      st="$(ps -o stat= -p "${lp}" 2>/dev/null | tr -d ' ')"
      [ -n "${st}" ] && case "${st}" in Z*) : ;; *) alive=1 ;; esac
    done
    if [ "${alive}" = "0" ]; then
      all_gone=1
      echo "      Old process(es) exited (${i}s past port-free)."
      break
    fi
    sleep 1
  done
  if [ "${all_gone}" != "1" ]; then
    echo "      WARNING: old process still alive 30s after port-free — SIGKILL."
    for lp in ${LPIDS} ${KPIDS:-}; do
      if [ "${_LIFECYCLE_TEST_MODE}" = "1" ]; then
        [ "${lp}" = "${TOFU_LIFECYCLE_TEST_TARGET_PID}" ] \
          && _test_target_identity_matches \
          || _lifecycle_test_refuse
      fi
      kill -9 "${lp}" 2>/dev/null && echo "      SIGKILL -> ${lp}"
    done
    sleep 2
  fi
fi
# The EXACT precondition the new instance needs: the instance flock must be
# acquirable. server.py uses fcntl.flock(LOCK_EX|LOCK_NB) — the same lock
# namespace as the flock CLI (verified by tests/test_restart_lock_race.py).
# Bounded probe; on exhaustion the [3/5] lock-retry below is the backstop.
ILOCK="${PROJ}/data/.server.lock"
if [ -e "${ILOCK}" ] && command -v flock >/dev/null 2>&1; then
  for i in $(seq 1 10); do
    if flock -n "${ILOCK}" -c true 2>/dev/null; then
      [ "${i}" -gt 1 ] && echo "      Instance lock free (after ${i}s)."
      break
    fi
    [ "${i}" = "10" ] && echo "      WARNING: instance lock still held after 10s —"\
 && echo "             relying on the [3/5] lock-conflict retry."
    sleep 1
  done
fi

# ── [3/5] Relaunch EXACTLY as the process is really started (no --port). ──
#   Port comes from $PORT (server.py default 15000). We export it explicitly so
#   the bind is deterministic and never drifts via _find_free_port.
#
#   DETACH (2026-07-16): launch with `setsid nohup` — NOT bare `nohup … &`.
#   The bug was `nohup` ALONE: it only masks SIGHUP; the child stays in THIS
#   shell's session and process group, so when code-server reaps the whole
#   terminal session (its leader dies) the reap propagates to the server and it
#   takes a SIGTERM — the "terminal churn kills :15000" bug. `setsid` fixes the
#   root cause: it starts the server as the leader of a BRAND-NEW session with
#   no controlling terminal, so it is no longer a member of the terminal's
#   session/pgrp. We ALSO wrap in `nohup` as belt-and-suspenders (masks a stray
#   SIGHUP some reap paths broadcast first); the order `setsid nohup` matters —
#   setsid must be outermost so the new session is created regardless.
#
#   OUTPUT REDIRECTION (important): `> ${LOG} 2>&1` is done by THIS SHELL, not
#   by nohup — bash points fd1/fd2 at the log file BEFORE exec'ing setsid, and
#   setsid→nohup→python inherit those already-file fds. So ALL server output
#   goes to ${LOG}, NEVER to this VS Code terminal, with or without nohup. And
#   because stdout is already a file (not a tty), nohup never creates a stray
#   `nohup.out`. (Both facts verified empirically 2026-07-16.)
echo "[3/5] Relaunching (detached via setsid nohup): PORT=${PORT} setsid nohup ${PY} server.py >> ${LOG} 2>&1 &"
#   9>&- (pt_2a05e161b9814bc2): fd 9 holds the [pre/5b] restart serialization
#   flock. Without this close the relaunched server INHERITS fd 9 and keeps the
#   flock alive for its WHOLE lifetime (measured: a relaunched pid held
#   data/.restart.lock for 20+ min) — so every subsequent run of this script
#   blocks 60s at [pre/5b] and then aborts doing nothing. The lock must belong
#   to THIS script's lifetime only: it releases when this script exits. 9>&-
#   on an unopened fd (flock unavailable path) is a silent no-op.
# ── [3/5]+[4/5] Relaunch + health wait, with a BOUNDED retry on the
#   instance-lock race (pt_0c1d75f7eb824467). [2b/5] prevents the common case,
#   but a shutdown that outlives it must not be a silent one-shot failure: a
#   launch that dies with the lock signature in its log gets up to 3 attempts
#   with a 10s cooldown. Any OTHER death fails fast exactly as before — the
#   retry is for the lock race only, never a universal mask.
#   APPEND (2026-08-03): the relaunch used to TRUNCATE ${LOG} (`>`),
#   destroying the previous life's final lines — the wedged server's
#   last words were unrecoverable in the 11:14 incident. `>>` keeps
#   every life, with a demarcation banner between them. Consequence:
#   every "did THIS boot say X" probe below must be scoped to lines
#   written after THIS launch (LOG_MARK line offset / LAUNCH_STAMP
#   timestamp) — a stale line from a prior life must never count.
BASE="http://127.0.0.1:${PORT}"
launch_ok=0
{
  echo ""
  echo "════════════════ $(date '+%F %T') — launched by restart_15000.sh ════════════════"
} >> "${LOG}" 2>/dev/null
for attempt in 1 2 3; do
  if [ "${attempt}" -gt 1 ]; then
    echo "[3/5] Retry attempt ${attempt}/3 (lock-conflict backoff) ..."
    sleep 10
  fi
  LAUNCH_STAMP="$(date '+%F %T')"
  LOG_MARK="$(wc -l < "${LOG}" 2>/dev/null || echo 0)"
  PORT="${PORT}" BIND_HOST="${BIND_HOST:-0.0.0.0}" \
    TOFU_EXTERNAL_CONSOLE_LOG="${PROJ}/${LOG}" \
    setsid nohup "${PY}" server.py >> "${LOG}" 2>&1 9>&- &
  NEWPID=$!
  echo "      Launched pid ${NEWPID}; logging to ${LOG}"

  echo "[4/5] Waiting for the server to come up on :${PORT} ..."
  up_ok=0
  lock_death=0
  for i in $(seq 1 40); do
    if curl -s --max-time 2 "${BASE}/api/health" >/dev/null 2>&1; then
      up_ok=1
      echo "      Server responding (after ${i}s)."
      break
    fi
    # If the launched process already died, distinguish the lock race
    # (retryable) from every other startup death (fail fast). With
    # append-mode logs the signature must be found ONLY in lines written
    # after THIS launch — a prior life's lock death must not count.
    if ! kill -0 "${NEWPID}" 2>/dev/null; then
      if tail -n +$((LOG_MARK + 1)) "${LOG}" 2>/dev/null | grep -q "instance lock held by a LIVE local server\|Another server instance is already running"; then
        lock_death=1
      fi
      break
    fi
    sleep 1
  done

  if [ "${up_ok}" = "1" ]; then
    launch_ok=1
    break
  fi

  if [ "${lock_death}" = "1" ] && [ "${attempt}" -lt 3 ]; then
    echo "      Attempt ${attempt}: launched pid ${NEWPID} died on the instance"
    echo "      lock — the old server was still shutting down. Retrying after"
    echo "      a 10s cooldown (this is the pt_0c1d75f7eb824467 race)."
    continue
  fi

  if ! kill -0 "${NEWPID}" 2>/dev/null; then
    echo "      ERROR: launched pid ${NEWPID} exited during startup. Tail of ${LOG}:"
    tail -n 30 "${LOG}" 2>/dev/null
    echo "      If this is a stale instance lock, last resort:"
    echo "         PORT=${PORT} TOFU_SKIP_LOCK=1 nohup ${PY} server.py > ${LOG} 2>&1 &"
    exit 4
  fi

  echo "      ERROR: server did not respond within 40s. Tail of ${LOG}:"
  tail -n 30 "${LOG}" 2>/dev/null
  exit 4
done
if [ "${launch_ok}" != "1" ]; then
  echo "      ERROR: server did not come up after 3 attempts. Tail of ${LOG}:"
  tail -n 30 "${LOG}" 2>/dev/null
  exit 4
fi

# ── [4b/5] DETACH SELF-CHECK — prove the live listener really left the terminal.
#   The whole point of the setsid launch is that the server must NOT be a
#   foreground/background child of this VS Code terminal. Resolve the PID that
#   is actually LISTENING on :PORT (not $NEWPID — that can be a transient
#   launcher/re-exec parent) and assert, via `ps`:
#     • TTY is "?"  (no controlling terminal), and
#     • its session id (sid) is NOT this shell's sid.
#   STAT containing "+" (foreground process group of a tty) is a hard fail.
#   This is a WARNING, not a fatal exit: the server is already serving traffic;
#   we surface the regression loudly so a broken detach can never pass silently.
echo "[4b/5] Verifying the live :${PORT} listener is DETACHED from this terminal ..."
LISTEN_PID="$(listener_pids | head -n1)"
if [ -z "${LISTEN_PID}" ]; then
  echo "      ⚠️  Could not resolve the :${PORT} listener PID to check detach; skipping."
else
  MY_SID="$(ps -o sid= -p $$ 2>/dev/null | tr -d ' ')"
  L_TTY="$(ps -o tty= -p "${LISTEN_PID}" 2>/dev/null | tr -d ' ')"
  L_STAT="$(ps -o stat= -p "${LISTEN_PID}" 2>/dev/null | tr -d ' ')"
  L_SID="$(ps -o sid= -p "${LISTEN_PID}" 2>/dev/null | tr -d ' ')"
  detach_bad=0
  case "${L_TTY}" in ""|"?") : ;; *) detach_bad=1 ;; esac
  case "${L_STAT}" in *"+"*) detach_bad=1 ;; esac
  [ -n "${MY_SID}" ] && [ "${L_SID}" = "${MY_SID}" ] && detach_bad=1
  if [ "${detach_bad}" = "0" ]; then
    echo "      ✅ DETACHED: listener pid ${LISTEN_PID} tty=${L_TTY:-?} stat=${L_STAT} sid=${L_SID} (≠ this shell sid ${MY_SID})."
  else
    echo "      ❌ NOT DETACHED: listener pid ${LISTEN_PID} tty=${L_TTY} stat=${L_STAT} sid=${L_SID} (this shell sid ${MY_SID})."
    echo "         The server is STILL bound to this terminal's session — a"
    echo "         terminal/session reap will SIGTERM it again. The setsid launch"
    echo "         did not take effect (wrong shell, or started manually). The"
    echo "         durable fix is the rendered supervisord program"
    echo "         (deploy/supervisor/tofu.conf.template)."
  fi
fi

# ── [5/5] Self-verify the EVENT-LOOP-FREEZE FIX (commit c194e18) is actually
#          loaded in THIS running server — NOT some unrelated older feature.
#          Two independent, fix-specific probes, BOTH must pass:
#            (a) STATIC: the fix's new symbols import cleanly under the SAME
#                interpreter that launched the server. If HEAD predates the fix
#                these names don't exist → ImportError. This proves the code is
#                on disk + importable for the launch interpreter.
#            (b) RUNTIME: the new _serve guard code actually executed during
#                THIS boot — the running process emitted a "Loop blocking-guard"
#                line to ${LOG}. NOTE: that guard is DEFAULT OFF (set_debug is
#                unsafe as a 24/7 default on this high-concurrency service), so
#                the normal boot prints "Loop blocking-guard OFF (default)"; a
#                diagnostic boot (TOFU_LOOP_DEBUG_GUARD=1) prints "... armed".
#                EITHER line proves the NEW _serve code ran (both are new in
#                this fix); a boot on OLD code prints neither. A static import
#                can't prove the running process executed the new path — the
#                live log line does. The always-on LoopWatch 5s net is what
#                actually protects production; this guard is the opt-in
#                sub-stall detector.
#          Why probe THIS and not sticky-cwd: the previous [5/5] verified
#          get_conv_cwd/set_conv_cwd (a DIFFERENT commit). A green there says
#          nothing about whether the freeze fix shipped — the probe must assert
#          the change this restart is FOR.
echo "[5/5] Verifying the event-loop-freeze fix (c194e18) is loaded ..."
echo "────────────────────────────────────────────────────────────────"
probe_fail=0

# (a) STATIC — the fix's new symbols must import under the server interpreter.
if "${PY}" -c "from lib.translate.segment_backfill import _get_backfill_semaphore, _translate_and_stamp_eligible" 2>/dev/null; then
  echo "✅ (a) CODE PRESENT: off-loop backfill symbols import from lib.translate.segment_backfill."
else
  echo "❌ (a) CODE ABSENT: _get_backfill_semaphore/_translate_and_stamp_eligible do NOT import."
  echo "       git HEAD is missing commit c194e18, or ${PY} is the wrong interpreter."
  probe_fail=1
fi

# (b) RUNTIME — the new _serve guard code must have run this boot. Match the
#     shared "Loop blocking-guard" prefix so BOTH the default "OFF" line and the
#     opt-in "armed" line count as proof the new path executed.
#     STREAM FIX (2026-08-03): the proof line is emitted at INFO level, and
#     the stdout stream captured in ${LOG} carries WARNING+ ONLY (measured:
#     0 INFO lines vs 990 WARNING+ lines) — grepping ${LOG} can never match
#     and false-FATALed a healthy boot. Read the stream that actually
#     carries INFO — logs/app.log — and scope it to THIS launch by
#     timestamp (app.log is append-only across lives; LAUNCH_STAMP is
#     captured per attempt above). String timestamps sort chronologically.
guard_ok=0
APPLOG="${PROJ}/logs/app.log"
for i in $(seq 1 10); do
  if [ -f "${APPLOG}" ] \
     && awk -v s="${LAUNCH_STAMP}" 'substr($0,1,19) >= s && /Loop blocking-guard/ {ok=1} END{exit !ok}' "${APPLOG}"; then
    guard_ok=1; break
  fi
  sleep 1
done
if [ "${guard_ok}" = "1" ]; then
  echo "✅ (b) NEW _serve CODE RAN: '$(awk -v s="${LAUNCH_STAMP}" 'substr($0,1,19) >= s && /Loop blocking-guard/ {print; exit}' "${APPLOG}" | sed 's/^[^[]*//')'"
else
  echo "❌ (b) NEW _serve CODE DID NOT RUN: no 'Loop blocking-guard' line in ${APPLOG} since launch (${LAUNCH_STAMP})."
  echo "       The running process is NOT executing the new _serve code —"
  echo "       git HEAD likely predates the fix, or the wrong file booted."
  probe_fail=1
fi

# (c) WINDOWED FIRST-OPEN — the byte-bounded conversation-open fix (commit
#     0c03be2) must be live: a large conversation served over ?window=N must
#     come back WINDOWED + heavy-field-TRIMMED and its body must be a fraction
#     of the multi-MB full blob (the reported freeze-victim mrbu5j9azz8gi8 was
#     5.78 MB → ~237 KB). This proves THIS fix shipped, not just the freeze fix.
#     Resilient: if the probe conv is absent on this deployment (404 / not
#     found), SKIP rather than fail (the endpoint contract is still checked by
#     tests/test_conv_windowed_blob_slice.py); only FAIL if it exists but is
#     served UNwindowed or over-size.
probe_c_skipped=0
PROBE_CONV="${TOFU_WINDOW_PROBE_CONV:-mrbu5j9azz8gi8}"
PROBE_URL="${BASE}/api/v1/conversations/${PROBE_CONV}?window=60"
PROBE_JSON="$(curl -s --max-time 20 "${PROBE_URL}" 2>/dev/null)"
if [ -z "${PROBE_JSON}" ] || printf '%s' "${PROBE_JSON}" | grep -qiE '"error"|not.?found'; then
  probe_c_skipped=1
  echo "⏭️  (c) SKIPPED — windowed byte-trim NOT VERIFIED: probe conv '${PROBE_CONV}'"
  echo "       is not present on this deployment (override with"
  echo "       TOFU_WINDOW_PROBE_CONV=<an existing large conv id> to actually"
  echo "       verify the live byte-trim). The endpoint contract is still covered"
  echo "       offline by tests/test_conv_windowed_blob_slice.py, but THIS restart"
  echo "       did NOT confirm the trim is live — do not treat it as fully proven."
else
  # Parse windowed/trimmed flags + byte size with the server interpreter (no jq dep).
  PROBE_VERDICT="$("${PY}" - "$PROBE_URL" <<'PYEOF' 2>/dev/null
import sys, json, urllib.request
url = sys.argv[1]
try:
    raw = urllib.request.urlopen(url, timeout=20).read()
except Exception as e:
    print("ERR fetch %s" % e); sys.exit(0)
n = len(raw)
try:
    d = json.loads(raw)
except Exception as e:
    print("ERR json %s" % e); sys.exit(0)
w = d.get('windowed') is True
t = d.get('trimmed') is True
under = n < 1024 * 1024
print("bytes=%d windowed=%s trimmed=%s under1MB=%s served=%d total=%s"
      % (n, w, t, under, len(d.get('messages') or []), d.get('totalCount')))
print("VERDICT_OK" if (w and t and under) else "VERDICT_BAD")
PYEOF
)"
  echo "      ${PROBE_VERDICT}" | grep -v VERDICT_
  if printf '%s' "${PROBE_VERDICT}" | grep -q "VERDICT_OK"; then
    echo "✅ (c) WINDOWED-OPEN LIVE: '${PROBE_CONV}' served windowed+trimmed, body < 1 MB."
  else
    echo "❌ (c) WINDOWED-OPEN NOT LIVE: '${PROBE_CONV}' served UNwindowed or over 1 MB."
    echo "       get_conv is shipping the full blob — commit 0c03be2 did not load."
    probe_fail=1
  fi
fi

# (d) CACHE-FIX GENERATION — the served process must self-report an in-memory
#     CACHE_FIX_GEN >= the expected baseline. This ties restart success to the
#     CURRENT prefix-cache deploy target (the whole ab161bf..1920827 chain =
#     gen 5), not just the older event-loop/windowed commits above. Uses the
#     boot-identity fields /api/health now returns (bootId + cacheFixGen). The
#     check is >= (not ==) so a FUTURE gen never false-fails this baseline; the
#     expected value tracks lib/llm/cache.CACHE_FIX_GEN and is overridable via
#     env. If /api/health predates the field (old build) the loaded code is by
#     definition older than gen 5 → FAIL (that is the point). Best-effort parse
#     with the server interpreter (no jq dep).
EXPECT_GEN="${CACHE_FIX_GEN_EXPECT:-5}"
GEN_VERDICT="$("${PY}" - "${BASE}/api/health" "${EXPECT_GEN}" <<'PYEOF' 2>/dev/null
import sys, json, urllib.request
url, expect = sys.argv[1], int(sys.argv[2])
try:
    d = json.loads(urllib.request.urlopen(url, timeout=10).read())
except Exception as e:
    print("ERR %s" % e); sys.exit(0)
gen = d.get('cacheFixGen')
if not isinstance(gen, int):
    print("gen=%r boot=%s NO_FIELD" % (gen, d.get('bootId'))); sys.exit(0)
print("gen=%d expect>=%d boot=%s %s"
      % (gen, expect, d.get('bootId'), "OK" if gen >= expect else "OLD"))
PYEOF
)"
echo "      ${GEN_VERDICT}"
if printf '%s' "${GEN_VERDICT}" | grep -q " OK$"; then
  echo "✅ (d) CACHE-FIX LIVE: served process self-reports CACHE_FIX_GEN >= ${EXPECT_GEN} (in-memory)."
elif printf '%s' "${GEN_VERDICT}" | grep -q "NO_FIELD"; then
  echo "❌ (d) CACHE-FIX NOT LIVE: /api/health has no cacheFixGen field — the"
  echo "       served code predates the boot-identity/self-report chain (< gen ${EXPECT_GEN})."
  probe_fail=1
else
  echo "❌ (d) CACHE-FIX OLD: served CACHE_FIX_GEN < ${EXPECT_GEN} — the prefix-cache"
  echo "       fix chain is NOT the loaded bytecode. Wrong tree booted / stale copy."
  probe_fail=1
fi

if [ "${probe_fail}" = "1" ]; then
  echo "────────────────────────────────────────────────────────────────"
  echo "FATAL: a fix is NOT fully live on :${PORT} (pid ${NEWPID}). See above."
  exit 5
fi
if [ "${probe_c_skipped}" = "1" ]; then
  echo "────────────────────────────────────────────────────────────────"
  echo "⚠️  PARTIAL: off-loop backfill + new _serve guard + CACHE_FIX_GEN>=${EXPECT_GEN}"
  echo "    are LIVE on :${PORT} (pid ${NEWPID}), but the windowed byte-trim (c) was"
  echo "    SKIPPED and is NOT verified live this restart (probe conv absent). Re-run"
  echo "    with TOFU_WINDOW_PROBE_CONV=<existing large conv id> to confirm the trim."
  echo "════════════════════════════════════════════════════════════════"
else
  echo "✅ FIX LIVE: off-loop backfill + new _serve guard + windowed byte-bounded open + CACHE_FIX_GEN>=${EXPECT_GEN} on :${PORT} (pid ${NEWPID})."
  echo "════════════════════════════════════════════════════════════════"
fi
