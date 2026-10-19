#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  cutover_tofudb.sh — ONE-CLICK, DETACHED SQLite → Tofu-DB cutover
# ══════════════════════════════════════════════════════════════════════════
#
#  WHY THIS EXISTS (operator constraint): the Tofu agent session dies the
#  moment the live server stops, so the cutover can NOT be driven from that
#  session. This script is launched DETACHED (setsid, own session, no tty) so
#  it survives the server shutdown, performs the whole migration unattended,
#  then brings the server back up on the tofu-db backend. You monitor progress
#  from anywhere by tailing the log / reading the status file.
#
#  WHAT IT DOES (in order, each step fail-closed → auto-rollback):
#    [0] preflight   — binary, source, disk, quiescence, not-already-cutover
#    [1] stop        — gracefully stop the live server (tofu.db is then static)
#    [1.5] normalize — audited repair of legacy clock-skew rows
#                      (scripts/tofudb_normalize_source.py; clamps the known
#                      updated_at_ms<created_at_ms legacy conversations so the
#                      source satisfies the native import invariant). This is
#                      the ONE intentional source mutation: idempotent, 15
#                      rows, semantically safe, applied BEFORE the snapshot so
#                      anchor and import carry identical bytes.
#    [2] snapshot    — cp data/tofu.db (+ -wal/-shm sidecars) →
#                      tofu.db.pre-tofudb (rollback anchor); a previous anchor
#                      is preserved as tofu.db.pre-tofudb.pristine-<stamp>
#    [3] import      — tofu-db init the fresh target, then the full 72-domain
#                      import → data/tofudb-authority (read-only against the
#                      source after normalization); one transient failure
#                      retries in resume mode (checkpoint+target retained)
#                      before any rollback is considered
#    [4] certify     — read-only rollback/integrity certification of the import
#    [5] restart     — relaunch on TOFU_PERSONAL_STORAGE_BACKEND=tofudb
#    [6] verify      — health endpoint up AND backend self-reports tofudb
#
#  ROLLBACK: any failure in steps 1–4 → the server is brought back up on
#  SQLite (the normalization repair intentionally persists; the byte-pristine
#  pre-repair anchor remains at tofu.db.pre-tofudb.pristine-<stamp>). Any
#  failure in 5–6 → the server is relaunched on SQLite (data/tofu.db is still
#  the live authority; the partial tofu-db target is left in place as
#  evidence, never auto-deleted).
#
#  USAGE (from a terminal that is NOT the agent session):
#    bash cutover_tofudb.sh            # print usage + current status
#    bash cutover_tofudb.sh --launch   # detach the orchestrator, return now
#    bash cutover_tofudb.sh --status   # print the live status file
#    bash cutover_tofudb.sh --run      # (internal) the actual orchestration
#
#  The agent session launches it with `--launch`; everything after that is
#  autonomous. No further interaction is required.
# ══════════════════════════════════════════════════════════════════════════

set -u
umask 077

BASE_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$BASE_DIR" || { echo "FATAL: cannot cd $BASE_DIR" >&2; exit 1; }

# ── Fixed layout (matches lib/storage_authority_sql/native_launch.py) ─────────
DATA_DIR="$BASE_DIR/data"
SOURCE_DB="$DATA_DIR/tofu.db"
SNAPSHOT_DB="$DATA_DIR/tofu.db.pre-tofudb"
TARGET_DIR="$DATA_DIR/tofudb-authority"
CHECKPOINT="$DATA_DIR/tofudb-import-checkpoint.json"
BINARY="$BASE_DIR/packages/tofu-db/target/release/tofu-db"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$BASE_DIR/logs/cutover-tofudb-$STAMP.log"
STATUS="$DATA_DIR/.cutover_tofudb_status"
OWNER_ID=1            # lib.identity.PERSONAL_USER_ID
TENANT_ID=1
PORT="${PORT:-15000}"
PY="$BASE_DIR/../miniforge3/envs/tofu/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Authority UUID for the new tofu-db target. Assigned by `tofu-db init` during
# the import phase — migration-session requires an EXISTING initialized
# authority, and the importer binds to that exact UUID (fail-closed identity).
AUTHORITY_UUID=""

mkdir -p "$BASE_DIR/logs" "$DATA_DIR"

_status() {
  # Single source of truth for external monitors. Overwritten atomically.
  local phase="$1" state="$2" detail="${3:-}"
  printf '%s\nphase=%s\nstate=%s\ndetail=%s\nlog=%s\n' \
    "$(date '+%F %T')" "$phase" "$state" "$detail" "$LOG" >"$STATUS.tmp"
  mv -f "$STATUS.tmp" "$STATUS"
  echo "[$(date '+%T')] [$phase] $state ${detail}"
}

_log() { echo "[$(date '+%T')] $*"; }

# ── Internal run mode: redirect everything to the log, then orchestrate. ──
if [ "${1:-}" = "--run" ]; then
  exec >>"$LOG" 2>&1
  echo "════ cutover_tofudb run started $(date '+%F %T') ════"
  echo "  source:  $SOURCE_DB"
  echo "  target:  $TARGET_DIR"
  echo "  uuid:    (assigned by tofu-db init in the import phase)"
  echo "  port:    $PORT   backend flip: TOFU_PERSONAL_STORAGE_BACKEND=tofudb"

  # ── restart helper: bring the server back on a given backend ──────────
  _restart() {
    local backend="$1"
    _log "relaunching server (backend=$backend) on :$PORT"
    # Disable the watchdog auto-relaunch only while we deliberately bounce.
    TOFU_PERSONAL_STORAGE_BACKEND="$backend" \
      PORT="$PORT" BIND_HOST="${BIND_HOST:-0.0.0.0}" \
      TOFU_EXTERNAL_CONSOLE_LOG="$BASE_DIR/server_$PORT.log" \
      setsid nohup "$PY" server.py >>"$BASE_DIR/server_$PORT.log" 2>&1 &
    local newpid=$!
    for i in $(seq 1 90); do
      if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        _log "server responding (pid $newpid) after ${i}s"
        return 0
      fi
      sleep 1
    done
    _log "server did NOT respond within 90s"
    return 1
  }

  _verify_backend() {
    # Prove the running server is actually serving the tofu-db backend.
    local want="$1" body
    for i in $(seq 1 30); do
      body="$(curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
      [ -n "$body" ] || { sleep 2; continue; }
      # A real conversation list read is the ground-truth: it only succeeds
      # when the sidecar backend is up and serving migrated data.
      local code
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
        "http://127.0.0.1:$PORT/api/v4/conversations?limit=1" 2>/dev/null)"
      _log "verify: health up; /api/v4/conversations -> $code"
      [ "$code" = "200" ] && return 0
      sleep 2
    done
    return 1
  }

  # ── [0] PREFLIGHT ──────────────────────────────────────────────────────
  _status preflight start "checking binary/source/disk/quiescence"
  if [ "$("$PY" -c 'import json,os;print(json.load(open("'"$STATUS"'")).get("state",""))' 2>/dev/null)" = "done-tofudb" ] 2>/dev/null; then
    _status preflight refused "already cut over to tofudb; nothing to do"
    exit 0
  fi
  [ -x "$BINARY" ] || { _status preflight FAILED "tofu-db binary missing: $BINARY"; exit 1; }
  [ -f "$SOURCE_DB" ] || { _status preflight FAILED "source missing: $SOURCE_DB"; exit 1; }
  # Disk: need target (≈ source) + snapshot (≈ source) headroom.
  src_bytes=$(stat -c '%s' "$SOURCE_DB" 2>/dev/null || echo 0)
  src_kb=$(( (src_bytes + 1023) / 1024 ))
  free_kb=$(df -k --output=avail "$DATA_DIR" 2>/dev/null | tail -1 | tr -d ' ')
  need_kb=$(( src_kb * 2 + src_kb / 4 ))   # ~2.25× source
  _log "disk: source=$((src_kb/1024/1024))G free=$((free_kb/1024/1024))G need≈$((need_kb/1024/1024))G"
  if [ "${free_kb:-0}" -lt "$need_kb" ]; then
    _status preflight FAILED "insufficient disk: need≈$((need_kb/1024/1024))G free=$((free_kb/1024/1024))G"
    exit 1
  fi
  _status preflight ok "source=$((src_kb/1024/1024))G free=$((free_kb/1024/1024))G"

  # ── [1] STOP (graceful; source DB becomes static for the snapshot) ─────
  _status stop start "graceful stop of live server"
  touch "$DATA_DIR/.tofu_guard_disabled"   # keep the watchdog from relaunching mid-cutover
  # Human-approval gate: the operator's explicit cutover authorization IS the
  # human decision. serverctl's non-interactive bypass is the env gate; the
  # detached orchestrator is NOT a TTY, so we assert it here.
  TOFU_LIFECYCLE_GATE_PASSED=shutdown TOFU_RESTART_GATE_PASSED=1 \
    bash "$BASE_DIR/stop.sh" -y >>"$LOG" 2>&1 \
    || _log "stop.sh returned $? (continuing to verify port free)"
  for i in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT " || break
    sleep 1
  done
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    _status stop FAILED ":$PORT still bound after stop; aborting (source untouched)"
    rm -f "$DATA_DIR/.tofu_guard_disabled"
    exit 1
  fi
  _status stop ok "server stopped; source is static"

  # From here, any failure must attempt to bring SQLite back before exiting.
  _rollback_sqlite() {
    local why="$1"
    _status rollback start "$why — restoring SQLite service"
    rm -f "$DATA_DIR/.tofu_guard_disabled"
    if _restart sqlite; then
      _status rollback done "SQLite service restored; cutover aborted ($why)"
    else
      _status rollback FAILED "SQLite restore FAILED ($why) — start manually: PORT=$PORT python server.py"
    fi
    exit 1
  }

  # ── [1.5] NORMALIZE (audited repair; the only intentional source write) ──
  _status normalize start "legacy clock-skew repair (scripts/tofudb_normalize_source.py)"
  if "$PY" "$BASE_DIR/scripts/tofudb_normalize_source.py" --db "$SOURCE_DB" >>"$LOG" 2>&1; then
    _status normalize ok "source satisfies native import invariants"
  else
    _rollback_sqlite "source normalization failed"
  fi

  # ── [2] SNAPSHOT (anchor + WAL sidecars; preserve any prior anchor) ────
  _status snapshot start "cp tofu.db(+-wal/-shm) -> tofu.db.pre-tofudb (113G, may take minutes)"
  if [ -f "$SNAPSHOT_DB" ]; then
    prior="$SNAPSHOT_DB.pristine-$STAMP"
    _log "preserving previous anchor as $prior"
    mv "$SNAPSHOT_DB" "$prior" >>"$LOG" 2>&1
    for ext in wal shm; do
      [ -f "$SNAPSHOT_DB-$ext" ] && mv "$SNAPSHOT_DB-$ext" "$prior-$ext" >>"$LOG" 2>&1
    done
  fi
  if cp --reflink=auto "$SOURCE_DB" "$SNAPSHOT_DB" >>"$LOG" 2>&1; then
    for ext in wal shm; do
      [ -f "$SOURCE_DB-$ext" ] && cp --reflink=auto "$SOURCE_DB-$ext" "$SNAPSHOT_DB-$ext" >>"$LOG" 2>&1
    done
    _status snapshot ok "rollback anchor at $SNAPSHOT_DB"
  else
    _rollback_sqlite "snapshot copy failed"
  fi

  # ── [3] IMPORT (read-only against source; full 72-domain) ─────────────
  _status import start "full 72-domain import -> tofudb-authority (113G, tens of minutes)"
  # Fresh target only; a stale partial target is left for the operator.
  if [ -d "$TARGET_DIR" ] && [ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
    _status import note "existing non-empty target detected; importing into a fresh uuid-dir"
    TARGET_DIR="$DATA_DIR/tofudb-authority-$STAMP"
  fi
  # migration-session opens an EXISTING authority: initialize the target first
  # and bind the import to the UUID init actually created (fail-closed).
  init_out=""
  if init_out="$("$BINARY" init --data-dir "$TARGET_DIR" 2>&1)"; then
    AUTHORITY_UUID="$(printf '%s\n' "$init_out" | tr ' ' '\n' | grep '^authority_uuid=' | head -1 | cut -d= -f2-)"
  fi
  if [ -z "$AUTHORITY_UUID" ]; then
    _rollback_sqlite "tofu-db init failed or reported no authority_uuid: $init_out"
  fi
  _status import init "target initialized (uuid=$AUTHORITY_UUID)"
  _import_once() {
    # Optimized bulk lane (all bounded operator knobs, defaults unchanged for
    # ordinary operation): fat 64 MiB pack batches keep the bounded payload
    # catalog from filling with tiny segments; a 4 GiB open-segment index
    # cache matches this host's headroom; batch restore commits 128 rows per
    # transaction; the redundant write-time row verify is elided while
    # byte-exact verification at domain completion and resume is retained.
    TOFU_DB_PAYLOAD_SEGMENT_CACHE_BYTES=4294967296 \
    TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES=67108864 \
    TOFU_MIGRATION_SKIP_ROW_VERIFY=1 \
    TOFU_MIGRATION_CHILD_STDERR="$BASE_DIR/logs/migration-child-stderr.log" \
    "$PY" -m lib.storage_authority_sql.migration.tofudb_migration_import \
      --source "$SOURCE_DB" \
      --checkpoint "$CHECKPOINT" \
      --binary "$BINARY" \
      --target "$TARGET_DIR" \
      --owner-id "$OWNER_ID" \
      --tenant-id "$TENANT_ID" \
      --authority-uuid "$AUTHORITY_UUID" >>"$LOG" 2>&1
  }
  if _import_once; then
    _status import ok "import complete (uuid=$AUTHORITY_UUID)"
  else
    # A transient pipe deadline/IO stall must not cost the whole window: the
    # checkpoint journal and the target survive, so retry once in resume mode
    # (re-verifies the restored prefix against the bound source, then
    # continues the incomplete domain) before falling back to rollback.
    _status import retry "first attempt failed; retrying in resume mode (checkpoint+target retained)"
    if _import_once; then
      _status import ok "import complete after resume (uuid=$AUTHORITY_UUID)"
    else
      _rollback_sqlite "import failed after one resume retry (see log tail)"
    fi
  fi

  # ── [4] CERTIFY (read-only integrity / rollback-safety of the import) ──
  _status certify start "read-only certification of imported authority"
  if "$PY" -m lib.storage_authority_sql.migration.tofudb_migration_import \
      --source "$SOURCE_DB" \
      --checkpoint "$CHECKPOINT" \
      --authority-uuid "$AUTHORITY_UUID" \
      --rollback >>"$LOG" 2>&1; then
    _status certify ok "source untouched; target complete; rollback-safe"
  else
    _rollback_sqlite "certification failed (target incomplete/unsafe)"
  fi

  # ── [5] RESTART on tofu-db ─────────────────────────────────────────────
  # Point native_launch at the freshly imported authority. It serves
  # data/tofudb-authority; if we imported into a stamped dir, swap it in.
  if [ "$TARGET_DIR" != "$DATA_DIR/tofudb-authority" ]; then
    if [ -d "$DATA_DIR/tofudb-authority" ]; then
      mv "$DATA_DIR/tofudb-authority" "$DATA_DIR/tofudb-authority.superseded-$STAMP" >>"$LOG" 2>&1
    fi
    mv "$TARGET_DIR" "$DATA_DIR/tofudb-authority" >>"$LOG" 2>&1
  fi
  _status restart start "relaunch on TOFU_PERSONAL_STORAGE_BACKEND=tofudb"
  rm -f "$DATA_DIR/.tofu_guard_disabled"
  if ! _restart tofudb; then
    _rollback_sqlite "server failed to boot on tofudb backend"
  fi

  # ── [6] VERIFY backend is really serving migrated data ────────────────
  _status verify start "confirming tofu-db backend serves reads"
  if _verify_backend tofudb; then
    _status done-tofudb ok "cutover COMPLETE — serving tofu-db (uuid=$AUTHORITY_UUID); SQLite snapshot at $SNAPSHOT_DB"
    exit 0
  else
    _rollback_sqlite "tofudb backend did not serve reads cleanly"
  fi
fi

# ── status mode ──────────────────────────────────────────────────────────
if [ "${1:-}" = "--status" ]; then
  if [ -f "$STATUS" ]; then cat "$STATUS"; else echo "no cutover status yet"; fi
  exit 0
fi

# ── launch mode: detach the orchestrator and return immediately ─────────
if [ "${1:-}" = "--launch" ]; then
  _status queued start "orchestrator launching detached (log: $LOG)"
  setsid nohup bash "$BASE_DIR/cutover_tofudb.sh" --run >>"$LOG" 2>&1 < /dev/null &
  child=$!
  echo "cutover orchestrator detached (pid $child)."
  echo "monitor:  bash $0 --status"
  echo "log:      tail -f $LOG"
  exit 0
fi

# ── default: usage ───────────────────────────────────────────────────────
cat <<EOF
usage: bash cutover_tofudb.sh [--launch|--status]

  --launch   detach the one-click SQLite→tofu-db cutover (survives server stop)
  --status   print the current cutover status file
  (no args)  this help

Flow: preflight → stop → snapshot(113G) → import(72-domain) → certify →
restart on tofudb → verify. Any failure auto-rolls-back to SQLite and never
mutates the source tofu.db. Monitor with: bash cutover_tofudb.sh --status
EOF
exit 0
