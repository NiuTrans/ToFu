"""Task lifecycle management — creation, events, persistence, cleanup, streaming."""

import json
import re
import threading
import time
import uuid
from datetime import datetime

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db, json_dumps_pg
from lib.llm_dispatch import dispatch_stream
from lib.log import get_logger

logger = get_logger(__name__)

tasks = {}
tasks_lock = threading.Lock()

def create_task(conv_id, messages, config):
    task_id = str(uuid.uuid4())
    # ── Extract the user's original question from the last user message ──
    # This is passed to the content filter alongside the search query so
    # the filter can assess relevance against the ORIGINAL intent, not just
    # the model-generated search keywords.
    last_user_query = ''
    for m in reversed(messages or []):
        if m.get('role') == 'user':
            c = m.get('content', '')
            if isinstance(c, list):
                # multimodal: extract text blocks
                c = ' '.join(b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text')
            last_user_query = (c or '')[:500]
            break
    task = {
        'id': task_id, 'convId': conv_id, 'messages': messages, 'config': config,
        'status': 'running', 'content': '', 'thinking': '', 'error': None,
        'aborted': False, 'searchRounds': [], 'events': [],
        'events_lock': threading.Lock(), 'content_lock': threading.Lock(),
        'created_at': time.time(),
        'finishReason': None, 'usage': None, 'toolSummary': None,
        'phase': None,  # ★ Current phase for polling fallback
        'lastUserQuery': last_user_query,  # ★ Original user question for content filter relevance
        '_initial_msg_count': len(messages or []),  # ★ For cross-talk detection in _sync_result_to_conversation
    }
    with tasks_lock:
        tasks[task_id] = task
    logger.info('[Task %s] Created for conv=%s lastUserQuery=%r', task_id[:8], conv_id, last_user_query[:80])
    return task

def _strip_base64_for_snapshot(messages):
    """Strip large base64 data from messages for debug snapshot (keep structure, save bandwidth)."""
    import re
    stripped = []
    for msg in messages:
        m = dict(msg)
        content = m.get('content')
        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'image_url':
                    url = block.get('image_url', {}).get('url', '')
                    size = len(url)
                    # Replace base64 data with placeholder showing size
                    new_blocks.append({'type': 'image_url', 'image_url': {'url': f'[base64 image, {size:,} chars]'}})
                else:
                    new_blocks.append(block)
            m['content'] = new_blocks
        elif isinstance(content, str) and len(content) > 100000:
            m['content'] = content[:1000] + f'\n... [{len(content):,} chars total]'
        # Strip tool call arguments that are too large (e.g. write_file content)
        if 'tool_calls' in m:
            new_tcs = []
            for tc in m['tool_calls']:
                tc2 = dict(tc)
                fn = tc2.get('function', {})
                args_str = fn.get('arguments', '')
                if isinstance(args_str, str) and len(args_str) > 50000:
                    fn2 = dict(fn)
                    fn2['arguments'] = args_str[:2000] + f'\n... [{len(args_str):,} chars total]'
                    tc2['function'] = fn2
                new_tcs.append(tc2)
            m['tool_calls'] = new_tcs
        stripped.append(m)
    return stripped

def append_event(task, event):
    with task['events_lock']:
        task['events'].append(event)
    # ★ Track phase in task for polling fallback
    if event.get('type') == 'phase':
        p = {'phase': event['phase'], 'detail': event.get('detail', '')}
        if event.get('toolContext'): p['toolContext'] = event['toolContext']
        if event.get('tools'): p['tools'] = event['tools']
        if event.get('round'): p['round'] = event['round']
        task['phase'] = p
    elif event.get('type') == 'delta':
        task['phase'] = None  # Clear phase when LLM starts producing tokens

def persist_task_result(task):
    content_len = len(task.get('content') or '')
    thinking_len = len(task.get('thinking') or '')
    error = task.get('error')
    status = task.get('status')
    task_id_short = task['id'][:8]
    conv_id_short = task.get('convId', '')

    finish_reason = task.get('finishReason') or 'unknown'
    model = task.get('model') or '?'
    provider = task.get('provider_id') or '?'

    # ★ Diagnostic: warn about suspiciously empty results
    if status == 'done' and content_len == 0 and thinking_len == 0 and not error and not task.get('aborted'):
        logger.warning('[Task %s] conv=%s ⚠️ PERSISTING EMPTY RESULT — task completed with no content, no thinking, no error. '
                       'finishReason=%s model=%s provider=%s. '
                       'This likely indicates a stream that never received LLM tokens.',
                       task_id_short, conv_id_short, finish_reason, model, provider)
    elif status == 'done' and content_len == 0 and thinking_len > 0:
        logger.warning('[Task %s] conv=%s ⚠️ PERSISTING THINKING-ONLY result — content is empty but thinking has %d chars. '
                       'finishReason=%s model=%s provider=%s. '
                       'The LLM may have been interrupted after thinking but before generating content.',
                       task_id_short, conv_id_short, thinking_len, finish_reason, model, provider)
    else:
        logger.info('[Task %s] conv=%s Persisting result: status=%s content=%dchars thinking=%dchars '
                    'finishReason=%s model=%s provider=%s error=%s',
                     task_id_short, conv_id_short, status, content_len, thinking_len,
                     finish_reason, model, provider, error or 'none')

    # Build meta BEFORE the try so it's always available for _sync_result_to_conversation
    meta = {}
    if task.get('finishReason'): meta['finishReason'] = task['finishReason']
    if task.get('usage'): meta['usage'] = task['usage']
    if task.get('preset'): meta['preset'] = task['preset']
    if task.get('toolSummary'): meta['toolSummary'] = task['toolSummary']
    if task.get('_fallback_model'): meta['fallbackModel'] = task['_fallback_model']; meta['fallbackFrom'] = task.get('_fallback_from', '')
    if task.get('model'): meta['model'] = task['model']
    if task.get('provider_id'): meta['provider_id'] = task['provider_id']
    if task.get('thinkingDepth'): meta['thinkingDepth'] = task['thinkingDepth']
    if task.get('apiRounds'): meta['apiRounds'] = task['apiRounds']
    if task.get('modifiedFiles'): meta['modifiedFiles'] = task['modifiedFiles']
    if task.get('modifiedFileList'): meta['modifiedFileList'] = task['modifiedFileList']

    try:
        db = get_thread_db(DOMAIN_CHAT)
        sr_json = json.dumps(task.get('searchRounds') or [], ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        db_execute_with_retry(db, '''INSERT OR REPLACE INTO task_results
            (task_id,conv_id,content,thinking,error,status,search_rounds,metadata,created_at,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (task['id'], task['convId'], task['content'], task['thinking'],
             task['error'], task['status'], sr_json, meta_json,
             int(task['created_at']*1000), int(time.time()*1000)))
        logger.debug('[Task %s] conv=%s Persisted to DB successfully', task_id_short, conv_id_short)
    except Exception:
        logger.error('[Task %s] conv=%s ❌ Persist FAILED — content (%d chars) and thinking (%d chars) may be lost!',
                     task_id_short, conv_id_short, content_len, thinking_len, exc_info=True)

    # ★ Write result back to conversation — ensures data survives even if
    #   no frontend client is connected (SSE closed, user closed tab, etc.)
    # For endpoint mode tasks, the multi-turn sync happens in endpoint.py
    # via _sync_endpoint_turns_to_conversation(). We still call the regular
    # sync as a fallback for the single-turn content + metadata.
    if not task.get('endpoint_mode') or not task.get('_endpoint_turns'):
        _sync_result_to_conversation(task, meta)
    else:
        logger.info('[Task %s] conv=%s Skipping single-turn sync — endpoint mode with %d turns '
                     '(already synced by endpoint loop)',
                     task['id'][:8], task.get('convId', ''), len(task.get('_endpoint_turns', [])))

    # ★ Update proactive scheduler task execution status
    _update_proactive_execution_status(task)


def _update_proactive_execution_status(task):
    """Update the proactive scheduler task's execution status when its agentic task completes."""
    task_id = task.get('id', '')
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        # Find any proactive task whose last_execution_task_id matches this task
        row = db.execute(
            'SELECT id FROM scheduled_tasks WHERE last_execution_task_id=? AND task_type=?',
            [task_id, 'agent']
        ).fetchone()
        if not row:
            return  # Not a proactive execution

        sched_id = row['id']
        status = task.get('status', 'done')
        exec_status = 'ok' if status == 'done' and not task.get('error') else 'error'
        now = datetime.now().isoformat()

        db.execute(
            'UPDATE scheduled_tasks SET last_execution_status=?, updated_at=? WHERE id=?',
            [exec_status, now, sched_id]
        )
        db.commit()
        logger.info('[Proactive:%s] Execution %s completed with status=%s',
                    sched_id[:8], task_id[:8], exec_status)
    except Exception as e:
        logger.warning('[Proactive] Failed to update execution status for task %s: %s',
                       task_id[:8], e, exc_info=True)


def _sync_result_to_conversation(task, meta):
    """Write the completed task result into the conversation's messages in the DB.

    Finds or creates the last assistant message and fills in content, thinking,
    searchRounds, finishReason, etc.  This makes the backend self-sufficient —
    even if no frontend client receives the 'done' SSE event, the conversation
    is updated.

    Runs in a separate try/except so failures don't affect task_results persistence.
    """
    conv_id = task.get('convId', '')
    task_id_short = task['id'][:8]
    pfx = f'[SyncConv {task_id_short}]'

    content = task.get('content') or ''
    thinking = task.get('thinking') or ''
    error = task.get('error')

    # Skip if there's truly nothing to write (e.g. aborted before any tokens)
    if not content and not thinking and not error:
        logger.debug('%s conv=%s Skipping conv sync — no content/thinking/error to write', pfx, conv_id)
        return

    db = None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()

        if not row:
            logger.warning('%s conv=%s Conversation not found in DB — cannot sync result back', pfx, conv_id)
            return

        # Capture updated_at for optimistic locking (CAS guard)
        _row_updated_at = row['updated_at'] if hasattr(row, 'get') else row[1]
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError):
            logger.error('%s conv=%s Failed to parse existing messages JSON', pfx, conv_id, exc_info=True)
            return

        if not messages:
            logger.warning('%s conv=%s Conversation has 0 messages — cannot sync result back', pfx, conv_id)
            return

        # ── CROSS-TALK DETECTION: verify message count consistency ──
        # If the DB has significantly more messages than expected, it may indicate
        # that the frontend injected messages from another conversation's stream.
        expected_msg_count = task.get('_initial_msg_count')
        if expected_msg_count is not None and len(messages) > expected_msg_count + 2:
            logger.error(
                '%s conv=%s ⛔ MESSAGE COUNT ANOMALY: DB has %d messages but task started '
                'with %d — %d extra messages may indicate cross-talk injection! '
                'Extra msgs: %s',
                pfx, conv_id, len(messages), expected_msg_count,
                len(messages) - expected_msg_count,
                [(m.get('role'), len(m.get('content') or ''), m.get('model', 'N/A'))
                 for m in messages[expected_msg_count:]]
            )

        # Find the last assistant message to fill in
        last_msg = messages[-1]

        if last_msg.get('role') != 'assistant':
            # No trailing assistant message — append one
            logger.info('%s conv=%s Last message is role=%s, appending new assistant message',
                       pfx, conv_id, last_msg.get('role'))
            last_msg = {'role': 'assistant', 'content': '', 'thinking': ''}
            messages.append(last_msg)

        # ── Guard: don't overwrite with LESS content ──
        # The frontend may have already synced a fuller version via PUT
        existing_content_len = len(last_msg.get('content') or '')
        existing_thinking_len = len(last_msg.get('thinking') or '')
        new_content_len = len(content)
        new_thinking_len = len(thinking)

        if existing_content_len > new_content_len and existing_thinking_len > new_thinking_len:
            logger.info('%s conv=%s Server already has MORE content (existing=%d+%d > new=%d+%d) — '
                       'frontend likely already synced. Skipping.',
                       pfx, conv_id, existing_content_len, existing_thinking_len,
                       new_content_len, new_thinking_len)
            return

        # ── Fill in the assistant message ──
        if content:
            last_msg['content'] = content
        if thinking:
            last_msg['thinking'] = thinking
        if error:
            last_msg['error'] = error

        # Copy metadata fields that the frontend would normally set
        search_rounds = task.get('searchRounds')
        if search_rounds:
            last_msg['searchRounds'] = search_rounds
        if meta.get('finishReason'):
            last_msg['finishReason'] = meta['finishReason']
        if meta.get('usage'):
            last_msg['usage'] = meta['usage']
        if meta.get('preset'):
            last_msg['preset'] = meta['preset']
        if meta.get('toolSummary'):
            last_msg['toolSummary'] = meta['toolSummary']
        if meta.get('model'):
            last_msg['model'] = meta['model']
        if meta.get('fallbackModel'):
            last_msg['fallbackModel'] = meta['fallbackModel']
            last_msg['fallbackFrom'] = meta.get('fallbackFrom', '')
        if meta.get('apiRounds'):
            last_msg['apiRounds'] = meta['apiRounds']
        if meta.get('modifiedFiles'):
            last_msg['modifiedFiles'] = meta['modifiedFiles']
        if meta.get('modifiedFileList'):
            last_msg['modifiedFileList'] = meta['modifiedFileList']

        # Serialize and write back — json_dumps_pg strips null bytes from
        # raw data AND removes \u0000 escapes from the JSON text.
        messages_json = json_dumps_pg(messages)
        now_ms = int(time.time() * 1000)

        # ── Also clear activeTaskId from settings so subsequent reloads
        #    don't re-trigger Case B recovery for an already-synced task ──
        settings_row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        settings_json = None
        if settings_row:
            try:
                s = json.loads(settings_row[0] or '{}')
                changed = False
                if s.get('activeTaskId'):
                    s['activeTaskId'] = None
                    changed = True
                # ★ Update lastMsgRole/lastMsgTimestamp so metadata shells
                # reflect the new last message (assistant, not user) for Case E
                if messages:
                    lm = messages[-1]
                    if s.get('lastMsgRole') != lm.get('role') or s.get('lastMsgTimestamp') != lm.get('timestamp'):
                        s['lastMsgRole'] = lm.get('role')
                        s['lastMsgTimestamp'] = lm.get('timestamp')
                        changed = True
                if changed:
                    settings_json = json.dumps(s, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Task] Failed to parse/clear activeTaskId from settings for conv=%s: %s', conv_id, e, exc_info=True)

        # ── Optimistic lock: only update if no concurrent write occurred ──
        # Use updated_at as CAS guard to prevent overwriting a fresher
        # frontend sync.  If the row was updated since our SELECT, our
        # read-modify-write would clobber the frontend's data.
        # ── Also update search_text for fast conversation search ──
        from routes.conversations import build_search_text
        search_text = build_search_text(messages)
        if settings_json:
            db_execute_with_retry(
                db,
                '''UPDATE conversations
                   SET messages=?, updated_at=?, msg_count=?, settings=?, search_text=?,
                       search_tsv=to_tsvector('simple', left(?, 50000))
                   WHERE id=? AND user_id=1 AND updated_at=?''',
                (messages_json, now_ms, len(messages), settings_json, search_text, search_text, conv_id, _row_updated_at)
            )
        else:
            db_execute_with_retry(
                db,
                '''UPDATE conversations
                   SET messages=?, updated_at=?, msg_count=?, search_text=?,
                       search_tsv=to_tsvector('simple', left(?, 50000))
                   WHERE id=? AND user_id=1 AND updated_at=?''',
                (messages_json, now_ms, len(messages), search_text, search_text, conv_id, _row_updated_at)
            )
        # Check if optimistic lock succeeded (row was updated)
        # db_execute_with_retry returns None but the last execute sets rowcount
        _check_row = db.execute(
            'SELECT updated_at FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        _actual_ts = _check_row[0] if _check_row else None
        _cas_succeeded = (_actual_ts == now_ms)
        if not _cas_succeeded:
            logger.info('%s conv=%s Optimistic lock missed — row was updated concurrently '
                       '(read_ts=%s, expected=%s, actual=%s). '
                       'Frontend likely synced first; backend sync skipped (safe).',
                       pfx, conv_id, _row_updated_at, now_ms, _actual_ts)
        else:
            logger.info('%s conv=%s ✅ Synced result to conversation — content=%dchars thinking=%dchars '
                        'msgs=%d (was: content=%d thinking=%d)',
                        pfx, conv_id, new_content_len, new_thinking_len, len(messages),
                        existing_content_len, existing_thinking_len)

        # ── Invalidate meta cache so subsequent GET /api/conversations
        #    returns the cleared activeTaskId immediately ──
        try:
            from routes.common import _invalidate_meta_cache
            _invalidate_meta_cache()
        except Exception as e:
            logger.debug('[Manager] meta cache invalidation skipped: %s', e)

        # ── Auto-translate: server-side safety net for translation ──
        # Ensures translation happens even if the frontend is offline / switched away.
        # Skip if optimistic lock missed (frontend already has the data).
        if _cas_succeeded and content and not error:
            try:
                _maybe_auto_translate_assistant(conv_id, content, len(messages) - 1, db)
            except Exception as te:
                logger.warning('%s conv=%s Auto-translate trigger failed (non-fatal): %s',
                               pfx, conv_id, te)

    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to sync result to conversation: %s',
                     pfx, conv_id, e, exc_info=True)


def _needs_translation(text):
    """Heuristic: return True if text is predominantly English and needs Chinese translation.

    Mirrors the frontend logic in finishStream: strip code blocks, count Latin words
    vs Chinese characters.
    """
    # Strip code blocks and inline code
    plain = re.sub(r'```[\s\S]*?```', '', text)
    plain = re.sub(r'`[^`]+`', '', plain)
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', plain))
    latin_words = len(re.findall(r'[a-zA-Z]{2,}', plain))
    return latin_words >= 3 and chinese_chars < latin_words * 2


def _maybe_auto_translate_assistant(conv_id, content, msg_idx, db):
    """Automatically translate the assistant's response on the server side.

    Called from _sync_result_to_conversation after the assistant content is persisted.
    This is the server-side safety net — ensures translation happens even if the
    frontend is offline, switched away, or the SSE stream closed prematurely.

    Respects the per-conversation autoTranslate setting (frozen at send-time by
    the frontend — won't be overwritten while a task is active).
    """
    pfx = '[AutoTranslate]'
    try:
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return

        # ── Check autoTranslate setting (default true, matching frontend behavior) ──
        settings = json.loads(row[1] or '{}') if row[1] else {}
        auto_translate = settings.get('autoTranslate', True)
        if not auto_translate:
            logger.debug('%s conv=%s autoTranslate is OFF — skipping',
                         pfx, conv_id[:8])
            return

        # Check if translation already exists (frontend may have triggered it first)
        messages = json.loads(row[0] or '[]')
        if msg_idx < len(messages):
            existing_tc = messages[msg_idx].get('translatedContent')
            if existing_tc and len(existing_tc) > 0:
                # ★ FIX: detect stale partial translations — if the existing translation
                # is less than 15% of the content length, it was translated from partial
                # content (e.g. mid-stream) and needs re-translation with the full content.
                content_len = len(content)
                tc_len = len(existing_tc)
                if content_len > 0 and tc_len < content_len * 0.15:
                    logger.info('%s conv=%s msg=%d stale translatedContent detected: '
                                'tc=%d chars vs content=%d chars (%.1f%%) — re-translating',
                                pfx, conv_id[:8], msg_idx, tc_len, content_len,
                                tc_len / content_len * 100)
                    # Clear the stale translation so we re-translate
                    messages[msg_idx].pop('translatedContent', None)
                    messages[msg_idx].pop('_translateDone', None)
                    messages[msg_idx].pop('_translateTaskId', None)
                    messages[msg_idx].pop('_translatedCache', None)
                    # Persist the cleared state
                    try:
                        db_execute_with_retry(
                            db,
                            'UPDATE conversations SET messages=? WHERE id=? AND user_id=1',
                            (json_dumps_pg(messages), conv_id)
                        )
                    except Exception as ce:
                        logger.warning('%s conv=%s Failed to clear stale translation: %s',
                                       pfx, conv_id[:8], ce)
                else:
                    logger.debug('%s conv=%s msg=%d already has translatedContent (%d chars) — skipping',
                                 pfx, conv_id[:8], msg_idx, len(existing_tc))
                    return

        # ★ FIX: When autoTranslate is explicitly ON, always translate — don't
        # rely on the language heuristic which fails for bilingual/mixed responses
        # (e.g. when the LLM responds with an English intro + Chinese body).
        # The heuristic is only a fallback when autoTranslate state is unknown.
        # Since we already checked auto_translate=True above, skip the heuristic.
        # The _needs_translation() function is kept for external callers but
        # no longer gates auto-translation when autoTranslate is explicitly on.
        logger.debug('%s conv=%s msg=%d autoTranslate is ON — translating regardless of content language',
                     pfx, conv_id[:8], msg_idx)

        # ── Check for already-running translate task from the frontend ──
        # Import lazily to avoid circular imports
        from routes.translate import _translate_tasks, _translate_tasks_lock
        with _translate_tasks_lock:
            for tid, tt in _translate_tasks.items():
                if (tt.get('convId') == conv_id and
                    tt.get('msgIdx') == msg_idx and
                    tt.get('field') == 'translatedContent' and
                    tt['status'] == 'running'):
                    logger.info('%s conv=%s msg=%d Frontend already started translate task %s — skipping',
                                pfx, conv_id[:8], msg_idx, tid)
                    return

        # ── Start background translation thread ──
        logger.info('%s conv=%s msg=%d Starting server-side auto-translation (%d chars)',
                    pfx, conv_id[:8], msg_idx, len(content))

        def _run_translate():
            try:
                from routes.translate import _do_translate, _translate_tasks, _translate_tasks_lock
                task_id = str(uuid.uuid4())[:12]
                task = {
                    'id': task_id,
                    'status': 'running',
                    'result': None,
                    'error': None,
                    'model': None,
                    'progress': None,
                    'convId': conv_id,
                    'msgIdx': msg_idx,
                    'field': 'translatedContent',
                    'targetLang': 'Chinese',
                    'textLen': len(content),
                    'created_at': time.time(),
                    'completed_at': None,
                }
                with _translate_tasks_lock:
                    _translate_tasks[task_id] = task
                logger.info('%s task=%s conv=%s Translate thread started', pfx, task_id, conv_id[:8])
                _do_translate(task_id, content, 'Chinese', 'English', conv_id, msg_idx, 'translatedContent')
            except Exception as e:
                logger.error('%s conv=%s Translate thread failed: %s', pfx, conv_id[:8], e, exc_info=True)

        threading.Thread(target=_run_translate, daemon=True,
                         name=f'auto-translate-{conv_id[:8]}').start()

    except Exception as e:
        logger.warning('%s conv=%s Failed to check/start auto-translate: %s',
                       pfx, conv_id[:8], e)


def checkpoint_task_partial(task):
    """Persist the current in-flight task state to DB so it survives a server crash.

    Called after each tool-execution round in the orchestrator loop.
    Writes to both task_results (for poll recovery) and the conversation
    (for direct page-reload recovery).

    Uses status='running' so the frontend can distinguish a partial checkpoint
    from a final result (status='done'|'error').
    """
    content_len = len(task.get('content') or '')
    thinking_len = len(task.get('thinking') or '')
    task_id_short = task['id'][:8]
    conv_id = task.get('convId', '')

    # Don't bother checkpointing if there's nothing meaningful yet
    if content_len == 0 and thinking_len == 0:
        return

    try:
        db = get_thread_db(DOMAIN_CHAT)
        sr_json = json.dumps(task.get('searchRounds') or [], ensure_ascii=False)
        meta = {}
        if task.get('model'): meta['model'] = task['model']
        if task.get('preset'): meta['preset'] = task['preset']
        if task.get('thinkingDepth'): meta['thinkingDepth'] = task['thinkingDepth']
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        db_execute_with_retry(db, '''INSERT OR REPLACE INTO task_results
            (task_id,conv_id,content,thinking,error,status,search_rounds,metadata,created_at,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (task['id'], conv_id, task.get('content') or '', task.get('thinking') or '',
             task.get('error'), 'running', sr_json, meta_json,
             int(task['created_at']*1000), int(time.time()*1000)))
        logger.debug('[Checkpoint %s] conv=%s Saved partial: content=%dchars thinking=%dchars',
                     task_id_short, conv_id, content_len, thinking_len)
    except Exception as e:
        logger.warning('[Checkpoint %s] conv=%s Failed to checkpoint: %s',
                       task_id_short, conv_id, e, exc_info=True)

    # Also sync partial content into the conversation's messages in DB
    # For endpoint mode, skip — endpoint.py handles multi-turn sync
    if not task.get('endpoint_mode'):
        _sync_partial_to_conversation(task)

    # ── CROSS-TALK DETECTION: log when multiple tasks are being checkpointed concurrently ──
    with tasks_lock:
        running_tasks = [(tid[:8], t.get('convId', '')[:8])
                         for tid, t in tasks.items()
                         if t.get('status') == 'running' and tid != task['id']]
    if running_tasks:
        logger.debug(
            '[Checkpoint %s] conv=%s ⚠️ %d other running task(s): %s — '
            'concurrent streams increase cross-talk risk on frontend',
            task_id_short, conv_id, len(running_tasks),
            running_tasks
        )


def _sync_partial_to_conversation(task):
    """Write partial streaming content into the conversation's last assistant message.

    Similar to _sync_result_to_conversation but lighter — only updates content
    and thinking, doesn't touch metadata/finishReason/usage since they're not
    final yet.
    """
    conv_id = task.get('convId', '')
    content = task.get('content') or ''
    thinking = task.get('thinking') or ''
    if not content and not thinking:
        return

    db = None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return

        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning('[Manager] Unparseable conversation messages for conv=%s: %s', conv_id, exc, exc_info=True)
            return

        if not messages:
            return

        last_msg = messages[-1]
        if last_msg.get('role') != 'assistant':
            last_msg = {'role': 'assistant', 'content': '', 'thinking': ''}
            messages.append(last_msg)

        # Only update if we have MORE content than what's already in DB
        existing_content_len = len(last_msg.get('content') or '')
        existing_thinking_len = len(last_msg.get('thinking') or '')
        if len(content) <= existing_content_len and len(thinking) <= existing_thinking_len:
            return  # DB already has equal or more — skip

        if content and len(content) > existing_content_len:
            last_msg['content'] = content
        if thinking and len(thinking) > existing_thinking_len:
            last_msg['thinking'] = thinking

        # Also write searchRounds if available
        search_rounds = task.get('searchRounds')
        if search_rounds:
            last_msg['searchRounds'] = search_rounds

        from routes.conversations import build_search_text
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(
            db,
            '''UPDATE conversations SET messages=?, updated_at=?, msg_count=?, search_text=?,
                   search_tsv=to_tsvector('simple', left(?, 50000))
               WHERE id=? AND user_id=1''',
            (messages_json, now_ms, len(messages), search_text, search_text, conv_id)
        )
        logger.debug('[Checkpoint] conv=%s Synced partial to conversation: content=%d→%d thinking=%d→%d',
                     conv_id, existing_content_len, len(content),
                     existing_thinking_len, len(thinking))
    except Exception as e:
        logger.debug('[Checkpoint] conv=%s Failed to sync partial to conversation: %s',
                     conv_id, e, exc_info=True)


def cleanup_old_tasks():
    now = time.time()
    with tasks_lock:
        to_rm = [tid for tid, t in tasks.items() if now - t['created_at'] > 3600 and t['status'] != 'running']
        for tid in to_rm: del tasks[tid]

# ── Streaming checkpoint interval (seconds) ──
# During LLM token streaming, we periodically persist partial content to
# the DB so data survives server crashes even when there are no tool rounds.
_STREAM_CHECKPOINT_INTERVAL = 5

def stream_llm_response(task, body, tag='', on_tool_call_ready=None):
    """Stream an LLM response, wiring deltas into the task's event system.

    Delegates all key selection, retry, 429/401/403 failover to the
    central ``dispatch_stream`` — no duplicate logic needed here.

    Args:
        on_tool_call_ready: callback(tool_call_dict) — fired as each tool
            call's arguments finish streaming.  The orchestrator uses this
            to start executing read-only tools while the model is still
            generating the next tool call (streaming tool execution).

    ★ Crash-recovery: periodically checkpoints to DB every ~5s during
    streaming so that even pure-LLM responses (no tool calls) survive
    a server crash with minimal data loss.
    """
    pfx = f'[Task {task["id"][:8]}][{tag}]'
    model = body.get('model', '?')
    _last_stream_ckpt = time.time()

    def _maybe_checkpoint_during_stream():
        """Called on every content/thinking delta — checkpoint if interval elapsed."""
        nonlocal _last_stream_ckpt
        now = time.time()
        if now - _last_stream_ckpt >= _STREAM_CHECKPOINT_INTERVAL:
            _last_stream_ckpt = now
            try:
                checkpoint_task_partial(task)
            except Exception as e:
                logger.debug('%s streaming checkpoint failed (non-fatal): %s', pfx, e)

    def _on_thinking(td):
        with task['content_lock']:
            task['thinking'] += td
        append_event(task, {'type': 'delta', 'thinking': td})
        _maybe_checkpoint_during_stream()

    def _on_content(cd):
        with task['content_lock']:
            task['content'] += cd
        append_event(task, {'type': 'delta', 'content': cd})
        _maybe_checkpoint_during_stream()

    def _on_retry(attempt, reason='', status_code=0):
        """Emit SSE phase event so user sees retry status instead of 'Waiting…'."""
        detail = f'Retrying… {reason}' if reason else 'Retrying…'
        append_event(task, {
            'type': 'phase',
            'phase': 'retrying',
            'detail': detail,
            'attempt': attempt,
            'statusCode': status_code,
        })

    msg, finish_reason, usage = dispatch_stream(
        body,
        on_thinking=_on_thinking,
        on_content=_on_content,
        on_tool_call_ready=on_tool_call_ready,
        abort_check=lambda: task['aborted'],
        prefer_model=model,
        log_prefix=pfx,
        # ★ User-facing request: the user explicitly chose this model in
        #   the frontend preset selector.  429 retries must stay within
        #   this model's slots (different keys / alias group) — never
        #   silently fall back to a cheaper/different model.
        strict_model=True,
        on_retry=_on_retry,
    )

    # ★ Propagate provider_id from dispatch metadata into task
    _dispatch = (usage or {}).get('_dispatch', {})
    if _dispatch.get('provider_id'):
        task['provider_id'] = _dispatch['provider_id']

    # ★ Notify user if a model token limit was auto-learned during this request
    _limit_info = (usage or {}).get('_model_limit_learned')
    if _limit_info:
        _lim_notice = (
            f'\n\n> ⚙️ **Auto-detected model limit**: `{_limit_info["model"]}` '
            f'max output tokens = **{_limit_info["new_limit"]:,}** '
            f'(requested {_limit_info["old_limit"]:,}). '
            f'Saved permanently for future requests.\n\n'
        )
        with task['content_lock']:
            task['content'] += _lim_notice
        append_event(task, {'type': 'delta', 'content': _lim_notice})
        logger.info('%s ⚙️ Model limit auto-learned and user notified: %s max_tokens=%d',
                    pfx, _limit_info['model'], _limit_info['new_limit'])

    _content_len = len(msg.get('content', '') or '')
    _thinking_len = len(msg.get('reasoning_content', '') or '')
    _tool_calls = len(msg.get('tool_calls', []))
    _provider = task.get('provider_id', '?')
    logger.info('%s conv=%s stream_llm_response complete: finish_reason=%s model=%s '
                'provider=%s content=%dchars thinking=%dchars tool_calls=%d',
                pfx, task.get('convId', ''), finish_reason, model,
                _provider, _content_len, _thinking_len, _tool_calls)

    return msg, finish_reason, usage
