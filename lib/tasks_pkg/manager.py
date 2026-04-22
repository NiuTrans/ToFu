"""Task lifecycle management — creation, events, persistence, cleanup, streaming."""

import json
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

# ── Conversation → latest task_id mapping for freshness guard ──
# When a new task starts for a conv, the old task becomes stale and its
# _sync_result_to_conversation writes should be rejected.
_conv_latest_task = {}   # conv_id → task_id
_conv_latest_task_lock = threading.Lock()

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
        'aborted': False, 'toolRounds': [], 'events': [],
        'events_lock': threading.Lock(), 'content_lock': threading.Lock(),
        'created_at': time.time(),
        'finishReason': None, 'usage': None, 'toolSummary': None,
        'phase': None,  # ★ Current phase for polling fallback
        'lastUserQuery': last_user_query,  # ★ Original user question for content filter relevance
        '_initial_msg_count': len(messages or []),  # ★ For cross-talk detection in _sync_result_to_conversation
    }
    with tasks_lock:
        tasks[task_id] = task
    # ★ Register as the LATEST task for this conversation — freshness guard
    if conv_id:
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task_id
    logger.info('[Task %s] Created for conv=%s lastUserQuery=%r', task_id[:8], conv_id, last_user_query[:80])
    return task

def abort_running_tasks_for_conv(conv_id: str, exclude_task_id: str | None = None) -> int:
    """Abort all running tasks for a conversation, except the excluded one.

    Called when starting a new task (send/regenerate/edit) to ensure the old
    task stops writing to the conversation DB. Returns the count of aborted tasks.

    This is the **critical fix** for the stale-task-overwrites-regeneration bug:
    without this, the old task's _sync_result_to_conversation races with the
    new task and may overwrite the conversation with stale content.
    """
    aborted = 0
    with tasks_lock:
        for tid, t in tasks.items():
            if (t.get('convId') == conv_id
                    and t['status'] == 'running'
                    and tid != exclude_task_id
                    and not t.get('aborted')):
                t['aborted'] = True
                t['_abort_timestamp'] = time.time()
                t['_abort_reason'] = 'superseded_by_new_task'
                aborted += 1
                logger.info(
                    '[Task %s] conv=%s ⚠️ AUTO-ABORTED: superseded by new task %s — '
                    'content=%dchars elapsed=%.1fs',
                    tid[:8], conv_id[:8],
                    (exclude_task_id or '?')[:8],
                    len(t.get('content') or ''),
                    time.time() - t.get('created_at', time.time()),
                )
                try:
                    from lib.log import audit_log as _audit
                    _audit('task_abort',
                           task_id=tid,
                           conv_id=conv_id,
                           reason='superseded_by_new_task',
                           superseding_task_id=exclude_task_id or '',
                           content_chars=len(t.get('content') or ''),
                           elapsed_s=round(time.time() - t.get('created_at', time.time()), 2))
                except Exception as _aerr:
                    logger.debug('[Manager] audit_log task_abort failed: %s', _aerr)
    if aborted:
        logger.info('[Manager] conv=%s Auto-aborted %d stale task(s) before starting new task %s',
                    conv_id[:8], aborted, (exclude_task_id or '?')[:8])
    return aborted


def _strip_base64_for_snapshot(messages):
    """Strip large base64 data from messages for debug snapshot (keep structure, save bandwidth)."""
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

    # ★ Merge checkpoint toolRounds for DB persistence (continue flow)
    _merged_tr = list(task.get('_checkpointToolRounds') or []) + (task.get('toolRounds') or [])

    try:
        db = get_thread_db(DOMAIN_CHAT)
        tr_json = json.dumps(_merged_tr, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        db_execute_with_retry(db, '''INSERT OR REPLACE INTO task_results
            (task_id,conv_id,content,thinking,error,status,tool_rounds,metadata,created_at,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (task['id'], task['convId'], task['content'], task['thinking'],
             task['error'], task['status'], tr_json, meta_json,
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

    # ★ Auto-dispatch next queued message (server-side queue)
    _dispatch_queued_message(task)


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


def _dispatch_queued_message(task):
    """Check for queued messages and dispatch the next one after task completion.

    Runs in a fire-and-forget manner — failures are logged but don't affect
    the calling task's persistence.

    When a task is aborted by the user, queued messages are still dispatched —
    the user explicitly stopped the current generation, so the next queued
    message should proceed.  Only on errors do we skip dispatch (user may
    want to fix something before the queued message runs).
    """
    conv_id = task.get('convId', '')
    if not conv_id:
        return

    try:
        from lib.message_queue import dispatch_next_queued, get_queue_depth
        # Check if there are queued messages before dispatching
        depth = get_queue_depth(conv_id)
        if depth == 0:
            return

        if task.get('aborted'):
            logger.info('[Queue] Task was aborted for conv=%s — dispatching next queued message (depth=%d)',
                        conv_id[:8], depth)
        new_task_id = dispatch_next_queued(conv_id)
        if new_task_id:
            logger.info('[Queue] Auto-dispatched queued message → task %s for conv=%s',
                        new_task_id[:8], conv_id[:8])
    except Exception as e:
        logger.warning('[Queue] Auto-dispatch failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)


def _sync_result_to_conversation(task, meta):
    """Write the completed task result into the conversation's messages in the DB.

    Finds or creates the last assistant message and fills in content, thinking,
    toolRounds, finishReason, etc.  This makes the backend self-sufficient —
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

    # ── FRESHNESS GUARD: reject writes from stale/superseded tasks ──
    # When a user stops a task and regenerates, a new task becomes the
    # "latest" for this conversation. The old task may still be winding
    # down (abort is cooperative), and its _sync_result_to_conversation
    # would overwrite the new task's data. This guard prevents that.
    if conv_id:
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        if latest and latest != task['id']:
            _abort_reason = task.get('_abort_reason', '')
            logger.warning(
                '%s conv=%s ⛔ STALE TASK — refusing conv sync. '
                'This task (%s) was superseded by task %s. '
                'abort_reason=%s content=%dchars. '
                'Without this guard, old task data would overwrite the new task\'s content.',
                pfx, conv_id[:8], task_id_short, latest[:8],
                _abort_reason, len(content),
            )
            return

    # ── External-caller short-circuit ──
    # Tasks started via /api/chat/start with inline `messages` in the POST
    # body (SWE-bench harness, eval tools, external backends) have no
    # corresponding row in the `conversations` table — results are read by
    # the caller from `task_results` directly. Skip the write-back path so
    # we don't flood error.log with "Conversation not found" warnings.
    if task.get('_inline_messages'):
        logger.debug('%s conv=%s Inline-message task — skipping conv sync by design', pfx, conv_id)
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
        # Normal cause: the frontend saves completed previous turns to the DB
        # between task creation and task completion, so the DB naturally has more
        # messages than the snapshot sent to create_task().  We only flag as a
        # true anomaly when the extra messages contain consecutive same-role
        # entries (e.g. assistant-assistant or user-user), which cannot arise
        # from normal turn-taking and may indicate cross-talk or data corruption.
        expected_msg_count = task.get('_initial_msg_count')
        if expected_msg_count is not None and len(messages) > expected_msg_count + 2:
            extra_msgs = messages[expected_msg_count:]
            extra_summary = [(m.get('role'), len(m.get('content') or ''), m.get('model', 'N/A'))
                             for m in extra_msgs]
            # ── Skip dedup when endpoint-mode history is present ──
            # Endpoint mode legitimately produces consecutive-same-role messages
            # by design: planner+worker are both role=assistant, and critic+next
            # turn user are both role=user. These are NOT cross-talk anomalies.
            # Dedup here would destroy workers (shorter than planners) and, worse,
            # drop a short new user follow-up (e.g. "why did it stop?") in favor
            # of the preceding long critic review.  The message builder already
            # collapses historical endpoint sessions before sending to the LLM,
            # so we don't need to touch the persisted conversation.
            has_endpoint_history = any(
                (m.get('_isEndpointPlanner')
                 or m.get('_isEndpointReview')
                 or m.get('_epIteration') is not None
                 or m.get('_epIter') is not None
                 or m.get('_epPlannerIteration') is not None
                 or m.get('_epNextPhase'))
                for m in messages
            )
            # Check for consecutive same-role messages in the extras
            has_consecutive_same_role = any(
                extra_msgs[i].get('role') == extra_msgs[i + 1].get('role')
                for i in range(len(extra_msgs) - 1)
            )
            if has_consecutive_same_role and has_endpoint_history:
                logger.info(
                    '%s conv=%s Message count drift (DB=%d, task_start=%d, delta=%d) '
                    'with consecutive same-role — but ENDPOINT history detected. '
                    'Skipping dedup (planner+worker and critic+next-user are '
                    'expected same-role pairs). Extra msgs: %s',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )
            elif has_consecutive_same_role:
                logger.error(
                    '%s conv=%s ⛔ MESSAGE COUNT ANOMALY with consecutive same-role: '
                    'DB has %d messages but task started with %d — %d extra. '
                    'Extra msgs: %s — auto-deduplicating',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )
                # Auto-fix: remove consecutive duplicate-role messages
                # Keep the message with more content when two same-role msgs are adjacent.
                # ★ Guard: NEVER drop the last two messages (trailing user + assistant
                #   slot) — a short new user follow-up (e.g. "why?") must always win
                #   over any earlier same-role message it might be adjacent to.
                _tail_protect_idx = max(0, len(messages) - 2)
                deduped = [messages[0]]
                for idx, m in enumerate(messages[1:], start=1):
                    if (m.get('role') == deduped[-1].get('role')
                            and idx < _tail_protect_idx):
                        # Keep the one with more content
                        existing_len = len(deduped[-1].get('content') or '')
                        new_len = len(m.get('content') or '')
                        if new_len > existing_len:
                            deduped[-1] = m
                        logger.info('%s conv=%s Removed duplicate %s message (kept %d chars, dropped %d chars)',
                                   pfx, conv_id, m.get('role'), max(existing_len, new_len), min(existing_len, new_len))
                    else:
                        deduped.append(m)
                messages = deduped
                logger.info('%s conv=%s After dedup: %d messages (was %d)',
                           pfx, conv_id, len(messages), expected_msg_count + len(extra_msgs))
            else:
                logger.debug(
                    '%s conv=%s Message count drift (DB=%d, task_start=%d, delta=%d) — '
                    'normal frontend save of previous turns. Extra msgs: %s',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
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

        # ★ Merge checkpoint toolRounds for continue flow
        _cp_tr = task.get('_checkpointToolRounds') or []
        _new_tr = task.get('toolRounds') or []
        tool_rounds = (list(_cp_tr) + _new_tr) if _cp_tr else _new_tr

        if existing_content_len > new_content_len and existing_thinking_len > new_thinking_len:
            # ★ FIX: Even when frontend has more content (synced before us),
            #   still update toolRounds + metadata — the backend has richer
            #   tool data (toolContent, assistantContent) that the frontend
            #   may have missed if the SSE stream broke mid-delivery.
            #   Without this, page refresh → Continue loses toolContent
            #   because the frontend's stale sync overwrote our checkpoint.
            _tr_updated = False
            if tool_rounds:
                _existing_tr = last_msg.get('toolRounds') or []
                # Only replace if we have more rounds or the existing rounds
                # are missing toolContent (frontend sync race condition)
                _existing_has_tc = all(r.get('toolContent') for r in _existing_tr if r.get('status') == 'done')
                _new_has_tc = any(r.get('toolContent') for r in tool_rounds if r.get('status') == 'done')
                if len(tool_rounds) > len(_existing_tr) or (not _existing_has_tc and _new_has_tc):
                    last_msg['toolRounds'] = tool_rounds
                    _tr_updated = True
            # Always update finishReason/metadata (frontend may not have received done event)
            if meta.get('finishReason') and not last_msg.get('finishReason'):
                last_msg['finishReason'] = meta['finishReason']
            if meta.get('usage') and not last_msg.get('usage'):
                last_msg['usage'] = meta['usage']
            if meta.get('model') and not last_msg.get('model'):
                last_msg['model'] = meta['model']
            if _tr_updated or meta.get('finishReason'):
                logger.info('%s conv=%s Content guard: existing=%d+%d > new=%d+%d, '
                           'but still updating toolRounds=%s metadata=%s',
                           pfx, conv_id, existing_content_len, existing_thinking_len,
                           new_content_len, new_thinking_len,
                           _tr_updated, bool(meta.get('finishReason')))
            else:
                logger.info('%s conv=%s Server already has MORE content (existing=%d+%d > new=%d+%d) — '
                           'frontend likely already synced. Skipping.',
                           pfx, conv_id, existing_content_len, existing_thinking_len,
                           new_content_len, new_thinking_len)
                return

        else:
            # Normal path: backend has equal or more content — update everything
            if content:
                last_msg['content'] = content
            if thinking:
                last_msg['thinking'] = thinking
            if error:
                last_msg['error'] = error

        # Copy metadata fields that the frontend would normally set
        if tool_rounds:
            last_msg['toolRounds'] = tool_rounds
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

        # emit_to_user: persist emitted tool content for inline display
        if task.get('_emitContent'):
            last_msg['_emitContent'] = task['_emitContent']
        if task.get('_emitToolName'):
            last_msg['_emitToolName'] = task['_emitToolName']

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
                   SET messages=?, updated_at=?, msg_count=?, settings=?, search_text=?
                   WHERE id=? AND user_id=1 AND updated_at=?''',
                (messages_json, now_ms, len(messages), settings_json, search_text, conv_id, _row_updated_at)
            )
        else:
            db_execute_with_retry(
                db,
                '''UPDATE conversations
                   SET messages=?, updated_at=?, msg_count=?, search_text=?
                   WHERE id=? AND user_id=1 AND updated_at=?''',
                (messages_json, now_ms, len(messages), search_text, conv_id, _row_updated_at)
            )
        # Update FTS5 index
        if search_text:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                    "SELECT rowid, ? FROM conversations WHERE id = ?",
                    (search_text, conv_id)
                )
                db.commit()
            except Exception as _fts_err:
                logger.debug('[Task] FTS update failed (non-fatal): %s', _fts_err)
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
            logger.info('%s conv=%s msg=%d autoTranslate=false in settings — '
                        'skipping (settings.autoTranslate=%r)',
                        pfx, conv_id[:8], msg_idx,
                        settings.get('autoTranslate'))
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
                    # Persist the cleared state (with CAS to avoid clobbering
                    # concurrent frontend writes)
                    try:
                        _ua_row = db.execute(
                            'SELECT updated_at FROM conversations WHERE id=? AND user_id=1',
                            (conv_id,)
                        ).fetchone()
                        if _ua_row:
                            _now_ms = int(time.time() * 1000)
                            db_execute_with_retry(
                                db,
                                'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1 AND updated_at=?',
                                (json_dumps_pg(messages), _now_ms, conv_id, _ua_row[0])
                            )
                    except Exception as ce:
                        logger.warning('%s conv=%s Failed to clear stale translation: %s',
                                       pfx, conv_id[:8], ce)
                else:
                    logger.debug('%s conv=%s msg=%d already has translatedContent (%d chars) — skipping',
                                 pfx, conv_id[:8], msg_idx, len(existing_tc))
                    return

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


def _maybe_auto_translate_critic(conv_id, content, msg_idx, db):
    """Server-side auto-translate for endpoint-mode critic review messages.

    Endpoint-mode critic output is authored by the Critic LLM (English by
    default, sometimes mixed) and is stored as ``role='user'`` with
    ``_isEndpointReview=true`` in the conversation's ``messages`` list.  The
    existing ``_maybe_auto_translate_assistant`` safety-net commits to
    ``messages[msg_idx]`` by index regardless of role, so we reuse it
    directly and only override the log prefix + source-lang hint for
    observability.

    This path is only invoked from
    ``endpoint._trigger_endpoint_auto_translate``.  The per-conv
    ``autoTranslate`` gate, dedup against running frontend translate tasks,
    and stale-partial re-translation logic are inherited verbatim.
    """
    pfx = '[AutoTranslate:Critic]'
    if not conv_id or not content:
        logger.debug('%s conv=%s msg=%s — empty conv/content; skipping',
                     pfx, conv_id[:8] if conv_id else '?', msg_idx)
        return
    # Delegate to the shared helper — it is role-agnostic at the commit
    # layer (writes to messages[msg_idx]).  We only log the role flavour
    # here so operators can distinguish critic translations in the log.
    logger.info('%s conv=%s msg=%d content=%dchars — delegating to '
                '_maybe_auto_translate_assistant safety net',
                pfx, conv_id[:8], msg_idx, len(content))
    _maybe_auto_translate_assistant(conv_id, content, msg_idx, db)


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

    # ★ Merge checkpoint toolRounds for continue flow
    _cp_tr = task.get('_checkpointToolRounds') or []
    _cur_tr = task.get('toolRounds') or []
    _merged_tr = (list(_cp_tr) + _cur_tr) if _cp_tr else _cur_tr

    try:
        db = get_thread_db(DOMAIN_CHAT)
        tr_json = json.dumps(_merged_tr, ensure_ascii=False)
        meta = {}
        if task.get('model'): meta['model'] = task['model']
        if task.get('preset'): meta['preset'] = task['preset']
        if task.get('thinkingDepth'): meta['thinkingDepth'] = task['thinkingDepth']
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        db_execute_with_retry(db, '''INSERT OR REPLACE INTO task_results
            (task_id,conv_id,content,thinking,error,status,tool_rounds,metadata,created_at,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (task['id'], conv_id, task.get('content') or '', task.get('thinking') or '',
             task.get('error'), 'running', tr_json, meta_json,
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

    # ── FRESHNESS GUARD: reject checkpoint writes from stale tasks ──
    if conv_id:
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        if latest and latest != task['id']:
            logger.debug('[Checkpoint] conv=%s Stale task %s — skipping partial sync (latest=%s)',
                         conv_id[:8], task['id'][:8], latest[:8])
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

        # Also write toolRounds if available
        # ★ Merge checkpoint toolRounds for continue flow
        _cp_tr = task.get('_checkpointToolRounds') or []
        _cur_tr = task.get('toolRounds') or []
        tool_rounds = (list(_cp_tr) + _cur_tr) if _cp_tr else _cur_tr

        # Only update if we have MORE content than what's already in DB
        existing_content_len = len(last_msg.get('content') or '')
        existing_thinking_len = len(last_msg.get('thinking') or '')
        if len(content) <= existing_content_len and len(thinking) <= existing_thinking_len:
            # ★ FIX: Even when content didn't grow, still update toolRounds
            #   if the backend has richer data (toolContent).  This prevents
            #   the frontend's stale sync from permanently losing tool content.
            if tool_rounds and len(tool_rounds) > len(last_msg.get('toolRounds') or []):
                last_msg['toolRounds'] = tool_rounds
            else:
                return  # DB already has equal or more — skip

        if content and len(content) > existing_content_len:
            last_msg['content'] = content
        if thinking and len(thinking) > existing_thinking_len:
            last_msg['thinking'] = thinking

        if tool_rounds:
            last_msg['toolRounds'] = tool_rounds

        from routes.conversations import build_search_text
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        # ★ FIX: Use optimistic locking (CAS via updated_at) — same as
        #   _sync_result_to_conversation — to avoid clobbering fresher data
        #   written by the frontend's syncConversationToServer.
        cur_row = db.execute(
            'SELECT updated_at FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not cur_row:
            return
        cur_updated_at = cur_row[0]
        rc = db_execute_with_retry(
            db,
            '''UPDATE conversations SET messages=?, updated_at=?, msg_count=?, search_text=?
               WHERE id=? AND user_id=1 AND updated_at=?''',
            (messages_json, now_ms, len(messages), search_text, conv_id, cur_updated_at)
        )
        # Update FTS5 index
        if search_text:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                    "SELECT rowid, ? FROM conversations WHERE id = ?",
                    (search_text, conv_id)
                )
                db.commit()
            except Exception as _fts_err:
                logger.debug('[Checkpoint] FTS update failed (non-fatal): %s', _fts_err)
        if hasattr(rc, 'rowcount') and rc.rowcount == 0:
            logger.debug('[Checkpoint] conv=%s CAS conflict — another writer updated first, skipping',
                         conv_id)
            return
        logger.debug('[Checkpoint] conv=%s Synced partial to conversation: content=%d→%d thinking=%d→%d',
                     conv_id, existing_content_len, len(content),
                     existing_thinking_len, len(thinking))
    except Exception as e:
        logger.debug('[Checkpoint] conv=%s Failed to sync partial to conversation: %s',
                     conv_id, e, exc_info=True)



def recover_stale_tasks_on_startup():
    """Clean up stale tasks from a previous server crash at startup time.

    When the server crashes mid-generation:
    - task_results has entries with status='running' (from checkpoints)
    - conversations have activeTaskId set in settings (never cleared)

    This function:
    1. Marks all stale task_results as 'interrupted'
    2. Clears activeTaskId from all conversation settings
    3. Syncs interrupted task content into conversation messages

    This ensures the frontend doesn't need to do Case B recovery for every
    stale conversation on every page load, which dramatically speeds up boot.
    """
    try:
        db = get_thread_db(DOMAIN_CHAT)

        # ── Step 1: Mark stale running tasks as interrupted ──
        stale_rows = db.execute(
            "SELECT task_id, conv_id, content, thinking FROM task_results WHERE status='running'"
        ).fetchall()

        if stale_rows:
            for row in stale_rows:
                tid = row['task_id']
                cid = row['conv_id'] or ''
                clen = len(row['content'] or '')
                tlen = len(row['thinking'] or '')
                logger.info('[Startup] Marking stale task %s (conv=%s) as interrupted: '
                            'content=%dchars thinking=%dchars',
                            tid[:8], cid[:8], clen, tlen)
            db.execute("UPDATE task_results SET status='interrupted' WHERE status='running'")
            db.commit()
            logger.info('[Startup] Marked %d stale running task(s) as interrupted', len(stale_rows))

        # ── Step 2: Clear activeTaskId from all conversation settings ──
        # Find conversations that still have activeTaskId set
        conv_rows = db.execute(
            "SELECT id, settings, messages FROM conversations WHERE user_id=1 "
            "AND settings IS NOT NULL AND CAST(settings AS TEXT) LIKE '%activeTaskId%'"
        ).fetchall()

        cleared = 0
        for crow in conv_rows:
            cid = crow['id']
            try:
                settings = json.loads(crow['settings'] or '{}')
            except (json.JSONDecodeError, TypeError):
                continue
            atid = settings.get('activeTaskId')
            if not atid:
                continue
            # Clear activeTaskId
            settings['activeTaskId'] = None
            settings_json = json.dumps(settings, ensure_ascii=False)

            # ── Step 3: If there's interrupted task data, ensure it's in the
            #    conversation messages (the checkpoint may have partial content) ──
            task_row = db.execute(
                "SELECT content, thinking, tool_rounds, metadata FROM task_results WHERE task_id=?",
                (atid,)
            ).fetchone()

            messages_json = None
            if task_row:
                task_content = task_row['content'] or ''
                task_thinking = task_row['thinking'] or ''
                if task_content or task_thinking:
                    try:
                        messages = json.loads(crow['messages'] or '[]')
                        if messages:
                            last_msg = messages[-1]
                            if last_msg.get('role') == 'assistant':
                                # Only update if task has more content
                                existing_content = len(last_msg.get('content') or '')
                                existing_thinking = len(last_msg.get('thinking') or '')
                                if len(task_content) > existing_content:
                                    last_msg['content'] = task_content
                                if len(task_thinking) > existing_thinking:
                                    last_msg['thinking'] = task_thinking
                                if not last_msg.get('finishReason'):
                                    last_msg['finishReason'] = 'interrupted'
                                # Merge toolRounds from task
                                if task_row['tool_rounds']:
                                    try:
                                        tr = json.loads(task_row['tool_rounds'])
                                        if tr and len(tr) > len(last_msg.get('toolRounds') or []):
                                            last_msg['toolRounds'] = tr
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                # Merge metadata
                                if task_row['metadata']:
                                    try:
                                        meta = json.loads(task_row['metadata'])
                                        if meta.get('model') and not last_msg.get('model'):
                                            last_msg['model'] = meta['model']
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                messages_json = json_dumps_pg(messages)
                            elif last_msg.get('role') == 'user':
                                # Task started but no assistant msg was appended yet
                                new_msg = {
                                    'role': 'assistant',
                                    'content': task_content,
                                    'thinking': task_thinking,
                                    'finishReason': 'interrupted',
                                    'timestamp': int(time.time() * 1000),
                                }
                                if task_row['tool_rounds']:
                                    try:
                                        new_msg['toolRounds'] = json.loads(task_row['tool_rounds'])
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                if task_row['metadata']:
                                    try:
                                        meta = json.loads(task_row['metadata'])
                                        if meta.get('model'):
                                            new_msg['model'] = meta['model']
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                messages.append(new_msg)
                                messages_json = json_dumps_pg(messages)
                    except (json.JSONDecodeError, TypeError) as exc:
                        logger.warning('[Startup] Failed to parse messages for conv=%s: %s',
                                       cid[:8], exc)

            now_ms = int(time.time() * 1000)
            if messages_json:
                from routes.conversations import build_search_text
                messages_parsed = json.loads(messages_json)
                search_text = build_search_text(messages_parsed)
                db.execute(
                    "UPDATE conversations SET settings=?, messages=?, updated_at=?, "
                    "msg_count=?, search_text=? WHERE id=? AND user_id=1",
                    (settings_json, messages_json, now_ms,
                     len(messages_parsed), search_text, cid)
                )
            else:
                db.execute(
                    "UPDATE conversations SET settings=?, updated_at=? WHERE id=? AND user_id=1",
                    (settings_json, now_ms, cid)
                )
            cleared += 1
            logger.info('[Startup] Cleared activeTaskId=%s from conv=%s '
                        '(messages_updated=%s)',
                        atid[:8], cid[:8], bool(messages_json))

        if cleared:
            db.commit()
            logger.info('[Startup] Cleared activeTaskId from %d conversation(s)', cleared)

        total = len(stale_rows) + cleared
        if total:
            logger.info('[Startup] ✅ Stale task recovery complete: %d task(s) interrupted, '
                        '%d conv(s) cleaned', len(stale_rows), cleared)
            # Invalidate meta cache so first frontend request gets clean data
            try:
                from routes.common import _invalidate_meta_cache
                _invalidate_meta_cache()
            except Exception:
                pass
        else:
            logger.debug('[Startup] No stale tasks or activeTaskIds found — clean shutdown')

    except Exception as e:
        logger.error('[Startup] Stale task recovery failed (non-fatal): %s', e, exc_info=True)


def cleanup_old_tasks():
    now = time.time()
    with tasks_lock:
        to_rm = [tid for tid, t in tasks.items() if now - t['created_at'] > 3600 and t['status'] != 'running']
        for tid in to_rm: del tasks[tid]
    # Also clean up _conv_latest_task entries whose tasks no longer exist
    if to_rm:
        _rm_set = set(to_rm)
        with _conv_latest_task_lock:
            stale_convs = [cid for cid, tid in _conv_latest_task.items() if tid in _rm_set]
            for cid in stale_convs:
                del _conv_latest_task[cid]

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
        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The limit is persisted automatically.
        append_event(task, {
            'type': 'phase',
            'phase': 'retrying',
            'detail': (f'⚙️ Auto-detected model limit: {_limit_info["model"]} '
                       f'max_tokens={_limit_info["new_limit"]:,} '
                       f'(was {_limit_info["old_limit"]:,})'),
        })
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
