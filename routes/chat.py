"""routes/chat.py — Chat start, streaming, polling, abort."""

import json
import threading
import time

from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_CHAT, get_db
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.request_parser import parse_body
from routes.api_v1.auth import current_auth, require_scope

from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

import re

from lib.database import db_execute_with_retry, get_thread_db, json_dumps_pg
from routes.common import DEFAULT_USER_ID, _invalidate_meta_cache

logger = get_logger(__name__)

chat_bp = Blueprint('chat', __name__)
# v1 blueprint for the JSON routes (the carve-out /api/chat/stream/<id> stays on chat_bp).
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402


def _extract_db_meta(row):
    """Extract metadata dict from a DB task_results row."""
    meta = {}
    if row['metadata']:
        try:
            meta = json.loads(row['metadata'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Chat] Failed to parse task metadata JSON (task_id=%s): %s', row['task_id'], e, exc_info=True)
    return meta


def _extract_task_meta(task):
    """Extract metadata fields from an in-memory task dict.

    MUST stay in sync with ``_extract_db_meta`` (DB-row equivalent) and
    with the ``meta`` dict built in ``manager.persist_task_result``.  Any
    field added here must also appear in:
      * persist_task_result ’s ``meta`` dict (so it lands in task_results)
      * the chat_poll DB-path field loop (so /api/chat/poll returns it)
      * the cold-replay synth-done in chat_stream (so Last-Event-ID
        replay after server restart returns the same shape)
    Asymmetry between these four paths historically caused "my apiRounds
    disappeared after I came back" / "modifiedFiles missing on reload".
    """
    meta = {}
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
    if task.get('_fallback_model'):
        meta['fallbackModel'] = task['_fallback_model']
    if task.get('_fallback_from'):
        meta['fallbackFrom'] = task['_fallback_from']
    return meta


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
        result = [{'id': t['id'], 'convId': t['convId'], 'status': t['status'],
                   'aborted': bool(t.get('aborted'))}
                  for t in tasks.values()]
    return jsonify(result)


@api_v1_chat_bp.route('/api/v1/chat/start', methods=['POST'], endpoint='ui_chat_start')
@require_scope('chat')
def chat_start():
    """Start a chat task. Body is ``{convId, config[, messages, agentBackend]}``.

    Default flow: load messages from the DB via ``build_api_messages_from_db``,
    then dispatch to the built-in orchestrator. External callers (SWE-bench,
    eval harnesses) may pass ``messages`` inline, which sets ``_inline_messages``
    so :func:`_sync_result_to_conversation` skips DB write-back. Selecting a
    non-builtin ``config.agentBackend`` (codex / claude_code / …) routes to
    :func:`_start_external_backend` instead.

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

    # ★ Abort stale running tasks for this conversation before starting a new one
    from lib.tasks_pkg import abort_running_tasks_for_conv
    abort_running_tasks_for_conv(conv_id)

    # ── Backend dispatch: external backends get their own flow ──
    backend_name = cfg.get('agentBackend', 'builtin')
    if backend_name and backend_name != 'builtin':
        return _start_external_backend(data, messages, backend_name)

    # ── Default: built-in Tofu backend ──
    task = create_task(conv_id, messages, cfg)
    # Tag tasks that were started with inline messages (no DB-backed
    # conversation row). These tasks skip _sync_result_to_conversation()
    # entirely — external callers read results from task_results directly.
    if inline_messages:
        task['_inline_messages'] = True
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

# Max time (seconds) for the synchronous auto-translate during /api/chat/send.
# Must stay comfortably below the frontend's safety abort timer
# (`_sendTimeout` in static/js/main.js — currently 90 s) so the user sees a
# clean fallback ("sending original text") rather than a generic AbortError.
_TRANSLATE_SEND_TIMEOUT = 45


# ══════════════════════════════════════════════════════════
#  Send-path translate status (per-conv)
#
#  The atomic /api/chat/send handler translates synchronously, blocking the
#  HTTP response until the translate either succeeds or hits
#  _TRANSLATE_SEND_TIMEOUT. If the backend is retrying (429, empty output),
#  the user sees only the frontend "Translating…" bubble with no progress
#  clue. We expose a tiny poll endpoint — /api/chat/send-translate-status —
#  so the frontend can display the current retry reason underneath the
#  bubble while the send call is in flight.
# ══════════════════════════════════════════════════════════
_send_translate_status = {}           # conv_id -> {statusMessage, statusKind, updatedAt}
_send_translate_status_lock = threading.Lock()

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
_send_abort_marker = {}               # conv_id -> abort_wall_clock_seconds
_send_abort_marker_lock = threading.Lock()


def _mark_conv_aborted(conv_id):
    """Record that this conv was aborted at wall clock ``time.time()``."""
    if not conv_id:
        return
    with _send_abort_marker_lock:
        _send_abort_marker[conv_id] = time.time()


def _was_aborted_after(conv_id, since_ts):
    """Return True if /api/chat/abort-conv ran for this conv after ``since_ts``."""
    if not conv_id or since_ts is None:
        return False
    with _send_abort_marker_lock:
        ts = _send_abort_marker.get(conv_id)
    return ts is not None and ts >= since_ts


def _set_send_translate_status(conv_id, event):
    """Record a status event for an in-flight send-path translate.

    Called from the _translate_one_chunk status callback. ``event`` is a
    dict carrying ``kind``, ``attempt``, ``elapsed``, ``detail`` — see
    ``routes/translate._format_status_message``.
    """
    if not conv_id:
        return
    try:
        from routes.translate import _format_status_message
        msg = _format_status_message(event)
    except Exception as e:
        logger.debug('[Send] _format_status_message failed: %s', e)
        msg = event.get('kind', '')
    with _send_translate_status_lock:
        _send_translate_status[conv_id] = {
            'statusMessage': msg,
            'statusKind': event.get('kind', ''),
            'updatedAt': time.time(),
        }


def _clear_send_translate_status(conv_id):
    if not conv_id:
        return
    with _send_translate_status_lock:
        _send_translate_status.pop(conv_id, None)


@api_v1_chat_bp.route('/api/v1/chat/translate-status/<conv_id>', methods=['GET'], endpoint='ui_chat_send_translate_status')
@require_scope('chat')
def chat_send_translate_status(conv_id):
    """Return the current send-path translate retry status for a conv.

    Returns ``{statusMessage, statusKind, updatedAt}`` or ``{}`` if no
    translate is currently in flight (or hasn't yet hit its first retry).
    """
    with _send_translate_status_lock:
        st = _send_translate_status.get(conv_id)
    return jsonify(st or {})


def _auto_translate_user(text, config, conv_id=None):
    """Translate Chinese user text to English if autoTranslate is on.

    Capped at ``_TRANSLATE_SEND_TIMEOUT`` seconds to prevent the synchronous
    HTTP handler from blocking long enough to trigger the frontend's abort.

    When ``conv_id`` is provided, transient retry statuses are published
    to ``_send_translate_status[conv_id]`` so the frontend can poll for
    them and surface them below the "Translating…" bubble.

    Returns:
        (translated_text, original_text_or_None, model_or_None)
    """
    auto_translate = config.get('autoTranslate', False)
    if not auto_translate or not text:
        return text, None, None

    has_chinese = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    if not has_chinese:
        return text, None, None

    import concurrent.futures

    def _status_cb(event):
        """Forward chunk-level status events into the per-conv dict."""
        _set_send_translate_status(conv_id, event)

    def _do_translate():
        from routes.translate import (
            _build_translate_prompt,
            _translate_one_chunk,
            _extract_notranslate_blocks,
            _reattach_notranslate_blocks,
            _strip_notranslate_tags,
        )
        system_prompt = _build_translate_prompt('English', 'Chinese')
        # ── Extract <notranslate>/<nt> blocks so the LLM doesn't see the tags ──
        # Without this, the tags leak into the translated English `content`
        # (and stay visible in the "译文" display).
        inner_text, nt_blocks = _extract_notranslate_blocks(text)
        if nt_blocks and not inner_text.strip():
            # Whole message was inside <notranslate> — nothing to translate.
            return _strip_notranslate_tags(text), {'model': 'skipped',
                                                   '_dispatch': {'model': 'skipped'}}
        translate_target = inner_text if nt_blocks else text
        # Tighter inner deadline than the outer wait — leaves a small
        # margin for status publication and pool teardown so the HTTP
        # response arrives well before the frontend safety abort.
        translated, _u = _translate_one_chunk(
            translate_target, system_prompt, chunk_label=':send',
            source='Chinese', target='English',
            status_cb=_status_cb if conv_id else None,
            overall_deadline=max(5.0, _TRANSLATE_SEND_TIMEOUT - 5),
        )
        if nt_blocks and translated:
            translated = _reattach_notranslate_blocks(translated, nt_blocks)
        return translated, _u

    # Build the executor manually — a `with` block calls shutdown(wait=True)
    # on exit, which would block the HTTP request until the worker actually
    # finishes (up to _translate_one_chunk's internal 10-min retry deadline)
    # and defeat the whole point of the timeout. We tear it down with
    # wait=False / cancel_futures=True so the request returns as soon as the
    # timeout hits, even if the worker thread is mid-LLM-call.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = None
    timed_out = False
    started_at = time.time()
    # Heartbeat: if the inner translate hasn't surfaced its own status yet,
    # publish a "still translating" event every few seconds so the polling
    # frontend doesn't show a static "Translating…" with no clue.
    _heartbeat_stop = threading.Event()

    def _heartbeat():
        while not _heartbeat_stop.wait(4.0):
            try:
                _set_send_translate_status(conv_id, {
                    'kind': 'in_progress',
                    'attempt': 0,
                    'elapsed': time.time() - started_at,
                    'detail': '',
                })
            except Exception as e:
                logger.debug('[Send] heartbeat status publish failed conv=%s: %s',
                             (conv_id or '?')[:8], e)

    _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    if conv_id:
        _hb_thread.start()

    try:
        future = pool.submit(_do_translate)
        try:
            result, _usage = future.result(timeout=_TRANSLATE_SEND_TIMEOUT)
        except concurrent.futures.TimeoutError:
            timed_out = True
            elapsed = time.time() - started_at
            logger.warning('[Send] Auto-translate timed out after %.1fs, '
                           'sending original text (conv=%s, %d chars)',
                           elapsed, (conv_id or '?')[:8], len(text))
            # Surface the timeout to the frontend BEFORE we clear the
            # status dict in `finally` — the poll loop will pick it up
            # on the very next tick and the user sees a concrete reason.
            _set_send_translate_status(conv_id, {
                'kind': 'timed_out',
                'attempt': 1,
                'elapsed': elapsed,
                'detail': f'no result after {_TRANSLATE_SEND_TIMEOUT}s',
            })
            # Do NOT block on shutdown — the worker may still be mid-LLM
            # call. cancel_futures requires Python 3.9+; we already require it.
            pool.shutdown(wait=False, cancel_futures=True)
            return text, None, None
        if result and result.strip():
            _model = None
            if isinstance(_usage, dict):
                _disp = _usage.get('_dispatch', {})
                _model = _disp.get('model', _usage.get('model'))
            # Strip any <notranslate>/<nt> tags that the LLM may have leaked
            # through (defense in depth — _reattach above already handles the
            # common case, but the LLM sometimes echoes the tags literally).
            from routes.translate import _strip_notranslate_tags
            clean = _strip_notranslate_tags(result.strip()).strip()
            logger.info('[Send] Auto-translated user message: %d→%d chars model=%s nt_stripped=%s',
                        len(text), len(clean), _model, clean != result.strip())
            return clean, text, _model
    except Exception as e:
        logger.warning('[Send] Auto-translate failed: %s', e, exc_info=True)
    finally:
        _heartbeat_stop.set()
        # Tear down the pool. When we timed out we already shut it down
        # non-blocking above. On the success / generic-error paths the
        # future is finished, so a regular shutdown(wait=False) returns
        # immediately and the worker thread is collected by the GC.
        if not timed_out:
            try:
                pool.shutdown(wait=False)
            except Exception as e:
                logger.debug('[Send] translate pool shutdown failed: %s', e)
        # Always clear the per-conv status on exit — no stale retry hint
        # should leak into a subsequent poll after the send returns.
        _clear_send_translate_status(conv_id)

    return text, None, None


def _resolve_conv_refs(conv_refs):
    """Resolve a list of conversation references into formatted text.

    Each ref is ``{id, title}``.  Loads the conversation from DB and formats
    it using ``lib/conv_ref.get_conversation`` (which handles tool details,
    PDFs, truncation, etc.).

    Args:
        conv_refs: List of dicts with ``id`` and ``title`` keys.

    Returns:
        List of ``{id, title, text}`` dicts, one per resolved ref.
    """
    if not conv_refs:
        return []
    from lib.conv_ref import get_conversation
    results = []
    for cr in conv_refs:
        ref_id = cr.get('id', '')
        ref_title = cr.get('title', '')
        if not ref_id:
            continue
        try:
            text = get_conversation(
                conversation_id=ref_id,
                include_tool_details=False,
            )
            results.append({'id': ref_id, 'title': ref_title, 'text': text})
        except Exception as e:
            logger.warning('[Send] Failed to resolve conv ref %s: %s', ref_id[:12], e)
            results.append({'id': ref_id, 'title': ref_title,
                            'text': f'[Error loading conversation: {e}]'})
    logger.info('[Send] Resolved %d conv refs', len(results))
    return results


def _build_user_msg_from_payload(payload, config, conv_id=None):
    """Build a user message dict from frontend payload + optional auto-translate.

    Args:
        payload: dict with text, images, pdfTexts, replyQuotes, convRefs, convRefTexts, timestamp
        config: task config dict (reads autoTranslate)
        conv_id: optional — when provided, transient translate retry
            statuses are exposed via /api/chat/send-translate-status/<conv_id>
            so the frontend can display retry reasons under the "Translating…"
            bubble.

    Returns:
        user_msg dict ready to append to conv.messages
    """
    text = payload.get('text', '')
    timestamp = payload.get('timestamp') or int(time.time() * 1000)

    translated_text, original_text, translate_model = _auto_translate_user(text, config, conv_id=conv_id)

    user_msg = {
        'role': 'user',
        'content': translated_text,
        'timestamp': timestamp,
    }
    if original_text:
        user_msg['originalContent'] = original_text
        user_msg['_translateDone'] = True
        if translate_model:
            user_msg['_translateModel'] = translate_model
    if payload.get('images'):
        user_msg['images'] = payload['images']
    if payload.get('pdfTexts'):
        user_msg['pdfTexts'] = payload['pdfTexts']
    if payload.get('replyQuotes'):
        user_msg['replyQuotes'] = payload['replyQuotes']
    if payload.get('convRefs'):
        user_msg['convRefs'] = payload['convRefs']
    # Resolve convRefTexts server-side from convRefs if not already provided
    conv_ref_texts = payload.get('convRefTexts')
    if not conv_ref_texts and payload.get('convRefs'):
        conv_ref_texts = _resolve_conv_refs(payload['convRefs'])
    if conv_ref_texts:
        user_msg['convRefTexts'] = conv_ref_texts

    return user_msg


def _append_user_msg_idempotent(messages, user_msg):
    """Append ``user_msg`` to ``messages`` unless the tail is already it.

    Root-cause guard for the duplicate-user-message bug: the frontend pushes
    an optimistic user message into the in-memory conversation, and a racing
    sync (the "rescue local-only conv" PUT from loadConversationsFromServer,
    another browser tab, or a /chat/send network retry) can plant that
    optimistic copy as the conversation's last row BEFORE this handler runs.
    A blind ``messages.append`` would then produce two user rows for one send.

    The optimistic copy and the server-built ``user_msg`` share the SAME
    ``timestamp`` (the frontend's payload timestamp flows through
    ``_build_user_msg_from_payload``), so we treat a trailing user message
    with a matching timestamp as "the same logical message" and reconcile it
    in place instead of appending a duplicate.  The server copy wins because
    it carries the authoritative translation fields (``content`` =
    translated, ``originalContent``, ``_translateDone``, ``_translateModel``).

    Args:
        messages: The conversation message list (mutated in place).
        user_msg: The freshly-built server-side user message dict.

    Returns:
        True if a new row was appended; False if an existing tail row was
        reconciled (duplicate prevented).
    """
    if messages:
        tail = messages[-1]
        if (isinstance(tail, dict)
                and tail.get('role') == 'user'
                and tail.get('timestamp') == user_msg.get('timestamp')):
            # Same logical message already present (optimistic copy planted by
            # a racing sync). Overwrite it with the authoritative server copy,
            # preserving any stable _msgId already assigned to the tail.
            preserved_id = tail.get('_msgId')
            tail.clear()
            tail.update(user_msg)
            if preserved_id and '_msgId' not in tail:
                tail['_msgId'] = preserved_id
            logger.info('[Send] Reconciled duplicate optimistic user msg in place '
                        '(ts=%s) — prevented duplicate row', user_msg.get('timestamp'))
            return False
    messages.append(user_msg)
    return True


def _load_or_create_conv(db, conv_id, config, payload):
    """Load existing conversation messages or create a new one.

    Returns:
        (messages_list, is_new, title) or raises.
    """
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()

    if row:
        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Send] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
            messages = []
        return messages, False, row['title']

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

    db_execute_with_retry(db, '''
        INSERT INTO conversations (id, user_id, title, messages, created_at, updated_at, settings, msg_count, search_text)
        VALUES (?, ?, ?, '[]', ?, ?, ?, 0, '')
    ''', (conv_id, DEFAULT_USER_ID, title, now_ms, now_ms,
          json.dumps(settings, ensure_ascii=False)))

    return [], True, title


def _persist_conv_messages(db, conv_id, messages, title, settings_patch=None):
    """Write messages + metadata to the conversation row.

    Backfills stable per-message ``_msgId`` UUIDs before writing.  Every
    code path that mutates ``messages`` and persists the array (send,
    regenerate, edit, continue, chat_continue) goes through this helper,
    so this is the single point of truth for id assignment on the chat
    write side \u2014 mirroring ``_assign_message_ids`` calls in
    ``manager.py`` for the partial/result sync paths.  Without this,
    newly appended messages on those flows would have no ``_msgId``,
    forcing PATCH /messages/by-id to silently fall back to index lookup.
    """
    # Lazy import to avoid the routes \u2192 lib.tasks_pkg.manager cycle.
    from lib.tasks_pkg.manager import _assign_message_ids
    _assign_message_ids(messages)
    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)

    from routes.conversations import build_search_text
    search_text = build_search_text(messages)

    # Build settings update
    settings_update = {}
    if settings_patch:
        settings_update.update(settings_patch)

    # Always inject lastMsgRole/lastMsgTimestamp
    if messages:
        last = messages[-1]
        settings_update['lastMsgRole'] = last.get('role')
        settings_update['lastMsgTimestamp'] = last.get('timestamp')

    # Merge with existing settings AND preserve original created_at
    existing = db.execute(
        'SELECT settings, created_at FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if existing:
        try:
            settings = json.loads(existing['settings'] or '{}')
        except (json.JSONDecodeError, TypeError) as _e_audit:
            logger.debug('[chat] _persist_conv_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
            settings = {}
        settings.update(settings_update)
        # ★ Preserve original created_at — INSERT OR REPLACE would overwrite
        #   it with now_ms, causing all conversations to lose their real
        #   creation timestamp on every message send/regenerate/edit.
        created_at = existing['created_at'] or now_ms
    else:
        settings = settings_update
        created_at = now_ms

    settings_json = json.dumps(settings, ensure_ascii=False)

    db_execute_with_retry(db, '''
        INSERT OR REPLACE INTO conversations (id, user_id, title, messages, created_at, updated_at,
                                   settings, msg_count, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (conv_id, DEFAULT_USER_ID, title, messages_json, created_at, now_ms,
          settings_json, len(messages), search_text))
    # Update FTS5 index
    if search_text:
        try:
            db.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (search_text, conv_id)
            )
            db.commit()
        except Exception as e:
            logger.debug('[_persist_conv_messages] FTS update failed (non-fatal): %s', e)


def _start_task_for_conv(conv_id, config, data=None):
    """Build API messages from DB and start a task. Returns (taskId, error_response).

    Automatically routes to endpoint mode (planner → worker → critic loop)
    when ``config['endpointMode']`` is truthy, so callers (chat_send,
    chat_regenerate, etc.) don't need separate routing logic.

    ★ CRITICAL: Before starting a new task, all existing running tasks for
    this conversation are auto-aborted. This prevents the "stale task
    overwrites regeneration" bug where an old task's _sync_result_to_conversation
    races with the new task and corrupts the conversation DB.
    """
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    from lib.tasks_pkg import abort_running_tasks_for_conv

    # ``excludeLast`` is honored so /api/chat/continue can rebuild messages
    # without the assistant message that is about to be regenerated.
    _exclude_last = bool(config.get('excludeLast', False))
    api_messages = build_api_messages_from_db(conv_id, config, exclude_last=_exclude_last)
    if api_messages is None:
        return None, (jsonify({'error': 'Conversation not found after save'}), 500)
    if not api_messages:
        return None, (jsonify({'error': 'No messages to process'}), 400)

    # ★ CRITICAL: abort any stale running tasks for this conversation BEFORE
    #   creating the new one. Without this, the old task's background thread
    #   may still be running (abort is cooperative) and its persist/sync
    #   writes will overwrite the new task's content in the DB.
    _aborted_count = abort_running_tasks_for_conv(conv_id)
    if _aborted_count:
        logger.info('[Chat] conv=%s Auto-aborted %d stale task(s) before new task',
                    conv_id[:8], _aborted_count)

    cleanup_old_tasks()

    # External backend support
    backend_name = config.get('agentBackend', 'builtin')
    if backend_name and backend_name != 'builtin':
        # Reuse existing external backend flow
        full_data = {'convId': conv_id, 'config': config}
        if data:
            full_data.update(data)
        return None, _start_external_backend(full_data, api_messages, backend_name)

    task = create_task(conv_id, api_messages, config)
    task_id = task['id']
    _cfg_model = config.get('model', '?')

    # ★ Endpoint mode: route to the autonomous planner → worker → critic loop
    is_endpoint = config.get('endpointMode', False)

    # ★ Autopilot is mutually exclusive with endpoint mode — both share the
    #   same "model stopped → loop again" boundary, so running them together
    #   would produce a double-loop with confusing semantics.  Endpoint
    #   wins; the autopilot flag is silently dropped.
    if is_endpoint and config.get('autopilot'):
        logger.warning('[Chat] conv=%s autopilot=True dropped — '
                       'endpointMode=True takes precedence',
                       conv_id[:8])
        config = dict(config)
        config['autopilot'] = False
        task['config'] = config

    if is_endpoint:
        from lib.tasks_pkg.endpoint import run_endpoint_task
        task['endpoint_mode'] = True
        task['_endpoint_phase'] = 'planning'
        task['_endpoint_iteration'] = 0
        logger.info('[Chat] Starting ENDPOINT task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            threading.Thread(target=run_endpoint_task, args=(task,), daemon=True).start()
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start endpoint thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start endpoint task thread.',
                model=config.get('model', ''),
                context='endpoint-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return None, (jsonify({'error': 'Failed to start task'}), 500)
    else:
        logger.info('[Chat] Starting task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            from lib.tasks_pkg import spawn_task
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start task thread.',
                model=config.get('model', ''),
                context='task-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return None, (jsonify({'error': 'Failed to start task'}), 500)

    return task_id, None


@api_v1_chat_bp.route('/api/v1/chat/send', methods=['POST'], endpoint='ui_chat_send')
@require_scope('chat')
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

        # 2a. If the user clicked Stop while we were inside the auto-
        #     translate call, drop this message entirely — do NOT persist,
        #     enqueue, or dispatch. This prevents the 'translation finishes
        #     after abort → enqueue → fires after regen completes' double-
        #     send bug.
        if _was_aborted_after(conv_id, _send_started_at):
            logger.info('[Send] conv=%s ⚠️ Aborted during translate — dropping message '
                        '(translated=%s)',
                        conv_id[:8], bool(user_msg.get('originalContent')))
            return jsonify({
                'aborted': True,
                'convId': conv_id,
            })

        # 3. Compute title for first user message
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) == 0 and text:
            title_text = re.sub(r'</?(?:notranslate|nt)>', '', text, flags=re.IGNORECASE)
            title = title_text[:60] + ('...' if len(title_text) > 60 else '')

        logger.info('[Send] conv=%s msgs=%d title=%.50s isNew=%s translated=%s',
                    conv_id[:8], len(messages), title, is_new,
                    bool(user_msg.get('originalContent')))

        # ★ 3a. If the frontend reports a recently-aborted task, mark it
        #   as aborted NOW — this handles the race where the user clicks
        #   Stop and immediately sends a new message, and the fire-and-
        #   forget abort fetch hasn't arrived yet.
        abort_task_id = data.get('abortTaskId')
        if abort_task_id:
            with tasks_lock:
                abort_target = tasks.get(abort_task_id)
                if (abort_target
                        and not abort_target.get('aborted')
                        and abort_target.get('convId') == conv_id):
                    abort_target['aborted'] = True
                    abort_target['_abort_timestamp'] = time.time()
                    abort_target['_abort_reason'] = 'superseded_by_send'
                    logger.info('[Send] conv=%s ⚠️ Abort-on-send: task %s marked aborted '
                                '(frontend reported recently stopped task)',
                                conv_id[:8], abort_task_id[:8])

        # ★ 3b. Check if a task is already running for this conversation.
        #   If so, enqueue instead of starting — the backend dispatches
        #   automatically when the current task finishes.
        #   ★ CRITICAL: exclude aborted tasks — when the user clicks Stop
        #   and immediately sends a new message, the old task may still
        #   have status='running' (abort is cooperative) but should NOT
        #   cause the new message to be enqueued.
        has_running_task = False
        with tasks_lock:
            for t in tasks.values():
                if (t.get('convId') == conv_id
                        and t.get('status') == 'running'
                        and not t.get('aborted')):
                    has_running_task = True
                    break

        if has_running_task:
            from lib.message_queue import enqueue_message
            # ★ Enqueue for later dispatch.  The user message is NOT
            # persisted to the conversation DB — it only lives in the
            # queue.  This prevents it from appearing in chatInner
            # during streaming or disappearing on refresh.
            # Store the pre-built user_msg so dispatch_next_queued
            # can append it without re-translating.
            queue_payload = dict(payload)
            queue_payload['_user_msg'] = user_msg
            queue_result = enqueue_message(conv_id, queue_payload, config)
            logger.info('[Send] conv=%s ➡ QUEUED (active task running) queueId=%s position=%d',
                        conv_id[:8], queue_result['queueId'][:8], queue_result['position'])

            # Persist title update for new conversations (but NOT the user message)
            if is_new:
                _persist_conv_messages(db, conv_id, messages, title, settings_patch)

            _invalidate_meta_cache()

            return jsonify({
                'queued': True,
                'queueId': queue_result['queueId'],
                'position': queue_result['position'],
                'convId': conv_id,
                'title': title,
                'userMessage': user_msg,
                'isNew': is_new,
                'msgCount': len(messages),  # excludes the queued user msg
            })

        # 4. Append user message and persist (only for immediate start).
        #    Idempotent: if a racing sync already planted the optimistic copy
        #    as the tail (matching timestamp), reconcile in place instead of
        #    appending a duplicate. This is the root-cause guard — the
        #    frontend _sendInFlight flag merely avoids triggering the race.
        _append_user_msg_idempotent(messages, user_msg)
        _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # 5. Start task (no active task — send immediately)
        task_id, err_resp = _start_task_for_conv(conv_id, config, data)
        if err_resp is not None:
            # External backend returns a full Response directly
            if isinstance(err_resp, tuple):
                return err_resp
            return err_resp  # direct Response from _start_external_backend

        # 6. Update activeTaskId in settings
        try:
            _persist_conv_messages(db, conv_id, messages, title,
                                   {'activeTaskId': task_id})
        except Exception as e:
            logger.warning('[Send] Failed to update activeTaskId: %s', e)

        _invalidate_meta_cache()

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

        # External backend support
        backend_name = cfg.get('agentBackend', 'builtin')
        if backend_name and backend_name != 'builtin':
            full_data = {'convId': conv_id, 'config': cfg}
            return _start_external_backend(full_data, api_messages, backend_name)

        task = create_task(conv_id, api_messages, cfg)
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

        # 3. Auto-translate if needed
        text = user_msg.get('content', '')
        auto_translate = config.get('autoTranslate', False)
        if auto_translate and text:
            has_chinese = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
            # If the message already has originalContent and _translateDone, skip re-translation
            # (user didn't edit the text, just regenerating)
            already_translated = (user_msg.get('originalContent')
                                  and user_msg.get('_translateDone')
                                  and edited_content is None)
            if has_chinese and not already_translated:
                translated, original, model = _auto_translate_user(text, config, conv_id=conv_id)
                if original:
                    user_msg['content'] = translated
                    user_msg['originalContent'] = original
                    user_msg['_translateDone'] = True
                    if model:
                        user_msg['_translateModel'] = model

        # 4. Update title if this is the only user message
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) == 1 and text:
            original_text = user_msg.get('originalContent') or text
            title_text = re.sub(r'</?(?:notranslate|nt)>', '', original_text, flags=re.IGNORECASE)
            title = title_text[:60] + ('...' if len(title_text) > 60 else '')

        # 5. Persist truncated messages to DB
        _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # 5a. Defensive: clear any messages still sitting in the server-side
        #     queue for this conv. If a previous /api/chat/send was aborted
        #     mid-translate but its enqueue still landed (or a different race
        #     left the queue non-empty), regenerating should start clean —
        #     otherwise the queue would auto-dispatch a phantom turn after
        #     this regen completes.
        try:
            from lib.message_queue import clear_queue
            _cleared = clear_queue(conv_id)
            if _cleared:
                logger.info('[Regen] conv=%s cleared %d stale queued message(s) before regen',
                            conv_id[:8], _cleared)
        except Exception as e:
            logger.warning('[Regen] Failed to clear queue for conv=%s: %s', conv_id[:8], e)

        logger.info('[Regen] conv=%s truncated to idx=%d msgs=%d edited=%s title=%.50s',
                    conv_id[:8], truncate_to, len(messages),
                    edited_content is not None, title)

        # 6. Start task
        task_id, err_resp = _start_task_for_conv(conv_id, config, data)
        if err_resp is not None:
            if isinstance(err_resp, tuple):
                return err_resp
            return err_resp

        # 7. Update activeTaskId
        try:
            _persist_conv_messages(db, conv_id, messages, title,
                                   {'activeTaskId': task_id})
        except Exception as e:
            logger.warning('[Regen] Failed to update activeTaskId: %s', e)

        _invalidate_meta_cache()

        return jsonify({
            'taskId': task_id,
            'convId': conv_id,
            'title': title,
            'msgCount': len(messages),
            'userMessage': user_msg if edited_content is not None else None,
        })

    except Exception as e:
        logger.error('[Regen] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


# ══════════════════════════════════════════════════════════
#  Continue: checkpoint-based resumption of an assistant turn
# ══════════════════════════════════════════════════════════

def _build_tool_history_round(batch):
    """Server-side port of ``_buildToolHistoryRound()`` (static/js/main.js).

    Takes a batch of raw ``toolRounds`` entries (all from the same LLM round)
    and converts them into the ``toolHistory[i]`` shape consumed by
    ``lib/tasks_pkg/message_builder.inject_tool_history``.
    """
    round_out: dict = {
        'assistantContent': '',
        'toolCalls': [],
        'toolResults': [],
    }
    for r in batch:
        if not round_out['assistantContent'] and r.get('assistantContent'):
            round_out['assistantContent'] = r.get('assistantContent')
        if not round_out.get('thinking') and r.get('thinking'):
            round_out['thinking'] = r.get('thinking')
        if not round_out.get('thinkingSignature') and r.get('thinkingSignature'):
            round_out['thinkingSignature'] = r.get('thinkingSignature')
        tc = {
            'id': r.get('toolCallId'),
            'name': r.get('toolName'),
            'arguments': r.get('toolArgs') or '{}',
        }
        if r.get('extraContent'):
            tc['extraContent'] = r.get('extraContent')
        round_out['toolCalls'].append(tc)
        round_out['toolResults'].append({
            'tool_call_id': r.get('toolCallId'),
            'content': r.get('toolContent') or '',
        })
    return round_out


def _scan_continue_checkpoint(assistant_msg):
    """Scan the last assistant message's ``toolRounds`` for the latest recoverable
    checkpoint.  Mirrors ``continueAssistant()`` (static/js/main.js:2214-2410).

    Returns:
        dict with keys:
          kept_rounds (list), discarded_rounds (int),
          tool_history (list), preserved_content (str),
          preserved_thinking_chars (int),
          discarded_content (int), discarded_thinking (int),
          original_content_len (int), original_thinking_len (int)
        OR ``None`` if no recoverable checkpoint (caller falls back to
        full regeneration / pop-and-resend).
    """
    all_rounds = assistant_msg.get('toolRounds') or []
    if not all_rounds:
        return None
    has_tool_call_ids = any(r.get('toolCallId') for r in all_rounds)
    if not has_tool_call_ids:
        return None

    has_llm_round = any(r.get('llmRound') is not None for r in all_rounds)
    batches: dict = {}
    batch_key = 0
    last_complete_idx = -1

    for i, r in enumerate(all_rounds):
        if not r.get('toolCallId'):
            continue
        if r.get('status') != 'done':
            break
        # Attempt to reconstruct toolContent from results metadata if missing
        # (parity with the JS scan — happens after DB round-trip when backend
        # checkpoint was written before toolContent was available).
        if r.get('toolContent') is None:
            results = r.get('results') or []
            reconstructed = ''
            if results:
                parts = []
                for res in results:
                    if not isinstance(res, dict):
                        continue
                    parts.append(res.get('snippet') or res.get('title') or res.get('content') or '')
                reconstructed = '\n'.join(p for p in parts if p)
            if not reconstructed:
                break
            r['toolContent'] = reconstructed or '[tool result not available]'
        if has_llm_round:
            batch_key = r.get('llmRound')
        else:
            prev = all_rounds[i - 1] if i > 0 else None
            if prev and prev.get('toolCallId') and r.get('roundNum', 0) > prev.get('roundNum', -999) + 1:
                batch_key += 1
        batches.setdefault(batch_key, []).append(r)
        last_complete_idx = i

    if last_complete_idx < 0:
        return None

    tool_history = [_build_tool_history_round(batch) for batch in batches.values()]
    kept_rounds = all_rounds[:last_complete_idx + 1]
    discarded_rounds = len(all_rounds) - len(kept_rounds)

    preserved_content_parts = [r.get('assistantContent') or '' for r in kept_rounds]
    preserved_content = '\n\n'.join(p for p in preserved_content_parts if p)
    original_content = assistant_msg.get('content') or ''
    # Fallback: if assistantContent was never populated on rounds (legacy DB rows),
    # reuse the full prior content so the visible text is preserved.
    if not preserved_content and kept_rounds and original_content:
        preserved_content = original_content
    discarded_content = max(0, len(original_content) - len(preserved_content))
    # The prose tail dropped on rollback — surfaced to the UI as a display-only
    # "Earlier Response" block (priorContent), mirroring discarded_thinking_text.
    # Cannot be replayed on the wire (the model regenerates from the tool-result
    # checkpoint), so it is stripped by _strip_non_api_fields before any LLM call.
    if discarded_content > 0:
        discarded_content_text = (
            original_content[len(preserved_content):].lstrip('\n')
            if original_content.startswith(preserved_content)
            else original_content
        )
    else:
        discarded_content_text = ''

    preserved_thinking_chars = sum(len(r.get('thinking') or '') for r in kept_rounds)
    original_thinking = assistant_msg.get('thinking') or ''
    discarded_thinking = max(0, len(original_thinking) - preserved_thinking_chars)
    # Capture the message-level thinking text whenever it is not fully covered
    # by per-round thinking — this is the trailing reasoning the model emitted
    # after the last completed tool batch.  We can never replay it on the wire
    # (Anthropic rejects orphan thinking blocks; OpenAI-compat strips reasoning
    # server-side), but it is still useful to surface to the user as a
    # display-only "earlier thinking" block on the rolled-back turn.
    discarded_thinking_text = original_thinking if discarded_thinking > 0 else ''

    return {
        'kept_rounds': kept_rounds,
        'discarded_rounds': discarded_rounds,
        'tool_history': tool_history,
        'preserved_content': preserved_content,
        'preserved_thinking_chars': preserved_thinking_chars,
        'discarded_content': discarded_content,
        'discarded_content_text': discarded_content_text,
        'discarded_thinking': discarded_thinking,
        'discarded_thinking_text': discarded_thinking_text,
        'original_content_len': len(original_content),
        'original_thinking_len': len(original_thinking),
    }


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
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    config = data.get('config') or {}
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
            logger.warning('[Continue] Failed to parse messages for conv=%s: %s',
                           conv_id[:8], e)
            return api_internal_error('Failed to parse conversation')

        title = row['title']

        if not messages:
            return api_bad_request('Conversation has no messages')
        if messages[-1].get('role') != 'assistant':
            return api_bad_request('Last message is not an assistant message')

        assistant_msg = messages[-1]
        # Trivial case: empty content & thinking → no checkpoint needed; ask
        # the frontend to fall back to pop-and-resend (full regeneration).
        if not assistant_msg.get('content') and not assistant_msg.get('thinking') \
                and not (assistant_msg.get('toolRounds') or []):
            logger.info('[Continue] conv=%s last assistant is empty — fallback to regenerate',
                        conv_id[:8])
            return jsonify({'fallback': 'regenerate', 'reason': 'empty_assistant'})

        scan = _scan_continue_checkpoint(assistant_msg)
        if scan is None:
            logger.info('[Continue] conv=%s no tool-call checkpoint available — fallback to regenerate',
                        conv_id[:8])
            return jsonify({'fallback': 'regenerate', 'reason': 'no_checkpoint'})

        # ── Apply rollback in place on the assistant message ──
        preserved_content = scan['preserved_content']
        assistant_msg['toolRounds'] = scan['kept_rounds']
        assistant_msg['content'] = preserved_content
        # Strip live thinking — any replay-worthy thinking already lives on
        # keptRounds[i].thinking and is carried forward via toolHistory.
        # If there was trailing message-level thinking (reasoning emitted
        # after the last completed tool batch) we can't replay it on the
        # wire, but we stash it on a display-only field so the UI can
        # render it as a collapsed "earlier thinking" block.  Stripped by
        # _strip_non_api_fields before any LLM call (not in _API_MESSAGE_FIELDS).
        assistant_msg['thinking'] = ''
        if scan.get('discarded_thinking_text'):
            # Replace rather than append — a Continue cycle that produced new
            # trailing thinking is the freshest signal of "what the model was
            # reasoning about right before we resumed."  Older priorThinking
            # from a prior Continue is no longer the immediate context.
            assistant_msg['priorThinking'] = scan['discarded_thinking_text']
        # else: leave any existing priorThinking from a previous Continue cycle
        # in place — streaming this turn produced no extra trailing thinking,
        # so the prior "earlier thinking" remains the most recent discard.
        # Same treatment for the discarded prose tail (display-only priorContent)
        # so a post-Continue page refresh (DB reload) doesn't lose the visible
        # record of what was rolled back — keeping the content area honest
        # rather than silently empty beside an unchanged tool panel.
        if scan.get('discarded_content_text'):
            assistant_msg['priorContent'] = scan['discarded_content_text']
        for stale_key in ('finishReason', 'toolSummary', 'error'):
            assistant_msg.pop(stale_key, None)

        # Stash pre-checkpoint metadata on cfg for the task + for DB merge.
        kept_usage = assistant_msg.get('usage') or None
        kept_api_rounds = assistant_msg.get('apiRounds') or []
        kept_modified_files = assistant_msg.get('modifiedFiles') or None
        kept_modified_file_list = assistant_msg.get('modifiedFileList') or []

        # Persist rolled-back state BEFORE starting the task — mirrors the
        # order used in chat_regenerate to avoid the streaming task
        # overwriting the rollback in ``_sync_result_to_conversation``.
        _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        logger.info(
            '[Continue] conv=%s kept=%d rounds discarded=%d rounds preservedContent=%d '
            'discardedContent=%d preservedThinking=%d discardedThinking=%d priorThinking=%s',
            conv_id[:8], len(scan['kept_rounds']), scan['discarded_rounds'],
            len(preserved_content), scan['discarded_content'],
            scan['preserved_thinking_chars'], scan['discarded_thinking'],
            'preserved' if scan.get('discarded_thinking_text') else 'none',
        )

        # Build cfg payload — same shape the frontend used to build.
        cfg_payload = dict(config)
        cfg_payload['excludeLast'] = True
        if scan['tool_history']:
            cfg_payload['toolHistory'] = scan['tool_history']
        if preserved_content:
            cfg_payload['contentPrefix'] = preserved_content
        if scan['kept_rounds']:
            cfg_payload['checkpointToolRounds'] = scan['kept_rounds']
        if kept_usage:
            cfg_payload['checkpointUsage'] = kept_usage
        if kept_api_rounds:
            cfg_payload['checkpointApiRounds'] = kept_api_rounds
        if kept_modified_files:
            cfg_payload['checkpointModifiedFiles'] = kept_modified_files
        if kept_modified_file_list:
            cfg_payload['checkpointModifiedFileList'] = kept_modified_file_list

        # Start the task.
        task_id, err_resp = _start_task_for_conv(conv_id, cfg_payload, data)
        if err_resp is not None:
            return err_resp if not isinstance(err_resp, tuple) else err_resp

        # Persist activeTaskId (same as chat_regenerate).
        try:
            _persist_conv_messages(db, conv_id, messages, title,
                                   {'activeTaskId': task_id})
        except Exception as e:
            logger.warning('[Continue] Failed to update activeTaskId: %s', e)

        _invalidate_meta_cache()
        try:
            from lib.log import audit_log as _audit_log
            _audit_log(
                'continue_checkpoint',
                conv_id=conv_id,
                kept=len(scan['kept_rounds']),
                discarded=scan['discarded_rounds'],
                preservedContentLen=len(preserved_content),
                discardedContentLen=scan['discarded_content'],
                preservedThinking=scan['preserved_thinking_chars'],
                discardedThinking=scan['discarded_thinking'],
                priorThinkingChars=len(scan.get('discarded_thinking_text') or ''),
            )
        except Exception as e:
            logger.debug('[Continue] audit_log failed (non-fatal): %s', e)

        return jsonify({
            'taskId': task_id,
            'convId': conv_id,
            'checkpoint': {
                'keptRounds': len(scan['kept_rounds']),
                'discardedRounds': scan['discarded_rounds'],
                'preservedContentLen': len(preserved_content),
                'discardedContentLen': scan['discarded_content'],
                'preservedThinkingChars': scan['preserved_thinking_chars'],
                'discardedThinking': scan['discarded_thinking'],
            },
        })

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


def _start_external_backend(data, messages, backend_name):
    """Start a task using an external CLI agent backend (Claude Code, Codex, etc.).

    Validates backend availability/auth, creates a task, then spawns a thread
    that calls ``backend.start_turn()`` and pipes NormalizedEvents through
    ``normalized_to_sse()`` into ``append_event()``.

    The existing SSE streaming (``chat_stream``) and polling (``chat_poll``)
    work unchanged — they read from the same ``task['events']`` queue.
    """
    from lib.agent_backends import get_backend
    from lib.agent_backends.sse_bridge import SSEBridgeState
    from lib.tasks_pkg.manager import append_event, persist_task_result

    backend = get_backend(backend_name)
    if backend is None:
        return api_bad_request(f'Unknown backend: {backend_name}')
    if not backend.is_available():
        return jsonify({
            'error': f'{backend.display_name} CLI is not installed. '
                     f'Install it first, then try again.',
        }), 400
    if not backend.is_authenticated():
        return jsonify({
            'error': f'{backend.display_name} is not authenticated. '
                     f'Run the CLI and log in first.',
        }), 401

    task = create_task(data.get('convId', ''), messages, data.get('config', {}))
    task['_backend'] = backend_name

    # Extract the last user message text
    user_message = ''
    for m in reversed(messages):
        if m.get('role') == 'user':
            content = m.get('content', '')
            if isinstance(content, list):
                content = ' '.join(
                    b.get('text', '') for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                )
            user_message = content or ''
            break

    project_path = data.get('config', {}).get('projectPath')
    conv_id = data.get('convId', '')
    session_id = backend.get_session_id(conv_id) if conv_id else None

    logger.info('[Chat] Starting EXTERNAL task %s for conv %s backend=%s project=%s session=%s',
                task['id'], conv_id, backend_name,
                project_path or 'none', session_id[:16] if session_id else 'none')

    def _run_external():
        bridge = SSEBridgeState()

        try:
            accumulated_content = ''
            accumulated_thinking = ''

            for event in backend.start_turn(
                task, user_message,
                project_path=project_path,
                session_id=session_id,
            ):
                # Accumulate text for persistence
                if event.kind == 'text_delta':
                    accumulated_content += event.text
                    with task.get('content_lock', threading.Lock()):
                        task['content'] = accumulated_content
                elif event.kind == 'thinking_delta':
                    accumulated_thinking += event.text
                    task['thinking'] = accumulated_thinking

                # ── Track toolRounds on the task dict for persistence ──
                if event.kind == 'tool_start':
                    # Translate first so bridge assigns roundNum
                    sse_event = bridge.translate(event)
                    if sse_event:
                        # Build search round entry (mirrors tool_display.py)
                        rn = sse_event.get('roundNum', 0)
                        round_entry = {
                            'roundNum': rn,
                            'query': sse_event.get('query', ''),
                            'results': None,
                            'status': 'searching',
                            'toolName': sse_event.get('toolName', event.tool_name or 'tool'),
                            'toolCallId': sse_event.get('toolCallId', event.tool_id or ''),
                            'toolArgs': sse_event.get('toolArgs', ''),
                        }
                        task['toolRounds'].append(round_entry)
                        append_event(task, sse_event)
                    continue

                if event.kind == 'tool_complete':
                    sse_event = bridge.translate(event)
                    if sse_event:
                        # Update the matching search round
                        rn = sse_event.get('roundNum', 0)
                        for sr in task.get('toolRounds', []):
                            if sr.get('roundNum') == rn:
                                sr['results'] = sse_event.get('results', [])
                                sr['status'] = 'done'
                                if sse_event.get('engineBreakdown'):
                                    sr['engineBreakdown'] = sse_event['engineBreakdown']
                                break
                        append_event(task, sse_event)
                    continue

                # Translate all other events normally
                sse_event = bridge.translate(event)
                if sse_event:
                    append_event(task, sse_event)

                # Store session ID from done event
                if event.session_id:
                    task['_external_session_id'] = event.session_id

                # Store usage from done event
                if event.kind == 'done' and event.usage:
                    task['usage'] = event.usage
                if event.kind == 'done' and event.finish_reason:
                    task['finishReason'] = event.finish_reason

            task['status'] = 'done'
            task['model'] = backend_name  # Show backend name as "model"

            # Ensure done event was emitted
            has_done = any(
                e.get('type') == 'done'
                for e in task.get('events', [])
            )
            if not has_done:
                done_evt = {'type': 'done', 'finishReason': task.get('finishReason', 'stop')}
                if task.get('usage'):
                    done_evt['usage'] = task['usage']
                append_event(task, done_evt)

            # Persist to DB
            try:
                persist_task_result(task)
            except Exception as e:
                logger.warning('[Chat] Failed to persist external task result: %s', e)

            logger.info('[Chat] External task %s completed — backend=%s content=%dchars toolRounds=%d',
                        task['id'][:8], backend_name, len(accumulated_content),
                        len(task.get('toolRounds', [])))

        except Exception as e:
            logger.error('[Chat] External task %s failed: %s',
                         task['id'][:8], e, exc_info=True)
            from lib.error_envelope import from_exception as _err_from_exc
            envelope = _err_from_exc(
                e, model=data.get('config', {}).get('model', ''),
                context=f'external-backend:{backend_name}',
                source='routes.chat',
            )
            task['error'] = envelope
            task['status'] = 'done'
            append_event(task, {'type': 'done', 'error': envelope, 'finishReason': 'error'})
            try:
                persist_task_result(task)
            except Exception as e:
                logger.warning('[Chat] persist_task_result failed for task %s: %s', task['id'][:8], e)

    try:
        threading.Thread(target=_run_external, daemon=True).start()
    except Exception as _spawn_err:
        logger.exception('[Chat] Failed to start external backend thread for task %s',
                         task['id'])
        from lib.error_envelope import make_envelope as _make_env
        task['status'] = 'error'
        task['error'] = _make_env(
            'internal',
            detail='Server failed to start backend thread.',
            model=data.get('config', {}).get('model', ''),
            context=f'external-backend:{backend_name}',
            source='routes.chat',
            raw=str(_spawn_err),
        )
        return api_internal_error('Failed to start task')

    return jsonify({'taskId': task['id']})


@chat_bp.route('/api/chat/stream/<task_id>', methods=['GET'])
async def chat_stream(task_id):
    import asyncio
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        # ★ Cold-path resumption: if the client provides Last-Event-ID and we
        #   still have persisted events for this task, replay from the table.
        #   This makes Last-Event-ID resumption durable across cleanup_old_tasks
        #   and server restart — without it, the stale reader would receive
        #   only a synthetic `state`+`done` snapshot and lose every intermediate
        #   tool/phase event the user saw before the disconnect.
        _replay_cursor_hdr = request.headers.get('Last-Event-ID', '').strip()
        if _replay_cursor_hdr:
            try:
                _replay_cursor = int(_replay_cursor_hdr)
            except (ValueError, TypeError) as _e_audit:
                logger.debug('[chat] chat_stream caught %s: %s', type(_e_audit).__name__, _e_audit)
                _replay_cursor = None
            if _replay_cursor is not None and _replay_cursor >= 0:
                from lib.tasks_pkg.event_log import read_events as _read_events
                _persisted = await asyncio.to_thread(
                    _read_events, task_id, since_event_id=_replay_cursor)
                if _persisted:
                    logger.info('[Chat] Stream %s cold replay from event_log: %d event(s) since id=%d',
                                task_id[:8], len(_persisted), _replay_cursor)

                    def gen_persisted():
                        for _ in range(4):
                            yield ':' + ' ' * 2048 + '\n\n'
                        for ev in _persisted:
                            eid = ev['event_id']
                            payload = ev['payload']
                            yield f'id: {eid}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                            if isinstance(payload, dict) and payload.get('type') == 'done':
                                return
                        # No persisted 'done' — synthesize state+done from task_results.
                        # We MUST emit a 'state' event before 'done' here: a
                        # client whose Last-Event-ID points past the end of
                        # the persisted log (e.g. TTL prune ran, or the
                        # client's last cursor was very recent) would
                        # otherwise see only metadata and lose all text.
                        # Mirrors the warm-fallback shape further down.
                        try:
                            db_local = get_db(DOMAIN_CHAT)
                            row_local = db_local.execute(
                                'SELECT content,thinking,error,status,tool_rounds,metadata '
                                'FROM task_results WHERE task_id=?',
                                (task_id,)
                            ).fetchone()
                            if row_local:
                                state_local = {
                                    'type': 'state',
                                    'content': row_local['content'] or '',
                                    'thinking': row_local['thinking'] or '',
                                    'status': row_local['status'],
                                }
                                if row_local['tool_rounds']:
                                    try:
                                        state_local['toolRounds'] = json.loads(row_local['tool_rounds'])
                                    except (json.JSONDecodeError, TypeError) as _e:
                                        logger.debug('[Chat] cold-replay tool_rounds parse failed: %s', _e)
                                if row_local['error']:
                                    from lib.error_envelope import from_json as _err_from_json
                                    state_local['error'] = _err_from_json(row_local['error'])
                                yield f'data: {json.dumps(state_local, ensure_ascii=False)}\n\n'
                            done_evt_local = {'type': 'done'}
                            if row_local:
                                if row_local['metadata']:
                                    try:
                                        m = json.loads(row_local['metadata'])
                                        # Field list MUST mirror _extract_task_meta /
                                        # _extract_db_meta / chat_poll's DB-path loop.
                                        # See _extract_task_meta docstring for why.
                                        for k in ('finishReason', 'usage', 'preset', 'toolSummary',
                                                  'model', 'provider_id', 'thinkingDepth',
                                                  'apiRounds', 'modifiedFiles', 'modifiedFileList',
                                                  'fallbackModel', 'fallbackFrom'):
                                            if m.get(k):
                                                done_evt_local[k] = m[k]
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[chat] gen_persisted caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                if row_local['error']:
                                    from lib.error_envelope import from_json as _err_from_json
                                    done_evt_local['error'] = _err_from_json(row_local['error'])
                            yield f'data: {json.dumps(done_evt_local, ensure_ascii=False)}\n\n'
                        except Exception as _e:
                            logger.debug('[Chat] cold-replay synthetic done failed: %s', _e)

                    return Response(gen_persisted(), mimetype='text/event-stream', headers={
                        'Content-Type': 'text/event-stream; charset=utf-8',
                        'Cache-Control': 'no-cache, no-transform',
                        'X-Accel-Buffering': 'no',
                        'Connection': 'keep-alive',
                    })

        db = get_db(DOMAIN_CHAT)
        row = await asyncio.to_thread(
            lambda: db.execute(
                'SELECT content,thinking,error,status,tool_rounds,metadata FROM task_results WHERE task_id=?',
                (task_id,)
            ).fetchone())
        if row:
            state = {
                'type': 'state', 'content': row['content'],
                'thinking': row['thinking'], 'status': row['status'],
            }
            if row['error']:
                from lib.error_envelope import from_json as _err_from_json
                state['error'] = _err_from_json(row['error'])
            if row['tool_rounds']:
                try:
                    state['toolRounds'] = json.loads(row['tool_rounds'])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning('[Chat] Failed to parse tool_rounds for task %s: %s', task_id, e, exc_info=True)
            meta = _extract_db_meta(row)
            # Field lists MUST stay aligned with _extract_task_meta and
            # the chat_poll DB-path loop. See _extract_task_meta docstring.
            for key in ('finishReason', 'usage', 'preset', 'model',
                        'provider_id', 'thinkingDepth',
                        'apiRounds', 'modifiedFiles', 'modifiedFileList'):
                if meta.get(key):
                    state[key] = meta[key]
            done_evt = {'type': 'done'}
            for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                        'model', 'provider_id', 'thinkingDepth',
                        'apiRounds', 'modifiedFiles', 'modifiedFileList'):
                if meta.get(key):
                    done_evt[key] = meta[key]
            if meta.get('fallbackModel'):
                done_evt['fallbackModel'] = meta['fallbackModel']
                done_evt['fallbackFrom'] = meta.get('fallbackFrom', '')
            if row['error']:
                from lib.error_envelope import from_json as _err_from_json
                done_evt['error'] = _err_from_json(row['error'])

            logger.info('[Chat] Stream %s served from DB — status=%s content=%dchars '
                       'finishReason=%s model=%s error=%s',
                       task_id[:8], row['status'], len(row['content'] or ''),
                       meta.get('finishReason', '?'), meta.get('model', '?'),
                       row['error'] or 'none')

            def gen_done():
                for _ in range(4):
                    yield ':' + ' ' * 2048 + '\n\n'
                yield f'data: {json.dumps(state, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps(done_evt, ensure_ascii=False)}\n\n'

            return Response(gen_done(), mimetype='text/event-stream', headers={
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            })
        logger.warning('[Chat] Task %s not found (stream)', task_id)
        return api_not_found('Task not found')

    # ★ SSE reader dedup: supersede any previous SSE reader for this task.
    #   When a client reconnects (proxy timeout, page switch), the old reader
    #   should detect it's been replaced and exit, freeing the thread.
    _sse_gen = task.get('_sse_gen_id', 0) + 1
    task['_sse_gen_id'] = _sse_gen

    # ★ Item 6: Last-Event-ID reconnection — if the client provides a cursor,
    #   skip the full state snapshot and resume from that event index.
    _last_event_id = request.headers.get('Last-Event-ID', '').strip()
    _resume_cursor = None
    if _last_event_id:
        try:
            _resume_cursor = int(_last_event_id)
            logger.info('[Chat] SSE stream %s reconnecting with Last-Event-ID=%d',
                        task_id[:8], _resume_cursor)
        except (ValueError, TypeError):
            logger.debug('[Chat] SSE stream %s ignoring invalid Last-Event-ID: %s',
                         task_id[:8], _last_event_id)

    _stream_start = time.time()
    _events_sent = 0
    async def generate():
        nonlocal _events_sent
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'

        with task['events_lock']:
            # ★ If resuming via Last-Event-ID, skip the state snapshot and
            #   replay only events AFTER the cursor. Per the SSE spec,
            #   Last-Event-ID is the id of the last *received* event, so
            #   we resume from cursor + 1 to avoid re-sending it.
            if _resume_cursor is not None and _resume_cursor >= 0:
                resume_from = _resume_cursor + 1
                missed_evts = task['events'][resume_from:]
            else:
                missed_evts = None
                resume_from = None
                cursor = len(task['events'])

        if resume_from is not None:
            cursor = resume_from
            # Resume path: replay missed events since Last-Event-ID
            for idx, ev in enumerate(missed_evts):
                eid = resume_from + idx
                yield f'id: {eid}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n'
                _events_sent += 1
                if ev.get('type') == 'done':
                    return
            # Advance cursor past replayed events for live streaming loop
            cursor = resume_from + len(missed_evts)
            if task['status'] != 'running' and not missed_evts:
                late_done = {'type': 'done'}
                late_meta = _extract_task_meta(task)
                late_done.update(late_meta)
                if task['error']:
                    late_done['error'] = task['error']
                yield f'id: {cursor}\ndata: {json.dumps(late_done, ensure_ascii=False)}\n\n'
                return
        else:
            # Fresh connection path: send full state snapshot
            with task['events_lock']:
                state = {
                    'type': 'state', 'content': task['content'],
                    'thinking': task['thinking'], 'status': task['status'],
                }
                if task['error']:
                    state['error'] = task['error']
                if task['toolRounds']:
                    state['toolRounds'] = task['toolRounds']
                meta = _extract_task_meta(task)
                for key in ('finishReason', 'usage', 'model', 'thinkingDepth'):
                    if meta.get(key):
                        state[key] = meta[key]
                if task.get('preset'):
                    state['preset'] = task['preset']
                if task.get('_memoryPrefetch'):
                    state['memoryPrefetch'] = task['_memoryPrefetch']
                # ★ Endpoint mode: include phase and completed turns for reconnection
                if task.get('endpoint_mode'):
                    state['endpointMode'] = True
                    state['endpointPhase'] = task.get('_endpoint_phase', 'planning')
                    state['endpointIteration'] = task.get('_endpoint_iteration', 0)
                    ep_turns = task.get('_endpoint_turns')
                    if ep_turns:
                        state['endpointTurns'] = ep_turns
                    # ★ Authoritative finished signal — when task has completed
                    #   (_finalize set _endpoint_phase='done'), propagate the
                    #   stop reason so the frontend's reconnect paths never
                    #   create a ghost worker after Critic STOP approval.
                    if task.get('_endpoint_stop_reason'):
                        state['endpointStopReason'] = task['_endpoint_stop_reason']
                cursor = len(task['events'])

            # ★ State snapshot gets NO id: field — it's synthetic, not a real
            #   event from the events array. Only real events (deltas, phases,
            #   done) get id: fields. This prevents the id collision between
            #   the state snapshot and the first live event at the same cursor.
            #   If the client only received the state snapshot and reconnects,
            #   _lastEventId will be null → fresh connection with full state.
            yield f'data: {json.dumps(state, ensure_ascii=False)}\n\n'

            if task['status'] != 'running':
                done_evt = {'type': 'done'}
                done_evt.update(meta)
                if task['error']:
                    done_evt['error'] = task['error']
                yield f'id: {cursor}\ndata: {json.dumps(done_evt, ensure_ascii=False)}\n\n'
                return

        _MAX_SSE_DURATION = 7200  # 2 hours — absolute max SSE stream lifetime
        last_t = time.time()
        while True:
            # ── Guard: absolute SSE stream duration limit ──
            _elapsed = time.time() - _stream_start
            if _elapsed > _MAX_SSE_DURATION:
                _conv_id = task.get('convId', '?')
                logger.warning('[Chat] SSE stream %s conv=%s closing after %.0fs (max %ds) — '
                               'task still running (status=%s), %d events sent so far. '
                               'Frontend will switch to polling to pick up the result.',
                               task_id[:8], _conv_id, _elapsed, _MAX_SSE_DURATION,
                               task.get('status', '?'), _events_sent)
                # ★ DO NOT abort the backend task — it's still doing useful work.
                # Send an informational event (NOT 'done') so the frontend shows a toast,
                # then close the SSE stream. The frontend detects the stream closed
                # without a 'done' event → _trySSE returns false → _pollFallback kicks in.
                timeout_notice = {'type': 'sse_timeout',
                                  'message': 'SSE connection reached maximum duration. Switching to polling — task is still running.'}
                yield f'data: {json.dumps(timeout_notice, ensure_ascii=False)}\n\n'
                return

            with task['events_lock']:
                new_evts = task['events'][cursor:]
                _cursor_before = cursor
                cursor = len(task['events'])
            for idx, ev in enumerate(new_evts):
                eid = _cursor_before + idx
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
                    logger.info('[Chat] SSE stream %s finished normally — %d events sent in %.1fs '
                               'finishReason=%s error=%s',
                               task_id[:8], _events_sent, time.time() - _stream_start,
                               _done_fr, _done_err_summary)
                    return
            if task['status'] != 'running' and not new_evts:
                late_done = {'type': 'done'}
                late_meta = _extract_task_meta(task)
                late_done.update(late_meta)
                if task['error']:
                    late_done['error'] = task['error']
                # ★ Severity split: an aborted/interrupted/normally-stopped
                #   task that needs a LATE done synthesis is expected:
                #   - aborted/interrupted: user hit Stop after orchestrator
                #     already flushed its own done event.
                #   - stop: task finished normally between the queue poll and
                #     this status check (the common 157-line/day pattern).
                #   Anything else (length, content_filter, error, etc.) still
                #   signals a missed done-event — keep as warning.
                _late_fr = late_meta.get('finishReason', '?')
                _is_benign = _late_fr in ('aborted', 'interrupted', 'stop')
                _log_fn = logger.info if _is_benign else logger.warning
                _err_obj = task['error']
                _err_summary = (
                    _err_obj.get('detail') or _err_obj.get('message') or _err_obj.get('kind')
                    if isinstance(_err_obj, dict) else (_err_obj or 'none')
                )
                _log_fn('[Chat] SSE stream %s emitting LATE done '
                        '(task finished but no done event in queue) — '
                        'finishReason=%s model=%s error=%s',
                        task_id[:8], _late_fr,
                        late_meta.get('model', '?'), _err_summary)
                yield f'id: {cursor}\ndata: {json.dumps(late_done, ensure_ascii=False)}\n\n'
                return
            # ★ SSE reader dedup: if a newer SSE reader connected, exit this one
            if task.get('_sse_gen_id', _sse_gen) != _sse_gen:
                logger.info('[Chat] SSE stream %s superseded by newer reader (gen %d→%d) — '
                           'closing stale reader after %d events in %.1fs',
                           task_id[:8], _sse_gen, task.get('_sse_gen_id', -1),
                           _events_sent, time.time() - _stream_start)
                return
            if time.time() - last_t > 15:
                yield ': keepalive\n\n'
                last_t = time.time()
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
            elapsed = time.time() - _stream_start
            content_len = len(task.get('content') or '')
            _fr = task.get('finishReason') or '?'
            _model = task.get('model') or '?'
            _provider = task.get('provider_id') or '?'
            _err = task.get('error')
            if not done_sent:
                # Severity-aware: zero-events = real problem (SSE opened
                # but nothing delivered); events>0 = normal client-side
                # tab-close / network-retry — client poll fallback will
                # pick up the rest.
                if _events_sent == 0:
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

    resp = Response(generate_with_disconnect_log(), mimetype='text/event-stream', headers={
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })
    resp.timeout = None
    return resp


@api_v1_chat_bp.route('/api/v1/chat/abort-conv/<conv_id>', methods=['POST'], endpoint='ui_chat_abort_conv')
@require_scope('chat')
def chat_abort_conv(conv_id):
    """Abort all running tasks for a conversation by conv ID.

    Used when the frontend aborts during translation and never received a
    taskId — the server may have already started a task that needs to be
    killed.  This is the convId-based counterpart of ``/api/chat/abort/<task_id>``.

    Also records a per-conv abort marker so any /api/chat/send still
    blocked inside auto-translate can detect the abort and bail out
    before persisting / enqueueing / dispatching the message.
    """
    from lib.tasks_pkg import abort_running_tasks_for_conv
    _mark_conv_aborted(conv_id)
    aborted = abort_running_tasks_for_conv(conv_id)
    if aborted:
        logger.info('[Chat] Abort-by-conv conv=%s — aborted %d task(s)', conv_id[:8], aborted)
    else:
        logger.debug('[Chat] Abort-by-conv conv=%s — no running tasks found', conv_id[:8])
    return api_ok({'aborted': aborted})
@api_v1_chat_bp.route('/api/v1/chat/abort/<task_id>', methods=['POST'], endpoint='ui_chat_abort')
@require_scope('chat')
def chat_abort(task_id):
    """Abort a running task by id.

    Sets ``task['aborted']`` (the orchestrator polls this between rounds),
    SIGTERMs any spawned ``run_command`` subprocess, and signals the external
    backend if one is in use. Idempotent — a duplicate abort logs at WARNING
    and returns ok.

    This is the single, authoritative abort handler — it carries the real
    subprocess / external-backend kill logic. The previous duplicate stub in
    ``routes/api_v1/chat.py`` (which only flipped ``aborted``) was removed.
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return api_not_found('Not found')
    was_already_aborted = task.get('aborted', False)
    task['aborted'] = True
    task['_abort_timestamp'] = time.time()
    audit_log('api_chat_abort',
              key_id=(current_auth().key_id if current_auth() else ''),
              task_id=task_id)
    # Log comprehensive abort context
    _status = task.get('status', '?')
    _elapsed = time.time() - task.get('created_at', time.time())
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _model = task.get('model', '?')
    _conv_id = task.get('convId', '?')
    if was_already_aborted:
        logger.warning('[Chat] Task %s abort DUPLICATE — already aborted. conv=%s status=%s',
                       task_id, _conv_id, _status)
    else:
        logger.info('[Chat] Task %s ABORT RECEIVED — conv=%s model=%s status=%s '
                    'elapsed=%.1fs content=%dchars thinking=%dchars',
                    task_id, _conv_id, _model, _status, _elapsed, _content_len, _thinking_len)
    # ── Kill any running subprocess (run_command) ──
    _sub_pid = task.get('_subprocess_pid')
    if _sub_pid:
        try:
            import os as _os
            import signal as _signal
            _pgid = task.get('_subprocess_pgid')
            if _pgid:
                _os.killpg(_pgid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess process group pgid=%d',
                            task_id[:8], _pgid)
            else:
                _os.kill(_sub_pid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess pid=%d',
                            task_id[:8], _sub_pid)
        except (OSError, ProcessLookupError) as e:
            logger.debug('[Chat] Task %s — subprocess kill skipped: %s', task_id[:8], e)

    # ── External backend: also signal the subprocess to terminate ──
    _backend_name = task.get('_backend')
    if _backend_name and _backend_name != 'builtin':
        try:
            from lib.agent_backends import get_backend
            backend = get_backend(_backend_name)
            if backend:
                backend.abort(task_id)
                logger.info('[Chat] Sent abort to external backend %s for task %s',
                            _backend_name, task_id[:8])
        except Exception as e:
            logger.warning('[Chat] Failed to abort external backend %s: %s',
                           _backend_name, e)
    return api_ok()
@api_v1_chat_bp.route('/api/v1/chat/poll/<task_id>', methods=['GET'], endpoint='ui_chat_poll')
@require_scope('chat')
def chat_poll(task_id):
    """Poll a task by id; returns the live in-memory task or its DB checkpoint.

    Memory hit returns the full live task dict; DB hit deserialises the
    persisted ``task_results`` row. Tasks whose DB row says ``status='running'``
    but which are absent from memory are reported as ``status='interrupted'``
    (server crashed mid-task) so the frontend stops polling and recovers the
    partial content.
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if task:
        content_len = len(task.get('content') or '')
        thinking_len = len(task.get('thinking') or '')
        finish_reason = task.get('finishReason') or '?'
        model = task.get('model') or '?'
        logger.debug('[Chat] Poll %s from memory — status=%s content=%dchars thinking=%dchars '
                     'finishReason=%s model=%s error=%s',
                     task_id[:8], task['status'], content_len, thinking_len,
                     finish_reason, model, bool(task.get('error')))
        if task['status'] == 'done' and content_len == 0 and thinking_len == 0 and not task.get('error'):
            logger.warning('[Chat] Poll %s ⚠️ RETURNING EMPTY RESULT — task is done but has no content or thinking! '
                          'finishReason=%s model=%s',
                          task_id[:8], finish_reason, model)
        r = {
            'id': task['id'], 'status': task['status'],
            'content': task['content'], 'thinking': task['thinking'],
        }
        # Field list MUST mirror chat_poll's DB-path loop and
        # _extract_task_meta. See _extract_task_meta docstring.
        for key in ('error', 'toolRounds', 'finishReason', 'usage', 'preset',
                     'toolSummary', 'phase', 'modifiedFiles', 'modifiedFileList',
                     'model', 'provider_id', 'thinkingDepth', 'apiRounds'):
            if task.get(key):
                r[key] = task[key]
        if task.get('id'):
            r['taskId'] = task['id']
        if task.get('_fallback_model'):
            r['fallbackModel'] = task['_fallback_model']
        if task.get('_fallback_from'):
            r['fallbackFrom'] = task['_fallback_from']
        # ★ Memory prefetch indicator (persists through poll fallback + reload)
        if task.get('_memoryPrefetch'):
            r['memoryPrefetch'] = task['_memoryPrefetch']
        # ★ Include endpoint turns for endpoint mode tasks so _pollFallback
        #   can reconstruct the full multi-turn conversation
        if task.get('endpoint_mode') and task.get('_endpoint_turns'):
            r['endpointMode'] = True
            r['endpointTurns'] = task['_endpoint_turns']
        return jsonify(r)

    logger.debug('[Chat] Poll %s — not in memory, checking DB', task_id[:8])
    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT task_id,content,thinking,error,status,tool_rounds,metadata FROM task_results WHERE task_id=?',
        (task_id,)
    ).fetchone()
    if row:
        _db_content_len = len(row['content'] or '')
        _db_thinking_len = len(row['thinking'] or '')
        _db_meta = _extract_db_meta(row)
        _db_finish = _db_meta.get('finishReason', '?')
        _db_model = _db_meta.get('model', '?')
        # ★ If the DB has status='running' but the task is NOT in memory,
        #   the server crashed/restarted mid-task. Mark it as 'interrupted'
        #   so the frontend stops polling and recovers the partial content.
        effective_status = row['status']
        if effective_status == 'running':
            logger.warning('[Chat] Poll %s — found stale checkpoint (status=running) in DB but task is NOT in memory. '
                           'Server likely crashed mid-task. Returning status=interrupted with %dchars content, %dchars thinking.',
                           task_id[:8], _db_content_len, _db_thinking_len)
            effective_status = 'interrupted'
            # ★ Update DB so future polls don't re-trigger this warning
            try:
                db.execute("UPDATE task_results SET status='interrupted' WHERE task_id=?", (task_id,))
                db.commit()
            except Exception as e:
                logger.warning('[Chat] Failed to update stale task %s to interrupted: %s', task_id[:8], e)
        else:
            logger.debug('[Chat] Poll %s from DB — status=%s content=%dchars thinking=%dchars '
                         'finishReason=%s model=%s error=%s',
                         task_id[:8], row['status'], _db_content_len, _db_thinking_len,
                         _db_finish, _db_model, bool(row['error']))
        r = {
            'id': row['task_id'], 'status': effective_status,
            'content': row['content'], 'thinking': row['thinking'],
        }
        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            r['error'] = _err_from_json(row['error'])
        if row['tool_rounds']:
            try:
                r['toolRounds'] = json.loads(row['tool_rounds'])
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Chat] Failed to parse tool_rounds in poll for task %s: %s', task_id, e, exc_info=True)
        # Field list MUST mirror chat_poll's in-memory loop and
        # _extract_task_meta. provider_id was previously dropped here
        # even though persist_task_result writes it into meta_json,
        # silently round-tripping through the DB.
        for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                     'model', 'provider_id', 'thinkingDepth', 'apiRounds',
                     'modifiedFiles', 'modifiedFileList'):
            if _db_meta.get(key):
                r[key] = _db_meta[key]
        if _db_meta.get('fallbackModel'):
            r['fallbackModel'] = _db_meta['fallbackModel']
            r['fallbackFrom'] = _db_meta.get('fallbackFrom', '')
        return jsonify(r)

    logger.warning('[Chat] Poll %s — NOT FOUND in memory or DB! Task may have been cleaned up. '
                   'Client will receive 404 and may lose accumulated content.',
                   task_id[:8])
    return api_not_found('Task not found')


# ══════════════════════════════════════════════════════════
#  Stdin / human-guidance response endpoints
#  → moved to routes/chat_human_io.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════
