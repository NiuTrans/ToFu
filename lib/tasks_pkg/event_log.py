"""Persisted SSE event log — durable Last-Event-ID replay.

Every event that goes through ``manager.append_event`` is mirrored into the
``task_events`` table.  This decouples event replay from in-memory task
state, so SSE reconnection survives:

  * task removal by ``cleanup_old_tasks`` (1h threshold)
  * server restart
  * cross-process readers (when a future deployment fans tasks across
    multiple Flask workers)

Two read paths exist on the SSE handler:

  1. **Hot path** — when the task is still in ``tasks`` dict, replay reads
     directly from ``task['events']`` (no DB hit, lower latency).
  2. **Cold path** — when the task is gone (cleanup or restart), the SSE
     handler falls back to ``read_events`` here.

Pruning is opportunistic: every Nth ``append_event`` call performs a TTL
sweep on the table, deleting rows whose task is terminal and older than
``EVENT_TTL_MS``.  This keeps the table bounded without a background
thread.

Note on persistence semantics: every event (including each delta) is
persisted as its own row on arrival.  No in-memory buffering, no
coalescing — the previous "250 ms delta coalesce" behaviour was removed
in 2026-05 because cold-replay required exact-cursor reconstruction.
``flush_pending`` is retained as a no-op for API compatibility with the
sole caller in ``manager.append_event``; it does not need to be called
by new code.
"""

import json
import random
import time

from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
from lib.log import get_logger

logger = get_logger(__name__)

# 6 hours — generous enough to span any realistic SSE reconnect window
# (page refresh, network blip, proxy timeout) for a finished task.
EVENT_TTL_MS = 6 * 3600 * 1000

# ── Tiered retention (docs/DEBUG_PANEL_REDESIGN.md §10.4) ──
# The 6h window above exists ONLY to serve SSE reconnects, and it used to
# take the Request Inspector's data down with it: a task from two hours ago
# already read "event log expired". Structural events (the request payloads
# + their usage/round markers) are what the inspector renders, so they get a
# 30-day tier. This is only affordable BECAUSE snapshots are now stored as
# deltas (§10) — measured 31.5x smaller across 493 real rounds. Order matters:
# never extend retention before the delta projection is in place.
STRUCTURAL_EVENT_TYPES = (
    'messages_snapshot', 'round_usage', 'round_start', 'round_end',
)
STRUCTURAL_TTL_MS = 30 * 24 * 3600 * 1000

# Sample-based pruning: every ~Nth call runs a TTL sweep
_PRUNE_PROBABILITY = 1 / 1024

# Sample-based delta compaction of LEFTOVER full snapshot rows. Rarer than
# the prune sweep because each pass rewrites rows (the prune only deletes),
# but frequent enough that a busy server converges within an hour or two.
_COMPACT_PROBABILITY = 1 / 4096
# How many tasks one compaction pass may rewrite. Kept small: this runs on
# an SSE delta's thread, so a pass must stay far below any request budget.
_COMPACT_MAX_TASKS = 2

# Batched-delete tuning. Each prune pass deletes at most
# ``_PRUNE_BATCH_TASKS`` distinct task_ids per batch and runs at most
# ``_PRUNE_MAX_BATCHES`` batches per invocation, COMMITTING after each batch.
# The per-batch commit is the fix for the permanent-failure loop: an unbounded
# single DELETE that exceeds PG's statement_timeout (120s) is cancelled and
# rolled back WHOLE — zero progress — so a backlog only ever grows. Bounded
# batches keep each statement well under the timeout AND make partial progress
# durable, so a slow batch never discards the work of earlier ones.
_PRUNE_BATCH_TASKS = 200
_PRUNE_MAX_BATCHES = 25


def _row_payload_to_json(payload):
    """Serialize a payload dict for storage; tolerant of non-dict events.

    Uses ``json_dumps_pg`` so NUL bytes (``\\x00`` / ``\\u0000``) are stripped
    before the row hits the ``task_events.payload`` JSONB column — PostgreSQL's
    JSONB parser rejects ``\\u0000`` escapes, which would otherwise make the
    INSERT raise and silently drop the event (e.g. a ``messages_snapshot``
    carrying binary image data) from cold replay.
    """
    try:
        return json_dumps_pg(payload)
    except (TypeError, ValueError) as e:
        logger.debug('[EventLog] payload serialize failed: %s', e)
        return json.dumps({'type': 'error', 'detail': 'unserializable'})


def append_persistent_event(task_id, event_id, event):
    """Persist one event to the task_events table immediately.

    Every event (including deltas) is written as its own row on arrival.
    No in-memory buffering — a process crash never loses persisted state.

    This function MUST be cheap — it runs on every SSE delta.  It uses
    the per-thread DB connection (get_thread_db) and never throws.
    """
    if not task_id:
        return
    # ── Reject None event_id explicitly ──
    # The legacy fallback path in ``manager.append_event`` (when a task is
    # not registered in TaskRuntime) historically used ``seq=None``.  Letting
    # that flow through here would either crash on ``int(None)`` or insert
    # a NULL primary-key column, so we log loudly and skip — cold replay
    # would silently drop these events otherwise.
    if event_id is None:
        logger.warning('[EventLog] Refusing to persist event with event_id=None for task=%s '
                       'type=%s — caller (likely manager.append_event legacy fallback) bypassed '
                       'TaskRuntime sequencing. Cold replay would have a hole here.',
                       task_id[:8], (event or {}).get('type', '?'))
        return
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[EventLog] thread db unavailable: %s', e)
        return

    etype = (event or {}).get('type', '')
    now = time.time()

    # ── Snapshot delta projection (docs/DEBUG_PANEL_REDESIGN.md §10) ──
    # messages_snapshot rows were 92.4% of this table's bytes because every
    # round re-stored the WHOLE messages array plus a byte-identical tools
    # array (measured: 123.2 MB for one 167-round task; 1.9 MB as deltas).
    # We project ONLY the row that gets persisted — the ``event`` object the
    # caller pushes to SSE subscribers is never touched, so live rendering is
    # byte-identical. Rebuild happens server-side on read
    # (snapshot_delta.rebuild_snapshots), so no consumer sees the delta form.
    # Best-effort: a projection failure falls back to storing the full row.
    row_event = event
    if etype == 'messages_snapshot':
        try:
            from lib.tasks_pkg.snapshot_delta import get_projector
            row_event = get_projector().project(task_id, event)
        except Exception as e:
            logger.warning('[EventLog] snapshot delta projection failed for '
                           'task=%s (storing full row): %s', task_id[:8], e)
            row_event = event

    # ON CONFLICT (task_id, event_id) DO NOTHING because the composite PK
    # guarantees idempotency on retry — but a real duplicate (caller minted
    # the same seq twice for different events) WOULD silently drop data.  We
    # detect that by checking ``rowcount`` and warning, so cold-replay holes
    # are observable in logs/error.log instead of being invisible.
    #
    # NOTE: retry=False is REQUIRED here — upsert(retry=True) routes through
    # db_execute_with_retry which returns None, destroying the cur.rowcount
    # the collision canary below depends on.  DO-NOTHING rowcount semantics
    # (insert→1, duplicate→0) are verified identical on PG and sqlite3.
    try:
        from lib.database._core_schema import TASK_EVENTS, upsert
        cur = upsert(
            db, TASK_EVENTS,
            {'task_id': task_id, 'event_id': event_id, 'ts_ms': int(now * 1000),
             'type': etype or 'unknown', 'payload': _row_payload_to_json(row_event)},
            conflict_cols=['task_id', 'event_id'],
            insert_cols=['task_id', 'event_id', 'ts_ms', 'type', 'payload'],
            update_cols=[],  # DO NOTHING — append-only event log
            commit=True, retry=False,
        )
        rc = getattr(cur, 'rowcount', 1)
        if rc == 0:
            # Either an exact retry (harmless — same row already there) or
            # two distinct events colliding on event_id (DATA LOSS).  We can't
            # cheaply distinguish, but a non-zero rate is the canary.
            logger.warning('[EventLog] event_id collision on task=%s event_id=%d type=%s — '
                           'ON CONFLICT DO NOTHING dropped the row.  If this is not a retry, the '
                           'caller minted a duplicate seq and cold replay will be missing this '
                           'event.', task_id[:8], int(event_id), etype or 'unknown')
    except Exception as e:
        # Catch broadly because this runs on every SSE delta — we never want
        # a transient DB blip to abort the stream.  Logged at WARNING (was
        # DEBUG): a silent persist failure means cold replay returns nothing
        # for that window, which the user perceives as data loss.
        logger.warning('[EventLog] persist event failed for task=%s type=%s: %s',
                       task_id[:8], etype, e)

    if random.random() < _PRUNE_PROBABILITY:
        try:
            _opportunistic_prune(db)
        except Exception as e:
            logger.debug('[EventLog] prune failed (non-fatal): %s', e)

    # Read-your-writes for the Request Inspector's read cache: this task's
    # cached event rows are now stale. The cache's TTL bounds staleness in
    # wall-clock terms, but a live task that appends a round and is polled
    # immediately after must see it — so drop the entry at the write.
    try:
        from lib.tasks_pkg.request_inspector import invalidate_task_cache
        invalidate_task_cache(task_id)
    except Exception as e:
        logger.debug('[EventLog] inspector cache invalidation skipped: %s', e)

    # ── Self-healing delta compaction (docs/DEBUG_PANEL_REDESIGN.md §10) ──
    # The projection above only applies to rows THIS process writes. A
    # deployment where an older process is still serving (no restart yet)
    # keeps appending FULL rows, and the one-shot migration only covers the
    # backlog that existed when it ran — measured: +519 MB accumulated
    # between two checks. Piggy-backing on the same sampled hook means any
    # process running this code compacts leftovers, so the table converges
    # WITHOUT requiring a coordinated restart.
    if random.random() < _COMPACT_PROBABILITY:
        try:
            _opportunistic_compact(db)
        except Exception as e:
            logger.debug('[EventLog] compaction failed (non-fatal): %s', e)


def flush_pending(task_id):
    """No-op kept for API compatibility.

    Historically this drained a 250 ms delta coalescer.  The coalescer was
    removed (see module docstring) because cold-replay needs every event
    at its real cursor.  Now every event is persisted on arrival, so
    there is nothing to flush.
    """
    pass


def read_events(task_id, since_event_id=None, limit=10000):
    """Read persisted events for a task, ordered by event_id.

    Args:
        task_id: task identifier.
        since_event_id: if set, returns only events with event_id > N.
        limit: maximum rows to return (defensive cap).

    Returns:
        list of dicts: [{'event_id': N, 'type': ..., 'payload': {...}}, ...]
    """
    if not task_id:
        return []
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[EventLog] read thread db unavailable: %s', e)
        return []
    try:
        if since_event_id is not None:
            rows = db.execute(
                'SELECT event_id, type, payload FROM task_events '
                'WHERE task_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?',
                (task_id, int(since_event_id), int(limit))
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT event_id, type, payload FROM task_events '
                'WHERE task_id=? ORDER BY event_id ASC LIMIT ?',
                (task_id, int(limit))
            ).fetchall()
    except Exception as e:
        logger.warning('[EventLog] read failed for task=%s: %s', task_id[:8], e)
        return []
    out = []
    for r in rows:
        try:
            payload_raw = r['payload'] if 'payload' in r.keys() else r[2]
        except Exception as e:
            logger.debug('[EventLog] row.keys() unavailable, falling back to positional access: %s', e)
            payload_raw = r[2]
        if isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            try:
                payload = json.loads(payload_raw or '{}')
            except (TypeError, ValueError, json.JSONDecodeError) as _e_audit:
                # WARNING (was DEBUG): an unparseable payload row means cold
                # replay silently degrades this event to {'type': ...} — a
                # data-integrity degradation, same severity as the persist-side
                # 'persist event failed' warning above.
                logger.warning('[EventLog] read_events: unparseable payload row for task=%s, '
                               'degrading to type-only: %s', task_id[:8], _e_audit)
                payload = {'type': r['type'] if 'type' in r.keys() else r[1]}
        try:
            eid = int(r['event_id'] if 'event_id' in r.keys() else r[0])
        except Exception as e:
            logger.debug('[EventLog] row missing event_id, dropping: %s', e)
            continue
        out.append({'event_id': eid, 'payload': payload})
    return out


def has_terminal_event(task_id):
    """Return True if a 'done' event has been persisted for this task."""
    if not task_id:
        return False
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND type='done' LIMIT 1",
            (task_id,)
        ).fetchone()
        return bool(row)
    except Exception as e:
        logger.debug('[EventLog] has_terminal_event failed for task=%s: %s', task_id[:8], e)
        return False


def _opportunistic_compact(db):
    """Compact a few tasks' leftover FULL snapshot rows into delta form.

    Why this exists (and is not just the one-shot migration): the write-path
    projection in :func:`append_persistent_event` only shrinks rows that THIS
    process writes. Until every serving process runs that code, full rows keep
    arriving — and the offline migration is a point-in-time sweep, so the gap
    re-opens the moment it finishes (measured: +519 MB between two checks).
    This hook lets any process running this build heal the backlog
    continuously, so the table converges WITHOUT a coordinated restart.

    Reuses ``_migrate_snapshot_deltas.migrate_task`` VERBATIM so there is ONE
    implementation of the verify-then-write contract (§11): project → rebuild
    → compare byte-for-byte → write only on an exact match, else leave that
    task untouched. Never raises.
    """
    try:
        import importlib.util
        import os
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            'tests', '_migrate_snapshot_deltas.py')
        if not os.path.exists(script):
            return
        spec = importlib.util.spec_from_file_location('_snap_migrate', script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.debug('[EventLog] compaction helper unavailable: %s', e)
        return

    try:
        tasks = mod._tasks_with_full_rows(db, limit=_COMPACT_MAX_TASKS)
    except Exception as e:
        logger.debug('[EventLog] compaction scan failed: %s', e)
        return
    if not tasks:
        return

    healed = failed = 0
    for tid in tasks:
        try:
            rep = mod.migrate_task(db, tid)
        except Exception as e:
            logger.debug('[EventLog] compaction of task=%s raised: %s',
                         str(tid)[:8], e)
            failed += 1
            continue
        if rep.get('status') == 'ok':
            healed += 1
        elif rep.get('status') == 'FAILED':
            failed += 1
            logger.warning('[EventLog] compaction REFUSED task=%s (rows left '
                           'untouched): %s', str(tid)[:8], rep.get('reason'))
    if healed or failed:
        logger.info('[EventLog] Delta-compacted %d task(s) of leftover full '
                    'snapshot rows (%d refused)', healed, failed)


def _opportunistic_prune(db):
    """Delete stale task_events rows in two passes, both bounded by EVENT_TTL_MS.

    Pass 1 (terminal tasks): rows whose ``task_id`` JOINs a ``task_results``
    row in a terminal status with ``completed_at`` older than the TTL. This
    is the normal lifecycle reaper — uses ``task_results.completed_at`` as the
    authoritative terminal timestamp.

    Pass 2 (ORPHANED rows): rows whose ``task_id`` has NO ``task_results`` row
    at all and whose own ``task_events.ts_ms`` is older than the TTL. Pass 1
    structurally cannot see these — its JOIN drops any row without a matching
    ``task_results`` entry — so without Pass 2 they would never be reaped
    (permanent litter). Orphans arise whenever something runs the tool
    executor on a task dict whose id is not registered in the chat
    TaskRuntime and which never writes a task_results row: e.g. the 2026-06-28
    timer-poll-proxy collision bug left ~160 orphaned ``(tmr_*, 0/1)`` rows
    (the first, successful write of each colliding pair). The ``ts_ms <
    cutoff`` age guard is the safety mechanism — it guarantees we never reap
    events of a legitimately in-flight unregistered task, since any single
    poll's lifetime and the SSE-reconnect window are far under EVENT_TTL_MS.
    This also future-proofs the reaper against any new orphaned-id writer.
    """
    cutoff = int((time.time() * 1000) - EVENT_TTL_MS)
    structural_cutoff = int((time.time() * 1000) - STRUCTURAL_TTL_MS)
    _struct_ph = ','.join(['?'] * len(STRUCTURAL_EVENT_TYPES))

    # ── Pass 1: terminal tasks (JOIN task_results), deleted in bounded batches ──
    # TIERED (§10.4): this pass reaps the STREAMING NOISE (delta / phase /
    # tool_progress / …) at the 6h SSE-reconnect horizon but SPARES the
    # structural events the Request Inspector renders; those are reaped by
    # pass 1b at the 30-day horizon. Previously this deleted every row of an
    # eligible task, which is why a 2-hour-old task showed "log expired".
    total = 0
    for _ in range(_PRUNE_MAX_BATCHES):
        try:
            cur = db.execute(
                "DELETE FROM task_events WHERE ts_ms < ? "
                f"  AND type NOT IN ({_struct_ph}) "
                "  AND task_id IN ("
                "  SELECT te.task_id FROM task_events te "
                "  JOIN task_results tr ON tr.task_id = te.task_id "
                "  WHERE tr.status IN ('done','error','aborted','interrupted') "
                "    AND tr.completed_at IS NOT NULL "
                "    AND tr.completed_at < ? "
                "  GROUP BY te.task_id "
                "  LIMIT ?"
                ")",
                (cutoff, *STRUCTURAL_EVENT_TYPES, cutoff, _PRUNE_BATCH_TASKS)
            )
            db.commit()
        except Exception as e:
            logger.debug('[EventLog] prune query failed: %s', e)
            try:
                db.rollback()
            except Exception as re:
                logger.debug('[EventLog] rollback after prune failure: %s', re)
            break
        rc = getattr(cur, 'rowcount', 0) or 0
        total += rc
        if rc == 0:
            break
    if total > 0:
        logger.info('[EventLog] Pruned %d stale streaming event row(s) '
                    '(cutoff=%d, structural events spared)', total, cutoff)

    # ── Pass 1b: STRUCTURAL events past the 30-day tier (§10.4) ──
    # Same batched-commit shape; only the horizon and the type filter differ.
    total = 0
    for _ in range(_PRUNE_MAX_BATCHES):
        try:
            cur = db.execute(
                "DELETE FROM task_events WHERE ts_ms < ? "
                f"  AND type IN ({_struct_ph}) "
                "  AND task_id IN ("
                "  SELECT te.task_id FROM task_events te "
                "  JOIN task_results tr ON tr.task_id = te.task_id "
                "  WHERE tr.status IN ('done','error','aborted','interrupted') "
                "    AND tr.completed_at IS NOT NULL "
                "    AND tr.completed_at < ? "
                "  GROUP BY te.task_id "
                "  LIMIT ?"
                ")",
                (structural_cutoff, *STRUCTURAL_EVENT_TYPES,
                 structural_cutoff, _PRUNE_BATCH_TASKS)
            )
            db.commit()
        except Exception as e:
            logger.debug('[EventLog] structural prune query failed: %s', e)
            try:
                db.rollback()
            except Exception as re:
                logger.debug('[EventLog] rollback after structural prune: %s', re)
            break
        rc = getattr(cur, 'rowcount', 0) or 0
        total += rc
        if rc == 0:
            break
    if total > 0:
        logger.info('[EventLog] Pruned %d structural event row(s) past the '
                    '30-day tier (cutoff=%d)', total, structural_cutoff)

    # ── Pass 2: orphaned rows (no task_results row), aged out by own ts_ms ──
    # Same batched-commit strategy. Deletes by the row's own primary key
    # (task_id, event_id) picked in bounded chunks so the correlated NOT EXISTS
    # subquery never runs against the whole table in one un-committable statement.
    total = 0
    for _ in range(_PRUNE_MAX_BATCHES):
        try:
            rows = db.execute(
                "SELECT task_id, event_id FROM task_events "
                "WHERE (( ts_ms < ? AND type NOT IN (%s) ) "
                "    OR ( ts_ms < ? AND type IN (%s) )) "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM task_results tr WHERE tr.task_id = task_events.task_id"
                "  ) "
                "LIMIT ?" % (_struct_ph, _struct_ph),
                (cutoff, *STRUCTURAL_EVENT_TYPES,
                 structural_cutoff, *STRUCTURAL_EVENT_TYPES,
                 _PRUNE_BATCH_TASKS)
            ).fetchall()
        except Exception as e:
            logger.debug('[EventLog] orphan prune select failed: %s', e)
            break
        if not rows:
            break
        try:
            db.executemany(
                'DELETE FROM task_events WHERE task_id=? AND event_id=?',
                [(r[0], r[1]) for r in rows]
            )
            db.commit()
        except Exception as e:
            logger.debug('[EventLog] orphan prune delete failed: %s', e)
            try:
                db.rollback()
            except Exception as re:
                logger.debug('[EventLog] rollback after orphan prune failure: %s', re)
            break
        total += len(rows)
        if len(rows) < _PRUNE_BATCH_TASKS:
            break
    if total > 0:
        logger.info('[EventLog] Pruned %d orphaned event row(s) with no task_results '
                    '(cutoff=%d)', total, cutoff)
