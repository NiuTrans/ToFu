"""routes/chat.py — Chat start, streaming, polling, abort."""

import json
import threading
import time

from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_CHAT, get_db
from lib.log import get_logger

from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

import re

from lib.database import db_execute_with_retry, get_thread_db, json_dumps_pg
from routes.common import DEFAULT_USER_ID, _invalidate_meta_cache

logger = get_logger(__name__)

chat_bp = Blueprint('chat', __name__)


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
    """Extract metadata fields from an in-memory task dict."""
    meta = {}
    if task.get('finishReason'):
        meta['finishReason'] = task['finishReason']
    if task.get('usage'):
        meta['usage'] = task['usage']
    if task.get('preset'):
        meta['preset'] = task['preset']
    if task.get('model'):
        meta['model'] = task['model']
    if task.get('thinkingDepth'):
        meta['thinkingDepth'] = task['thinkingDepth']
    if task.get('toolSummary'):
        meta['toolSummary'] = task['toolSummary']
    if task.get('_fallback_model'):
        meta['fallbackModel'] = task['_fallback_model']
    if task.get('_fallback_from'):
        meta['fallbackFrom'] = task['_fallback_from']
    return meta


@chat_bp.route('/api/chat/active', methods=['GET'])
def chat_active():
    cleanup_old_tasks()
    with tasks_lock:
        result = [{'id': t['id'], 'convId': t['convId'], 'status': t['status'],
                   'aborted': bool(t.get('aborted'))}
                  for t in tasks.values()]
    return jsonify(result)


@chat_bp.route('/api/chat/start', methods=['POST'])

def chat_start():
    data = request.get_json(silent=True) or {}
    conv_id = data.get('convId', '')
    cfg = data.get('config', {})

    # ── Server-side message building ──
    # The frontend now sends {convId, config} only.
    # Messages are loaded from the DB and transformed server-side.
    # Legacy path: if frontend still sends 'messages', use them as fallback.
    messages = data.get('messages')
    if not messages:
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        exclude_last = cfg.get('excludeLast', False)
        messages = build_api_messages_from_db(conv_id, cfg, exclude_last=exclude_last)
        if messages is None:
            return jsonify({'error': 'Conversation not found'}), 404
        if not messages:
            return jsonify({'error': 'No messages'}), 400
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
    from lib.tasks_pkg import run_task
    _cfg_model = cfg.get('model', '?')
    _cfg_preset = cfg.get('preset', cfg.get('effort', '?'))
    logger.info('[Chat] Starting task %s for conv %s model=%s preset=%s',
                task['id'], task['convId'], _cfg_model, _cfg_preset)
    try:
        threading.Thread(target=run_task, args=(task,), daemon=True).start()
    except Exception:
        logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                         task['id'], task['convId'])
        task['status'] = 'error'
        task['error'] = 'Server failed to start task thread'
        return jsonify({'error': 'Failed to start task'}), 500

    return jsonify({'taskId': task['id']})


# ══════════════════════════════════════════════════════════
#  Atomic send: user message creation + task start
# ══════════════════════════════════════════════════════════

# Max time (seconds) for auto-translate during send — prevents frontend timeout
_TRANSLATE_SEND_TIMEOUT = 20


def _auto_translate_user(text, config):
    """Translate Chinese user text to English if autoTranslate is on.

    Capped at ``_TRANSLATE_SEND_TIMEOUT`` seconds to prevent the synchronous
    HTTP handler from blocking long enough to trigger the frontend's abort.

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

    def _do_translate():
        from routes.translate import _build_translate_prompt, _translate_one_chunk
        system_prompt = _build_translate_prompt('English', 'Chinese')
        return _translate_one_chunk(
            text, system_prompt, chunk_label=':send',
            source='Chinese', target='English',
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_do_translate)
            result, _usage = future.result(timeout=_TRANSLATE_SEND_TIMEOUT)
        if result and result.strip():
            _model = None
            if isinstance(_usage, dict):
                _disp = _usage.get('_dispatch', {})
                _model = _disp.get('model', _usage.get('model'))
            logger.info('[Send] Auto-translated user message: %d→%d chars model=%s',
                        len(text), len(result.strip()), _model)
            return result.strip(), text, _model
    except concurrent.futures.TimeoutError:
        logger.warning('[Send] Auto-translate timed out after %ds, sending original text',
                       _TRANSLATE_SEND_TIMEOUT)
    except Exception as e:
        logger.warning('[Send] Auto-translate failed: %s', e)

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


def _build_user_msg_from_payload(payload, config):
    """Build a user message dict from frontend payload + optional auto-translate.

    Args:
        payload: dict with text, images, pdfTexts, replyQuotes, convRefs, convRefTexts, timestamp
        config: task config dict (reads autoTranslate)

    Returns:
        user_msg dict ready to append to conv.messages
    """
    text = payload.get('text', '')
    timestamp = payload.get('timestamp') or int(time.time() * 1000)

    translated_text, original_text, translate_model = _auto_translate_user(text, config)

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
    """Write messages + metadata to the conversation row."""
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
        except (json.JSONDecodeError, TypeError):
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
    from lib.tasks_pkg import run_task, abort_running_tasks_for_conv

    api_messages = build_api_messages_from_db(conv_id, config)
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

    if is_endpoint:
        from lib.tasks_pkg.endpoint import run_endpoint_task
        task['endpoint_mode'] = True
        task['_endpoint_phase'] = 'planning'
        task['_endpoint_iteration'] = 0
        logger.info('[Chat] Starting ENDPOINT task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            threading.Thread(target=run_endpoint_task, args=(task,), daemon=True).start()
        except Exception:
            logger.exception('[Chat] Failed to start endpoint thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            task['status'] = 'error'
            task['error'] = 'Server failed to start task thread'
            return None, (jsonify({'error': 'Failed to start task'}), 500)
    else:
        logger.info('[Chat] Starting task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            threading.Thread(target=run_task, args=(task,), daemon=True).start()
        except Exception:
            logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            task['status'] = 'error'
            task['error'] = 'Server failed to start task thread'
            return None, (jsonify({'error': 'Failed to start task'}), 500)

    return task_id, None


@chat_bp.route('/api/chat/send', methods=['POST'])
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
    data = request.get_json(silent=True) or {}
    conv_id = data.get('convId', '')
    if not conv_id:
        return jsonify({'error': 'convId required'}), 400

    payload = data.get('message', {})
    config = data.get('config', {})
    settings_patch = data.get('settings')

    text = payload.get('text', '')
    if not text and not payload.get('images') and not payload.get('pdfTexts'):
        return jsonify({'error': 'Empty message'}), 400

    try:
        db = get_thread_db(DOMAIN_CHAT)

        # 1. Load or create conversation
        messages, is_new, title = _load_or_create_conv(db, conv_id, config, payload)

        # 2. Build user message (with auto-translate)
        user_msg = _build_user_msg_from_payload(payload, config)

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

        # 4. Append user message and persist (only for immediate start)
        messages.append(user_msg)
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
        return jsonify({'error': str(e)}), 500



@chat_bp.route('/api/chat/branch/start', methods=['POST'])
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
    data = request.get_json(silent=True) or {}
    conv_id = data.get('convId', '')
    if not conv_id:
        return jsonify({'error': 'convId required'}), 400

    msg_idx = data.get('msgIdx')
    branch_idx = data.get('branchIdx')
    if msg_idx is None or branch_idx is None:
        return jsonify({'error': 'msgIdx and branchIdx required'}), 400

    cfg = data.get('config', {})

    try:
        from lib.tasks_pkg.conv_message_builder import build_branch_api_messages

        api_messages = build_branch_api_messages(conv_id, msg_idx, branch_idx, cfg)
        if api_messages is None:
            return jsonify({'error': 'Branch not found'}), 404
        if not api_messages:
            return jsonify({'error': 'No messages to process'}), 400

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

        from lib.tasks_pkg import run_task
        try:
            threading.Thread(target=run_task, args=(task,), daemon=True).start()
        except Exception:
            logger.exception('[Branch] Failed to start thread for task %s', task_id[:8])
            task['status'] = 'error'
            task['error'] = 'Server failed to start task thread'
            return jsonify({'error': 'Failed to start task'}), 500

        return jsonify({'taskId': task_id})

    except Exception as e:
        logger.error('[Branch] Failed for conv=%s msg=%d branch=%d: %s',
                     conv_id[:8], msg_idx, branch_idx, e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/chat/regenerate', methods=['POST'])
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
    data = request.get_json(silent=True) or {}
    conv_id = data.get('convId', '')
    if not conv_id:
        return jsonify({'error': 'convId required'}), 400

    truncate_to = data.get('truncateToIndex')
    if truncate_to is None:
        return jsonify({'error': 'truncateToIndex required'}), 400

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
            return jsonify({'error': 'Conversation not found'}), 404

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Regen] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
            return jsonify({'error': 'Failed to parse conversation'}), 500

        title = row['title']

        if truncate_to < 0 or truncate_to >= len(messages):
            return jsonify({'error': f'truncateToIndex {truncate_to} out of range (0..{len(messages)-1})'}), 400

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
                translated, original, model = _auto_translate_user(text, config)
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
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/chat/tool-state/<conv_id>', methods=['PATCH'])
def chat_tool_state(conv_id):
    """Lightweight tool-state sync: merge tool settings into conversation settings.

    Unlike the full PUT /api/conversations/<id>, this only touches the settings
    column — no messages, no msg_count, no search_text update.
    Safe to call frequently (e.g. on every tool toggle).

    Body: { model?, searchMode?, fetchEnabled?, browserEnabled?, projectPath?, ... }
    """
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({'error': 'No settings provided'}), 400

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()

        if not row:
            # Conv not in DB yet (no messages sent) — that's OK, skip
            return jsonify({'ok': True, 'skipped': True})

        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError):
            settings = {}

        settings.update(data)
        settings_json = json.dumps(settings, ensure_ascii=False)

        db_execute_with_retry(db, '''
            UPDATE conversations SET settings=? WHERE id=? AND user_id=?
        ''', (settings_json, conv_id, DEFAULT_USER_ID))

        logger.debug('[ToolState] conv=%s patched %d keys: %s',
                     conv_id[:8], len(data), list(data.keys())[:10])
        return jsonify({'ok': True})

    except Exception as e:
        logger.error('[ToolState] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════
#  Server-side message queue
# ══════════════════════════════════════════════════════════

@chat_bp.route('/api/chat/queue', methods=['POST'])
def chat_queue_enqueue():
    """Legacy enqueue endpoint — kept for programmatic/API use.

    The primary send path is now ``/api/chat/send`` which auto-detects
    whether to start immediately or enqueue.  This endpoint is a thin
    wrapper around ``enqueue_message`` for backward compat.
    """
    data = request.get_json(silent=True) or {}
    conv_id = data.get('convId', '')
    if not conv_id:
        return jsonify({'error': 'convId required'}), 400

    message_data = data.get('message', {})
    config = data.get('config', {})

    if not message_data.get('text') and not message_data.get('images') and not message_data.get('pdfTexts'):
        return jsonify({'error': 'Empty message'}), 400

    from lib.message_queue import enqueue_message
    result = enqueue_message(conv_id, message_data, config)
    return jsonify(result)


@chat_bp.route('/api/chat/queue/<conv_id>', methods=['GET'])
def chat_queue_get(conv_id):
    """Get all queued messages for a conversation."""
    from lib.message_queue import get_queue
    queue = get_queue(conv_id)
    return jsonify(queue)


@chat_bp.route('/api/chat/queue/<conv_id>/<queue_id>', methods=['DELETE'])
def chat_queue_remove(conv_id, queue_id):
    """Remove a specific message from the queue."""
    from lib.message_queue import remove_from_queue
    removed = remove_from_queue(conv_id, queue_id)
    if not removed:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/queue/<conv_id>', methods=['DELETE'])
def chat_queue_clear(conv_id):
    """Clear all queued messages for a conversation."""
    from lib.message_queue import clear_queue
    count = clear_queue(conv_id)
    return jsonify({'cleared': count})


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
        return jsonify({'error': f'Unknown backend: {backend_name}'}), 400
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
            task['error'] = str(e)
            task['status'] = 'done'
            append_event(task, {'type': 'done', 'error': str(e), 'finishReason': 'error'})
            try:
                persist_task_result(task)
            except Exception as e:
                logger.warning('[Chat] persist_task_result failed for task %s: %s', task['id'][:8], e)

    try:
        threading.Thread(target=_run_external, daemon=True).start()
    except Exception:
        logger.exception('[Chat] Failed to start external backend thread for task %s',
                         task['id'])
        task['status'] = 'error'
        task['error'] = 'Server failed to start backend thread'
        return jsonify({'error': 'Failed to start task'}), 500

    return jsonify({'taskId': task['id']})


@chat_bp.route('/api/chat/stream/<task_id>', methods=['GET'])
def chat_stream(task_id):
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        db = get_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT content,thinking,error,status,tool_rounds,metadata FROM task_results WHERE task_id=?',
            (task_id,)
        ).fetchone()
        if row:
            state = {
                'type': 'state', 'content': row['content'],
                'thinking': row['thinking'], 'status': row['status'],
            }
            if row['error']:
                state['error'] = row['error']
            if row['tool_rounds']:
                try:
                    state['toolRounds'] = json.loads(row['tool_rounds'])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning('[Chat] Failed to parse tool_rounds for task %s: %s', task_id, e, exc_info=True)
            meta = _extract_db_meta(row)
            for key in ('finishReason', 'usage', 'preset', 'model', 'thinkingDepth'):
                if meta.get(key):
                    state[key] = meta[key]
            done_evt = {'type': 'done'}
            for key in ('finishReason', 'usage', 'preset', 'toolSummary', 'model', 'thinkingDepth'):
                if meta.get(key):
                    done_evt[key] = meta[key]
            if meta.get('fallbackModel'):
                done_evt['fallbackModel'] = meta['fallbackModel']
                done_evt['fallbackFrom'] = meta.get('fallbackFrom', '')
            if row['error']:
                done_evt['error'] = row['error']

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
        return jsonify({'error': 'Task not found'}), 404

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
    def generate():
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
                # ★ Endpoint mode: include phase and completed turns for reconnection
                if task.get('endpoint_mode'):
                    state['endpointMode'] = True
                    state['endpointPhase'] = task.get('_endpoint_phase', 'planning')
                    state['endpointIteration'] = task.get('_endpoint_iteration', 0)
                    ep_turns = task.get('_endpoint_turns')
                    if ep_turns:
                        state['endpointTurns'] = ep_turns
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
                    logger.info('[Chat] SSE stream %s finished normally — %d events sent in %.1fs '
                               'finishReason=%s error=%s',
                               task_id[:8], _events_sent, time.time() - _stream_start,
                               _done_fr, _done_err or 'none')
                    return
            if task['status'] != 'running' and not new_evts:
                late_done = {'type': 'done'}
                late_meta = _extract_task_meta(task)
                late_done.update(late_meta)
                if task['error']:
                    late_done['error'] = task['error']
                logger.warning('[Chat] SSE stream %s emitting LATE done (task finished but no done event in queue) — '
                             'finishReason=%s model=%s error=%s',
                             task_id[:8], late_meta.get('finishReason', '?'),
                             late_meta.get('model', '?'), task['error'] or 'none')
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
            time.sleep(0.05)

    def generate_with_disconnect_log():
        """Wrap generate() to detect client disconnect (SSE premature close)."""
        done_sent = False
        try:
            for chunk in generate():
                if '"type"' in chunk and ('"type": "done"' in chunk or '"type":"done"' in chunk):
                    done_sent = True
                yield chunk
        except GeneratorExit:
            logger.debug('[Chat] SSE stream closed by client (GeneratorExit)', exc_info=True)
        finally:
            elapsed = time.time() - _stream_start
            content_len = len(task.get('content') or '')
            _fr = task.get('finishReason') or '?'
            _model = task.get('model') or '?'
            _provider = task.get('provider_id') or '?'
            _err = task.get('error')
            if not done_sent:
                logger.warning('[Chat] SSE stream %s DISCONNECTED PREMATURELY — '
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

    return Response(generate_with_disconnect_log(), mimetype='text/event-stream', headers={
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })


@chat_bp.route('/api/chat/abort-conv/<conv_id>', methods=['POST'])
def chat_abort_conv(conv_id):
    """Abort all running tasks for a conversation by conv ID.

    Used when the frontend aborts during translation and never received a
    taskId — the server may have already started a task that needs to be
    killed.  This is the convId-based counterpart of ``/api/chat/abort/<task_id>``.
    """
    from lib.tasks_pkg import abort_running_tasks_for_conv
    aborted = abort_running_tasks_for_conv(conv_id)
    if aborted:
        logger.info('[Chat] Abort-by-conv conv=%s — aborted %d task(s)', conv_id[:8], aborted)
    else:
        logger.debug('[Chat] Abort-by-conv conv=%s — no running tasks found', conv_id[:8])
    return jsonify({'ok': True, 'aborted': aborted})


@chat_bp.route('/api/chat/abort/<task_id>', methods=['POST'])
def chat_abort(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Not found'}), 404
    was_already_aborted = task.get('aborted', False)
    task['aborted'] = True
    task['_abort_timestamp'] = time.time()
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
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/poll/<task_id>', methods=['GET'])
def chat_poll(task_id):
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
        for key in ('error', 'toolRounds', 'finishReason', 'usage', 'preset',
                     'toolSummary', 'phase', 'modifiedFiles', 'modifiedFileList',
                     'model', 'thinkingDepth', 'apiRounds'):
            if task.get(key):
                r[key] = task[key]
        if task.get('id'):
            r['taskId'] = task['id']
        if task.get('_fallback_model'):
            r['fallbackModel'] = task['_fallback_model']
        if task.get('_fallback_from'):
            r['fallbackFrom'] = task['_fallback_from']
        # ★ emit_to_user: include emitted tool content so poll fallback
        #   and Case B recovery can display inline emit blocks.
        if task.get('_emitContent'):
            r['emitContent'] = task['_emitContent']
        if task.get('_emitToolName'):
            r['emitToolName'] = task['_emitToolName']
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
            r['error'] = row['error']
        if row['tool_rounds']:
            try:
                r['toolRounds'] = json.loads(row['tool_rounds'])
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Chat] Failed to parse tool_rounds in poll for task %s: %s', task_id, e, exc_info=True)
        for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                     'model', 'thinkingDepth', 'apiRounds',
                     'modifiedFiles', 'modifiedFileList'):
            if _db_meta.get(key):
                r[key] = _db_meta[key]
        if _db_meta.get('fallbackModel'):
            r['fallbackModel'] = _db_meta['fallbackModel']
            r['fallbackFrom'] = _db_meta.get('fallbackFrom', '')
        # ★ emit_to_user data is not stored in task_results metadata —
        #   it's persisted directly into conversation messages by
        #   _sync_result_to_conversation. For DB-sourced polls, the
        #   frontend recovers it from loadConversationMessages on page load.
        return jsonify(r)

    logger.warning('[Chat] Poll %s — NOT FOUND in memory or DB! Task may have been cleaned up. '
                   'Client will receive 404 and may lose accumulated content.',
                   task_id[:8])
    return jsonify({'error': 'Task not found'}), 404


@chat_bp.route('/api/chat/stdin_response', methods=['POST'])
def chat_stdin_response():
    """Provide stdin input to a subprocess waiting for user input.

    Body: { "stdinId": "stdin_...", "input": "user's text", "eof": false }
    If ``eof`` is true, stdin is closed (no input is sent).
    """
    data = request.get_json(silent=True) or {}
    stdin_id = data.get('stdinId', '')
    is_eof = data.get('eof', False)
    input_text = data.get('input', '')
    logger.info('[Stdin] /api/chat/stdin_response received: '
                'stdinId=%s, eof=%s, input_len=%d',
                stdin_id, is_eof, len(input_text))
    if not stdin_id:
        logger.warning('[Stdin] Rejected — missing stdinId')
        return jsonify({'error': 'No stdinId'}), 400

    from lib.tasks_pkg import resolve_stdin
    # EOF → resolve with None to signal stdin close
    resolved_text = None if is_eof else input_text
    try:
        ok = resolve_stdin(stdin_id, resolved_text)
    except Exception as e:
        logger.error('[Stdin] Exception resolving %s: %s',
                     stdin_id, e, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    if not ok:
        logger.warning('[Stdin] Request not found or expired: stdinId=%s',
                       stdin_id)
        return jsonify({'error': 'Stdin request not found or expired'}), 404
    logger.info('[Stdin] Successfully resolved %s', stdin_id)
    return jsonify({'ok': True, 'stdinId': stdin_id})


@chat_bp.route('/api/chat/human_response', methods=['POST'])
def chat_human_response():
    """Resolve a human guidance request — the user has answered a question.

    Body: { "guidanceId": "hg_...", "response": "user's answer text" }
    """
    data = request.get_json(silent=True) or {}
    guidance_id = data.get('guidanceId', '')
    response_text = data.get('response', '')
    logger.info('[HumanGuidance] /api/chat/human_response received: '
                'guidanceId=%s, response_len=%d',
                guidance_id, len(response_text))
    if not guidance_id:
        logger.warning('[HumanGuidance] Rejected — missing guidanceId')
        return jsonify({'error': 'No guidanceId'}), 400
    if not response_text:
        logger.warning('[HumanGuidance] Rejected — empty response for '
                       'guidanceId=%s', guidance_id)
        return jsonify({'error': 'No response text'}), 400

    from lib.tasks_pkg import resolve_human_guidance
    try:
        ok = resolve_human_guidance(guidance_id, response_text)
    except Exception as e:
        logger.error('[HumanGuidance] Exception resolving %s: %s',
                     guidance_id, e, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    if not ok:
        logger.warning('[HumanGuidance] Guidance request not found or '
                       'expired: guidanceId=%s', guidance_id)
        return jsonify({'error': 'Guidance request not found or expired'}), 404
    logger.info('[HumanGuidance] Successfully resolved %s (response_len=%d)',
                guidance_id, len(response_text))
    return jsonify({'ok': True, 'guidanceId': guidance_id})
