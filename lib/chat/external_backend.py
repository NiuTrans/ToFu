"""External agent-backend runner (Claude Code, Codex, …).

Moved out of ``routes/chat.py`` so the SSE-bridge worker engine lives in lib.
The function returns a plain result dict (NOT a Flask response) so it stays
framework-agnostic; the thin route wrapper in ``routes/chat.py`` maps the
dict to an HTTP response.

Result dict shapes:
  * success           → ``{'taskId': <id>}``
  * validation error  → ``{'error': <msg>, 'status': <http_code>}``
"""

import threading

from lib.log import get_logger

logger = get_logger(__name__)


def run_external_backend(data, messages, backend_name):
    """Start a task using an external CLI agent backend (Claude Code, Codex, etc.).

    Validates backend availability/auth, creates a task, then spawns a thread
    that calls ``backend.start_turn()`` and pipes NormalizedEvents through
    ``normalized_to_sse()`` into ``append_event()``.

    The existing SSE streaming (``chat_stream``) and polling (``chat_poll``)
    work unchanged — they read from the same ``task['events']`` queue.

    Returns:
        dict — ``{'taskId': id}`` on success, or
        ``{'error': msg, 'status': http_code}`` on validation/spawn failure.
    """
    from lib.agent_backends import get_backend
    from lib.agent_backends.sse_bridge import SSEBridgeState
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.manager import append_event, persist_task_result

    backend = get_backend(backend_name)
    if backend is None:
        return {'error': f'Unknown backend: {backend_name}', 'status': 400}
    if not backend.is_available():
        return {'error': f'{backend.display_name} CLI is not installed. '
                         f'Install it first, then try again.', 'status': 400}
    if not backend.is_authenticated():
        return {'error': f'{backend.display_name} is not authenticated. '
                         f'Run the CLI and log in first.', 'status': 401}

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
                source='lib.chat.external_backend',
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
            source='lib.chat.external_backend',
            raw=str(_spawn_err),
        )
        return {'error': 'Failed to start task', 'status': 500}

    return {'taskId': task['id']}


__all__ = ['run_external_backend']
