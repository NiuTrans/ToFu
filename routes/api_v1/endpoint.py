"""routes/api_v1/endpoint.py — Endpoint mode (Planner → Worker → Critic).

Routes:
  POST /api/v1/endpoint/start              — start an endpoint task
  GET  /api/v1/endpoint/status/<task_id>   — endpoint-specific status

Streaming, polling, and abort reuse the existing chat task plumbing
(``GET /api/chat/stream/<task_id>``, etc.). The endpoint surface is
intentionally narrow: just the two routes the UI / SDK need to launch
and inspect a Worker→Critic loop.
"""

from __future__ import annotations

import threading

from flask import Blueprint, jsonify

from lib.api_response import api_bad_request, api_not_found
from lib.log import get_logger
from lib.openapi import api_meta
from lib.rate_limiter import rate_limit
from lib.request_parser import parse_body
from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

from .auth import require_auth

logger = get_logger(__name__)

api_v1_endpoint_bp = Blueprint('api_v1_endpoint', __name__)


@api_v1_endpoint_bp.route('/api/v1/endpoint/start', methods=['POST'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(
    summary='Start an endpoint (Planner → Worker → Critic) task',
    description=(
        'Launches an autonomous task that loops Planner → Worker → Critic '
        'until the critic returns ``[VERDICT: STOP]`` or the replan/'
        'iteration budget is exhausted. Streams via the existing '
        '``GET /api/chat/stream/<task_id>`` SSE endpoint with extra event '
        'types: ``endpoint_iteration``, ``endpoint_planner_done``, '
        '``endpoint_critic_msg``, ``endpoint_new_turn``, '
        '``endpoint_complete``.'
    ),
    tags=['chat'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'properties': {
            'messages': {'type': 'array',
                          'description': 'Optional. If omitted, messages '
                                          'are built server-side from the '
                                          'conversation referenced by '
                                          '``convId``.'},
            'convId': {'type': 'string'},
            'config': {'type': 'object',
                        'description': '32-field config (model, preset, '
                                        'thinkingDepth, searchMode, '
                                        'fetchEnabled, codeExecEnabled, '
                                        'browserEnabled, memoryEnabled, '
                                        '...). The Critic reuses the same '
                                        'model and tools as the Worker.'}}}}}},
)
def endpoint_start():
    data = parse_body()
    conv_id = data.get('convId', '')
    config = data.get('config') or {}

    messages = data.get('messages')
    if not messages:
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        exclude_last = config.get('excludeLast', False)
        messages = build_api_messages_from_db(
            conv_id, config, exclude_last=exclude_last)
        if messages is None:
            return api_not_found('Conversation not found')
        if not messages:
            return api_bad_request('No messages')
        logger.info('[Endpoint.v1] Built %d API messages from DB for conv %s',
                    len(messages), conv_id[:8])

    has_user_msg = any(
        m.get('role') == 'user' and m.get('content') for m in messages
    )
    if not has_user_msg:
        return api_bad_request(
            'At least one user message with content required',
            field='messages')
    config['endpointMode'] = True

    cleanup_old_tasks()
    task = create_task(conv_id, messages, config)
    task['endpoint_mode'] = True
    # ★ Initial phase set BEFORE thread start to avoid the SSE snapshot
    # defaulting to 'working' (which would briefly show Agent instead of
    # Planner in the UI).
    task['_endpoint_phase'] = 'planning'
    task['_endpoint_iteration'] = 0

    logger.info('[Endpoint.v1] Starting endpoint task %s for conv %s '
                '(model=%s, critic=same)',
                task['id'], task['convId'],
                config.get('model', '(default)'))

    from lib.tasks_pkg.endpoint import run_endpoint_task
    threading.Thread(target=run_endpoint_task,
                     args=(task,), daemon=True).start()

    # Bare {taskId, convId} shape preserved from the legacy route — the
    # frontend reads data.taskId directly.
    return jsonify({'taskId': task['id'], 'convId': task['convId']})


@api_v1_endpoint_bp.route('/api/v1/endpoint/status/<task_id>',
                            methods=['GET'])
@require_auth
@api_meta(
    summary='Endpoint-task status (iterations + critic verdicts)',
    description=(
        'Returns the canonical task status fields plus an endpoint-mode '
        'summary: total iterations completed, completion reason, and a '
        'preview of each critic message emitted.'
    ),
    tags=['chat'],
)
def endpoint_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return api_not_found('Task not found')

    total_iterations = 0
    reason = None
    critic_msgs: list[dict] = []

    with task.get('events_lock', threading.Lock()):
        for ev in task.get('events', []):
            if ev.get('type') == 'endpoint_critic_msg':
                critic_msgs.append({
                    'iteration': ev.get('iteration'),
                    'should_stop': ev.get('should_stop', False),
                    'contentPreview': (ev.get('content', '')[:200]),
                })
            elif ev.get('type') == 'endpoint_complete':
                total_iterations = ev.get('totalIterations', 0)
                reason = ev.get('reason')

    # Preserve the bare-dict legacy shape for the UI panel.
    return jsonify({
        'id': task['id'],
        'status': task['status'],
        'endpointMode': True,
        'totalIterations': total_iterations,
        'reason': reason,
        'criticMessages': critic_msgs,
        'content': task.get('content', ''),
        'error': task.get('error'),
        'usage': task.get('usage'),
    })


__all__ = ['api_v1_endpoint_bp']
