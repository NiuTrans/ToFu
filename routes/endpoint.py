"""routes/endpoint.py — Endpoint mode: autonomous work → critic review loop.

Provides:
  POST /api/endpoint/start   — start an endpoint task
  GET  /api/endpoint/status/<task_id> — endpoint-specific status

Streaming, polling, and abort reuse the existing /api/chat/* routes since
endpoint tasks use the same task/event infrastructure.
"""

import threading

from flask import Blueprint, jsonify, request

from lib.log import get_logger
from lib.rate_limiter import rate_limit
from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

logger = get_logger(__name__)

endpoint_bp = Blueprint('endpoint', __name__)


@endpoint_bp.route('/api/endpoint/start', methods=['POST'])
@rate_limit(limit=10, per=60)
def endpoint_start():
    """Start an autonomous endpoint task (Worker → Critic loop).

    Request JSON:
      messages — list of chat messages (required, at least one user msg)
      convId   — conversation ID (optional, auto-generated if missing)
      config   — dict with standard config keys:
        model, preset, thinkingDepth, searchMode, fetchEnabled,
        codeExecEnabled, browserEnabled, skillsEnabled, etc.
        (The Critic uses the same model and tools as the Worker.)

    Response JSON:
      taskId — task ID for streaming/polling/abort
      convId — conversation ID

    Streaming:
      Use GET /api/chat/stream/<taskId> — same SSE stream as normal chat.
      Additional event types:
        endpoint_iteration   — {type, iteration, phase: "planning"|"working"|"reviewing"}
        endpoint_planner_done— {type, content, thinking, usage}
        endpoint_critic_msg  — {type, iteration, content, should_stop}
        endpoint_new_turn    — {type, iteration}
        endpoint_complete    — {type, totalIterations, reason}
    """
    data = request.get_json(silent=True) or {}
    messages = data.get('messages', [])
    if not messages:
        return jsonify({'error': 'No messages provided'}), 400

    has_user_msg = any(
        m.get('role') == 'user' and m.get('content')
        for m in messages
    )
    if not has_user_msg:
        return jsonify({'error': 'At least one user message with content required'}), 400

    config = data.get('config', {})
    config['endpointMode'] = True

    cleanup_old_tasks()
    task = create_task(data.get('convId', ''), messages, config)
    task['endpoint_mode'] = True
    # ★ Set initial phase BEFORE starting the thread to avoid a race:
    # the SSE state snapshot defaults _endpoint_phase to 'working' if unset,
    # which causes the frontend to immediately show Agent instead of Planner.
    task['_endpoint_phase'] = 'planning'
    task['_endpoint_iteration'] = 0

    logger.info('[Endpoint] Starting endpoint task %s for conv %s '
                '(model=%s, critic=same)',
                task['id'], task['convId'],
                config.get('model', '(default)'))

    from lib.tasks_pkg.endpoint import run_endpoint_task
    threading.Thread(target=run_endpoint_task, args=(task,), daemon=True).start()

    return jsonify({
        'taskId': task['id'],
        'convId': task['convId'],
    })


@endpoint_bp.route('/api/endpoint/status/<task_id>', methods=['GET'])
def endpoint_status(task_id):
    """Get endpoint-specific status."""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    total_iterations = 0
    reason = None
    critic_msgs = []

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
