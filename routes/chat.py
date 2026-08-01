"""routes/chat.py — Chat start, streaming, polling, abort."""

import json
import threading
import time

import orjson

from flask import Blueprint, jsonify, request

from lib.agent_core.events import EventType, build_event
from lib.database import DOMAIN_CHAT, get_db
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_error, api_internal_error, api_not_found, api_ok, sse_response
from lib.request_parser import parse_body
from routes.api_v1.auth import current_auth, require_scope

from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

import re

from lib.database import get_thread_db
# Back-compat aliases — the implementations moved to lib/chat/messages.py
# (2026-06) to break the lib→routes circular import. Internal callers below
# and external importers (lib.message_queue) keep using these private names.
from lib.chat import (  # noqa: F401
    append_user_msg_idempotent as _append_user_msg_idempotent,
    auto_translate_user as _auto_translate_user,
    build_tool_history_round as _build_tool_history_round,
    append_pending_user_msg as _append_pending_user_msg,
    build_user_msg_from_payload as _build_user_msg_from_payload,
    extract_db_meta as _extract_db_meta,
    extract_task_meta as _extract_task_meta,
    get_send_translate_status,
    load_or_create_conv as _load_or_create_conv,
    persist_conv_messages as _persist_conv_messages,
    resolve_conv_refs as _resolve_conv_refs,
    scan_continue_checkpoint as _scan_continue_checkpoint,
)
from lib.idempotency import idempotent_post
from routes.common import DEFAULT_USER_ID, _notify_conv_changed, _request_user_id

logger = get_logger(__name__)

chat_bp = Blueprint('chat', __name__)


# ─── Pure helpers extracted to routes/chat_helpers.py (pt_04686ac6 slice 1) ───
# The four functions below (_dumps_yielding, _running_checkpoint_verdict,
# _log_poll_task_id_mismatch, _loads_yielding) moved to routes/chat_helpers.py.
# Re-exported here so every ``from routes.chat import _dumps_yielding``-style
# call site keeps working unchanged. See tests/test_routes_chat_wire_parity.py.
from routes.chat_helpers import (  # noqa: E402,F401  (re-export, kept for external imports)
    _dumps_yielding,
    _loads_yielding,
    _log_poll_task_id_mismatch,
    _running_checkpoint_verdict,
)
# v1 blueprint for the JSON routes (the carve-out /api/chat/stream/<id> stays on chat_bp).
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402


@api_v1_chat_bp.route('/api/v1/chat/active', methods=['GET'], endpoint='ui_chat_active')
@require_scope('chat')
def chat_active():
    """List in-memory tasks (id, conv, status, abort flag).

    Used by the frontend on reload to decide whether to resume polling
    a task it knew about before navigation. Cleans up stale finished
    tasks as a side effect.
    """
    cleanup_old_tasks()
    with tasks_lock:
        # ``tasks`` is a process-global registry mutated by many code paths
        # (the orchestrator, external/eval harnesses, tests). A malformed
        # entry missing optional keys must not 500 this status endpoint, so
        # read every field defensively.
        #
        # ★ Exclude non-streaming CARRIER/HOLDER tasks (``_inline_messages`` /
        #   ``_vu_subtask``): the autopilot VU + reporter sub-turns and the
        #   summarize carrier use ``create_task`` purely as a message container
        #   and never stream a ``done`` event. If the frontend sees one here it
        #   runs orphan-recovery (initActiveTasks Case C / cross-tab reconcile),
        #   pushes an empty assistant placeholder, and connects an SSE that
        #   never completes → a permanently-stuck "Waiting…" bubble. Reconnect
        #   only ever makes sense for real UI-streaming tasks, so hide carriers.
        #   (The autopilot-kick carrier sets ``_autopilot_kick``, NOT these
        #   flags, so it is still reported and reconnectable.)
        #   ★ SINGLE SOURCE OF TRUTH: the same is_carrier_task predicate the
        #     restart guard (list_running_tasks) uses, so the reconnect view and
        #     the restart-guard count can never diverge about carriers.
        from lib.tasks_pkg.manager import is_carrier_task
        result = [{'id': t.get('id', ''), 'convId': t.get('convId', ''),
                   'status': t.get('status', ''),
                   'aborted': bool(t.get('aborted'))}
                  for t in tasks.values()
                  if not is_carrier_task(t)]
    return jsonify(result)


@api_v1_chat_bp.route('/api/v1/chat/start', methods=['POST'], endpoint='ui_chat_start')
@require_scope('chat')
@idempotent_post()
def chat_start():
    """Start a chat task. Body is ``{convId, config[, messages]}``.

    Default flow: load messages from the DB via ``build_api_messages_from_db``,
    then dispatch to the built-in orchestrator. External callers (SWE-bench,
    eval harnesses) may pass ``messages`` inline, which sets ``_inline_messages``
    so :func:`_sync_result_to_conversation` skips DB write-back.

    Returns ``{taskId}``; the client polls ``/api/v1/chat/poll/<taskId>``.
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    cfg = data.get('config', {})

    # ── Server-side message building ──
    # The frontend now sends {convId, config} only.
    # Messages are loaded from the DB and transformed server-side.
    # Legacy / external-caller path: if the POST body ships 'messages' inline
    # (SWE-bench harness, eval tools, external backends), use them as-is and
    # skip the DB-backed conversation write-back later — see `_inline_messages`
    # flag below which is consumed by _sync_result_to_conversation().
    messages = data.get('messages')
    inline_messages = bool(messages)
    if not messages:
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        exclude_last = cfg.get('excludeLast', False)
        messages = build_api_messages_from_db(conv_id, cfg, exclude_last=exclude_last)
        if messages is None:
            return api_not_found('Conversation not found')
        if not messages:
            return api_bad_request('No messages')
        logger.info('[Chat] Built %d API messages from DB for conv %s',
                    len(messages), conv_id[:8])

    cleanup_old_tasks()

    # ★ Interrupted-tail guard (single source of truth = the RAW conv DB
    #   tail — the API transform REBUILDS assistant rows and strips the
    #   lifecycle fields (interruptedReason / finishReason), so this MUST
    #   probe the raw row, never the built prompt list).
    #   A bare start whose tail is an INTERRUPTED assistant stub must not
    #   APPEND a sibling turn — that produces the U A U A(stub) A(answer)
    #   doubled layout (the ms43foj3 incident, 2026-07-28: the frontend's
    #   pop-and-regenerate escape hatch reached /chat/start with the rolled-
    #   back stub still at the tail, and the fresh task appended a twin
    #   answer instead of resuming the stub). The continue contract IS the
    #   truthful behavior: roll the stub back to its checkpoint and RESUME it
    #   in place; only when nothing is recoverable do we drop the stub and
    #   start fresh — either way, one user turn keeps ONE agent bubble.
    if not inline_messages and not cfg.get('excludeLast'):
        _raw_tail = None
        try:
            _db_guard = get_thread_db(DOMAIN_CHAT)
            _guard_row = _db_guard.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=?',
                (conv_id, DEFAULT_USER_ID)).fetchone()
            if _guard_row:
                _guard_msgs = json.loads(_guard_row['messages'] or '[]')
                if _guard_msgs:
                    _raw_tail = _guard_msgs[-1]
        except (json.JSONDecodeError, TypeError) as _guard_e:
            logger.debug('[Chat] interrupted-tail probe parse failed: %s', _guard_e)
        if (isinstance(_raw_tail, dict)
                and _raw_tail.get('role') == 'assistant'
                and _raw_tail.get('interruptedReason')
                and not _raw_tail.get('finishReason')):
            from lib.conversations import set_conversation_settings as _scs
            from lib.log import audit_log as _al
            _ct_outcome = execute_chat_continue(
                conv_id, cfg, None, data,
                user_id=_request_user_id(),
                _start_task_for_conv=_start_task_for_conv,
                _persist_conv_messages=_persist_conv_messages,
                _set_conversation_settings=_scs,
                _notify_conv_changed=_notify_conv_changed,
                _audit_log=_al)
            if _ct_outcome.kind == 'started':
                logger.info('[Chat] /chat/start conv=%s tail is an interrupted '
                            'assistant stub — resumed in place via the continue '
                            'contract instead of appending a twin turn',
                            conv_id[:8])
                return jsonify(_ct_outcome.payload)
            if _ct_outcome.kind == 'error':
                return _ct_outcome.err_resp
            # fallback: nothing recoverable (empty husk, or no checkpoint and
            #   no resumable prefill) — drop the stub from the RAW conv
            #   messages, persist, then rebuild the API messages without it.
            #   The turn is REPLACED, never twinned.
            logger.info('[Chat] /chat/start conv=%s interrupted tail unrecoverable '
                        '(%s) — dropping stub and starting fresh',
                        conv_id[:8], (_ct_outcome.payload or {}).get('reason', '?'))
            _db_start = get_thread_db(DOMAIN_CHAT)
            _raw_row = _db_start.execute(
                'SELECT messages, title FROM conversations WHERE id=? AND user_id=?',
                (conv_id, DEFAULT_USER_ID)).fetchone()
            if _raw_row:
                _raw_msgs = json.loads(_raw_row['messages'] or '[]')
                if _raw_msgs and _raw_msgs[-1].get('role') == 'assistant':
                    _raw_msgs.pop()
                    _persist_conv_messages(_db_start, conv_id, _raw_msgs,
                                           _raw_row['title'], None)
            messages = build_api_messages_from_db(conv_id, cfg,
                                                  exclude_last=exclude_last)
            if not messages:
                return api_bad_request('No messages')

    # ★ Supersede (abort any stale running task for this conv) is now an
    #   invariant of create_task(supersede=True, the default) — no explicit
    #   pre-abort needed here. The ordering-critical explicit sweeps in
    #   _start_task_for_conv (before build_api_messages_from_db) and
    #   chat_abort_conv remain, because those must run BEFORE their DB read.
    task = create_task(conv_id, messages, cfg)
    # Human-attended UI task: a user is watching and can answer the write-
    # approval prompt, so Manual mode actually gates here (see
    # execute_tool_pipeline's attendance-aware default).
    task['_attended'] = True
    # Tag tasks that were started with inline messages (no DB-backed
    # conversation row). These tasks skip _sync_result_to_conversation()
    # entirely — external callers read results from task_results directly.
    if inline_messages:
        task['_inline_messages'] = True

    # ── Admission control (backpressure) ──
    # Cap concurrent in-flight tasks so a UI spawn storm can't exhaust the
    # agent-worker pool — the SAME ceiling the headless paths enforce
    # (controller.try_acquire → 503 + retry_after when full). The slot is
    # released on the task's terminal event via on_terminal (fires once from
    # the orchestrator worker thread), so a UI spawn can never permanently
    # consume capacity. Registered BEFORE spawn so no terminal signal is
    # missed.
    from lib.agent_core.admission import controller as _admission, on_terminal
    if not _admission.try_acquire():
        _stats = _admission.stats()
        logger.warning('[Chat] /chat/start refused for conv=%s — at inflight '
                       'capacity (%d/%d)', conv_id[:8],
                       _stats['in_flight'], _stats['capacity'])
        return jsonify({
            'ok': False,
            'error': {'kind': 'capacity',
                      'detail': 'Server is at task capacity. Retry shortly.',
                      'retry_after_s': 3},
        }), 503
    on_terminal(task['id'], lambda _tid: _admission.release())

    from lib.tasks_pkg import spawn_task
    _cfg_model = cfg.get('model', '?')
    _cfg_preset = cfg.get('preset', cfg.get('effort', '?'))
    logger.info('[Chat] Starting task %s for conv %s model=%s preset=%s',
                task['id'], task['convId'], _cfg_model, _cfg_preset)
    try:
        spawn_task(task)
    except Exception as _spawn_err:
        logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                         task['id'], task['convId'])
        # spawn failed → no terminal event will fire, so the on_terminal
        # release above never runs. Release the admission slot here to avoid
        # permanently leaking capacity on a spawn error.
        _admission.release()
        from lib.error_envelope import make_envelope as _make_env
        task['status'] = 'error'
        task['error'] = _make_env(
            'internal',
            detail='Server failed to start task thread.',
            model=cfg.get('model', ''),
            context='chat-start',
            source='routes.chat',
            raw=str(_spawn_err),
        )
        return api_internal_error('Failed to start task')

    return jsonify({'taskId': task['id']})


# ══════════════════════════════════════════════════════════
#  Atomic send: user message creation + task start
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  Per-conv abort marker for in-flight /api/chat/send.
#
#  When the user clicks Stop while a /api/chat/send is still inside the
#  synchronous auto-translate (and no task has been started yet), the
#  frontend hits /api/chat/abort-conv but there is nothing to abort —
#  the send handler will keep blocking, finish translating, then either
#  start a task or enqueue the message. We record the abort wall-clock
#  here so the send handler can detect 'this conv was aborted AFTER my
#  request started' and bail out before persisting / enqueueing /
#  dispatching anything.
# ══════════════════════════════════════════════════════════
# ─── Send-abort marker bundle extracted to routes/chat_state.py (pt_04686ac6 slice 2) ───
# _send_abort_marker (dict) + _send_abort_marker_lock (threading.Lock) +
# _mark_conv_aborted() + _was_aborted_after() moved as an ATOMIC BUNDLE — the state
# and the two functions that manipulate it MUST live in the same module so nobody can
# import half of the pair. Re-exported here so every "from routes.chat import
# _mark_conv_aborted / _was_aborted_after" call site keeps working unchanged.
# See tests/test_routes_chat_wire_parity.py for the guard.
from routes.chat_state import (  # noqa: E402,F401  (re-export)
    _mark_conv_aborted,
    _send_abort_marker,
    _send_abort_marker_lock,
    _was_aborted_after,
)


@api_v1_chat_bp.route('/api/v1/chat/translate-status/<conv_id>', methods=['GET'], endpoint='ui_chat_send_translate_status')
@require_scope('chat')
def chat_send_translate_status(conv_id):
    """Return the current send-path translate retry status for a conv.

    Returns ``{statusMessage, statusKind, updatedAt}`` or ``{}`` if no
    translate is currently in flight (or hasn't yet hit its first retry).
    """
    return jsonify(get_send_translate_status(conv_id) or {})


# ─── _truncate_conv_history moved to routes/chat_side_effects.py (pt_04686ac6 slice 3) ───
# The IO-heavy helper that clears the message-queue + server-message-store side
# channels after a conv history truncate. Kept out of routes/chat_helpers.py to
# preserve that module's "pure + import-lightweight" invariant; kept out of
# routes/chat_state.py because it owns no state (it acts on state living in
# OTHER lib.* packages). Re-exported here so
# ``from routes.chat import _truncate_conv_history`` (e.g.
# tests/test_regen_clears_msg_store.py) keeps working unchanged. See
# tests/test_routes_chat_wire_parity.py for the guard.
from routes.chat_side_effects import _truncate_conv_history  # noqa: E402,F401


# _start_task_for_conv moved to routes/chat_task_start.py (pt_04686ac6 slice 4).
# The ~150-line task-starter orchestrator (called from chat_send / chat_regenerate
# / chat_continue / chat_branch_start) lives there as a module-level callable.
# Re-exported here so `from routes.chat import _start_task_for_conv` (5 test
# monkeypatches at exactly that dotted path) keeps working unchanged. Every
# internal caller inside chat.py reads _start_task_for_conv via LOAD_GLOBAL, so
# a monkeypatch on `routes.chat._start_task_for_conv` still steers all 4 call
# sites — same-object identity guarded by tests/test_routes_chat_wire_parity.py.
from routes.chat_task_start import _start_task_for_conv  # noqa: E402,F401


@api_v1_chat_bp.route('/api/v1/chat/send', methods=['POST'], endpoint='ui_chat_send')
@require_scope('chat')
@idempotent_post()
def chat_send():
    """Atomic send: create user message + auto-translate + persist + start task.

    If a task is already running for this conversation, the message is
    auto-translated, persisted to the user-visible conversation (so it
    appears instantly on the frontend), and enqueued to ``message_queue``
    for automatic dispatch when the current task finishes.  The frontend
    receives ``{queued: true}`` and renders a queue indicator — it never
    needs to decide whether to queue or send.

    Body: {
        convId: str,
        message: { text, images?, pdfTexts?, replyQuotes?, convRefs?, convRefTexts?, folderId? },
        config: { model, searchMode, ... all tool settings },
        settings?: { per-conv tool state to persist }
    }

    Returns on immediate start:
        { taskId, convId, title, userMessage, isNew, msgCount }
    Returns on queue:
        { queued: true, queueId, position, convId, title, userMessage, isNew, msgCount }
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    payload = data.get('message', {})
    config = data.get('config', {})
    settings_patch = data.get('settings')

    text = payload.get('text', '')
    if not text and not payload.get('images') and not payload.get('pdfTexts'):
        return api_bad_request('Empty message')

    # Snapshot the request start wall-clock BEFORE the synchronous
    # auto-translate call. If /api/chat/abort-conv runs while we are
    # blocked in translation, _was_aborted_after() will catch it.
    _send_started_at = time.time()

    try:
        db = get_thread_db(DOMAIN_CHAT)

        # 1. Load or create conversation
        messages, is_new, title = _load_or_create_conv(db, conv_id, config, payload)

        # 2. Build user message (with auto-translate).
        #    Pass conv_id so send-path retries surface via
        #    /api/chat/send-translate-status/<conv_id>.
        user_msg = _build_user_msg_from_payload(payload, config, conv_id=conv_id)

        # 3. Compute title for first user message
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) == 0 and text:
            title_text = re.sub(r'</?(?:notranslate|nt)>', '', text, flags=re.IGNORECASE)
            title = title_text[:60] + ('...' if len(title_text) > 60 else '')

        logger.info('[Send] conv=%s msgs=%d title=%.50s isNew=%s translated=%s',
                    conv_id[:8], len(messages), title, is_new,
                    bool(user_msg.get('originalContent')))

        # ── Business-logic pipeline extracted to lib/chat_dispatch.py
        #    (pt_04686ac6 slice 5): abort-during-translate check, abort-on-
        #    send race, running-task classification, autopilot-followup
        #    supersede, inject-mode steer, queue path (with cross-device
        #    pending-row mirror). Returns None to signal "fall through to
        #    immediate start"; returns a SendIntent to short-circuit with
        #    the classifier's response.
        from lib.chat_dispatch import classify_send_intent
        _intent = classify_send_intent(
            db=db, conv_id=conv_id, config=config, payload=payload,
            data=data, messages=messages, is_new=is_new, title=title,
            user_msg=user_msg, settings_patch=settings_patch,
            text=text, send_started_at=_send_started_at,
        )
        if _intent is not None:
            return jsonify(_intent.response)

        # 4. Append user message and persist (only for immediate start).
        #    Idempotent: if a racing sync already planted the optimistic copy
        #    as the tail (matching timestamp), reconcile in place instead of
        #    appending a duplicate. This is the root-cause guard — the
        #    frontend _sendInFlight flag merely avoids triggering the race.
        _append_user_msg_idempotent(messages, user_msg)
        _send_rev = _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # 5. Start task (no active task — send immediately)
        # Anchor the turn-ctx capsule reconcile: stamp the freshly-built
        # user_msg's stable _msgId on the task so the DONE SSE frame carries
        # userMsgId → the frontend targets THIS user message even if a
        # concurrent VU / send has since appended a newer user row.
        task_id, err_resp = _start_task_for_conv(
            conv_id, config, data,
            user_msg_id=user_msg.get('_msgId') or '')
        if err_resp is not None:
            if isinstance(err_resp, tuple):
                return err_resp
            return err_resp

        # 6. Update activeTaskId in settings.
        #    ★ Settings-ONLY write (not a full-row _persist_conv_messages):
        #    the only new information here is activeTaskId. Rewriting the whole
        #    `messages` array with this route's stale user-only tail (and
        #    bumping updated_at) races the task thread, which may have ALREADY
        #    checkpointed the assistant slot via _sync_partial_to_conversation —
        #    clobbering the streamed content back to the pre-start snapshot
        #    ("Waiting…" on reload). set_conversation_settings does a per-conv
        #    serialized merge of just this key and never touches messages.
        try:
            from lib.conversations import set_conversation_settings
            # notify=False: each of these paths emits its own
            # _notify_conv_changed below, so the gate must invalidate the local
            # cache (structural guarantee) WITHOUT a second cross-device push.
            set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                      db=db, notify=False)
        except Exception as e:
            logger.warning('[Send] Failed to update activeTaskId: %s', e)

        # ★ Carry the REAL post-write rev (not None): the user message was just
        #   persisted (step 4) and the rev trigger bumped it. A rev-bearing
        #   frame makes a sibling device's rev-gate refetch the body — so the
        #   just-sent user message appears on the other device immediately,
        #   instead of a rev=None frame that only nudges the sidebar and leaves
        #   the message invisible until the assistant reply lands.
        _notify_conv_changed(conv_id, rev=_send_rev, user_id=_request_user_id())

        return jsonify({
            'taskId': task_id,
            'convId': conv_id,
            'title': title,
            'userMessage': user_msg,
            'isNew': is_new,
            'msgCount': len(messages),
        })

    except Exception as e:
        logger.error('[Send] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')



@api_v1_chat_bp.route('/api/v1/chat/branch', methods=['POST'], endpoint='ui_chat_branch_start')
@require_scope('chat')
def chat_branch_start():
    """Start a branch task with server-side message building.

    The backend loads the conversation from DB, extracts the main chat context
    up to the branch anchor point, appends the branch's own messages (decorated
    with topic + selection context), and runs the full message transform pipeline.
    This eliminates the frontend ``_buildBranchApiMessages`` → ``buildApiMessages``
    code path that could diverge from the backend builder.

    Body: {
        convId: str,
        msgIdx: int,           // index of the parent message the branch is attached to
        branchIdx: int,        // index of the branch within parent.branches[]
        config: { model, searchMode, branchKey, ... }
    }

    Returns: { taskId }
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    msg_idx = data.get('msgIdx')
    branch_idx = data.get('branchIdx')
    if msg_idx is None or branch_idx is None:
        return api_bad_request('msgIdx and branchIdx required')

    cfg = data.get('config', {})

    try:
        from lib.tasks_pkg.conv_message_builder import build_branch_api_messages

        api_messages = build_branch_api_messages(conv_id, msg_idx, branch_idx, cfg)
        if api_messages is None:
            return api_not_found('Branch not found')
        if not api_messages:
            return api_bad_request('No messages to process')

        cleanup_old_tasks()

        # ★ supersede=False: a branch is a DELIBERATE concurrency axis — it must
        #   run alongside the main task and sibling branches, so it must NOT
        #   abort other running tasks for this conv (see create_task docstring).
        task = create_task(conv_id, api_messages, cfg, supersede=False)
        task['_attended'] = True
        task_id = task['id']
        _cfg_model = cfg.get('model', '?')
        logger.info('[Branch] Starting task %s for conv %s msg=%d branch=%d model=%s',
                    task_id[:8], conv_id[:8], msg_idx, branch_idx, _cfg_model)

        from lib.tasks_pkg import spawn_task
        try:
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Branch] Failed to start thread for task %s', task_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start branch task thread.',
                model=cfg.get('model', ''),
                context='branch-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return api_internal_error('Failed to start task')

        return jsonify({'taskId': task_id})

    except Exception as e:
        logger.error('[Branch] Failed for conv=%s msg=%d branch=%d: %s',
                     conv_id[:8], msg_idx, branch_idx, e, exc_info=True)
        return api_internal_error('internal_error')


@api_v1_chat_bp.route('/api/v1/chat/regenerate', methods=['POST'], endpoint='ui_chat_regenerate')
@require_scope('chat')
def chat_regenerate():
    """Atomic regenerate/edit: truncate messages + optional edit + auto-translate + start task.

    Body: {
        convId: str,
        truncateToIndex: int,         // keep messages[0..truncateToIndex] inclusive
        editedContent?: str,          // if provided, replace the message at truncateToIndex
        editedImages?: [],            // replacement images (optional)
        editedPdfTexts?: [],          // replacement pdfTexts (optional)
        config: { model, searchMode, ... },
        settings?: { per-conv tool state to persist }
    }

    Returns: { taskId, convId, title, msgCount, userMessage? }
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    truncate_to = data.get('truncateToIndex')
    if truncate_to is None:
        return api_bad_request('truncateToIndex required')

    config = data.get('config', {})
    edited_content = data.get('editedContent')
    edited_images = data.get('editedImages')
    edited_pdf_texts = data.get('editedPdfTexts')
    settings_patch = data.get('settings')

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, title FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()

        if not row:
            return api_not_found('Conversation not found')

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Regen] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
            return api_internal_error('Failed to parse conversation')

        title = row['title']

        # ── Phase 3: msgId is authoritative; index is the fallback ──
        # The client sends ``truncateToMsgId`` (the stable id of the user
        # message to keep-and-resend-from) alongside the legacy
        # ``truncateToIndex``. If the id resolves in the freshly-loaded
        # messages, its CURRENT index wins — this is index-drift-proof if any
        # writer reordered messages between the client's read and this request.
        # When the id is absent (older client) or doesn't resolve (message
        # since deleted), we fall back to the supplied index unchanged, so the
        # behaviour is strictly additive.
        _truncate_msg_id = data.get('truncateToMsgId')
        if _truncate_msg_id:
            from lib.tasks_pkg.manager import find_message_by_id
            _resolved_idx, _ = find_message_by_id(messages, _truncate_msg_id)
            if _resolved_idx is not None:
                if _resolved_idx != truncate_to:
                    logger.info('[Regen] conv=%s truncateToMsgId resolved to index '
                                '%d (client sent index %d — drift corrected)',
                                conv_id[:8], _resolved_idx, truncate_to)
                truncate_to = _resolved_idx
            else:
                logger.debug('[Regen] conv=%s truncateToMsgId=%s did not resolve '
                             '— using index %d', conv_id[:8],
                             str(_truncate_msg_id)[:12], truncate_to)

        if truncate_to < 0 or truncate_to >= len(messages):
            return api_bad_request(f'truncateToIndex {truncate_to} out of range (0..{len(messages)-1})')

        # 1. Truncate
        messages = messages[:truncate_to + 1]

        # 2. Apply edit if provided
        user_msg = messages[truncate_to]
        if edited_content is not None:
            user_msg['content'] = edited_content
            user_msg.pop('originalContent', None)
            user_msg['timestamp'] = int(time.time() * 1000)
        if edited_images is not None:
            user_msg['images'] = edited_images
        if edited_pdf_texts is not None:
            user_msg['pdfTexts'] = edited_pdf_texts
        # Refresh the per-turn context snapshot when the client sends a fresh
        # one (edit-and-resend re-runs with possibly new model/tools). A plain
        # regenerate omits ``ctx`` so the original snapshot is preserved.
        # See static/js/info-rail.js.
        if data.get('ctx'):
            user_msg['_ctx'] = data['ctx']

        # 3. Auto-translate if needed
        text = user_msg.get('content', '')
        from lib.conv_config import resolve_auto_translate
        auto_translate = resolve_auto_translate(config)
        # Track whether we mutated user_msg so the response tells the frontend
        # to refresh its local copy (otherwise a later full-conv sync would PUT
        # the stale pre-restore content back into the DB).
        restored_original = False

        # ── Autopilot (virtual-user) / endpoint-critic messages ──
        # These are role='user' but DISPLAY-translated: `content` is the
        # model-language original (shown in the 原文 toggle) and
        # `translatedContent` is the UI-language rendering shown in the OUTER
        # bubble. This is the OPPOSITE wiring of a normal user message.
        # An edit changes `content`, which invalidates the cached
        # `translatedContent` — drop it (+ `_translatedCache`) so the outer
        # bubble re-renders the edited `content` instead of the stale 译文
        # (the reported "edit only shows in the toggle" bug). They must NOT go
        # through the normal user→English auto-translate path below, which
        # would set `originalContent` and corrupt the VU/critic structure into
        # a double-translated mess.
        _is_vu_critic = bool(user_msg.get('_isVirtualUser') or user_msg.get('_isEndpointReview'))
        if _is_vu_critic and edited_content is not None:
            for _k in ('translatedContent', '_translatedCache', '_translateDone',
                       '_translateModel', '_translateField', '_translateError'):
                user_msg.pop(_k, None)
            restored_original = True  # tell the client to refresh its local copy
            logger.info('[Regen] conv=%s VU/critic edit — cleared stale display '
                        'translation (translatedContent) before regenerate',
                        conv_id[:8])

        if _is_vu_critic:
            # Skip the normal user-message translate paths entirely for VU /
            # critic messages — their `content` is fed to the model as-is.
            auto_translate = False
        if not auto_translate and text and user_msg.get('originalContent'):
            # Auto-translate is OFF, but this message carries a translation from
            # an earlier auto-translate-ON turn: content=English (sent to the
            # model), originalContent=the user's original (what they see). A
            # plain regenerate sends no editedContent, so without this the model
            # would silently receive the stale English instead of the original.
            # Restore the original and drop the translation metadata so the run
            # honours the current (OFF) setting and matches the on-screen text.
            # (Edit-and-resend already pops originalContent in step 2, so this
            # only triggers for the no-edit regenerate path.)
            user_msg['content'] = user_msg['originalContent']
            user_msg.pop('originalContent', None)
            user_msg.pop('_translateDone', None)
            user_msg.pop('_translateModel', None)
            user_msg.pop('_translateFailed', None)
            text = user_msg['content']
            restored_original = True
            logger.info('[Regen] conv=%s auto-translate OFF — restored original '
                        'text (dropped stale translation) before regenerate',
                        conv_id[:8])
        if auto_translate and text:
            # Translate ANY non-English input to English (English is the
            # model's strongest language). The actual "is this already
            # English / what is the source language" decision lives inside
            # _auto_translate_user (it honours config.translateSourceLang and
            # falls back to a heuristic only when the source is unknown), so
            # we don't pre-gate on a Latin-script check here — that would
            # wrongly skip German/Spanish/etc. which are also Latin-script.
            # If the message already has originalContent and _translateDone, skip re-translation
            # (user didn't edit the text, just regenerating)
            already_translated = (user_msg.get('originalContent')
                                  and user_msg.get('_translateDone')
                                  and edited_content is None)
            if not already_translated:
                translated, original, model, fail_reason = _auto_translate_user(
                    text, config, conv_id=conv_id)
                if original:
                    user_msg['content'] = translated
                    user_msg['originalContent'] = original
                    user_msg['_translateDone'] = True
                    user_msg.pop('_translateFailed', None)
                    if model:
                        user_msg['_translateModel'] = model
                elif fail_reason:
                    # Translation attempted but failed/timed out — original text
                    # was sent. Flag it so the frontend shows a non-silent notice.
                    user_msg['_translateFailed'] = fail_reason

        # 4. Update title if this is the only user message
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) == 1 and text:
            original_text = user_msg.get('originalContent') or text
            title_text = re.sub(r'</?(?:notranslate|nt)>', '', original_text, flags=re.IGNORECASE)
            title = title_text[:60] + ('...' if len(title_text) > 60 else '')

        # 5. Persist truncated messages to DB
        _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # 5a+5b. Discharge the post-truncation obligations (clear the message
        #        queue + the in-memory server_message_store) via the single
        #        helper so the invariant can't be half-applied. See
        #        _truncate_conv_history for the full rationale.
        _truncate_conv_history(conv_id)

        logger.info('[Regen] conv=%s truncated to idx=%d msgs=%d edited=%s title=%.50s',
                    conv_id[:8], truncate_to, len(messages),
                    edited_content is not None, title)

        # 6. Start task
        # Anchor the turn-ctx capsule reconcile: regenerate targets a
        # SPECIFIC prior user_msg (the one at truncate_to). Ship its stable
        # _msgId so the DONE frame's userMsgId lands the model/depth/modes
        # correction on THAT bubble, not whatever happens to be last later.
        task_id, err_resp = _start_task_for_conv(
            conv_id, config, data,
            user_msg_id=user_msg.get('_msgId') or '')
        if err_resp is not None:
            if isinstance(err_resp, tuple):
                return err_resp
            return err_resp

        # 7. Update activeTaskId (settings-only — see the note in chat_send:
        #    a full-row rewrite here would clobber a task-thread checkpoint).
        try:
            from lib.conversations import set_conversation_settings
            # notify=False: each of these paths emits its own
            # _notify_conv_changed below, so the gate must invalidate the local
            # cache (structural guarantee) WITHOUT a second cross-device push.
            set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                      db=db, notify=False)
        except Exception as e:
            logger.warning('[Regen] Failed to update activeTaskId: %s', e)

        _notify_conv_changed(conv_id, rev=None, user_id=_request_user_id())

        return jsonify({
            'taskId': task_id,
            'convId': conv_id,
            'title': title,
            'msgCount': len(messages),
            'userMessage': (user_msg if (edited_content is not None
                                         or restored_original) else None),
            # Tells the frontend to DROP its stale local translation fields
            # (originalContent / _translateDone / …). Object.assign-merging
            # the returned user_msg alone wouldn't remove keys that the server
            # dropped, leaving a ghost bilingual block + a re-PUT risk.
            'restoredOriginal': restored_original,
        })

    except Exception as e:
        logger.error('[Regen] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


# ══════════════════════════════════════════════════════════
#  Continue: checkpoint-based resumption of an assistant turn
#  (_build_tool_history_round + _scan_continue_checkpoint moved to
#   lib/chat/turn_builder.py; re-exported at the top of this module)
# ══════════════════════════════════════════════════════════


# chat_continue's checkpoint/prefill core extracted to lib/chat_dispatch.py
# (epic pt_f5771a2e — previously the ~270-line route body; now the SINGLE
# continue contract shared with chat_start's interrupted-tail guard) — see
# ``execute_chat_continue``.
from lib.chat_dispatch import execute_chat_continue


@api_v1_chat_bp.route('/api/v1/chat/continue', methods=['POST'], endpoint='ui_chat_continue')
@require_scope('chat')
def chat_continue():
    """Atomic continue: roll back the last assistant message to its last
    complete tool-call checkpoint, persist the rolled-back state to DB,
    then start a new task that resumes from that checkpoint.

    Body: {
        convId: str,
        config: { model, ... },
        settings?: { per-conv tool state to persist }
    }

    Returns on success:
        { taskId, convId, checkpoint: {
            keptRounds, discardedRounds,
            preservedContentLen, discardedContentLen,
            preservedThinkingChars, discardedThinking,
        }}

    If no recoverable checkpoint is found (no complete tool rounds), returns
    ``{fallback: "regenerate"}`` and the frontend should pop-and-resend.

    Thin wrapper — the scan / rollback / persist / task-start contract lives
    in ``lib.chat_dispatch.execute_chat_continue`` (shared with chat_start's
    interrupted-tail guard); this handler only parses the body and maps the
    outcome to HTTP responses. ``_start_task_for_conv`` /
    ``_persist_conv_messages`` are passed as the routes.chat module bindings
    READ AT CALL TIME, so existing monkeypatches
    (``monkeypatch.setattr('routes.chat._start_task_for_conv', ...)``) keep
    steering the route exactly as before the extraction.
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    config = data.get('config') or {}
    settings_patch = data.get('settings')

    try:
        from lib.conversations import set_conversation_settings as _scs
        from lib.log import audit_log as _al
        outcome = execute_chat_continue(
            conv_id, config, settings_patch, data,
            user_id=_request_user_id(),
            _start_task_for_conv=_start_task_for_conv,
            _persist_conv_messages=_persist_conv_messages,
            _set_conversation_settings=_scs,
            _notify_conv_changed=_notify_conv_changed,
            _audit_log=_al)
        if outcome.kind == 'error':
            return outcome.err_resp
        return jsonify(outcome.payload)
    except Exception as e:
        logger.error('[Continue] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


# ══════════════════════════════════════════════════════════
#  Tool-state sync endpoint
#  → moved to routes/chat_tool_state.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════
#  Server-side message queue endpoints
#  → moved to routes/chat_queue.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════


# _warm_resume_serviceable moved to routes/chat_helpers.py (pt_04686ac6 slice 1).
# Re-exported so ``from routes.chat import _warm_resume_serviceable`` keeps working.
from routes.chat_helpers import _warm_resume_serviceable  # noqa: E402,F401


@chat_bp.route('/api/chat/stream/<task_id>', methods=['GET'])
async def chat_stream(task_id):
    import asyncio

    # ── Per-principal concurrent-SSE cap (backpressure) ──
    # A single process serves every LIVE SSE stream as a long-lived
    # connection; with no per-principal ceiling one client/IP can open
    # unbounded streams and exhaust the process. We acquire a slot keyed by
    # the request principal (user_id → key_id → client IP) ONLY for the live
    # streaming path (generate_with_disconnect_log) — the transient
    # DB-snapshot / cold-replay generators below finish in microseconds and
    # are the reconnect/replay safety valve, so they are intentionally NOT
    # counted against the cap. The live path acquires just before opening the
    # stream and releases in its finally, so a dropped/aborted/errored stream
    # can never leak a slot.
    from lib.agent_core.principal import principal_key
    from lib.agent_core.sse_limit import limiter as _sse_limiter
    _sse_principal = principal_key(current_auth())

    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        # Cold-path (task not in memory): persisted-event replay OR
        # DB snapshot OR not-found. Extracted to
        # lib.chat_dispatch.build_cold_replay_response (pt_04686ac6
        # slice 6). Byte-identical response shape to the pre-slice
        # inline block.
        from lib.chat_dispatch import build_cold_replay_response
        return await build_cold_replay_response(
            task_id, request.headers.get('Last-Event-ID', ''))

    # ★ SSE reader dedup: supersede any previous SSE reader for this task.
    #   When a client reconnects (proxy timeout, page switch), the old reader
    #   should detect it's been replaced and exit, freeing the thread.
    _sse_gen = task.get('_sse_gen_id', 0) + 1
    task['_sse_gen_id'] = _sse_gen

    # ★ Item 6: Last-Event-ID reconnection — if the client provides a cursor,
    #   skip the full state snapshot and resume from that event index.
    #   The Last-Event-ID parse + _warm_resume_serviceable verdict now
    #   live in ``plan_warm_resume`` (pt_04686ac6 slice 7). Capture the
    #   raw header here so the closure below can hand it to the planner.
    _last_event_id_hdr = request.headers.get('Last-Event-ID', '')

    _stream_start = time.time()
    _events_sent = 0
    # ★ Set when the live-tick driver closes this reader because a NEWER
    #   reader for the same task took over. A deliberate handover, NOT a
    #   client drop — see the teardown logger in
    #   generate_with_disconnect_log().
    _superseded = False
    #
    # ⚠ FORENSIC NOTE (2026-07-31, epic pt_8a2f741ee4634cdc): SSE requests do
    #   NOT appear in logs/access.log while they are alive. That log is written
    #   by ``hypercorn.access``, which emits on response COMPLETION, and an SSE
    #   response is held open for up to _MAX_SSE_DURATION (7200s). Measured: the
    #   substring 'chat/stream' appears ZERO times in every access log ever
    #   rotated, even in windows where this handler demonstrably ran and logged
    #   its own disconnects. So "access.log shows 0 stream hits" means ONLY
    #   "no stream has closed and been logged" — it is NOT evidence that the
    #   client failed to send the request. Use THIS module's own log lines
    #   (and lib/chat_dispatch.py's) as the source of truth for SSE liveness.
    # pt_04686ac6 slice 8: the two closures below (``_task_terminal``,
    # ``_apply_autopilot_baton``) moved to lib.chat_dispatch as
    # module-level helpers so warm-resume / fresh-terminal / LIVE-tick
    # planners can all share ONE definition. Bind local names here so
    # every call-site below keeps the pre-slice call shape.
    from lib.chat_dispatch import (
        is_task_terminal as _is_task_terminal_lib,
        apply_autopilot_baton as _apply_autopilot_baton_lib,
    )
    def _task_terminal():
        return _is_task_terminal_lib(task)
    def _apply_autopilot_baton(evt):
        return _apply_autopilot_baton_lib(task, evt)

    async def generate():
        nonlocal _events_sent, _superseded
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'

        # Warm-path resume planning (pt_04686ac6 slice 7): parse the
        # Last-Event-ID cursor, verdict _warm_resume_serviceable, build
        # the leading resume state — all under events_lock inside
        # plan_warm_resume. Returns None when the client sent no cursor
        # OR the cursor is ahead of the buffer (trimmed / stale) → fall
        # through to build_fresh_state_snapshot.
        from lib.chat_dispatch import plan_warm_resume, build_connect_snapshot
        _warm_plan = plan_warm_resume(task, _last_event_id_hdr, task_id[:8])

        if _warm_plan is not None:
            resume_from = _warm_plan.resume_from
            missed_evts = _warm_plan.replay_events
            cursor = resume_from
            # ★ VU carrier: resume_state is None by design (chat_dispatch
            #   plan_warm_resume) — yield nothing before the replay; the
            #   client's intact VU bubble only needs the missed frames.
            if _warm_plan.resume_state is not None:
                _resume_state_payload = json.dumps(_warm_plan.resume_state, ensure_ascii=False)
                yield f'data: {_resume_state_payload}\n\n'
                _events_sent += 1
            for idx, ev in enumerate(missed_evts):
                eid = resume_from + idx
                yield f'id: {eid}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n'
                _events_sent += 1
                if ev.get('type') == 'done':
                    return
            cursor = resume_from + len(missed_evts)
            if _task_terminal() and not missed_evts:
                # ★ VU carrier: close with the MINIMAL carrier done (no
                #   agent meta), mirroring the live-tick carrier branch.
                if task.get('_vu_subtask'):
                    from lib.chat_dispatch import build_carrier_terminal_done
                    yield (f'id: {cursor}\n'
                           f'data: {json.dumps(build_carrier_terminal_done(task), ensure_ascii=False)}\n\n')
                    return
                late_done = build_event(EventType.DONE)
                late_meta = _extract_task_meta(task)
                late_done.update(late_meta)
                if task['error']:
                    late_done['error'] = task['error']
                _apply_autopilot_baton(late_done)
                yield f'id: {cursor}\ndata: {json.dumps(late_done, ensure_ascii=False)}\n\n'
                return
        else:
            # Fresh connection path: build the full-state snapshot via
            # the extracted helper. ``state`` is the event dict, ``meta``
            # is reused for the terminal-done synth below, ``cursor`` is
            # where the live-stream loop starts reading from.
            # ★ build_connect_snapshot is flavor-aware: a _vu_subtask
            #   carrier gets an autopilot_vu_start + replaySnapshot frame
            #   (never an agent state — the Agent-mislabel fix); every
            #   other task gets the classic build_fresh_state_snapshot.
            state, meta, cursor = build_connect_snapshot(task)
            # ★ State snapshot gets NO id: field — it's synthetic, not a real
            #   event from the events array. Only real events (deltas, phases,
            #   done) get id: fields. This prevents an id collision between
            #   the state snapshot and the first live event at the same cursor.
            # ★ Offload the multi-MB state encode via ``_dumps_yielding``
            #   (orjson-first) on the executor: plain json.dumps holds
            #   the GIL for the whole call so to_thread alone would not
            #   free the loop for accept()/other connections.
            _state_payload = await asyncio.to_thread(_dumps_yielding, state)
            yield f'data: {_state_payload}\n\n'
            # ★ COUNT IT. This frame carries the whole accumulated
            #   content/thinking/toolRounds — on a reconnect it is the single
            #   most valuable thing on the wire. It used to be yielded WITHOUT
            #   touching the counter (while the warm-resume path incremented
            #   for its equivalent frame), so a connection that had delivered a
            #   complete snapshot still reported "0 events sent" and was logged
            #   as a premature-disconnect FAILURE. That false signal is what
            #   epic pt_8a2f741ee4634cdc was filed on — "every SSE dies in
            #   0.1s with 0 events" — when the transport was in fact healthy
            #   (the same tasks later streamed for 600-3300s).
            _events_sent += 1

            if _task_terminal():
                done_evt = build_event(EventType.DONE)
                done_evt.update(meta)
                if task['error']:
                    done_evt['error'] = task['error']
                _apply_autopilot_baton(done_evt)
                yield f'id: {cursor}\ndata: {json.dumps(done_evt, ensure_ascii=False)}\n\n'
                return

        # LIVE-path main loop (pt_04686ac6 slice 8): every tick's
        # decision (drain / late-done / supersede / keepalive / sleep /
        # timeout) lives in ``next_live_tick``. This driver is a thin
        # dispatcher: pass current cursor / time / stream_start in,
        # yield the frames the verdict names, advance the cursor.
        from lib.chat_dispatch import next_live_tick
        _MAX_SSE_DURATION = 7200  # 2 hours
        last_t = time.time()
        while True:
            _now = time.time()
            _tick = next_live_tick(
                task=task, cursor=cursor, sse_gen=_sse_gen,
                stream_start=_stream_start, sse_max_duration=_MAX_SSE_DURATION,
                last_t=last_t, now=_now, task_id_short=task_id[:8],
            )
            if _tick.kind == 'sse_timeout':
                yield f'data: {json.dumps(_tick.timeout_evt, ensure_ascii=False)}\n\n'
                return
            if _tick.kind == 'events':
                for eid, ev in _tick.frames:
                    yield f'id: {eid}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n'
                    _events_sent += 1
                    last_t = time.time()
                    if ev.get('type') == 'done':
                        _done_fr = ev.get('finishReason', '?')
                        _done_err = ev.get('error')
                        _done_err_summary = (
                            _done_err.get('kind') if isinstance(_done_err, dict)
                            else (_done_err or 'none')
                        )
                        logger.info(
                            '[Chat] SSE stream %s finished normally — %d events sent in %.1fs '
                            'finishReason=%s error=%s',
                            task_id[:8], _events_sent, time.time() - _stream_start,
                            _done_fr, _done_err_summary)
                        return
                cursor = _tick.next_cursor
                continue
            if _tick.kind == 'late_done':
                yield (f'id: {_tick.next_cursor}\n'
                       f'data: {json.dumps(_tick.late_done_evt, ensure_ascii=False)}\n\n')
                return
            if _tick.kind == 'superseded':
                # ★ A NEWER READER for this task took over (reconnect / second
                #   tab). Closing this reader is the supersede design WORKING,
                #   not a failure — record it so the teardown logger does not
                #   report a deliberate handover as "client may lose data".
                _superseded = True
                return
            if _tick.kind == 'keepalive':
                yield ': keepalive\n\n'
                last_t = time.time()
                _sse_limiter.refresh(_sse_token)
                continue
            # 'sleep' — nothing to do this tick.
            await asyncio.sleep(0.05)

    async def generate_with_disconnect_log():
        """Wrap generate() to detect client disconnect (SSE premature close)."""
        done_sent = False
        try:
            async for chunk in generate():
                if '"type"' in chunk and ('"type": "done"' in chunk or '"type":"done"' in chunk):
                    done_sent = True
                yield chunk
        except (GeneratorExit, asyncio.CancelledError):
            logger.debug('[Chat] SSE stream closed by client (GeneratorExit)', exc_info=True)
        finally:
            # Release the per-principal SSE slot FIRST — a dropped / aborted /
            # errored stream must free its slot before anything else can raise.
            _sse_limiter.release(_sse_token)
            elapsed = time.time() - _stream_start
            content_len = len(task.get('content') or '')
            _fr = task.get('finishReason') or '?'
            _model = task.get('model') or '?'
            _provider = task.get('provider_id') or '?'
            _err = task.get('error')
            if not done_sent:
                # ★ SUPERSEDED — a newer reader for this task owns the stream
                #   now, so this reader closing is the design working. It used
                #   to fall into the zero-event branch below and be reported at
                #   WARNING with "Client may lose data if poll fallback fails!",
                #   which is both alarming and untrue: the successor delivers
                #   everything. Measured on 2026-07-31, 11 of the 12 "0 events
                #   sent in 0.1s" alarms were exactly this — the false evidence
                #   epic pt_8a2f741ee4634cdc was filed on.
                if _superseded:
                    logger.info('[Chat] SSE stream %s handed over to a newer reader '
                                '— %d events sent in %.1fs, task status=%s. '
                                'Not a disconnect: the successor reader owns this '
                                'stream and continues delivery.',
                                task_id[:8], _events_sent, elapsed,
                                task.get('status', '?'))
                # Severity-aware: zero-events = real problem (SSE opened
                # but nothing delivered); events>0 = normal client-side
                # tab-close / network-retry — client poll fallback will
                # pick up the rest.
                elif _events_sent == 0:
                    logger.warning('[Chat] SSE stream %s DISCONNECTED PREMATURELY — '
                                 '%d events sent in %.1fs, task status=%s, content=%dchars, '
                                 'finishReason=%s model=%s provider=%s error=%s. '
                                 'Client may lose data if poll fallback fails!',
                                 task_id[:8], _events_sent, elapsed,
                                 task.get('status', '?'), content_len,
                                 _fr, _model, _provider, _err or 'none')
                else:
                    logger.info('[Chat] SSE stream %s DISCONNECTED PREMATURELY — '
                               '%d events sent in %.1fs, task status=%s, content=%dchars, '
                               'finishReason=%s model=%s provider=%s error=%s. '
                               'Client may lose data if poll fallback fails!',
                               task_id[:8], _events_sent, elapsed,
                               task.get('status', '?'), content_len,
                               _fr, _model, _provider, _err or 'none')
            else:
                logger.info('[Chat] SSE stream %s closed after done — %d events, %.1fs, %dchars, '
                           'finishReason=%s model=%s provider=%s',
                           task_id[:8], _events_sent, elapsed, content_len,
                           _fr, _model, _provider)

    _sse_token = _sse_limiter.try_acquire(_sse_principal)
    if _sse_token is None:
        _active = _sse_limiter.active(_sse_principal)
        logger.warning('[Chat] SSE stream refused for principal=%s task=%s — '
                       'at concurrent-stream cap (%d active, cap=%d)',
                       _sse_principal, task_id[:8], _active, _sse_limiter.cap)
        resp = jsonify({
            'ok': False,
            'error': {'kind': 'rate_limited',
                      'detail': 'Too many concurrent streams for this '
                                'principal. Close an existing stream or '
                                'retry shortly.',
                      'retry_after_s': 5},
        })
        resp.headers['Retry-After'] = '5'
        return resp, 429

    return sse_response(generate_with_disconnect_log(), timeout_none=True)


# ── Slice 10: poll_abort cluster extracted ─────────────────────────
#
# The 4 handlers below (chat_abort_conv, chat_abort, chat_poll,
# chat_flow_trace) moved to routes/chat_poll_abort.py (pt_04686ac6
# slice 10). They still attach to api_v1_chat_bp — routes/__init__.py
# side-effect imports the new module so its @route decorators fire.
# Re-exported here so ``from routes.chat import chat_poll`` (external
# tests + code) keeps resolving.
from routes.chat_poll_abort import (  # noqa: E402,F401 (re-export)
    chat_abort_conv,
    chat_abort,
    chat_poll,
    chat_flow_trace,
)


# ══════════════════════════════════════════════════════════
#  Stdin / human-guidance response endpoints
#  → moved to routes/chat_human_io.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════
