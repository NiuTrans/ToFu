#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  deploy/supervisor/install.sh — hand the :15000 server to supervisord.
# ════════════════════════════════════════════════════════════════════════════
#
#  WHY: the server was running as a bare PPID=1 process nobody relaunches, so a
#  crash/OOM left it dead and every manual `python server.py` lost the :15000
#  bind race to the still-alive old process. This installs the repo's
#  deploy/supervisor/tofu.conf into the host supervisord (autostart +
#  autorestart), so after a ONE-TIME handoff every future restart is just
#  `supervisorctl restart tofu` and a crash self-heals.
#
#  IDEMPOTENT: safe to re-run. It (1) validates the repo conf, (2) installs it
#  only if changed, (3) does the one-time handoff (stop a hand-started bare
#  listener so supervisord owns the single instance), (4) reread+update, (5)
#  verifies the program reaches RUNNING.
#
#  PRIVILEGE: /etc/supervisor/conf.d is root-owned and the supervisorctl socket
#  is usually root-only. This script auto-detects whether it can act (root, or
#  passwordless sudo). If it CANNOT, it prints the EXACT copy-paste commands and
#  exits WITHOUT half-installing — never leaves a partial state.
#
#  Run from ANY shell (it does not kill the shell's own ancestry — it stops the
#  :15000 LISTENER by PID, and refuses if that PID is an ancestor of this shell,
#  same self-plug-pull guard as restart_15000.sh).

set -u
PROJ="/path/to/your/project"
CONF_SRC="${PROJ}/deploy/supervisor/tofu.conf"
CONF_DST="/etc/supervisor/conf.d/tofu.conf"
PROG="tofu"
PORT=15000

echo "════════════════════════════════════════════════════════════════"
echo "[0/6] deploy/supervisor/install.sh — hand :${PORT} to supervisord"
cd "${PROJ}" || { echo "FATAL: cannot cd into ${PROJ}"; exit 1; }

# ── privilege helper: run a command as root if possible, else signal caller. ──
# Prints nothing; returns 0 if it could run privileged, 1 if it could not.
_priv() {
  if [ "$(id -u)" = "0" ]; then "$@"; return $?; fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n "$@"; return $?
  fi
  return 1
}
_can_priv() {
  [ "$(id -u)" = "0" ] && return 0
  command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null && return 0
  return 1
}

# ── [1/6] Validate the repo conf BEFORE touching the host. ──
echo "[1/6] Validating ${CONF_SRC} ..."
[ -f "${CONF_SRC}" ] || { echo "FATAL: repo conf missing: ${CONF_SRC}"; exit 1; }
# Fast structural check (same fields tests/test_supervisor_conf.py enforces).
for kv in "command=" "directory=${PROJ}" "autostart=true" "autorestart=true" \
          "stopsignal=TERM" 'PORT="15000"'; do
  grep -q -- "${kv}" "${CONF_SRC}" || { echo "FATAL: conf missing '${kv}'"; exit 1; }
done
grep -q "envs/tofu/bin/python server.py" "${CONF_SRC}" \
  || { echo "FATAL: conf command must run the tofu env python on server.py"; exit 1; }
echo "      Conf OK (command / directory / autostart / autorestart / TERM / PORT)."

# ── command -v supervisord present? (the host daemon must exist). ──
if ! command -v supervisorctl >/dev/null 2>&1 && [ ! -S /var/run/supervisor.sock ]; then
  echo "[!] supervisorctl not found. Is supervisord installed on this host?"
  echo "    (A supervisord daemon was observed at PID 1139; if that is another"
  echo "     namespace, install supervisor here first.)"
fi

# ── [2/6] Privilege check — if we can't act, print exact commands and STOP. ──
if ! _can_priv; then
  echo "[2/6] No root / passwordless-sudo — cannot install to ${CONF_DST}."
  echo "      Run these EXACT commands yourself (copy-paste), then re-run me to verify:"
  echo ""
  echo "        sudo cp ${CONF_SRC} ${CONF_DST}"
  echo "        # one-time handoff: stop the hand-started listener so supervisord owns it"
  echo "        sudo kill \$(ss -ltnp 2>/dev/null | awk '/:${PORT} /{print}' | grep -oE 'pid=[0-9]+' | cut -d= -f1) 2>/dev/null; sleep 3"
  echo "        sudo supervisorctl reread && sudo supervisorctl update"
  echo "        sudo supervisorctl status ${PROG}     # expect RUNNING"
  echo ""
  echo "      (After this one-time install, restart with: sudo supervisorctl restart ${PROG})"
  exit 0
fi

# ── [3/6] Install the conf (only if changed — idempotent). ──
echo "[3/6] Installing conf to ${CONF_DST} ..."
if _priv test -f "${CONF_DST}" && _priv cmp -s "${CONF_SRC}" "${CONF_DST}"; then
  echo "      Already up-to-date (byte-identical) — skipping copy."
else
  _priv cp "${CONF_SRC}" "${CONF_DST}" || { echo "FATAL: cp to ${CONF_DST} failed"; exit 1; }
  echo "      Installed."
fi

# ── [4/6] One-time handoff: stop a hand-started bare listener so supervisord
#          owns the single instance (else the supervised one aborts on the
#          :${PORT} instance lock). Skip if the listener is ALREADY a
#          supervisord child (re-run case). Refuse if it is an ancestor of this
#          shell (self-plug-pull). ──
echo "[4/6] One-time handoff of any hand-started :${PORT} listener ..."
LPID="$(ss -ltnp 2>/dev/null | awk -v p=":${PORT} " '$0 ~ p{print}' | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f1)"
if [ -z "${LPID}" ]; then
  echo "      No current listener on :${PORT} — supervisord will start a fresh one."
else
  # self-plug-pull guard
  up=$$; anc=0
  for _ in 1 2 3 4 5 6 7 8; do
    { [ -z "${up}" ] || [ "${up}" = "1" ]; } && break
    [ "${up}" = "${LPID}" ] && { anc=1; break; }
    up="$(awk '/^PPid:/{print $2}' /proc/${up}/status 2>/dev/null)"
  done
  if [ "${anc}" = "1" ]; then
    echo "      FATAL: :${PORT} listener pid ${LPID} is an ANCESTOR of this shell."
    echo "             Run this script from a terminal that is NOT a child of the"
    echo "             Tofu server (a plain terminal), so stopping it won't kill us."
    exit 2
  fi
  # already supervised?
  is_sup=0; p="${LPID}"; hops=0
  while [ -n "${p}" ] && [ "${p}" != "1" ] && [ "${hops}" -lt 12 ]; do
    comm="$(ps -o comm= -p "${p}" 2>/dev/null | tr -d ' ')"
    case "${comm}" in *supervisord*) is_sup=1; break ;; esac
    p="$(ps -o ppid= -p "${p}" 2>/dev/null | tr -d ' ')"; hops=$((hops+1))
  done
  if [ "${is_sup}" = "1" ]; then
    echo "      Listener pid ${LPID} is ALREADY a supervisord child — no handoff needed."
  else
    echo "      Stopping hand-started listener pid ${LPID} (SIGTERM; wait; escalate) ..."
    _priv kill "${LPID}" 2>/dev/null
    for i in $(seq 1 20); do
      ss -ltn 2>/dev/null | grep -q ":${PORT} " || { echo "      Port :${PORT} freed after ${i}s."; break; }
      sleep 1
    done
    if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
      echo "      Port still bound — escalating to SIGKILL."
      _priv kill -9 "${LPID}" 2>/dev/null; sleep 2
    fi
  fi
fi

# ── [5/6] Load the program into supervisord. ──
echo "[5/6] supervisorctl reread && update ..."
_priv supervisorctl reread || echo "      (reread returned non-zero — continuing to update)"
_priv supervisorctl update || { echo "FATAL: supervisorctl update failed"; exit 3; }

# ── [6/6] Verify it reaches RUNNING (poll; startsecs=15 in the conf). ──
echo "[6/6] Verifying ${PROG} is RUNNING ..."
ok=0
for i in $(seq 1 30); do
  st="$(_priv supervisorctl status "${PROG}" 2>/dev/null)"
  case "${st}" in
    *RUNNING*) ok=1; echo "      ${st}"; break ;;
    *FATAL*|*BACKOFF*) echo "      ${st}"; echo "      FATAL: program entered ${st%% *} — check logs/supervisor_tofu.log"; exit 4 ;;
  esac
  sleep 1
done
if [ "${ok}" != "1" ]; then
  echo "      ERROR: ${PROG} did not reach RUNNING within 30s. Last status:"
  _priv supervisorctl status "${PROG}" 2>/dev/null || true
  echo "      Tail: tail -n 30 ${PROJ}/logs/supervisor_tofu.log"
  exit 4
fi

echo "════════════════════════════════════════════════════════════════"
echo "✅ Managed: :${PORT} is now supervised (autostart + autorestart)."
echo "   Restart from now on:   sudo supervisorctl restart ${PROG}"
echo "   Status:                sudo supervisorctl status ${PROG}"
echo "   Then verify the deploy: bash tests/cache_deploy_verdict.sh"
echo "════════════════════════════════════════════════════════════════"
