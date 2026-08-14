"""Chat persistence + task-metadata helpers.

The conversation load/create/persist functions and the task-metadata
extractors (in-memory task dict ↔ ``task_results`` DB row) moved out of
``routes/chat.py`` so the routes file stays a thin HTTP layer. None of these
touch Flask request state — they take an explicit ``db`` handle and plain
dicts — so they belong in lib.
"""

import json
import re
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # mirrors routes/common.py


def extract_db_meta(row):
    """Extract metadata dict from a DB task_results row."""
    meta = {}
    if row['metadata']:
        try:
            meta = json.loads(row['metadata'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Chat] Failed to parse task metadata JSON (task_id=%s): %s', row['task_id'], e, exc_info=True)
    return meta


def extract_task_meta(task):
    """Extract metadata fields from an in-memory task dict.

    MUST stay in sync with ``extract_db_meta`` (DB-row equivalent) and
    with the ``meta`` dict built in ``manager.persist_task_result``.  Any
    field added here must also appear in:
      * persist_task_result ’s ``meta`` dict (so it lands in task_results)
      * the chat_poll DB-path field loop (so /api/chat/poll returns it)
      * the cold-replay synth-done in chat_stream (so Last-Event-ID
        replay after server restart returns the same shape)
    Asymmetry between these four paths historically caused "my apiRounds
    disappeared after I came back" / "modifiedFiles missing on reload".
    """
    meta = {'contentEpoch': int(task.get('_contentEpoch') or 0)}
    if task.get('finishReason'):
        meta['finishReason'] = task['finishReason']
    if task.get('usage'):
        meta['usage'] = task['usage']
    if task.get('preset'):
        meta['preset'] = task['preset']
    if task.get('model'):
        meta['model'] = task['model']
    if task.get('provider_id'):
        meta['provider_id'] = task['provider_id']
    if task.get('thinkingDepth'):
        meta['thinkingDepth'] = task['thinkingDepth']
    if task.get('toolSummary'):
        meta['toolSummary'] = task['toolSummary']
    if task.get('apiRounds'):
        meta['apiRounds'] = task['apiRounds']
    if task.get('modifiedFiles'):
        meta['modifiedFiles'] = task['modifiedFiles']
    if task.get('modifiedFileList'):
        meta['modifiedFileList'] = task['modifiedFileList']
    if task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        meta['todoState'] = public_todo_state(task['_todoState'])
    if task.get('_todo_blocked'):
        meta['todoBlocked'] = task['_todo_blocked']
    if task.get('costExperiment'):
        meta['costExperiment'] = task['costExperiment']
    elif task.get('_costExperiment'):
        meta['costExperiment'] = task['_costExperiment']
    if task.get('_fallback_model'):
        meta['fallbackModel'] = task['_fallback_model']
    if task.get('_fallback_from'):
        meta['fallbackFrom'] = task['_fallback_from']
    if task.get('_fallback_reason'):
        meta['fallbackReason'] = task['_fallback_reason']
    if task.get('_fallback_kind'):
        meta['fallbackKind'] = task['_fallback_kind']
    return meta


def load_or_create_conv(db, conv_id, config, payload):
    """Load existing conversation messages or create a new one.

    Returns:
        (messages_list, is_new, title) or raises.
    """
    from lib.database.conversation_repository import load_conversation
    snapshot = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('title', 'settings'))
    if snapshot is not None:
        return snapshot.messages, False, snapshot['title']

    # New conversation — create it
    title = (payload.get('text') or 'New Chat')[:60]
    # Strip <notranslate>/<nt> tags from title
    title = re.sub(r'</?(?:notranslate|nt)>', '', title, flags=re.IGNORECASE)
    now_ms = int(time.time() * 1000)
    settings = {}
    if config.get('projectPath'):
        settings['projectPath'] = config['projectPath']
    if payload.get('folderId'):
        settings['folderId'] = payload['folderId']

    from lib.database.conversation_repository import upsert_conversation
    upsert_conversation(
        db, conv_id, [], user_id=DEFAULT_USER_ID, title=title,
        created_at=now_ms, updated_at=now_ms,
        settings=json.dumps(settings, ensure_ascii=False), full=True)

    return [], True, title


def settled_turn_facts(last_msg):
    """Derive the sidebar's settled-turn facts from the tail message.

    Single source of truth for the ``lastMsgRole`` / ``lastMsgTimestamp`` /
    ``lastFinishReason`` / ``lastMsgError`` / ``lastMsgHasOutput`` settings
    quintet the meta-only sidebar shell classifies its status dot from. RAW
    facts only — the incomplete/errored CLASSIFICATION stays in the
    frontend's ``_convStatusFlags``. Every write path that rebuilds the
    settings column from a messages array MUST re-derive these from that
    array's authoritative tail (never trust a client-echoed copy) — the
    full-conv PUT handler previously omitted them, so any client sync
    silently clobbered the manager-stamped error facts and the unloaded
    sidebar shell lost its error dot.
    """
    return {
        'lastMsgRole': last_msg.get('role'),
        'lastMsgTimestamp': last_msg.get('timestamp'),
        'lastFinishReason': last_msg.get('finishReason'),
        'lastMsgError': bool(last_msg.get('error')),
        'lastMsgHasOutput': bool(
            (last_msg.get('content') or '') or (last_msg.get('thinking') or '')
            or (last_msg.get('toolRounds') or []) or last_msg.get('_igResults')),
    }


def persist_conv_messages(db, conv_id, messages, title, settings_patch=None):
    """Write messages + metadata to the conversation row.

    Backfills stable per-message ``_msgId`` UUIDs before writing.  Every
    code path that mutates ``messages`` and persists the array (send,
    regenerate, edit, continue, chat_continue) goes through this helper,
    so this is the single point of truth for id assignment on the chat
    write side — mirroring ``_assign_message_ids`` calls in
    ``manager.py`` for the partial/result sync paths.  Without this,
    newly appended messages on those flows would have no ``_msgId``,
    forcing PATCH /messages/by-id to silently fall back to index lookup.

    Returns:
        The post-write ``rev`` (int) the ``conversations_rev_bump_trg`` trigger
        advanced to on this write, or ``None`` if it could not be read back.
        Callers that emit ``notify_conv_changed`` should pass this rev so a
        sibling device does a body refetch rather than a sidebar-only refresh.
    """
    # Durable one-time heal for send-race duplicate user rows (optimistic
    # frontend copy + server-built copy sharing one ``timestamp``). The
    # send route guards via ``append_user_msg_idempotent``, but historical
    # rows and writers that bypass the helper still land here; healing ONCE
    # at this write chokepoint means the rebuild-side
    # ``_dedup_duplicate_user_messages`` stays a no-op instead of re-healing
    # the same pair in memory on every context rebuild, forever.
    from lib.tasks_pkg.conv_message_builder._dedup import _dedup_duplicate_user_messages
    _healed = _dedup_duplicate_user_messages(messages)
    if len(_healed) != len(messages):
        logger.warning('[chat] persist_conv_messages healed %d duplicate '
                       'same-timestamp user row(s) conv=%s — durable one-time '
                       'fix (race-planted optimistic/server copies)',
                       len(messages) - len(_healed), (conv_id or '?')[:8])
        try:
            audit_log('conv_user_dup_healed', conv_id=conv_id,
                      dropped=len(messages) - len(_healed),
                      seam='persist_conv_messages')
        except Exception as _ae:
            logger.debug('[chat] audit_log for dup heal failed: %s', _ae)
    messages = _healed

    # Lazy import to avoid the lib.chat → lib.tasks_pkg.manager import cycle.
    from lib.tasks_pkg.manager import _assign_message_ids
    _assign_message_ids(messages)
    now_ms = int(time.time() * 1000)
    from lib.conversations import build_search_text
    search_text = build_search_text(messages)

    # Build settings update
    settings_update = {}
    if settings_patch:
        settings_update.update(settings_patch)

    # Always inject lastMsgRole/lastMsgTimestamp + the settled-turn facts the
    # sidebar needs to render an incomplete/errored dot WITHOUT loading the
    # (stripped) messages array. We store RAW facts only (finishReason / error
    # bool / has-output bool); the incomplete/errored CLASSIFICATION stays in
    # the frontend's _convStatusFlags so there is a single classifier.
    if messages:
        settings_update.update(settled_turn_facts(messages[-1]))

    # One repository-owned write transaction now covers the settings
    # read/merge, transitional blob write, canonical message rows, revision
    # marker, and FTS refresh.  The old sequence committed the blob first and
    # mirrored rows best-effort afterwards, creating an unavoidable split-
    # brain window on any exception or process death.
    from lib.database import write_transaction
    from lib.database.messages_rows import changed_message_seqs, rows_write_enabled
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
        upsert_conversation,
    )

    _rows_on = rows_write_enabled()
    with write_transaction(db, label='persist conversation messages'):
        # Read after BEGIN IMMEDIATE on SQLite: settings merge and dirty-set
        # derivation share the same snapshot as the following mutations.
        existing = load_conversation(
            db, conv_id, user_id=DEFAULT_USER_ID,
            metadata_columns=('settings', 'created_at'))
        if existing is not None:
            try:
                settings = json.loads(existing.get('settings') or '{}')
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[chat] persist_conv_messages caught %s: %s',
                             type(_e_audit).__name__, _e_audit)
                settings = {}
            settings.update(settings_update)
            # Preserve the original creation time across every append/edit.
            created_at = existing['created_at'] or now_ms
        else:
            settings = dict(settings_update)
            created_at = now_ms

        _mirror_changed_seqs = None
        if _rows_on and existing:
            try:
                _mirror_changed_seqs = changed_message_seqs(
                    existing.messages, messages)
            except Exception as _mirror_diff_err:
                # Strong mode must prefer extra writes over a possibly stale
                # equal-count row set.  Full dirty coverage is deterministic.
                logger.warning(
                    '[chat] mirror dirty-set derivation failed conv=%s: %s '
                    '(rewriting all message rows)',
                    (conv_id or '?')[:8], _mirror_diff_err)
                _mirror_changed_seqs = list(range(len(messages)))
        elif _rows_on:
            _mirror_changed_seqs = list(range(len(messages)))

        settings_json = json.dumps(settings, ensure_ascii=False)
        if existing is None:
            result = upsert_conversation(
                db, conv_id, messages, user_id=DEFAULT_USER_ID, title=title,
                created_at=created_at, updated_at=now_ms,
                settings=settings_json, search_text=search_text,
                changed_seqs=_mirror_changed_seqs, full=False)
        else:
            result = replace_messages(
                db, conv_id, messages, user_id=DEFAULT_USER_ID,
                expected_rev=existing['rev'],
                metadata={
                    'title': title,
                    'created_at': created_at,
                    'updated_at': now_ms,
                    'settings': settings_json,
                },
                changed_seqs=_mirror_changed_seqs, full=False)
            if not result.applied:
                raise RuntimeError(
                    f'conversation {conv_id} advanced during persist; '
                    'refusing to overwrite the concurrent writer')
        return result.rev


def append_pending_user_msg(db, conv_id, user_msg, valid_assistant_ids=None):
    """CAS-append a QUEUED user message as a display-only ``_pendingQueued`` row
    so a sibling device sees it immediately (before the current turn replies).

    Cross-device visibility fix (queued lane): the queued user message used to
    live ONLY in ``message_queue`` — never in the conversation body — so another
    device could not see it until the whole current turn finished and the NEXT
    task's first checkpoint bumped rev. This lands it in the body NOW, marked
    ``_pendingQueued`` (display-only; ``dispatch_next_queued`` later reconciles
    it in place by timestamp — never a duplicate — via
    ``append_user_msg_idempotent``, and the reconcile clears the marker).

    ORDER-SAFETY + SLOT-ADDRESSABILITY GATE (the load-bearing invariant). Both
    must hold or we DECLINE (return ``(False, None)``) and the caller falls back
    to today's queue-only behaviour (message still queued, just not instantly
    mirrored — safe, no regression):

      1. The current DB tail must be an ``assistant`` message — the running
         turn's assistant slot already exists — so the row lands as
         ``[…, userA, assistantA, userB]`` (correctly ordered). Appending onto a
         non-assistant tail would create a user→user adjacency AND misorder the
         eventual reply.
      2. That tail assistant's ``_msgId`` must be in ``valid_assistant_ids``
         (the ``_assistantMsgId`` set of the currently-running task(s)). This
         guarantees the running task's ``_sync_partial/_sync_result`` locates
         ITS slot BY ID (the id-first fix) and is NOT disturbed by the trailing
         pending row. Without this match the sync's tail fallback would see the
         pending ``user`` row and spawn a SECOND assistant — the exact
         two-writer truncation this design must avoid. ``None``/empty set →
         decline (a running task that shipped no stable id can't be protected).

    CAS-guarded on ``updated_at`` so it never clobbers the concurrent
    ``_sync_partial_to_conversation`` checkpoint of the running turn.

    Returns ``(appended: bool, rev: int|None)``.
    """
    _valid_ids = {i for i in (valid_assistant_ids or ()) if i}
    if not _valid_ids:
        logger.debug('[Send] pending-user append DECLINED conv=%s — no running-task '
                     'assistant id to protect; queue-only fallback', conv_id[:8])
        return False, None
    _MAX_CAS = 4
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
    )
    for attempt in range(_MAX_CAS):
        snapshot = load_conversation(db, conv_id, user_id=DEFAULT_USER_ID)
        if snapshot is None:
            logger.warning('[Send] pending-user append: conv=%s not found', conv_id[:8])
            return False, None
        messages = snapshot.messages
        cur_rev = snapshot['rev']  # Phase 4 W2: CAS token is rev (trigger-bumped); the
        # loop re-reads the row at the top of every attempt, so cur_rev is
        # refreshed each retry. updated_at is still stamped in SET, not the token.

        _tail = messages[-1] if messages else None
        if not _tail or _tail.get('role') != 'assistant':
            # Order-safety gate: tail isn't the running turn's assistant slot.
            logger.debug('[Send] pending-user append DECLINED conv=%s — tail role=%s '
                         '(not assistant); falling back to queue-only',
                         conv_id[:8], _tail.get('role') if _tail else None)
            return False, None
        if _tail.get('_msgId') not in _valid_ids:
            # Slot-addressability gate: the running task can't locate this tail
            # slot by id, so a trailing pending row would break its sync.
            logger.debug('[Send] pending-user append DECLINED conv=%s — tail assistant '
                         '_msgId not owned by a running task; queue-only fallback',
                         conv_id[:8])
            return False, None

        # Idempotent: if a racing writer already planted this exact turn as the
        # tail (same timestamp), don't add a second row.
        _ts = user_msg.get('timestamp')
        if (messages[-1].get('role') == 'user'
                and messages[-1].get('timestamp') == _ts):
            return False, None

        pending = dict(user_msg)
        pending['_pendingQueued'] = True
        from lib.tasks_pkg.manager import _assign_message_ids
        messages.append(pending)
        _assign_message_ids(messages)

        now_ms = int(time.time() * 1000)
        result = replace_messages(
            db, conv_id, messages, user_id=DEFAULT_USER_ID,
            expected_rev=cur_rev,
            metadata={'updated_at': now_ms, 'msg_count': len(messages)})
        if result.applied:
            return True, result.rev
        # CAS miss — a concurrent writer bumped updated_at; re-read + retry.
        logger.debug('[Send] pending-user append CAS miss conv=%s attempt %d/%d',
                     conv_id[:8], attempt + 1, _MAX_CAS)
        time.sleep(0.02 * (attempt + 1))

    logger.debug('[Send] pending-user append CAS exhausted conv=%s — queue-only fallback',
                 conv_id[:8])
    return False, None


def remove_pending_user_msgs(db, conv_id, timestamps=None):
    """Remove display-only ``_pendingQueued`` mirror rows from a conversation
    body — the cancel/clear companion of :func:`append_pending_user_msg`.

    The mirror row made a queued message visible cross-device the moment it
    was queued. Cancelling the queue entry (``message_queue.remove_from_queue
    ``/``clear_queue``) used to delete ONLY the queue row, stranding the
    mirror forever: a greyed "queued" bubble for a message that will never
    run, on every device, until someone hand-edited the history. This sweep
    removes those rows.

    Matching is by the queued user message's ``timestamp`` — the same
    identity ``append_user_msg_idempotent`` reconciles on dispatch — and
    restricted to rows STILL carrying ``_pendingQueued`` (a dispatched turn's
    row lost the marker and is a real turn; never touched).

    Args:
        db: thread db handle.
        conv_id: conversation id.
        timestamps: iterable of queued ``user_msg.timestamp`` values to
            sweep; ``None`` sweeps every pending row of the conv (the
            clear_queue semantics). An empty iterable is a no-op.

    Returns ``(removed_count, new_rev_or_None)``. CAS-guarded on ``rev`` like
    the append twin so a concurrent checkpoint is never clobbered; a CAS
    loss simply retries against the fresh row.
    """
    if timestamps is not None:
        timestamps = {t for t in timestamps if t is not None}
        if not timestamps:
            return 0, None
    _MAX_CAS = 4
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
    )
    for attempt in range(_MAX_CAS):
        snapshot = load_conversation(db, conv_id, user_id=DEFAULT_USER_ID)
        if snapshot is None:
            return 0, None
        messages = snapshot.messages
        cur_rev = snapshot['rev']
        kept = []
        removed = 0
        for m in messages:
            if (isinstance(m, dict) and m.get('_pendingQueued')
                    and m.get('role') == 'user'
                    and (timestamps is None or m.get('timestamp') in timestamps)):
                removed += 1
                continue
            kept.append(m)
        if not removed:
            return 0, None
        now_ms = int(time.time() * 1000)
        result = replace_messages(
            db, conv_id, kept, user_id=DEFAULT_USER_ID,
            expected_rev=cur_rev,
            metadata={'updated_at': now_ms, 'msg_count': len(kept)},
            # Removing rows can shift every later sequence.
            full=True)
        if result.applied:
            rev = result.rev
            logger.info('[Queue] swept %d pending mirror row(s) conv=%s (rev=%s)',
                        removed, conv_id[:8], rev)
            return removed, rev
        logger.debug('[Queue] pending-mirror sweep CAS miss conv=%s attempt %d/%d',
                     conv_id[:8], attempt + 1, _MAX_CAS)
        time.sleep(0.02 * (attempt + 1))
    logger.warning('[Queue] pending-mirror sweep CAS exhausted conv=%s — '
                   'mirror row left for the next reload reconcile', conv_id[:8])
    return 0, None


__all__ = [
    'extract_db_meta',
    'extract_task_meta',
    'load_or_create_conv',
    'persist_conv_messages',
    'append_pending_user_msg',
    'remove_pending_user_msgs',
    'settled_turn_facts',
]
__all__ = [
    'extract_db_meta',
    'extract_task_meta',
    'load_or_create_conv',
    'persist_conv_messages',
    'append_pending_user_msg',
    'settled_turn_facts',
]
