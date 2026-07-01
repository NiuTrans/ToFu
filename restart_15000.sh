#!/usr/bin/env bash
#
# restart_15000.sh — reload the Tofu server on :15000 so it picks up the
# Project Brain path-normalization backend fix (lib/conversations/project_{feed,board,charter}.py).
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │ RUN THIS FROM A TERMINAL THAT IS **NOT** A CHILD OF THE :15000 SERVER.    │
# │ A plain VS Code terminal is fine. Do NOT run it from inside a Tofu agent  │
# │ shell — that shell is a child of the :15000 process, so killing the       │
# │ server would also kill the shell running this script (self-plug-pull).    │
# └─────────────────────────────────────────────────────────────────────────┘
#
# Safe to re-run (idempotent): if no server is on :15000 it just launches one.
# It does NOT use `set -e` on the whole script so a missing process (nothing to
# kill) is not treated as a fatal error.

PROJ="/path/to/your/project"
PY="/path/to/your/project"
PORT=15000
PATTERN="server.py --port ${PORT}"
LOG="server_${PORT}.log"

echo "════════════════════════════════════════════════════════════════"
echo "[0/5] restart_15000.sh — reloading Tofu server on :${PORT}"
echo "      project: ${PROJ}"
cd "${PROJ}" || { echo "FATAL: cannot cd into project dir"; exit 1; }

# ── Guard: refuse to run if THIS shell is a descendant of the :15000 server. ──
# Walk our own parent chain; if we hit the current :15000 listener PID, abort —
# killing it would terminate this very shell.
LISTENER_PID="$(ss -ltnp 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p {print $0}' \
                | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)"
if [ -n "${LISTENER_PID}" ]; then
  up=$$
  for _ in 1 2 3 4 5 6 7 8; do
    [ -z "${up}" ] || [ "${up}" = "1" ] && break
    if [ "${up}" = "${LISTENER_PID}" ]; then
      echo "FATAL: this shell (pid $$) is a DESCENDANT of the :${PORT} server"
      echo "       (pid ${LISTENER_PID}). Killing it would terminate this shell."
      echo "       Re-run from a plain VS Code terminal, not a Tofu agent shell."
      exit 2
    fi
    up="$(ps -o ppid= -p "${up}" 2>/dev/null | tr -d ' ')"
  done
fi

# ── [1/5] Stop the current :15000 server. ──
echo "[1/5] Stopping current server (pattern: '${PATTERN}') ..."
if pgrep -f "${PATTERN}" >/dev/null 2>&1; then
  pkill -f "${PATTERN}"
  echo "      SIGTERM sent."
else
  echo "      No running '${PATTERN}' process found — nothing to stop."
fi

# ── [2/5] Wait for the port to free (up to ~20s). ──
echo "[2/5] Waiting for :${PORT} to free ..."
freed=0
for i in $(seq 1 20); do
  if ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    freed=1
    echo "      Port :${PORT} is free (after ${i}s)."
    break
  fi
  sleep 1
done
if [ "${freed}" != "1" ]; then
  echo "      WARNING: :${PORT} still bound after 20s. Forcing kill of stragglers ..."
  pkill -9 -f "${PATTERN}" 2>/dev/null
  sleep 2
fi

# ── [3/5] Relaunch exactly as the previous process was started. ──
echo "[3/5] Relaunching: nohup ${PY} server.py --port ${PORT} > ${LOG} 2>&1 &"
nohup "${PY}" server.py --port "${PORT}" > "${LOG}" 2>&1 &
NEWPID=$!
echo "      Launched pid ${NEWPID}; logging to ${LOG}"

# ── [4/5] Wait for the server to accept connections (up to ~40s; startup does
#          PG bootstrap + blueprint registration, so give it room). ──
echo "[4/5] Waiting for the server to come up on :${PORT} ..."
BASE="http://127.0.0.1:${PORT}"
up_ok=0
for i in $(seq 1 40); do
  if curl -s --max-time 2 "${BASE}/api/health" >/dev/null 2>&1; then
    up_ok=1
    echo "      Server responding (after ${i}s)."
    break
  fi
  sleep 1
done
if [ "${up_ok}" != "1" ]; then
  echo "      ERROR: server did not respond within 40s. Tail of ${LOG}:"
  tail -n 30 "${LOG}" 2>/dev/null
  exit 3
fi

# ── [5/5] Self-verify the fix took: BOTH the stripped and trailing-slash board
#          queries must now return done=3, and GET / must serve the fresh
#          bundle/style hashes. ──
echo "[5/5] Verifying the Project Brain path-normalization fix is LIVE ..."
enc="$(${PY} -c "import urllib.parse;print(urllib.parse.quote('${PROJ}'))")"
encs="$(${PY} -c "import urllib.parse;print(urllib.parse.quote('${PROJ}/'))")"

read_done () {  # $1 = url-encoded path -> prints "done=N total=M"
  curl -s --max-time 5 "${BASE}/api/v1/project/board?path=$1" \
    | ${PY} -c 'import sys,json;d=json.load(sys.stdin);print("done=%s total=%s"%(d.get("done"),len(d.get("tasks",[]))))' \
    2>/dev/null || echo "done=ERR total=ERR"
}

# The DOUBLE-encoded variant is what the VS Code proxy actually sends the
# browser's already-encoded query as (%2F -> %252F). This is THE case that
# blanked the panel; it must resolve too after the route-decode fix.
encd="$(${PY} -c "import urllib.parse;print(urllib.parse.quote(urllib.parse.quote('${PROJ}')))")"
stripped="$(read_done "${enc}")"
slashed="$(read_done "${encs}")"
doubled="$(read_done "${encd}")"
echo "      board (stripped path)   -> ${stripped}"
echo "      board (trailing slash)  -> ${slashed}"
echo "      board (double-encoded)  -> ${doubled}   [VS Code proxy case]"

echo "      GET / serves:"
curl -s --max-time 5 "${BASE}/" \
  | grep -oE '(bundle-[a-f0-9]+\.js|styles-[a-f0-9]+\.css)' | sort -u | sed 's/^/        /'

# All three variants must resolve to the same non-empty board for the fix to
# be live: stripped (baseline), trailing-slash (normalize fix), AND
# double-encoded (the proxy re-encode fix — the actual blank-panel cause).
sd="$(echo "${stripped}" | grep -oE 'done=[0-9]+' | cut -d= -f2)"
ld="$(echo "${slashed}"  | grep -oE 'done=[0-9]+' | cut -d= -f2)"
dd="$(echo "${doubled}"  | grep -oE 'done=[0-9]+' | cut -d= -f2)"
echo "────────────────────────────────────────────────────────────────"
if [ -n "${sd}" ] && [ "${sd}" = "${ld}" ] && [ "${sd}" = "${dd}" ] && [ "${sd}" != "0" ]; then
  echo "✅ FIX LIVE: stripped, trailing-slash AND double-encoded board all agree (done=${sd}). Restart complete."
else
  echo "❌ FIX NOT CONFIRMED: stripped done=${sd:-?}, slashed done=${ld:-?}, double done=${dd:-?}."
  echo "   Expected all three equal and > 0. The double-encoded case is the"
  echo "   VS Code proxy path — if only it is 0, routes/api_v1/project.py's"
  echo "   _decoded_path_arg fix is not loaded (stale bytecode → restart again)."
  exit 4
fi
echo "════════════════════════════════════════════════════════════════"
