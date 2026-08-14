"""routes/api_v1/scheduler.py — Scheduler & timer REST surface.

Routes (scheduler):
  GET    /api/v1/scheduler/tasks
  GET    /api/v1/scheduler/tasks/<id>
  POST   /api/v1/scheduler/tasks/<id>/pause           (admin)
  POST   /api/v1/scheduler/tasks/<id>/resume          (admin)
  DELETE /api/v1/scheduler/tasks/<id>                 (admin)
  GET    /api/v1/scheduler/tasks/<id>/history
  GET    /api/v1/scheduler/tasks/<id>/poll-log        (proactive agent log)
  GET    /api/v1/scheduler/proactive/status
  POST   /api/v1/scheduler/tasks/<id>/trigger         (admin)

Routes (timer):
  GET    /api/v1/timer/list
  GET    /api/v1/timer/<id>/status
  POST   /api/v1/timer/<id>/cancel                    (admin)
  POST   /api/v1/timer/<id>/trigger                   (admin)
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_conflict, api_internal_error, api_not_found, api_ok,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.scheduler import get_scheduler

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_scheduler_bp = Blueprint('api_v1_scheduler', __name__)


# ── Scheduler: read endpoints ────────────────────────────────────────

@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks', methods=['GET'])
@require_auth
@api_meta(summary='List scheduled tasks', tags=['scheduler'],
          description='Set ``include_disabled=true`` to include paused tasks.')
def list_tasks():
    mgr = get_scheduler()
    include_disabled = request.args.get('include_disabled', 'false').lower() == 'true'
    tasks = mgr.list_tasks(include_disabled=include_disabled)
    return api_ok({'tasks': tasks})


@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get a scheduled task', tags=['scheduler'])
def get_task(task_id):
    mgr = get_scheduler()
    tasks = mgr.list_tasks(include_disabled=True)
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        return api_not_found('Task not found')
    return api_ok({'task': task})


@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>/history',
                            methods=['GET'])
@require_auth
@api_meta(summary='Task execution history', tags=['scheduler'])
def task_history(task_id):
    mgr = get_scheduler()
    limit = request.args.get('limit', 20, type=int)
    history = mgr.get_task_history(task_id, limit=limit)
    return api_ok({'history': history})


@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>/poll-log',
                            methods=['GET'])
@require_auth
@api_meta(
    summary='Proactive-agent poll-decision log',
    description=(
        'Recent poll entries for a proactive agent task: time, decision '
        '(act/skip), reason, model, tokens, ``execution_task_id`` if '
        'triggered. Useful for debugging why a watcher did or did not fire.'
    ),
    tags=['scheduler'],
)
def proactive_poll_log(task_id):
    from lib.scheduler.proactive import get_poll_log
    limit = request.args.get('limit', 30, type=int)
    entries = get_poll_log(task_id, limit=limit)
    return api_ok({'poll_log': entries})


@api_v1_scheduler_bp.route('/api/v1/scheduler/proactive/status',
                            methods=['GET'])
@require_auth
@api_meta(
    summary='Proactive-agent summary status',
    description=(
        'Aggregate state of all proactive agent tasks: total / active / '
        'currently-executing counts plus per-task summary used by the '
        'scheduler badge / panel.'
    ),
    tags=['scheduler'],
)
def proactive_status():
    mgr = get_scheduler()
    all_tasks = mgr.list_tasks(include_disabled=True)
    agent_tasks = [t for t in all_tasks if t.get('task_type') == 'agent']

    active = [t for t in agent_tasks if t.get('enabled')]
    executing = [t for t in active
                 if t.get('last_execution_status') == 'running'
                 and t.get('last_execution_task_id')]

    summary = {
        'total': len(agent_tasks),
        'active': len(active),
        'executing': len(executing),
        'tasks': [{
            'id': t['id'],
            'name': t['name'],
            'enabled': t.get('enabled', False),
            'schedule': t.get('schedule', ''),
            'poll_count': t.get('poll_count', 0),
            'execution_count': t.get('execution_count', 0),
            'last_poll_decision': t.get('last_poll_decision', ''),
            'last_poll_reason': t.get('last_poll_reason', ''),
            'last_poll_at': t.get('last_poll_at', ''),
            'last_execution_at': t.get('last_execution_at', ''),
            'last_execution_status': t.get('last_execution_status', ''),
            'target_conv_id': t.get('target_conv_id', ''),
            'max_executions': t.get('max_executions', 0),
        } for t in agent_tasks],
    }
    return api_ok({'proactive': summary})


# ── Scheduler: mutation endpoints (admin) ────────────────────────────

@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>/pause',
                            methods=['POST'])
@require_scope('admin')
@api_meta(summary='Pause (disable) a scheduled task',
          tags=['scheduler'], scope='admin')
def pause_task(task_id):
    logger.info('[Scheduler.v1] pausing task %s', task_id)
    get_scheduler().update_task(task_id, enabled=False)
    return api_ok()


@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>/resume',
                            methods=['POST'])
@require_scope('admin')
@api_meta(summary='Resume (enable) a scheduled task',
          tags=['scheduler'], scope='admin')
def resume_task(task_id):
    logger.info('[Scheduler.v1] resuming task %s', task_id)
    get_scheduler().update_task(task_id, enabled=True)
    return api_ok()


@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>',
                            methods=['DELETE'])
@require_scope('admin')
@api_meta(summary='Delete a scheduled task',
          tags=['scheduler'], scope='admin')
def delete_task(task_id):
    logger.warning('[Scheduler.v1] deleting task %s', task_id)
    get_scheduler().delete_task(task_id)
    return api_ok()


@api_v1_scheduler_bp.route('/api/v1/scheduler/tasks/<task_id>/trigger',
                            methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Manually trigger a proactive agent task',
    description=(
        'Skips the polling phase and goes directly to execution. Returns '
        '``409`` if the task is already running, ``400`` if the task is '
        'not an agent task.'
    ),
    tags=['scheduler'], scope='admin',
)
def trigger_proactive_task(task_id):
    from lib.scheduler.proactive import execute_proactive_task, is_task_executing
    mgr = get_scheduler()

    tasks = mgr.list_tasks(include_disabled=True)
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        return api_not_found('Task not found')
    if task.get('task_type') != 'agent':
        return api_bad_request('Not an agent task', field='task_type')
    if is_task_executing(task):
        return api_conflict('Task is currently executing')

    exec_task_id = execute_proactive_task(task)
    if not exec_task_id:
        return api_internal_error('Execution failed to start',
                                  context='trigger_proactive',
                                  source='api_v1.scheduler.trigger')

    from datetime import datetime
    now = datetime.now().isoformat()
    mgr.update_task(task_id,
                     last_execution_at=now,
                     last_execution_task_id=exec_task_id,
                     last_execution_status='running',
                     execution_count=task.get('execution_count', 0) + 1)
    return api_ok({'execution_task_id': exec_task_id})


# ── Timer endpoints ──────────────────────────────────────────────────

@api_v1_scheduler_bp.route('/api/v1/timer/list', methods=['GET'])
@require_auth
@api_meta(summary='List timer watchers', tags=['scheduler'])
def timer_list():
    from lib.scheduler.timer import (
        get_active_timer_count,
        has_timer_history,
        list_active_timers,
    )
    if request.args.get('summary') == '1':
        return api_ok({
            'has_timers': has_timer_history(),
            'active_count': get_active_timer_count(),
        })
    return api_ok({
        'timers': list_active_timers(),
        'active_count': get_active_timer_count(),
    })


@api_v1_scheduler_bp.route('/api/v1/timer/<timer_id>/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Timer details + recent poll log',
    tags=['scheduler'],
)
def timer_status(timer_id):
    from lib.scheduler.timer import get_timer, get_timer_poll_log
    timer = get_timer(timer_id)
    if not timer:
        return api_not_found('Timer not found')
    limit = request.args.get('limit', 20, type=int)
    poll_log = get_timer_poll_log(timer_id, limit=limit)
    return api_ok({'timer': timer, 'poll_log': poll_log})


@api_v1_scheduler_bp.route('/api/v1/timer/<timer_id>/cancel', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Cancel an active timer',
          tags=['scheduler'], scope='admin')
def timer_cancel(timer_id):
    from lib.scheduler.timer import cancel_timer
    logger.info('[Timer.v1] Cancelling timer %s', timer_id)
    cancel_timer(timer_id)
    return api_ok()


@api_v1_scheduler_bp.route('/api/v1/timer/<timer_id>/trigger', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Force-trigger a timer (skip polling, execute now)',
    tags=['scheduler'], scope='admin',
)
def timer_trigger(timer_id):
    from lib.scheduler.timer import force_trigger_timer, get_timer
    timer = get_timer(timer_id)
    if not timer:
        return api_not_found('Timer not found')
    if timer['status'] != 'active':
        return api_bad_request(
            f'Timer is not active (status={timer["status"]})',
            field='status')
    logger.info('[Timer.v1] Force-triggering timer %s', timer_id)
    exec_task_id = force_trigger_timer(timer_id)
    if not exec_task_id:
        return api_internal_error('Trigger failed',
                                  context='timer_trigger',
                                  source='api_v1.scheduler.timer_trigger')
    return api_ok({'execution_task_id': exec_task_id})


__all__ = ['api_v1_scheduler_bp']
