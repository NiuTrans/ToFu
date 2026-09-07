#!/usr/bin/env bash
# Install a target-host Tofu program into an existing Linux supervisord.
# The repository contains no rendered host paths; this script resolves and
# validates them, renders tofu.conf.template, applies it transactionally, and
# proves both supervisord ownership and application readiness.
# Entrypoint: execute this file. Dependencies: render_config.py, serverctl.py,
# healthcheck.py, a Python 3.12+ Tofu environment, and a running supervisord.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
TEMPLATE="${SCRIPT_DIR}/tofu.conf.template"
RENDERER="${SCRIPT_DIR}/render_config.py"

PORT="${PORT:-15000}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
PYTHON_OVERRIDE="${TOFU_SUPERVISOR_PYTHON:-}"
RUN_USER="${TOFU_SUPERVISOR_USER:-}"
RUN_HOME="${TOFU_SUPERVISOR_HOME:-}"
CONFIG_DIRECTORY="${TOFU_SUPERVISOR_CONF_DIR:-}"
BROWSER_LIBS_DIRECTORY="${TOFU_BROWSER_LIBS_DIR:-}"
DRY_RUN=0
NO_HANDOFF=0

usage() {
  cat <<'EOF'
usage: deploy/supervisor/install.sh [OPTIONS]

Render and install a relocatable Tofu program into an existing Linux
supervisord. The checkout, Python environment, account, home directory, port,
and log path are resolved on this host; no repository file is rewritten.

Options:
  --port N             listener port (default: PORT or 15000)
  --bind-host ADDRESS  literal bind address (default: BIND_HOST or 0.0.0.0)
  --python PATH        installed Tofu Python (default: .tofu_env.json/.venv)
  --user NAME          non-root service account (default: sudo/project owner)
  --home PATH          service account home (default: operating-system record)
  --config-dir PATH    supervisord include directory (otherwise auto-detected)
  --no-handoff         refuse instead of stopping a proven project-local worker
  --dry-run            print the rendered config; do not inspect or mutate host state
  -h, --help           show this help without inspecting or mutating host state

Environment equivalents: TOFU_SUPERVISOR_PYTHON, TOFU_SUPERVISOR_USER,
TOFU_SUPERVISOR_HOME, TOFU_SUPERVISOR_CONF_DIR, TOFU_BROWSER_LIBS_DIR,
PORT, and BIND_HOST.
EOF
}

die() {
  echo "deploy/supervisor/install.sh: $*" >&2
  exit 1
}

option_value() {
  local option="$1"
  local remaining="$2"
  local value="${3-}"
  if [[ "$remaining" -lt 2 || -z "$value" || "$value" == --* ]]; then
    echo "deploy/supervisor/install.sh: ${option} requires a value" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --port) option_value "$1" "$#" "${2-}"; PORT="$2"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --bind-host) option_value "$1" "$#" "${2-}"; BIND_HOST="$2"; shift 2 ;;
    --bind-host=*) BIND_HOST="${1#*=}"; shift ;;
    --python) option_value "$1" "$#" "${2-}"; PYTHON_OVERRIDE="$2"; shift 2 ;;
    --python=*) PYTHON_OVERRIDE="${1#*=}"; shift ;;
    --user) option_value "$1" "$#" "${2-}"; RUN_USER="$2"; shift 2 ;;
    --user=*) RUN_USER="${1#*=}"; shift ;;
    --home) option_value "$1" "$#" "${2-}"; RUN_HOME="$2"; shift 2 ;;
    --home=*) RUN_HOME="${1#*=}"; shift ;;
    --config-dir) option_value "$1" "$#" "${2-}"; CONFIG_DIRECTORY="$2"; shift 2 ;;
    --config-dir=*) CONFIG_DIRECTORY="${1#*=}"; shift ;;
    --no-handoff) NO_HANDOFF=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *)
      echo "deploy/supervisor/install.sh: unknown option: $1" >&2
      echo "Try 'bash deploy/supervisor/install.sh --help'." >&2
      exit 2
      ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] && (( 10#$PORT >= 1 && 10#$PORT <= 65535 )) \
  || { echo "deploy/supervisor/install.sh: --port must be in 1..65535" >&2; exit 2; }
[[ -f "$TEMPLATE" ]] || die "template is missing: ${TEMPLATE}"
[[ -f "$RENDERER" ]] || die "renderer is missing: ${RENDERER}"
[[ -f "${PROJECT_ROOT}/server.py" ]] || die "cannot locate server.py under ${PROJECT_ROOT}"

resolve_runtime_python() {
  local selected="$PYTHON_OVERRIDE"
  local marker="${PROJECT_ROOT}/.tofu_env.json"
  local bootstrap=""
  local candidate=""

  if [[ -z "$selected" && -f "$marker" ]]; then
    for candidate in "${PROJECT_ROOT}/.venv/bin/python" "$(command -v python3 2>/dev/null || true)"; do
      if [[ -n "$candidate" && -x "$candidate" ]]; then
        bootstrap="$candidate"
        break
      fi
    done
    [[ -n "$bootstrap" ]] || die \
      "cannot parse ${marker}; pass --python PATH to the installed Python 3.12+"
    selected="$("$bootstrap" - "$marker" <<'PY'
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
PY
)" || die "${marker} is invalid; rerun install.sh or pass --python PATH"
    [[ -x "$selected" ]] || die \
      "${marker} points to a missing interpreter: ${selected}; rerun install.sh after moving this checkout"
  fi

  if [[ -z "$selected" && -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    selected="${PROJECT_ROOT}/.venv/bin/python"
  fi
  if [[ -z "$selected" ]]; then
    selected="$(command -v python3 2>/dev/null || true)"
  fi
  [[ -n "$selected" && -x "$selected" ]] || die \
    "no installed Python found; run install.sh first or pass --python PATH"
  "$selected" - <<'PY' >/dev/null 2>&1 || die \
    "selected interpreter is not Python 3.12+: ${selected}"
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)
PY
  "$selected" -c 'import os, sys; print(os.path.abspath(sys.executable))'
}

PYTHON_EXECUTABLE="$(resolve_runtime_python)"

if [[ -z "$RUN_USER" ]]; then
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    RUN_USER="$SUDO_USER"
  elif [[ "$(uname -s)" == "Linux" ]]; then
    RUN_USER="$(stat -c '%U' "$PROJECT_ROOT" 2>/dev/null || true)"
  fi
  [[ -n "$RUN_USER" && "$RUN_USER" != "UNKNOWN" ]] || RUN_USER="$(id -un)"
fi
id "$RUN_USER" >/dev/null 2>&1 || die "target user does not exist: ${RUN_USER}"

if [[ -z "$RUN_HOME" ]]; then
  RUN_HOME="$("$PYTHON_EXECUTABLE" - "$RUN_USER" <<'PY'
import pwd
import sys
try:
    sys.stdout.write(pwd.getpwnam(sys.argv[1]).pw_dir)
except KeyError:
    raise SystemExit(2)
PY
)" || die "cannot resolve home directory for ${RUN_USER}; pass --home PATH"
fi
[[ -d "$RUN_HOME" ]] || die "target home directory does not exist: ${RUN_HOME}"
if [[ -z "$BROWSER_LIBS_DIRECTORY" ]]; then
  BROWSER_LIBS_DIRECTORY="${RUN_HOME}/tofu-browser-libs"
fi

RENDER_ARGUMENTS=(
  "$PYTHON_EXECUTABLE" "$RENDERER"
  --project-root "$PROJECT_ROOT"
  --python "$PYTHON_EXECUTABLE"
  --user "$RUN_USER"
  --home "$RUN_HOME"
  --port "$PORT"
  --bind-host "$BIND_HOST"
  --browser-libs-dir "$BROWSER_LIBS_DIRECTORY"
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  exec "${RENDER_ARGUMENTS[@]}"
fi

[[ "$(uname -s)" == "Linux" ]] || die \
  "system-supervisord installation is supported only on Linux; use serverctl.py on this platform"
[[ "$RUN_USER" != "root" ]] || die \
  "refusing to run Tofu as root; pass --user NAME for a non-root service account"

for command_name in supervisorctl ss ps awk grep cut sort head id install cmp \
    mv sed seq tr find cat mktemp mkdir rmdir sleep; do
  command -v "$command_name" >/dev/null 2>&1 || die \
    "required command is unavailable: ${command_name}"
done

_priv() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

_can_priv() {
  [[ "$(id -u)" -eq 0 ]] && return 0
  command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1
}

if ! _can_priv; then
  echo "Root or passwordless sudo is required to install and activate a system supervisord program." >&2
  echo "Re-run interactively with sudo while preserving the target account explicitly:" >&2
  printf '  sudo bash %q --user %q --home %q --python %q --port %q\n' \
    "$0" "$RUN_USER" "$RUN_HOME" "$PYTHON_EXECUTABLE" "$PORT" >&2
  exit 77
fi

_as_target() {
  if [[ "$(id -un)" == "$RUN_USER" ]]; then
    "$@"
  elif [[ "$(id -u)" -eq 0 ]] && command -v runuser >/dev/null 2>&1; then
    runuser -u "$RUN_USER" -- "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n -u "$RUN_USER" -- "$@"
  else
    return 1
  fi
}

_as_target test -r "${PROJECT_ROOT}/server.py" \
  || die "${RUN_USER} cannot read ${PROJECT_ROOT}/server.py"
_as_target test -x "$PYTHON_EXECUTABLE" \
  || die "${RUN_USER} cannot execute ${PYTHON_EXECUTABLE}"
_as_target mkdir -p "${PROJECT_ROOT}/logs" \
  || die "${RUN_USER} cannot create ${PROJECT_ROOT}/logs"
_as_target test -w "${PROJECT_ROOT}/logs" \
  || die "${RUN_USER} cannot write ${PROJECT_ROOT}/logs"

if [[ -z "$CONFIG_DIRECTORY" ]]; then
  if [[ -d /etc/supervisor/conf.d ]]; then
    CONFIG_DIRECTORY=/etc/supervisor/conf.d
  elif [[ -d /etc/supervisord.d ]]; then
    CONFIG_DIRECTORY=/etc/supervisord.d
  else
    die "cannot find a supervisord include directory; pass --config-dir PATH"
  fi
fi
[[ "$CONFIG_DIRECTORY" == /* && -d "$CONFIG_DIRECTORY" ]] || die \
  "--config-dir must name an existing absolute directory: ${CONFIG_DIRECTORY}"
CONFIG_DIRECTORY="$(cd -- "$CONFIG_DIRECTORY" && pwd -P)" \
  || die "cannot resolve supervisord include directory: ${CONFIG_DIRECTORY}"
case "$CONFIG_DIRECTORY" in
  */supervisord.d) CONFIG_PATH="${CONFIG_DIRECTORY}/tofu.ini" ;;
  *) CONFIG_PATH="${CONFIG_DIRECTORY}/tofu.conf" ;;
esac

TEMP_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/tofu-supervisor-install.XXXXXX")"
RENDERED_CONFIG="${TEMP_DIRECTORY}/tofu.conf"
PREVIOUS_CONFIG="${TEMP_DIRECTORY}/previous.conf"
CONFIG_EXISTED=0
CONFIG_CHANGED=1
CONFIG_APPLIED=0
MANUAL_WORKER_STOPPED=0
STAGED_CONFIG=""
ROLLBACK_STAGE=""
ROLLBACK_SUCCEEDED=1

cleanup() {
  if [[ -n "$STAGED_CONFIG" ]]; then
    _priv find "$STAGED_CONFIG" -maxdepth 0 -type f -delete \
      >/dev/null 2>&1 || true
  fi
  if [[ -n "$ROLLBACK_STAGE" ]]; then
    _priv find "$ROLLBACK_STAGE" -maxdepth 0 -type f -delete \
      >/dev/null 2>&1 || true
  fi
  if [[ -d "$TEMP_DIRECTORY" ]]; then
    _priv find "$TEMP_DIRECTORY" -mindepth 1 -maxdepth 1 -type f -delete \
      >/dev/null 2>&1 || true
    rmdir "$TEMP_DIRECTORY" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

"${RENDER_ARGUMENTS[@]}" --output "$RENDERED_CONFIG"

if _priv test -f "$CONFIG_PATH"; then
  CONFIG_EXISTED=1
  _priv cat "$CONFIG_PATH" > "$PREVIOUS_CONFIG"
  if cmp -s "$RENDERED_CONFIG" "$PREVIOUS_CONFIG"; then
    CONFIG_CHANGED=0
  fi
fi

supervisor_status() {
  _priv supervisorctl status tofu 2>/dev/null || true
}

STATUS_BEFORE="$(supervisor_status)"
PROGRAM_WAS_ACTIVE=0
case "$STATUS_BEFORE" in
  *RUNNING*|*STARTING*|*STOPPING*) PROGRAM_WAS_ACTIVE=1 ;;
esac
SUPERVISOR_STATE_TOUCHED=0

if [[ "$CONFIG_CHANGED" -eq 1 && "$PROGRAM_WAS_ACTIVE" -eq 1 ]]; then
  if [[ -t 0 ]]; then
    echo "The installed tofu program is active; applying this config will restart it."
    printf 'Type UPDATE to continue: '
    read -r answer
    [[ "$answer" == "UPDATE" ]] || die "not confirmed; existing program was not changed"
  else
    (
      cd -- "$PROJECT_ROOT"
      _as_target "$PYTHON_EXECUTABLE" -m lib.lifecycle_approval --script-gate restart
    ) || die "live supervisord reconfiguration requires a human-approved restart token"
  fi
fi

rollback_config() {
  local rollback_status=""
  ROLLBACK_SUCCEEDED=1
  set +e
  if [[ "$CONFIG_APPLIED" -eq 1 ]]; then
    if [[ "$CONFIG_EXISTED" -eq 1 ]]; then
      ROLLBACK_STAGE="${CONFIG_PATH}.tofu-rollback-$$"
      _priv install -m 0644 "$PREVIOUS_CONFIG" "$ROLLBACK_STAGE" \
        || ROLLBACK_SUCCEEDED=0
      if [[ "$ROLLBACK_SUCCEEDED" -eq 1 ]]; then
        if _priv mv -f "$ROLLBACK_STAGE" "$CONFIG_PATH"; then
          ROLLBACK_STAGE=""
        else
          ROLLBACK_SUCCEEDED=0
        fi
      fi
    else
      if _priv test -e "$CONFIG_PATH"; then
        _priv find "$CONFIG_PATH" -maxdepth 0 -type f -delete \
          || ROLLBACK_SUCCEEDED=0
      fi
    fi
    _priv supervisorctl reread >/dev/null 2>&1 || ROLLBACK_SUCCEEDED=0
    _priv supervisorctl update >/dev/null 2>&1 || ROLLBACK_SUCCEEDED=0
  fi
  if [[ "$CONFIG_EXISTED" -eq 1 \
        && ( "$CONFIG_APPLIED" -eq 1 || "$SUPERVISOR_STATE_TOUCHED" -eq 1 ) ]]; then
    rollback_status="$(supervisor_status)"
    if [[ "$PROGRAM_WAS_ACTIVE" -eq 1 ]]; then
      case "$rollback_status" in
        *RUNNING*|*STARTING*|*STOPPING*) ;;
        *)
          if ! _priv supervisorctl start tofu >/dev/null 2>&1; then
            ROLLBACK_SUCCEEDED=0
          fi
          ;;
      esac
    else
      case "$rollback_status" in
        *RUNNING*|*STARTING*|*STOPPING*)
          if ! _priv supervisorctl stop tofu >/dev/null 2>&1; then
            ROLLBACK_SUCCEEDED=0
          fi
          ;;
      esac
    fi
  fi
  if [[ "$MANUAL_WORKER_STOPPED" -eq 1 ]]; then
    _as_target "$PYTHON_EXECUTABLE" "${PROJECT_ROOT}/serverctl.py" \
      start --wait 60 --source supervisor-install-rollback >/dev/null 2>&1 \
      || ROLLBACK_SUCCEEDED=0
  fi
  set -e
}

fail_applied() {
  local message="$1"
  local config_was_applied="$CONFIG_APPLIED"
  local manual_worker_was_stopped="$MANUAL_WORKER_STOPPED"
  rollback_config
  if [[ "$ROLLBACK_SUCCEEDED" -ne 1 ]]; then
    die "${message}; AUTOMATIC ROLLBACK FAILED — inspect ${CONFIG_PATH} and supervisorctl status tofu"
  elif [[ "$config_was_applied" -eq 1 ]]; then
    die "${message}; the previous lifecycle configuration was restored"
  elif [[ "$manual_worker_was_stopped" -eq 1 ]]; then
    die "${message}; project-local worker restoration was attempted"
  else
    die "$message"
  fi
}

if [[ "$CONFIG_CHANGED" -eq 1 ]]; then
  STAGED_CONFIG="${CONFIG_PATH}.tofu-install-$$"
  _priv install -m 0644 "$RENDERED_CONFIG" "$STAGED_CONFIG" \
    || die "cannot stage ${CONFIG_PATH}"
  _priv mv -f "$STAGED_CONFIG" "$CONFIG_PATH" \
    || die "cannot publish ${CONFIG_PATH}"
  STAGED_CONFIG=""
  CONFIG_APPLIED=1
  _priv supervisorctl reread >/dev/null \
    || fail_applied "supervisorctl rejected the rendered config"
fi

listener_lines() {
  _priv ss -H -ltnp 2>/dev/null \
    | awk -v pattern=":${PORT}\$" '$4 ~ pattern {print}'
}

if ! LISTENER_LINES="$(listener_lines)"; then
  fail_applied "cannot inspect port ${PORT} listeners with ss"
fi
LISTENER_PIDS="$(printf '%s\n' "$LISTENER_LINES" \
  | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
SUPERVISOR_PID="$(printf '%s\n' "$STATUS_BEFORE" \
  | sed -nE 's/.*pid ([0-9]+).*/\1/p' | head -n1)"

if [[ -n "$LISTENER_LINES" && -z "$LISTENER_PIDS" ]]; then
  fail_applied "port ${PORT} is occupied but its owner PID cannot be proven"
fi

for listener_pid in $LISTENER_PIDS; do
  if [[ -n "$SUPERVISOR_PID" && "$listener_pid" == "$SUPERVISOR_PID" ]]; then
    continue
  fi
  if [[ "$NO_HANDOFF" -eq 1 ]]; then
    fail_applied "port ${PORT} is already occupied by pid ${listener_pid} (--no-handoff)"
  fi

  ancestor="$$"
  for _ in 1 2 3 4 5 6 7 8; do
    [[ -n "$ancestor" && "$ancestor" != "1" ]] || break
    [[ "$ancestor" != "$listener_pid" ]] || fail_applied \
      "listener pid ${listener_pid} is an ancestor of this installer"
    ancestor="$(ps -o ppid= -p "$ancestor" 2>/dev/null | tr -d ' ')"
  done

  LOCK_OWNER_PID="$(_as_target "$PYTHON_EXECUTABLE" - "$PROJECT_ROOT" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
try:
    from server_manager import read_lock_status
    status = read_lock_status(sys.argv[1])
except Exception:
    raise SystemExit(0)
pid = status.get("pid")
if (
    status.get("running") is True
    and status.get("projectMatches") is True
    and status.get("externalOwner") in (None, "supervisor")
    and isinstance(pid, int)
):
    print(pid)
PY
)" || LOCK_OWNER_PID=""
  if [[ "$LOCK_OWNER_PID" != "$listener_pid" ]]; then
    fail_applied \
      "refusing to stop unknown listener pid ${listener_pid} on port ${PORT}"
  fi

  echo "Handing the proven project-local worker (pid ${listener_pid}) to supervisord..."
  _as_target "$PYTHON_EXECUTABLE" "${PROJECT_ROOT}/serverctl.py" \
    stop --source supervisor-install \
    || fail_applied "project lifecycle owner refused or failed the handoff"
  MANUAL_WORKER_STOPPED=1
done

if [[ "$MANUAL_WORKER_STOPPED" -eq 1 ]]; then
  LISTENER_LINES=""
  for _ in $(seq 1 30); do
    if ! LISTENER_LINES="$(listener_lines)"; then
      fail_applied "cannot verify port ${PORT} after the graceful handoff"
    fi
    [[ -z "$LISTENER_LINES" ]] && break
    sleep 1
  done
  [[ -z "$LISTENER_LINES" ]] \
    || fail_applied "port ${PORT} did not become free after the graceful handoff"
fi

SUPERVISOR_STATE_TOUCHED=1
_priv supervisorctl update >/dev/null \
  || fail_applied "supervisorctl update failed"

STATUS_AFTER="$(supervisor_status)"
if [[ "$STATUS_AFTER" != *RUNNING* && "$STATUS_AFTER" != *STARTING* ]]; then
  _priv supervisorctl start tofu >/dev/null \
    || fail_applied "supervisorctl could not start tofu"
fi

for _ in $(seq 1 45); do
  STATUS_AFTER="$(supervisor_status)"
  [[ "$STATUS_AFTER" == *RUNNING* ]] && break
  [[ "$STATUS_AFTER" != *FATAL* && "$STATUS_AFTER" != *BACKOFF* ]] \
    || fail_applied "tofu entered ${STATUS_AFTER}"
  sleep 1
done
[[ "$STATUS_AFTER" == *RUNNING* ]] \
  || fail_applied "tofu did not reach RUNNING within 45 seconds"

_as_target "$PYTHON_EXECUTABLE" "${PROJECT_ROOT}/healthcheck.py" \
  --runtime --port "$PORT" --wait 60 \
  || fail_applied "the supervised process failed the runtime health check"

CONFIG_APPLIED=0
echo "Tofu is supervised and runtime-healthy on port ${PORT}."
echo "Config: ${CONFIG_PATH}"
echo "Status: sudo supervisorctl status tofu"
echo "Logs:   ${PROJECT_ROOT}/logs/supervisor_tofu.log"
