"""Owned task snapshots and bounded completion waits for the unified poll tool.

The internal await_task adapter remains for dispatch compatibility. Status and
all wait exits share terminal-result rendering; nonterminal/unknown states must
never imply completion. Authorization is enforced by chat_task_runtime.
"""

import time as _time

from lib.log import get_logger
from lib.scheduler.executor._common import _coerce_int_arg
from lib.task_replay.task_replay import TASK_REPLAY_TERMINAL_STATUSES

logger = get_logger(__name__)


def _task_snapshot(task: dict) -> str:
    """Render complete terminal evidence consistently for every poll path."""
    status = task.get('status') or 'unknown'
    content = task.get('content') or ''
    elapsed = round(_time.time() - task.get('created_at', _time.time()))
    snapshot = (
        f'Task Status:\n'
        f'  ID: {task["id"]}\n'
        f'  Conversation: {task.get("convId", "?")}\n'
        f'  Status: {status}\n'
        f'  Elapsed: {elapsed}s\n'
        f'  Output: {len(content)} chars\n'
        f'  Error: {task.get("error") or "none"}'
    )
    if status in TASK_REPLAY_TERMINAL_STATUSES:
        label = 'Final output' if status == 'done' and not task.get('error') else 'Partial output'
        snapshot += f'\n  {label} ({len(content)} chars):\n{content or "(empty)"}'
    return snapshot


def _execute_await_task(fn_args, *, user_id: int):
    """List owned running tasks, inspect status, or wait at most 60 seconds."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    action = fn_args.get('action', 'list')

    if action == 'list':
        running = [
            {
                'task_id': task['id'],
                'conv_id': task.get('convId', '?'),
                'elapsed': round(_time.time() - task.get('created_at', _time.time())),
                'content_len': len(task.get('content') or ''),
            }
            for task in chat_task_runtime.snapshot_owned(user_id=int(user_id))
            if task.get('status') == 'running'
        ]
        if not running:
            return 'No tasks currently running. This is a status snapshot, not verified completion.'
        lines = [f'Currently Running Tasks ({len(running)}):', '-' * 50]
        for row in running:
            lines.append(
                f'  Task: {row["task_id"]}\n'
                f'     Conversation: {row["conv_id"][:12]}...\n'
                f'     Running for: {row["elapsed"]}s\n'
                f'     Output so far: {row["content_len"]} chars\n'
            )
        return '\n'.join(lines)

    task_id = fn_args.get('task_id', '')
    if not task_id:
        return 'Error: a task handle is required. Use poll() for a snapshot of accessible handles.'

    def read_owned():
        return chat_task_runtime.get_owned(task_id, user_id=int(user_id))

    unavailable = f'Task {task_id} is unavailable; no terminal outcome is verified.'
    if action == 'status':
        task = read_owned()
        return _task_snapshot(task) if task else unavailable

    if action == 'wait':
        timeout = max(0, min(_coerce_int_arg('timeout', fn_args.get('timeout', 60), 60), 60))
        poll_interval = max(_coerce_int_arg('poll_interval', fn_args.get('poll_interval', 5), 5), 2)
        deadline = _time.monotonic() + timeout
        parent_task = fn_args.get('_parent_task')

        task = read_owned()
        if not task:
            return unavailable
        if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES:
            return _task_snapshot(task)

        logger.info('[AwaitTask] Waiting for task %s (timeout=%ds, poll=%ds)',
                    task_id, timeout, poll_interval)
        while _time.monotonic() < deadline:
            if parent_task and parent_task.get('aborted'):
                return 'Wait cancelled: your task was aborted by the user.'
            _time.sleep(min(poll_interval, max(0, deadline - _time.monotonic())))
            if parent_task and parent_task.get('aborted'):
                return 'Wait cancelled: your task was aborted by the user.'
            task = read_owned()
            if not task:
                return unavailable
            if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES:
                return _task_snapshot(task)

        # Recheck at the deadline: completion racing with timeout has the same
        # full-result representation as an already-terminal status request.
        task = read_owned()
        if not task:
            return unavailable
        if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES:
            return _task_snapshot(task)
        return (
            f'Wait bound reached after {timeout}s; no terminal outcome is verified.\n'
            f'{_task_snapshot(task)}\n'
            f'Use poll(id={task_id!r}, wait_s=60) only when no useful work remains.'
        )

    return f'Error: Unknown task polling action: {action}. Use list/wait/status.'
