"""routes/chat.py — Chat start, streaming, polling, abort."""

import json
import threading
import time

from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_CHAT, get_db
from lib.log import get_logger
from lib.rate_limiter import rate_limit
from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

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
        result = [{'id': t['id'], 'convId': t['convId'], 'status': t['status']} for t in tasks.values()]
    return jsonify(result)


@chat_bp.route('/api/chat/start', methods=['POST'])
@rate_limit(limit=10, per=60)  # 10 requests per minute
def chat_start():
    data = request.get_json(silent=True) or {}
    messages = data.get('messages', [])
    if not messages:
        return jsonify({'error': 'No messages'}), 400
    cleanup_old_tasks()
    task = create_task(data.get('convId', ''), messages, data.get('config', {}))
    from lib.tasks_pkg import run_task
    _cfg_model = data.get('config', {}).get('model', '?')
    _cfg_preset = data.get('config', {}).get('preset', data.get('config', {}).get('effort', '?'))
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


@chat_bp.route('/api/chat/stream/<task_id>', methods=['GET'])
def chat_stream(task_id):
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        db = get_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT content,thinking,error,status,search_rounds,metadata FROM task_results WHERE task_id=?',
            (task_id,)
        ).fetchone()
        if row:
            state = {
                'type': 'state', 'content': row['content'],
                'thinking': row['thinking'], 'status': row['status'],
            }
            if row['error']:
                state['error'] = row['error']
            if row['search_rounds']:
                try:
                    state['searchRounds'] = json.loads(row['search_rounds'])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning('[Chat] Failed to parse search_rounds for task %s: %s', task_id, e, exc_info=True)
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
                if task['searchRounds']:
                    state['searchRounds'] = task['searchRounds']
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
        for key in ('error', 'searchRounds', 'finishReason', 'usage', 'preset',
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
        # ★ Include endpoint turns for endpoint mode tasks so _pollFallback
        #   can reconstruct the full multi-turn conversation
        if task.get('endpoint_mode') and task.get('_endpoint_turns'):
            r['endpointMode'] = True
            r['endpointTurns'] = task['_endpoint_turns']
        return jsonify(r)

    logger.debug('[Chat] Poll %s — not in memory, checking DB', task_id[:8])
    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT task_id,content,thinking,error,status,search_rounds,metadata FROM task_results WHERE task_id=?',
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
        if row['search_rounds']:
            try:
                r['searchRounds'] = json.loads(row['search_rounds'])
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Chat] Failed to parse search_rounds in poll for task %s: %s', task_id, e, exc_info=True)
        for key in ('finishReason', 'usage', 'preset', 'toolSummary'):
            if _db_meta.get(key):
                r[key] = _db_meta[key]
        if _db_meta.get('fallbackModel'):
            r['fallbackModel'] = _db_meta['fallbackModel']
            r['fallbackFrom'] = _db_meta.get('fallbackFrom', '')
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
