"""
Scheduler Routes — REST API for viewing/managing scheduled tasks.

Includes proactive agent endpoints for poll log, status, and SSE event push.
"""

from flask import Blueprint, jsonify, request

from lib.log import get_logger
from lib.scheduler import get_scheduler

logger = get_logger(__name__)

scheduler_bp = Blueprint('scheduler', __name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Standard CRUD endpoints
# ═══════════════════════════════════════════════════════════════════════════

@scheduler_bp.route('/api/scheduler/tasks', methods=['GET'])
def list_tasks():
    """List all scheduled tasks."""
    mgr = get_scheduler()
    include_disabled = request.args.get('include_disabled', 'false').lower() == 'true'
    tasks = mgr.list_tasks(include_disabled=include_disabled)
    return jsonify({'ok': True, 'tasks': tasks})


@scheduler_bp.route('/api/scheduler/tasks/<task_id>', methods=['GET'])
def get_task(task_id):
    """Get details of a specific task."""
    mgr = get_scheduler()
    tasks = mgr.list_tasks(include_disabled=True)
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        return jsonify({'ok': False, 'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': task})


@scheduler_bp.route('/api/scheduler/tasks/<task_id>/pause', methods=['POST'])
def pause_task(task_id):
    logger.info('[Scheduler] pausing task %s', task_id)
    mgr = get_scheduler()
    mgr.update_task(task_id, enabled=False)
    return jsonify({'ok': True})


@scheduler_bp.route('/api/scheduler/tasks/<task_id>/resume', methods=['POST'])
def resume_task(task_id):
    logger.info('[Scheduler] resuming task %s', task_id)
    mgr = get_scheduler()
    mgr.update_task(task_id, enabled=True)
    return jsonify({'ok': True})


@scheduler_bp.route('/api/scheduler/tasks/<task_id>', methods=['DELETE'])
def delete_task(task_id):
    logger.warning('[Scheduler] deleting task %s', task_id)
    mgr = get_scheduler()
    mgr.delete_task(task_id)
    return jsonify({'ok': True})


@scheduler_bp.route('/api/scheduler/tasks/<task_id>/history', methods=['GET'])
def task_history(task_id):
    """Get execution history for a task."""
    mgr = get_scheduler()
    limit = request.args.get('limit', 20, type=int)
    history = mgr.get_task_history(task_id, limit=limit)
    return jsonify({'ok': True, 'history': history})


# ═══════════════════════════════════════════════════════════════════════════
#  Proactive Agent endpoints
# ═══════════════════════════════════════════════════════════════════════════

@scheduler_bp.route('/api/scheduler/tasks/<task_id>/poll-log', methods=['GET'])
def proactive_poll_log(task_id):
    """Get the poll decision log for a proactive agent task.

    Returns recent poll entries showing: time, decision (act/skip), reason,
    model, tokens used, and execution_task_id if triggered.
    """
    from lib.scheduler.proactive import get_poll_log
    limit = request.args.get('limit', 30, type=int)
    entries = get_poll_log(task_id, limit=limit)
    return jsonify({'ok': True, 'poll_log': entries})


@scheduler_bp.route('/api/scheduler/proactive/status', methods=['GET'])
def proactive_status():
    """Get summary status of all proactive agent tasks.

    Used by the frontend scheduler badge to show:
    - Number of active proactive watchers
    - Most recent poll decisions
    - Any currently executing tasks
    """
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
    return jsonify({'ok': True, 'proactive': summary})


@scheduler_bp.route('/api/scheduler/tasks/<task_id>/trigger', methods=['POST'])
def trigger_proactive_task(task_id):
    """Manually trigger a proactive agent task (skip polling, go directly to execute).

    Useful for testing or forcing an execution.
    """
    from lib.scheduler.proactive import execute_proactive_task, is_task_executing
    mgr = get_scheduler()

    tasks = mgr.list_tasks(include_disabled=True)
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        return jsonify({'ok': False, 'error': 'Task not found'}), 404

    if task.get('task_type') != 'agent':
        return jsonify({'ok': False, 'error': 'Not an agent task'}), 400

    if is_task_executing(task):
        return jsonify({'ok': False, 'error': 'Task is currently executing'}), 409

    exec_task_id = execute_proactive_task(task)
    if exec_task_id:
        # Update execution state
        from datetime import datetime
        now = datetime.now().isoformat()
        mgr.update_task(task_id,
                        last_execution_at=now,
                        last_execution_task_id=exec_task_id,
                        last_execution_status='running',
                        execution_count=task.get('execution_count', 0) + 1)
        return jsonify({'ok': True, 'execution_task_id': exec_task_id})
    else:
        return jsonify({'ok': False, 'error': 'Execution failed to start'}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  Timer Watcher endpoints
# ═══════════════════════════════════════════════════════════════════════════

@scheduler_bp.route('/api/timer/list', methods=['GET'])
def timer_list():
    """List all timer watchers (active first, then recent)."""
    from lib.scheduler.timer import get_active_timer_count, list_active_timers
    timers = list_active_timers()
    return jsonify({
        'ok': True,
        'timers': timers,
        'active_count': get_active_timer_count(),
    })


@scheduler_bp.route('/api/timer/<timer_id>/status', methods=['GET'])
def timer_status(timer_id):
    """Get timer details + recent poll log."""
    from lib.scheduler.timer import get_timer, get_timer_poll_log
    timer = get_timer(timer_id)
    if not timer:
        return jsonify({'ok': False, 'error': 'Timer not found'}), 404
    limit = request.args.get('limit', 20, type=int)
    poll_log = get_timer_poll_log(timer_id, limit=limit)
    return jsonify({'ok': True, 'timer': timer, 'poll_log': poll_log})


@scheduler_bp.route('/api/timer/<timer_id>/cancel', methods=['POST'])
def timer_cancel(timer_id):
    """Cancel an active timer."""
    from lib.scheduler.timer import cancel_timer
    logger.info('[Timer] Cancelling timer %s via API', timer_id)
    cancel_timer(timer_id)
    return jsonify({'ok': True})


@scheduler_bp.route('/api/timer/<timer_id>/trigger', methods=['POST'])
def timer_trigger(timer_id):
    """Force-trigger a timer (skip polling, go directly to execute)."""
    from lib.scheduler.timer import force_trigger_timer, get_timer
    timer = get_timer(timer_id)
    if not timer:
        return jsonify({'ok': False, 'error': 'Timer not found'}), 404
    if timer['status'] != 'active':
        return jsonify({'ok': False, 'error': f'Timer is not active (status={timer["status"]})'}), 400
    logger.info('[Timer] Force-triggering timer %s via API', timer_id)
    exec_task_id = force_trigger_timer(timer_id)
    if exec_task_id:
        return jsonify({'ok': True, 'execution_task_id': exec_task_id})
    else:
        return jsonify({'ok': False, 'error': 'Trigger failed'}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  Scheduler lifecycle
# ═══════════════════════════════════════════════════════════════════════════

def start_scheduler_worker():
    """Start the background scheduler thread and resume active timers.

    Called from server.py / register_all.
    """
    mgr = get_scheduler()
    mgr.start()
    logger.info('[Scheduler] Background scheduler worker started')

    # Resume active timers AFTER schema init — deferred to background thread
    # so we don't query timer_watchers before init_db() creates it.
    # We poll for DB readiness instead of a fixed sleep, because init_db()
    # runs later in server.py (after register_all) and may take variable time.
    import threading
    def _deferred_resume():
        import time
        from lib.database import db_available
        # Wait up to 120s for init_db() to create the timer_watchers table
        for attempt in range(60):
            time.sleep(2)
            if not db_available:
                continue
            try:
                from lib.database import get_thread_db, DOMAIN_SYSTEM
                db = get_thread_db(DOMAIN_SYSTEM)
                db.execute("SELECT 1 FROM timer_watchers LIMIT 0")
                break  # table exists, proceed
            except Exception:
                logger.debug('[Scheduler] timer_watchers not ready yet (attempt %d/60)', attempt + 1)
                continue
        else:
            logger.warning('[Scheduler] timer_watchers table not available after 120s, skipping timer resume')
            return
        try:
            from lib.scheduler.timer import resume_active_timers
            resumed = resume_active_timers()
            if resumed > 0:
                logger.info('[Scheduler] Resumed %d active timer(s)', resumed)
        except Exception as e:
            logger.warning('[Scheduler] Failed to resume timers on startup: %s', e)
    threading.Thread(target=_deferred_resume, name='timer-resume', daemon=True).start()

    return mgr
