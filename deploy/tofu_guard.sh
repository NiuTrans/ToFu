#!/usr/bin/env bash
#
# tofu_guard.sh — userspace watchdog for the Tofu server on :15000.
#
# WHY THIS EXISTS (2026-07-27)
# ----------------------------
# The server is repeatedly SIGKILLed by the container's OOM killer (shared
# cgroup: 220 GiB limit filled by page cache + kernel slab + fat IDE
# siblings, zero swap). SIGKILL is untrappable, so NOTHING inside the
# process can react — the only correct layer is an OUTSIDE parent that
# notices the death and relaunches. The durable fix was meant to be the
# root supervisord program (deploy/supervisor/tofu.conf), but installing
# it needs sudo, which this container does not grant the tofu user.
#
# This watchdog is the no-root equivalent: a tiny detached loop that
# checks the :15000 listener every INTERVAL seconds and relaunches the
# server when it is gone. It is kept alive by two crontab lines
# (--install writes them): `@reboot` and `* * * * *` (each run is an
# idempotent --ensure; a flock makes sure at most one loop exists).
#
# WHAT IT DOES ON A DEATH
# -----------------------
#   1. Appends an evidence block to logs/watchdog.log: death timestamp,
#      cgroup usage/limit + oom_kill counter at that instant, and the
#      tail of logs/cgroup_pressure.log (the pressure curve leading up
#      to the kill, written by lib/cgroup_guard's monitor).
#   2. Relaunches exactly like restart_15000.sh does (setsid nohup +
#      env python from .tofu_env.json), output APPENDED to
#      server_15000.log so consecutive lives keep their history.
#   3. Backs off on crash storms: >MAX_CONSECUTIVE_DEATHS relaunches
#      inside STORM_WINDOW seconds → the guard stops relaunching and
#      logs loudly (a broken build must not hammer the box forever).
#
# MUTEX
# -----
# Stands down when the root supervisord already owns tofu (same rule as
# restart_15000.sh) — two lifecycle owners must never fight. Also stands
# down when data/.tofu_guard_disabled exists (manual off switch).
#
# 2026-08-03 INCIDENT HARDENING
# ----------------------------
#   • serve-mode replay: server.py records the protocol it ACTUALLY
#     serves in data/.last_serve_mode; relaunch replays it via
#     TOFU_TLS=0/1. A cron-env relaunch otherwise re-runs proxy
#     detection blind and came up TLS behind a plain-HTTP proxy —
#     "socket hang up" for every client.
#   • protocol-aware healthy(): probe the recorded scheme first, then
#     the other — an http-only probe reported a READY TLS server as
#     dead for 60s.
#   • heartbeat wedge arbitration: a frozen event loop keeps its
#     listener BOUND and its process alive for hours (measured 6.5h),
#     so "listener present" no longer means alive. Stale loop-heartbeat
#     + dead HTTP, sustained over a streak window, = wedged → kill +
#     relaunch. Fresh heartbeat = busy, never killed.
#
# USAGE
# -----
#   deploy/tofu_guard.sh --install     # write the 2 crontab lines (idempotent)
#   deploy/tofu_guard.sh --uninstall   # remove them
#   deploy/tofu_guard.sh --ensure      # start the loop iff absent (cron calls this)
#   deploy/tofu_guard.sh --once        # single check+relaunch-if-dead, then exit
#   deploy/tofu_guard.sh --status      # is the loop alive? is the server alive?
#   deploy/tofu_guard.sh --stop        # disable relaunching (touch the off switch)
#   deploy/tofu_guard.sh --start       # re-enable

set -u

# PATH hardening (2026-07-27, verified live): cron's default PATH is
# /usr/bin:/bin — /usr/sbin is NOT in it, and `ss` lives at /usr/sbin/ss.
# A cron-launched guard without this line can never run `ss`, so
# listener_pids() is always empty and the guard relaunches against a LIVE
# server until the storm brake kills it. Pin a complete PATH for cron.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

SCRIPT="$(readlink -f "$0" 2>/dev/null || echo "$0")"
PROJ="$(cd "$(dirname "${SCRIPT}")/.." && pwd)"
PORT="${PORT:-15000}"
INTERVAL="${TOFU_GUARD_INTERVAL:-15}"
LOCK="${PROJ}/data/.tofu_guard.lock"
DISABLED_FLAG="${PROJ}/data/.tofu_guard_disabled"
STATE="${PROJ}/data/.tofu_guard_state"
WLOG="${PROJ}/logs/watchdog.log"
SLOG="${PROJ}/server_${PORT}.log"
MAX_CONSECUTIVE_DEATHS=5
STORM_WINDOW=600
BOOT_GRACE=180          # seconds a young server.py may boot before we judge it dead

# Probe-command seams (unit tests + debugging): override the binaries
# the guard shells out to. Defaults are the real system tools.
SS_CMD="${TOFU_GUARD_SS:-ss}"
CURL_CMD="${TOFU_GUARD_CURL:-curl}"
# The protocol the last boot ACTUALLY served (written by server.py).
SERVE_MODE_FILE="${TOFU_SERVE_MODE_FILE:-${PROJ}/data/.last_serve_mode}"
# Wedge arbitration thresholds: heartbeat older than STALE proves the
# loop is wedged; the wedge must then persist for STREAK seconds
# (continuously observed) before we kill — hysteresis against
# transient FUSE stalls.
WEDGE_STALE_SECS="${TOFU_GUARD_WEDGE_STALE:-180}"
WEDGE_STREAK_SECS="${TOFU_GUARD_WEDGE_STREAK:-120}"
WEDGE_STATE="${PROJ}/data/.tofu_guard_wedge"

cd "${PROJ}" || { echo "FATAL: cannot cd ${PROJ}"; exit 1; }
mkdir -p "${PROJ}/data" "${PROJ}/logs"

# ── env python, mirroring restart_15000.sh / bootstrap.py (.tofu_env.json) ──
detect_python() {
  local marker="${PROJ}/.tofu_env.json"
  if [ -f "${marker}" ]; then
    local py
    py="$(sed -n 's/.*"python"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${marker}" | head -n1)"
    if [ -n "${py}" ] && [ -x "${py}" ]; then printf '%s' "${py}"; return 0; fi
  fi
  command -v python3 || command -v python
}
PY="$(detect_python)"

listener_pids() {
  "${SS_CMD}" -ltnp 2>/dev/null \
    | awk -v pat=":${PORT}\$" '$4 ~ pat {print}' \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

_serve_mode() {
  # The protocol the LAST boot actually served. Empty when unknown —
  # never invent a decision for the relaunch to replay: a wrong replay
  # is worse than none.
  local m=''
  [ -f "${SERVE_MODE_FILE}" ] && read -r m < "${SERVE_MODE_FILE}"
  case "${m}" in http|https) printf '%s' "${m}" ;; esac
}

_guard_tls_env() {
  case "$1" in
    https) printf 'TOFU_TLS=1' ;;
    http)  printf 'TOFU_TLS=0' ;;
  esac
}

healthy() {
  # Protocol-aware (2026-08-03): probing ONLY http:// misreported a
  # READY TLS server as dead for 60s. Try the recorded scheme first,
  # then the other — either answering means alive.
  if [ "$(_serve_mode)" = 'https' ]; then
    "${CURL_CMD}" -s -k --max-time 2 "https://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1 && return 0
    "${CURL_CMD}" -s    --max-time 2 "http://127.0.0.1:${PORT}/api/health"  >/dev/null 2>&1 && return 0
  else
    "${CURL_CMD}" -s    --max-time 2 "http://127.0.0.1:${PORT}/api/health"  >/dev/null 2>&1 && return 0
    "${CURL_CMD}" -s -k --max-time 2 "https://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1 && return 0
  fi
  return 1
}

log() {
  echo "$(date '+%F %T') $*" | tee -a "${WLOG}" >&2
}

supervisord_owns() {
  # Stand down iff the root supervisord manages tofu AND the live listener
  # traces back to it (conf present is not enough — it may be stale).
  [ -f /etc/supervisor/conf.d/tofu.conf ] || return 1
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

record_death_evidence() {
  {
    echo "──────────────── $(date '+%F %T') — :${PORT} listener GONE"
    echo "  cgroup usage/limit : $(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null) / $(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null) bytes"
    echo "  cgroup oom_kill    : $(grep '^oom_kill' /sys/fs/cgroup/memory/memory.oom_control 2>/dev/null | awk '{print $2}')"
    echo "  pressure tail      :"
    tail -n 4 "${PROJ}/logs/cgroup_pressure.log" 2>/dev/null | sed 's/^/    /'
    echo "  server log tail    :"
    tail -n 5 "${SLOG}" 2>/dev/null | sed 's/^/    /'
  } >> "${WLOG}" 2>/dev/null
}

_heartbeat_file() {
  # Mirror server.py's _heartbeat_dir(): TOFU_HEARTBEAT_DIR, else
  # <TOFU_DB_LOCAL_ROOT or /tmp/tofu>/heartbeat/server.heartbeat — a
  # local-disk sidecar, never FUSE.
  if [ -n "${TOFU_HEARTBEAT_DIR:-}" ]; then
    printf '%s/server.heartbeat' "${TOFU_HEARTBEAT_DIR}"
  else
    printf '%s/heartbeat/server.heartbeat' "${TOFU_DB_LOCAL_ROOT:-/tmp/tofu}"
  fi
}

_lock_recorded_pid() {
  local ilock="${PROJ}/data/.server.lock"
  [ -f "${ilock}" ] || return 0
  head -n1 "${ilock}" 2>/dev/null | cut -d@ -f1 | tr -dc '0-9'
}

_wedge_proof_age() {
  # Print the heartbeat AGE iff the sidecar PROVES pid $1's loop is
  # wedged (heartbeat belongs to $1 AND is older than WEDGE_STALE_SECS).
  # Mirrors server.py's _holder_wedge_age: every ambiguous case —
  # missing / unparsable / pid mismatch / fresh — is NOT proof.
  local want="$1" hb hb_pid hb_ts age
  hb="$(_heartbeat_file)"
  [ -f "${hb}" ] || return 1
  hb_pid="$(sed -n 's/.*"pid"[^0-9]*\([0-9][0-9]*\).*/\1/p' "${hb}" | head -n1)"
  hb_ts="$(sed -n 's/.*"ts"[^0-9]*\([0-9][0-9]*\).*/\1/p' "${hb}" | head -n1)"
  [ -n "${hb_pid}" ] && [ -n "${hb_ts}" ] || return 1
  [ "${hb_pid}" = "${want}" ] || return 1
  age=$(( $(date +%s) - hb_ts ))
  [ "${age}" -gt "${WEDGE_STALE_SECS}" ] || return 1
  printf '%s\n' "${age}"
  return 0
}

_wedge_note()  { [ -f "${WEDGE_STATE}" ] || date +%s > "${WEDGE_STATE}"; }
_wedge_clear() { rm -f "${WEDGE_STATE}" 2>/dev/null || true; }
_wedge_streak_ok() {
  local first
  first="$(cat "${WEDGE_STATE}" 2>/dev/null)" || return 1
  [ -n "${first}" ] || return 1
  [ $(( $(date +%s) - first )) -ge "${WEDGE_STREAK_SECS}" ]
}

_wedge_act() {
  # $1 = stale age (evidence), $2 = space-separated pids to kill
  # (wedged lock holder + any listener pids, usually identical).
  log "[guard] WEDGED server confirmed: loop heartbeat stale $1s, HTTP dead, streak >= ${WEDGE_STREAK_SECS}s — killing: $2"
  local p i any
  for p in $2; do kill "${p}" 2>/dev/null || true; done
  for i in $(seq 1 10); do
    any=0
    for p in $2; do kill -0 "${p}" 2>/dev/null && any=1; done
    [ "${any}" = 0 ] && break
    sleep 1
  done
  for p in $2; do
    if kill -0 "${p}" 2>/dev/null; then
      log "[guard] wedged pid ${p} survived SIGTERM — SIGKILL"
      kill -9 "${p}" 2>/dev/null || true
    fi
  done
  sleep 1
  _wedge_clear
  record_death_evidence
  if ! storm_check "$(date +%s)"; then
    log "[guard] CRASH STORM: >${MAX_CONSECUTIVE_DEATHS} deaths in ${STORM_WINDOW}s —" \
        "NOT relaunching. Fix the build, then: rm ${STATE}"
    return 1
  fi
  relaunch
}

storm_check() {
  # $1 = now epoch. Returns 0 OK to relaunch, 1 = storm, stop relaunching.
  local now="$1" first deaths
  if [ -f "${STATE}" ]; then
    first="$(sed -n '1p' "${STATE}")"
    deaths="$(sed -n '2p' "${STATE}")"
  else
    first=0; deaths=0
  fi
  if [ $((now - first)) -gt "${STORM_WINDOW}" ]; then
    first="${now}"; deaths=0
  fi
  deaths=$((deaths + 1))
  printf '%s\n%s\n' "${first}" "${deaths}" > "${STATE}"
  [ "${deaths}" -le "${MAX_CONSECUTIVE_DEATHS}" ]
}

relaunch() {
  local mode tls_env
  mode="$(_serve_mode)"
  tls_env="$(_guard_tls_env "${mode}")"
  log "[guard] relaunching (serve-mode=${mode:-auto}${tls_env:+ ${tls_env}}): PORT=${PORT} setsid nohup ${PY} server.py >> ${SLOG}"
  {
    echo ""
    echo "════════════════ $(date '+%F %T') — launched by tofu_guard (serve-mode=${mode:-auto}) ════════════════"
  } >> "${SLOG}" 2>/dev/null
  # ${tls_env} must NOT sit in the assignment-prefix slot: bash only
  # recognises LITERAL name=value words there, so an expanded "TOFU_TLS=0"
  # became the COMMAND NAME ("command not found") and every relaunch died
  # instantly (2026-08-06 outage: 11 dead relaunches during an OOM crash).
  # env(1) applies the optional VAR=VAL instead; empty stays absent.
  PORT="${PORT}" BIND_HOST="${BIND_HOST:-0.0.0.0}" \
    env ${tls_env:+"${tls_env}"} \
    setsid nohup "${PY}" server.py >> "${SLOG}" 2>&1 &
  local newpid=$!
  # persist launch epoch (state line 3) for check_once's boot grace
  { sed -n '1,2p' "${STATE}" 2>/dev/null; echo "$(date +%s)"; } > "${STATE}.tmp" \
    && mv "${STATE}.tmp" "${STATE}"
  local i
  for i in $(seq 1 60); do
    if healthy; then
      log "[guard] server up (pid ${newpid}, healthy after ~${i}s)"
      return 0
    fi
    kill -0 "${newpid}" 2>/dev/null || {
      log "[guard] launched pid ${newpid} DIED during startup — see ${SLOG} tail"
      return 1
    }
    sleep 1
  done
  log "[guard] server did not answer /api/health within 60s (pid ${newpid})"
  return 1
}

check_once() {
  # Single guard pass. Exit 0 when the world is fine (or intentionally down).
  # Order: cheapest/safest stand-downs first; declaring death is LAST.
  if [ -f "${DISABLED_FLAG}" ]; then
    return 0
  fi
  # (a) restart_15000.sh holds an flock on data/.restart.lock for its whole
  # kill→relaunch span. Stand down while a manual restart is in flight, or
  # we would relaunch in the middle of it and win the port race.
  local rlock="${PROJ}/data/.restart.lock"
  if [ -e "${rlock}" ] && command -v flock >/dev/null 2>&1 \
     && ! flock -n "${rlock}" -c true 2>/dev/null; then
    log "[guard] manual restart in progress (${rlock} held) — standing down"
    return 0
  fi
  # (b) boot grace: we (or a human) launched a server moments ago; boot does
  # PG bootstrap + blueprint registration before binding — don't judge yet.
  if [ -f "${STATE}" ]; then
    local last_launch
    last_launch="$(sed -n '3p' "${STATE}" 2>/dev/null)"
    if [ -n "${last_launch}" ] && [ $(( $(date +%s) - last_launch )) -lt 90 ]; then
      return 0
    fi
  fi
  # (b1) re-exec marker (pt_aa3cd224b3b346e7): an HTTP re-exec writes
  # data/.reexec_in_progress just before the old process closes; the new
  # image clears it at boot-ready. Between those points there is NO listener
  # and — because os.execv KEEPS the pid — the process-age check (d) can
  # never see the re-exec (etimes predates it): this marker is the only
  # truthful signal for the re-exec window. 300s = 6.7x the measured 45s
  # window; a stale marker just falls through to the checks below.
  local reexec_marker="${PROJ}/data/.reexec_in_progress"
  if [ -f "${reexec_marker}" ]; then
    local marker_mtime marker_age
    marker_mtime="$(stat -c %Y "${reexec_marker}" 2>/dev/null || echo 0)"
    marker_age=$(( $(date +%s) - ${marker_mtime:-0} ))
    if [ "${marker_age}" -lt 300 ]; then
      log "[guard] re-exec in progress (marker age ${marker_age}s) — standing down"
      return 0
    fi
    log "[guard] stale re-exec marker (age ${marker_age}s) — ignoring"
  fi
  # (c) the positive proofs of life. TWO independent signals, because the
  # first alone once lied: cron's minimal PATH has no `ss`, so an empty
  # listener_pids meant "ss missing", NOT "server dead" (2026-07-27).
  local _lp _holder _wage
  _lp="$(listener_pids)"
  if [ -n "${_lp}" ]; then
    if healthy; then
      _wedge_clear
      return 0
    fi
    # Listener present but HTTP dead. Busy ≠ wedged — a loaded server
    # can answer slowly; only the loop heartbeat arbitrates. 2026-08-03:
    # the loop froze 04:40→11:14 with the socket BOUND the whole time,
    # and the old guard kept silent because it saw "listener present".
    _holder="$(_lock_recorded_pid)"
    [ -z "${_holder}" ] && _holder="$(printf '%s\n' "${_lp}" | head -n1)"
    _wage="$(_wedge_proof_age "${_holder}" || true)"
    if [ -z "${_wage}" ]; then
      log "[guard] listener present but HTTP unhealthy; heartbeat not stale — busy, not wedged (standing down)"
      return 0
    fi
    _wedge_note
    if _wedge_streak_ok; then
      _wedge_act "${_wage}" "${_holder} ${_lp}"
      return $?
    fi
    log "[guard] possible wedge: heartbeat stale ${_wage}s — watching (${WEDGE_STREAK_SECS}s streak before action)"
    return 0
  fi
  if healthy; then
    _wedge_clear
    return 0   # no socket visible, but HTTP answers — alive; never phantom-relaunch
  fi
  # (b2) boot-in-progress via the instance lock (pt_aa3cd224b3b346e7): no
  # listener and no HTTP, but a LIVE server.py holds data/.server.lock —
  # the boot is in progress. A memory-pressured boot took ~17 min on
  # 2026-07-28 (5.7x BOOT_GRACE): racing it launched 4 duplicate relaunches
  # that all died on the lock and polluted the crash-storm counter. Yield
  # WITHOUT a TTL — but only when the lock's recorded <pid> is ALIVE and is
  # server.py: a dead recorded pid means a STALE lock (SIGKILL / orphan-held
  # fd / FUSE release lag), and yielding then would strand a genuinely dead
  # server. The relaunched server reclaims the stale lock itself
  # (_reclaim_stale_instance_lock in server.py).
  local ilock="${PROJ}/data/.server.lock"
  if [ -f "${ilock}" ] && command -v flock >/dev/null 2>&1 \
     && ! flock -n "${ilock}" -c true 2>/dev/null; then
    local il_pid
    il_pid="$(head -n1 "${ilock}" 2>/dev/null | cut -d@ -f1 | tr -dc '0-9')"
    if [ -n "${il_pid}" ] && kill -0 "${il_pid}" 2>/dev/null \
       && tr '\0' ' ' < "/proc/${il_pid}/cmdline" 2>/dev/null | grep -q 'server\.py'; then
      # Wedge tie-break BEFORE the yield: the old code yielded here
      # WITHOUT a TTL — including to a server whose serve task died
      # hours ago (the 2026-08-03 11:14 state). A stale heartbeat
      # proving a wedged loop overrides the yield; anything ambiguous
      # (boot in progress writes no heartbeat yet) keeps the safe yield.
      local _wage
      _wage="$(_wedge_proof_age "${il_pid}" || true)"
      if [ -n "${_wage}" ]; then
        _wedge_note
        if _wedge_streak_ok; then
          _wedge_act "${_wage}" "${il_pid}"
          return $?
        fi
        log "[guard] possible wedge (no listener): heartbeat stale ${_wage}s — watching"
        return 0
      fi
      local il_age
      il_age="$(ps -o etimes= -p "${il_pid}" 2>/dev/null | tr -d ' ')"
      if [ -n "${il_age}" ] && [ "${il_age}" -gt 600 ]; then
        log "[guard] WARNING: instance lock held ${il_age}s by pid ${il_pid} with no listener — boot unusually slow or wedged; still yielding (operator's call)"
      else
        log "[guard] instance lock held by live server.py pid ${il_pid} (boot in progress) — standing down"
      fi
      return 0
    fi
    log "[guard] instance lock held but recorded pid ${il_pid:-none} is dead/gone — STALE, proceeding"
  fi
  # (d) mid-boot: a young server.py process exists but has not bound yet.
  local spid etimes
  spid="$(pgrep -f 'python server\.py' | head -n1)"
  if [ -n "${spid}" ]; then
    etimes="$(ps -o etimes= -p "${spid}" 2>/dev/null | tr -d ' ')"
    if [ -n "${etimes}" ] && [ "${etimes}" -lt "${BOOT_GRACE}" ]; then
      return 0
    fi
  fi
  if supervisord_owns; then
    log "[guard] :${PORT} owned by root supervisord — standing down"
    return 0
  fi
  # Only NOW is death credible: no socket, no HTTP, no young process.
  record_death_evidence
  if ! storm_check "$(date +%s)"; then
    log "[guard] CRASH STORM: >${MAX_CONSECUTIVE_DEATHS} deaths in ${STORM_WINDOW}s —" \
        "NOT relaunching. Fix the build, then: rm ${STATE}"
    return 1
  fi
  relaunch
}

loop() {
  # Hold the singleton flock for the loop's whole life.
  exec 9>"${LOCK}"
  if ! flock -n 9; then
    exit 0  # another loop already owns it
  fi
  log "[guard] watchdog loop started (interval=${INTERVAL}s, pid $$)"
  while true; do
    check_once
    sleep "${INTERVAL}"
  done
}

ensure() {
  # Idempotent: start the detached loop iff none holds the lock.
  if command -v flock >/dev/null 2>&1; then
    if flock -n "${LOCK}" -c 'true' 2>/dev/null; then
      : # lock free — no loop alive; start one below
    else
      exit 0  # a loop is alive
    fi
  fi
  setsid nohup "${SCRIPT}" --loop >/dev/null 2>&1 &
  echo "[guard] watchdog loop launched (pid $!)"
}

CRON_TAG="# tofu_guard"
install_cron() {
  local current
  current="$(crontab -l 2>/dev/null | grep -v "${CRON_TAG}")"
  {
    [ -n "${current}" ] && printf '%s\n' "${current}"
    printf '@reboot %s --ensure >/dev/null 2>&1 %s\n' "${SCRIPT}" "${CRON_TAG}"
    printf '* * * * * %s --ensure >/dev/null 2>&1 %s\n' "${SCRIPT}" "${CRON_TAG}"
  } | crontab -
  echo "[guard] crontab installed:"
  crontab -l | grep "${CRON_TAG}"
}

uninstall_cron() {
  crontab -l 2>/dev/null | grep -v "${CRON_TAG}" | crontab -
  echo "[guard] crontab lines removed (the running loop, if any, keeps running;"
  echo "        kill it with: pkill -f 'tofu_guard.sh --loop')"
}

# Sourcing seam for unit tests: with TOFU_GUARD_SOURCE_ONLY=1 the
# functions load but nothing runs (no --ensure side effects).
if [ -z "${TOFU_GUARD_SOURCE_ONLY:-}" ]; then
case "${1:---ensure}" in
  --install)   install_cron ;;
  --uninstall) uninstall_cron ;;
  --ensure)    ensure ;;
  --loop)      loop ;;
  --once)      check_once ;;
  --stop)      touch "${DISABLED_FLAG}"; echo "[guard] relaunching DISABLED (${DISABLED_FLAG})" ;;
  --start)     rm -f "${DISABLED_FLAG}"; echo "[guard] relaunching enabled" ;;
  --status)
    echo "guard loop : $(flock -n "${LOCK}" -c 'echo not-running' 2>/dev/null || echo running)"
    echo "server     : $([ -n "$(listener_pids)" ] && echo "listening on :${PORT}" || echo DOWN)"
    echo "disabled   : $([ -f "${DISABLED_FLAG}" ] && echo yes || echo no)"
    echo "storm state: $(paste -sd/ "${STATE}" 2>/dev/null || echo none)"
    ;;
  *) echo "usage: $0 [--install|--uninstall|--ensure|--once|--stop|--start|--status]" >&2; exit 2 ;;
esac
fi
