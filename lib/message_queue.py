"""lib/message_queue.py — Unified priority turn-source queue for conversations.

The queue holds the *sources* of upcoming conversation turns, ordered by
priority.  Three kinds of source share one table:

  • ``real``          — a human message (highest priority).
  • ``workflow_step`` — a turn injected by an orchestration workflow
                        (medium priority; reserved for the workflow engine).
  • ``autopilot``     — a persistent armed-marker sentinel (lowest priority).

``real`` / ``workflow_step`` rows are *dispatchable*: when the active task
finishes, the highest-priority dispatchable row is dequeued and started as a
new task.  The ``autopilot`` row is NOT dispatched as a task — it is a flag
that the end-of-turn autopilot hook (:mod:`lib.tasks_pkg.autopilot`) consults
to decide whether the virtual user should take over.  It stays in the queue
(surviving page reloads) until the VU emits ``[VU: TASK_DONE]`` or the user
cancels it.

Because a human ``real`` row sorts ahead of the ``autopilot`` sentinel, a
message the user types while autopilot is armed is ALWAYS processed first;
autopilot only resumes once no dispatchable row remains.

This replaces the frontend-only ``pendingMessageQueue`` Map that was lost
on page refresh.
"""

import json
import threading
import time
import uuid

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

# Lock for dispatch coordination (prevent double-dispatch races)
_dispatch_lock = threading.Lock()

# ── Turn-source kinds + their default priorities (lower = higher) ──
KIND_REAL = 'real'
KIND_PEER_MSG = 'peer_msg'
KIND_WORKFLOW = 'workflow_step'
KIND_AUTOPILOT = 'autopilot'

_PRIORITY_FOR_KIND = {
    KIND_REAL: 10,
    # A peer message from a sibling conversation is advisory — the target sees
    # it on its NEXT turn (dispatchable, never interrupts a live turn). It
    # sorts AFTER a human 'real' turn (so a human always wins) but BEFORE a
    # brain-dispatch 'workflow_step' kickoff.
    KIND_PEER_MSG: 40,
    KIND_WORKFLOW: 50,
    KIND_AUTOPILOT: 90,
}


def _priority_for_kind(kind: str) -> int:
    return _PRIORITY_FOR_KIND.get(kind, 100)


def _ensure_table():
    """Create the message_queue table if it doesn't exist (migration-safe)."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('''
            CREATE TABLE IF NOT EXISTS message_queue (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                config TEXT NOT NULL DEFAULT '{}',
                position INTEGER NOT NULL DEFAULT 1,
                created_at BIGINT NOT NULL
            )
        ''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')
        db.commit()
    except Exception as e:
        logger.warning('[Queue] _ensure_table failed (queue will be unusable): %s', e, exc_info=True)
        try:
            db.rollback()
        except Exception as re:
            logger.debug('[Queue] rollback after _ensure_table failure: %s', re)

# Auto-create table on module load (safe for existing DBs)
_table_ensured = False

def _maybe_ensure_table():
    global _table_ensured
    if not _table_ensured:
        _ensure_table()
        _table_ensured = True


def enqueue_message(conv_id: str, message_data: dict, config: dict,
                    kind: str = KIND_REAL) -> dict:
    """Add a turn source to the server-side queue for a conversation.

    Args:
        conv_id: Conversation ID.
        message_data: Dict with keys: text, images, pdfTexts, replyQuotes,
                      convRefs, convRefTexts, originalContent, timestamp.
                      For an ``autopilot`` sentinel this is an empty/marker
                      dict (the row is never dispatched as a task).
        config: The chat config to use when dispatching this message
                (model, searchMode, tools, etc.).
        kind: Turn source — ``KIND_REAL`` (default), ``KIND_WORKFLOW`` or
              ``KIND_AUTOPILOT``.  Determines the priority bucket.

    Returns:
        Dict with queueId, position, kind.
    """
    _maybe_ensure_table()

    queue_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    timestamp = message_data.get('timestamp', now_ms)
    priority = _priority_for_kind(kind)

    db = get_thread_db(DOMAIN_CHAT)

    # Get current queue depth for position
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    position = (row[0] if row else 0) + 1

    payload = json.dumps(message_data, ensure_ascii=False)
    config_json = json.dumps(config, ensure_ascii=False)

    db_execute_with_retry(db, '''
        INSERT INTO message_queue (id, conv_id, payload, config, position, kind, priority, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (queue_id, conv_id, payload, config_json, position, kind, priority, timestamp))

    logger.info('[Queue] Enqueued %s source %s for conv=%s position=%d priority=%d text=%d chars',
                kind, queue_id[:8], conv_id[:8], position, priority,
                len(message_data.get('text', '')))

    return {'queueId': queue_id, 'position': position, 'kind': kind}


def arm_autopilot_marker(conv_id: str, config: dict) -> dict:
    """Enqueue (or reaffirm) the persistent autopilot armed-marker sentinel.

    Idempotent: at most one ``autopilot`` row exists per conversation.  When
    already armed, returns the existing row's id without inserting a second.
    The sentinel carries the resolved send ``config`` so the autopilot hook
    and any follow-up reuse the same model / tools the user had selected.

    Returns ``{queueId, armed}`` — ``armed`` True iff a NEW sentinel was added
    (False when one already existed).
    """
    _maybe_ensure_table()
    existing = _get_autopilot_marker(conv_id)
    if existing:
        return {'queueId': existing['queueId'], 'armed': False}
    res = enqueue_message(conv_id, {'_autopilotMarker': True}, config,
                          kind=KIND_AUTOPILOT)
    return {'queueId': res['queueId'], 'armed': True}


def _get_autopilot_marker(conv_id: str) -> dict | None:
    """Return ``{queueId}`` for the conv's autopilot sentinel, or None."""
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT id FROM message_queue WHERE conv_id=? AND kind=? LIMIT 1',
        (conv_id, KIND_AUTOPILOT)
    ).fetchone()
    return {'queueId': row['id']} if row else None


def get_autopilot_marker_config(conv_id: str) -> dict | None:
    """Return the send config stored on the conv's autopilot sentinel, or None.

    The armed-marker row carries the resolved send ``config`` (model / tools /
    searchMode …) captured at arm time.  Startup autopilot-resume reads it so a
    crash-recovered run re-kicks with the SAME config the user had selected,
    not a bare default.  Best-effort — returns None on any failure.
    """
    if not conv_id:
        return None
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT config FROM message_queue WHERE conv_id=? AND kind=? LIMIT 1',
            (conv_id, KIND_AUTOPILOT)
        ).fetchone()
        if not row:
            return None
        return json.loads(row['config'] or '{}')
    except Exception as e:
        logger.debug('[Queue] get_autopilot_marker_config failed: %s', e)
        return None


def list_armed_autopilot_convs() -> list[str]:
    """Return every conv_id that currently carries an autopilot armed-marker.

    The DURABLE source of truth for "which conversations are armed" — a marker
    survives restart (it is a DB row), so this is what startup autopilot-resume
    scans to re-kick every armed run, NOT the set of conversations that merely
    had an in-flight task at crash time. That distinction matters: a conv armed
    from idle (marker present, no task ever spawned, or the reply already
    finished before the crash) has an armed marker but was never a "recovered"
    task — scanning markers catches it; scanning recovered tasks would strand
    it. Best-effort — returns [] on any failure.
    """
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT DISTINCT conv_id FROM message_queue WHERE kind=?',
            (KIND_AUTOPILOT,)
        ).fetchall()
        return [r['conv_id'] for r in rows if r['conv_id']]
    except Exception as e:
        logger.warning('[Queue] list_armed_autopilot_convs failed: %s', e)
        return []


def list_orphaned_dispatchable_convs() -> list[str]:
    """Return every conv_id carrying a DISPATCHABLE queue row (real / peer /
    workflow_step — i.e. everything except the autopilot sentinel).

    This is the durable source of truth for "which conversations have a queued
    turn that no running task will ever drain". A queued human ``real`` row is
    written by ``/api/chat/send`` when a task is already running, and is drained
    ONLY by the post-task-completion hook / human-send / brain idle-drain — none
    of which fire after a server restart, because the task that would have
    triggered the completion hook died with the process. So on boot these rows
    are ORPHANED: shown in the queue bar (a DB row survives), never dispatched,
    no transcript trace = total loss. Startup re-dispatch
    (:func:`redispatch_orphaned_queue_on_startup`) scans this list to drain
    them, mirroring the autopilot armed-marker resume.

    Best-effort — returns [] on any failure.
    """
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT DISTINCT conv_id FROM message_queue WHERE kind!=?',
            (KIND_AUTOPILOT,)
        ).fetchall()
        return [r['conv_id'] for r in rows if r['conv_id']]
    except Exception as e:
        logger.warning('[Queue] list_orphaned_dispatchable_convs failed: %s', e)
        return []


def redispatch_orphaned_queue_on_startup() -> list[str]:
    """Re-dispatch every queued turn stranded by a server restart.

    A message enqueued while a task was running lives ONLY in ``message_queue``
    (never in ``conversations.messages`` — deliberate, so it doesn't render
    mid-stream). The queue row is durable, but the ONLY things that drain it are
    the post-task-completion hook, a human send, and the Project-Brain idle
    drain — NONE of which fire on a fresh boot for a conversation with no live
    task. So without this scan, a restart leaves the message shown in the queue
    bar but never processed, with no trace in the transcript = total loss (the
    ``KIND_REAL`` analogue of the autopilot armed-marker gap that
    :func:`~lib.tasks_pkg.autopilot.resume_armed_autopilot_after_crash` closes).

    For each conversation with a dispatchable row, we dispatch ONE task via the
    SAME :func:`dispatch_next_queued` seam every other caller uses — which pops
    the highest-priority queued row, appends its user message to
    ``conversations.messages`` (giving it a durable transcript home at last) and
    spawns the task. We deliberately start only ONE task per conv (not the whole
    queue) — the normal post-task-completion hook drains the remaining rows in
    priority order, exactly as in steady-state operation where a conversation
    only ever has one task running at a time.

    Ordering / safety:
      • Runs at startup AFTER ``recover_stale_tasks_on_startup`` has marked all
        crashed tasks ``interrupted`` and cleared dead ``activeTaskId`` pointers,
        so no conversation has a live in-memory task — draining cannot
        double-dispatch. A defensive live-task guard is applied per conv anyway.
      • ``dispatch_next_queued`` takes the non-reentrant ``_dispatch_lock``
        itself, so we must NOT hold it here.
      • Best-effort per conv: one failure never aborts the batch.

    Returns the list of task_ids spawned (one per conv that had a queued turn).
    """
    spawned: list[str] = []
    try:
        convs = list_orphaned_dispatchable_convs()
    except Exception as e:
        logger.warning('[Queue] redispatch-on-startup: scan failed: %s', e)
        return spawned

    if not convs:
        logger.debug('[Queue] redispatch-on-startup: no orphaned queued turns')
        return spawned

    logger.info('[Queue] redispatch-on-startup: %d conv(s) have orphaned queued '
                'turn(s): %s', len(convs), [c[:8] for c in convs])

    for conv_id in convs:
        if not conv_id:
            continue
        # Defensive: never drain a conv that already has a live task (a task
        # spawned earlier in the same boot, or a racing send). Mirrors
        # project_dispatch._conv_has_live_task's intent without importing it.
        try:
            from lib.tasks_pkg.manager import tasks, tasks_lock
            with tasks_lock:
                _live = any(
                    t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('aborted')
                    for t in tasks.values()
                )
            if _live:
                logger.info('[Queue] redispatch-on-startup: conv=%s already has a '
                            'live task — leaving its queue for the completion hook',
                            conv_id[:8])
                continue
        except Exception as e:
            logger.debug('[Queue] redispatch-on-startup live-task probe failed '
                         'for conv=%s: %s', conv_id[:8], e)

        # Dispatch ONE task for this conv; its completion hook drains the rest
        # of the queue (single-task-per-conv, as in steady state).
        try:
            tid = dispatch_next_queued(conv_id)
        except Exception as e:
            logger.warning('[Queue] redispatch-on-startup: dispatch failed for '
                           'conv=%s: %s', conv_id[:8], e, exc_info=True)
            continue
        if tid:
            spawned.append(tid)
            from lib.log import audit_log
            audit_log('queue_redispatch_after_restart', conv_id=conv_id, task_id=tid)
            logger.info('[Queue] redispatch-on-startup: conv=%s → task %s',
                        conv_id[:8], tid[:8])

    if spawned:
        logger.info('[Queue] redispatch-on-startup: spawned %d task(s) from '
                    'orphaned queue rows', len(spawned))
    return spawned


def has_autopilot_marker(conv_id: str) -> bool:
    """True iff a persistent autopilot armed-marker exists for the conv."""
    if not conv_id:
        return False
    try:
        _maybe_ensure_table()
        return _get_autopilot_marker(conv_id) is not None
    except Exception as e:
        logger.debug('[Queue] has_autopilot_marker probe failed: %s', e)
        return False


def clear_autopilot_marker(conv_id: str) -> bool:
    """Remove the conv's autopilot sentinel (disarm). True if one was removed."""
    if not conv_id:
        return False
    db = get_thread_db(DOMAIN_CHAT)
    marker = _get_autopilot_marker(conv_id)
    if not marker:
        return False
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?',
                          (marker['queueId'],))
    _renumber_positions(db, conv_id)
    logger.info('[Queue] Cleared autopilot marker for conv=%s', conv_id[:8])
    return True


def get_queue(conv_id: str) -> list[dict]:
    """Get all queued messages for a conversation, ordered by position.

    Returns:
        List of dicts with keys: queueId, position, text (preview),
        hasImages, hasPdfs, hasRefs, hasQuotes, timestamp.
    """
    _maybe_ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        'SELECT id, payload, position, kind, priority, created_at FROM message_queue '
        'WHERE conv_id=? ORDER BY priority ASC, position ASC',
        (conv_id,)
    ).fetchall()

    result = []
    for row in rows:
        try:
            data = json.loads(row['payload'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Queue] Failed to parse payload for queue_id=%s: %s', row['id'][:8], e)
            data = {}

        result.append({
            'queueId': row['id'],
            'position': row['position'],
            'kind': row['kind'] or KIND_REAL,
            'priority': row['priority'],
            'text': (data.get('text', '') or '')[:100],
            'hasImages': bool(data.get('images')),
            'hasPdfs': bool(data.get('pdfTexts')),
            'hasRefs': bool(data.get('convRefs')),
            'hasQuotes': bool(data.get('replyQuotes')),
            'timestamp': row['created_at'],
        })

    return result


def remove_from_queue(conv_id: str, queue_id: str) -> bool:
    """Remove a specific message from the queue.

    Returns:
        True if removed, False if not found.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT id FROM message_queue WHERE id=? AND conv_id=?',
        (queue_id, conv_id)
    ).fetchone()
    if not row:
        return False

    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?', (queue_id,))
    _renumber_positions(db, conv_id)

    logger.info('[Queue] Removed message %s from conv=%s', queue_id[:8], conv_id[:8])
    return True


def clear_queue(conv_id: str) -> int:
    """Clear all queued messages for a conversation.

    Returns:
        Number of messages removed.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    count = row[0] if row else 0

    if count > 0:
        db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (conv_id,))
        logger.info('[Queue] Cleared %d messages from conv=%s', count, conv_id[:8])

    return count



def _renumber_positions(db, conv_id: str):
    """Re-number position column after a deletion to keep them contiguous."""
    rows = db.execute(
        'SELECT id FROM message_queue WHERE conv_id=? ORDER BY position ASC',
        (conv_id,)
    ).fetchall()
    for i, row in enumerate(rows, 1):
        db.execute(
            'UPDATE message_queue SET position=? WHERE id=?',
            (i, row['id'])
        )
    db.commit()


def dequeue_next(conv_id: str) -> dict | None:
    """Pop the next message from the queue (lowest position).

    Returns:
        Full message dict (payload + config) or None if queue is empty.
    """
    db = get_thread_db(DOMAIN_CHAT)

    # Only dispatchable sources (real / workflow_step) are popped as tasks.
    # The autopilot sentinel is consulted by the end-of-turn hook, never
    # dequeued here.
    row = db.execute(
        'SELECT id, payload, config FROM message_queue '
        'WHERE conv_id=? AND kind!=? ORDER BY priority ASC, position ASC LIMIT 1',
        (conv_id, KIND_AUTOPILOT)
    ).fetchone()

    if not row:
        return None

    queue_id = row['id']
    try:
        payload = json.loads(row['payload'])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Queue] Failed to parse payload for dequeue queue_id=%s: %s', queue_id[:8], e)
        payload = {}

    try:
        config = json.loads(row['config'])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Queue] Failed to parse config for dequeue queue_id=%s: %s', queue_id[:8], e)
        config = {}

    # Remove from queue
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?', (queue_id,))
    _renumber_positions(db, conv_id)

    logger.info('[Queue] Dequeued message %s from conv=%s, text=%d chars',
                queue_id[:8], conv_id[:8], len(payload.get('text', '')))

    return {
        'queueId': queue_id,
        'payload': payload,
        'config': config,
    }


def dispatch_next_queued(conv_id: str) -> str | None:
    """Dispatch the next queued message for a conversation as a new task.

    Called after a task completes.  If there are queued messages, the first
    one is dequeued, its user message is appended to the conversation in the
    DB, and a new task is started.

    Returns:
        The new task_id if dispatched, None if queue was empty.
    """
    with _dispatch_lock:
        item = dequeue_next(conv_id)
        if not item:
            return None

        payload = item['payload']
        config = item['config']
        text = payload.get('text', '')
        # ★ _user_msg: pre-built (and already translated) user message dict
        #   from /api/chat/send.  If present, skip translation and use directly.
        pre_built_user_msg = payload.get('_user_msg')

        logger.info('[Queue] Dispatching queued message for conv=%s text=%d chars pre_built=%s',
                    conv_id[:8], len(text), bool(pre_built_user_msg))

        db = get_thread_db(DOMAIN_CHAT)

        if pre_built_user_msg:
            # ★ New path: /api/chat/send already built + translated the user message.
            #   Just append it to the conversation DB.
            row = db.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            if not row:
                logger.warning('[Queue] Conversation %s not found for dispatch', conv_id[:8])
                return None

            try:
                messages = json.loads(row['messages'] or '[]')
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Queue] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
                messages = []

            from lib.chat import append_user_msg_idempotent
            append_user_msg_idempotent(messages, pre_built_user_msg)

            from lib.database import json_dumps_pg
            now_ms = int(time.time() * 1000)
            db_execute_with_retry(db, '''
                UPDATE conversations SET messages=?, updated_at=?, msg_count=?
                WHERE id=? AND user_id=1
            ''', (json_dumps_pg(messages), now_ms, len(messages), conv_id))

            remaining = _get_queue_depth(db, conv_id)
            logger.info('[Queue] Appended pre-built user msg to conv=%s (now %d msgs)',
                        conv_id[:8], len(messages))

        else:
            # Legacy path: message was enqueued via /api/chat/queue (old API).
            # Need to translate and append to conversation ourselves.
            import re
            from lib.conv_config import resolve_auto_translate
            auto_translate = resolve_auto_translate(config)
            has_chinese = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text)) if text else False
            translated_text = text
            _translate_model = None
            if auto_translate and has_chinese:
                try:
                    from lib.translate import _build_translate_prompt, _translate_freetext
                    system_prompt = _build_translate_prompt('English', 'Chinese')
                    result, _usage = _translate_freetext(
                        text, system_prompt, chunk_label=':queue',
                        source='Chinese', target='English',
                    )
                    if result and result.strip():
                        translated_text = result.strip()
                        if isinstance(_usage, dict):
                            _disp = _usage.get('_dispatch', {})
                            _translate_model = _disp.get('model', _usage.get('model'))
                        logger.info('[Queue] Auto-translated queued message for conv=%s: %d→%d chars model=%s',
                                    conv_id[:8], len(text), len(translated_text), _translate_model)
                    else:
                        translated_text = text
                except Exception as e:
                    logger.warning('[Queue] Auto-translate failed for conv=%s: %s', conv_id[:8], e)
                    translated_text = text

            # Build user message
            user_msg = {
                'role': 'user',
                'content': translated_text if auto_translate and has_chinese else text,
                'timestamp': payload.get('timestamp', int(time.time() * 1000)),
            }
            if auto_translate and has_chinese and translated_text != text:
                user_msg['originalContent'] = text
                user_msg['_translateDone'] = True
                if _translate_model:
                    user_msg['_translateModel'] = _translate_model
            if payload.get('images'):
                user_msg['images'] = payload['images']
            if payload.get('pdfTexts'):
                user_msg['pdfTexts'] = payload['pdfTexts']
            if payload.get('replyQuotes'):
                user_msg['replyQuotes'] = payload['replyQuotes']
            if payload.get('convRefs'):
                user_msg['convRefs'] = payload['convRefs']
            # ── Peer-message attribution: a KIND_PEER_MSG turn is content-wise
            #    prefixed so the AGENT sees "[Peer message … (conv X)]", but the
            #    structured markers were being dropped here — the persisted turn
            #    then looked byte-identical to real user input, so the frontend
            #    could not visually distinguish/attribute it. Propagate the
            #    markers onto the message so the arrival is observable + the
            #    sender is attributable (renderer keys on `_peerMessage`). ──
            if payload.get('_peerMessage'):
                user_msg['_peerMessage'] = True
                user_msg['_fromConv'] = payload.get('_fromConv', '')
            # A human operator nudge (sent from the Team panel) is stamped so the
            # receiving banner attributes it to the operator, not to an agent
            # peer. Distinct provenance, same KIND_PEER_MSG lane. Kept as its own
            # guard (not nested in the block above) so the peer-marker block can
            # be neutered independently by its own negative-control test.
            if payload.get('_peerMessage') and payload.get('_peerHuman'):
                user_msg['_peerHuman'] = True
            # ── Brain-dispatch attribution: a Project-Brain autonomous kickoff
            #    (KIND_WORKFLOW, marked _brainDispatch by dispatch_epic) must be
            #    distinguishable from human input downstream — the frontend
            #    shows it started autonomously, and the task itself should not
            #    be mistaken for a user turn. Propagate the markers onto the
            #    persisted user turn (mirrors the _peerMessage block). ──
            if payload.get('_brainDispatch'):
                user_msg['_brainDispatch'] = True
                if payload.get('boardTaskId'):
                    user_msg['_boardTaskId'] = payload.get('boardTaskId')
            conv_ref_texts = payload.get('convRefTexts')
            if not conv_ref_texts and payload.get('convRefs'):
                try:
                    from lib.chat import resolve_conv_refs
                    conv_ref_texts = resolve_conv_refs(payload['convRefs'])
                except Exception as e:
                    logger.warning('[Queue] Failed to resolve conv refs for conv=%s: %s',
                                   conv_id[:8], e)
            if conv_ref_texts:
                user_msg['convRefTexts'] = conv_ref_texts

            # Append user message to conversation in DB
            row = db.execute(
                'SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()

            if not row:
                logger.warning('[Queue] Conversation %s not found for dispatch', conv_id[:8])
                return None

            try:
                messages = json.loads(row['messages'] or '[]')
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Queue] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
                messages = []

            from lib.chat import append_user_msg_idempotent
            append_user_msg_idempotent(messages, user_msg)

            from lib.database import json_dumps_pg
            now_ms = int(time.time() * 1000)
            messages_json = json_dumps_pg(messages)

            remaining = _get_queue_depth(db, conv_id)

            db_execute_with_retry(db, '''
                UPDATE conversations
                SET messages=?, updated_at=?, msg_count=?
                WHERE id=? AND user_id=1
            ''', (messages_json, now_ms, len(messages), conv_id))
        # (Legacy _msg_persisted path removed — no longer used)

        # 3. Build API messages and create task
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        api_messages = build_api_messages_from_db(conv_id, config)
        if not api_messages:
            logger.warning('[Queue] No API messages after building for conv=%s', conv_id[:8])
            return None

        from lib.tasks_pkg import create_task

        task = create_task(conv_id, api_messages, config)
        task_id = task['id']

        # Update conversation settings with the new activeTaskId
        try:
            settings_row = db.execute(
                'SELECT settings FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            settings = json.loads(settings_row[0] or '{}') if settings_row else {}
            settings['activeTaskId'] = task_id
            db_execute_with_retry(db, '''
                UPDATE conversations SET settings=? WHERE id=? AND user_id=1
            ''', (json.dumps(settings, ensure_ascii=False), conv_id))
        except Exception as e:
            logger.warning('[Queue] Failed to update activeTaskId for conv=%s: %s',
                           conv_id[:8], e, exc_info=True)

        # 4. Start the task in a background thread
        _cfg_model = config.get('model', '?')
        logger.info('[Queue] Starting dispatched task %s for conv=%s model=%s remaining=%d',
                    task_id[:8], conv_id[:8], _cfg_model, remaining)

        try:
            from lib.tasks_pkg import spawn_task
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Queue] Failed to start thread for dispatched task %s', task_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start queued task thread.',
                model=config.get('model', ''),
                context='queue-dispatch',
                source='message-queue',
                raw=str(_spawn_err),
            )
            return None

        # Invalidate meta cache so frontend sees the new task
        try:
            from lib.conversations import invalidate_meta_cache
            invalidate_meta_cache()
        except Exception as e:
            logger.debug('[Queue] meta cache invalidation failed: %s', e)

        return task_id


def _get_queue_depth(db, conv_id: str) -> int:
    """Number of DISPATCHABLE rows in queue (real / workflow_step only).

    Excludes the autopilot sentinel — it is never dispatched as a task, so
    callers gating "is there pending work to start" must not see it.  This is
    the lynchpin of human-over-autopilot priority: the autopilot hook calls
    this (via ``get_queue_depth``) and defers whenever it is > 0.
    """
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=? AND kind!=?',
        (conv_id, KIND_AUTOPILOT)
    ).fetchone()
    return row[0] if row else 0


def get_queue_depth(conv_id: str) -> int:
    """Public version: dispatchable queue depth with its own DB connection."""
    db = get_thread_db(DOMAIN_CHAT)
    return _get_queue_depth(db, conv_id)
