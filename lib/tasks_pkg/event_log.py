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

# Sample-based pruning: every ~Nth call runs a TTL sweep
_PRUNE_PROBABILITY = 1 / 1024


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

    # We use INSERT OR IGNORE because the (task_id, event_id) PK guarantees
    # idempotency on retry — but a real duplicate (caller minted the same
    # seq twice for different events) WOULD silently drop data.  We detect
    # that by checking ``rowcount`` and warning, so cold-replay holes are
    # observable in logs/error.log instead of being invisible.
    try:
        cur = db.execute(
            'INSERT OR IGNORE INTO task_events (task_id, event_id, ts_ms, type, payload) VALUES (?,?,?,?,?)',
            (task_id, event_id, int(now * 1000), etype or 'unknown',
             _row_payload_to_json(event)),
        )
        db.commit()
        rc = getattr(cur, 'rowcount', 1)
        if rc == 0:
            # Either an exact retry (harmless — same row already there) or
            # two distinct events colliding on event_id (DATA LOSS).  We can't
            # cheaply distinguish, but a non-zero rate is the canary.
            logger.warning('[EventLog] event_id collision on task=%s event_id=%d type=%s — '
                           'INSERT OR IGNORE dropped the row.  If this is not a retry, the '
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
                logger.debug('[event_log] read_events caught %s: %s', type(_e_audit).__name__, _e_audit)
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


def _opportunistic_prune(db):
    """Delete events for tasks that have been terminal for > EVENT_TTL_MS.

    Uses task_results.completed_at as the terminal timestamp.
    """
    cutoff = int((time.time() * 1000) - EVENT_TTL_MS)
    try:
        cur = db.execute(
            "DELETE FROM task_events WHERE task_id IN ("
            "  SELECT te.task_id FROM task_events te "
            "  JOIN task_results tr ON tr.task_id = te.task_id "
            "  WHERE tr.status IN ('done','error','aborted','interrupted') "
            "    AND tr.completed_at IS NOT NULL "
            "    AND tr.completed_at < ? "
            "  GROUP BY te.task_id"
            ")",
            (cutoff,)
        )
        db.commit()
        rc = getattr(cur, 'rowcount', 0) or 0
        if rc > 0:
            logger.info('[EventLog] Pruned %d stale event row(s) (cutoff=%d)', rc, cutoff)
    except Exception as e:
        logger.debug('[EventLog] prune query failed: %s', e)
        try:
            db.rollback()
        except Exception as re:
            logger.debug('[EventLog] rollback after prune failure: %s', re)
