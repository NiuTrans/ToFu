"""lib/scheduler/executor/_await.py — await_task tool handler.

Handles the ``await_task`` scheduler tool: list running cross-conversation
tasks, poll-wait for a specific task, or report its status.
"""

import time as _time

from lib.log import get_logger
from lib.scheduler.executor._common import _coerce_int_arg

logger = get_logger(__name__)


def _execute_await_task(fn_args, *, user_id: int):
    """Handle await_task tool — list/wait/status for cross-conversation tasks."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    action = fn_args.get('action', 'list')

    if action == 'list':
        running = [
                {
                    'task_id': t['id'],
                    'conv_id': t.get('convId', '?'),
                    'status': t['status'],
                    'elapsed': round(_time.time() - t.get('created_at', _time.time())),
                    'content_len': len(t.get('content', '')),
                }
                for t in chat_task_runtime.snapshot_owned(user_id=int(user_id))
                if t.get('status') == 'running'
            ]
        if not running:
            return 'No tasks currently running. All conversations are idle.'
        lines = [f'Currently Running Tasks ({len(running)}):']
        lines.append('-' * 50)
        for r in running:
            lines.append(
                f'  Task: {r["task_id"][:12]}...\n'
                f'     Conversation: {r["conv_id"][:12]}...\n'
                f'     Running for: {r["elapsed"]}s\n'
                f'     Output so far: {r["content_len"]} chars'
            )
            lines.append('')
        return '\n'.join(lines)

    task_id = fn_args.get('task_id', '')
    if not task_id:
        return 'Error: task_id is required for wait/status actions. Use action="list" to discover running tasks.'

    if action == 'status':
        t = chat_task_runtime.get_owned(task_id, user_id=int(user_id))
        if not t:
            return f'Error: Task {task_id} not found (may have already been cleaned up).'
        elapsed = round(_time.time() - t.get('created_at', _time.time()))
        return (
            f'Task Status:\n'
            f'  ID: {t["id"]}\n'
            f'  Conversation: {t.get("convId", "?")}\n'
            f'  Status: {t["status"]}\n'
            f'  Running for: {elapsed}s\n'
            f'  Output: {len(t.get("content", ""))} chars\n'
            f'  Error: {t.get("error") or "none"}'
        )

    if action == 'wait':
        timeout = min(_coerce_int_arg('timeout', fn_args.get('timeout', 600), 600), 3600)
        poll_interval = max(_coerce_int_arg('poll_interval', fn_args.get('poll_interval', 5), 5), 2)
        deadline = _time.time() + timeout
        parent_task = fn_args.get('_parent_task')  # injected by tool_dispatch

        # First check if it exists
        t = chat_task_runtime.get_owned(task_id, user_id=int(user_id))
        if not t:
            return f'Error: Task {task_id} not found. It may have already finished.'

        if t.get('status') != 'running':
            return (
                f'Task {task_id} already finished.\n'
                f'  Status: {t["status"]}\n'
                f'  Content length: {len(t.get("content", ""))} chars\n'
                f'  Error: {t.get("error") or "none"}'
            )

        logger.info('[AwaitTask] Waiting for task %s (timeout=%ds, poll=%ds)',
                    task_id, timeout, poll_interval)

        # Poll until done, timeout, or parent task aborted
        while _time.time() < deadline:
            _time.sleep(poll_interval)
            # ── Check if our own task was aborted by the user ──
            if parent_task and parent_task.get('aborted'):
                logger.info('[AwaitTask] Parent task aborted, stopping wait for %s', task_id)
                return 'Wait cancelled: your task was aborted by the user.'
            t = chat_task_runtime.get_owned(task_id, user_id=int(user_id))
            if not t:
                logger.info('[AwaitTask] Task %s completed and cleaned up', task_id)
                return f'Task {task_id} has completed and been cleaned up.'
            if t.get('status') != 'running':
                elapsed = round(_time.time() - t.get('created_at', _time.time()))
                snippet = t.get('content', '')[-500:] if t.get('content') else '(empty)'
                logger.info('[AwaitTask] Task %s finished with status=%s after %ds',
                            task_id, t['status'], elapsed)
                return (
                    f'Task {task_id} finished.\n'
                    f'  Status: {t["status"]}\n'
                    f'  Total time: {elapsed}s\n'
                    f'  Final output ({len(t.get("content", ""))} chars, last 500):\n'
                    f'  {snippet}\n'
                    f'  Error: {t.get("error") or "none"}'
                )

        # Timeout
        t = chat_task_runtime.get_owned(task_id, user_id=int(user_id))
        content_len = len(t.get('content', '')) if t else 0
        logger.warning('[AwaitTask] Timeout after %ds waiting for task %s (output=%d chars)',
                       timeout, task_id, content_len)
        return (
            f'Timeout after {timeout}s: task {task_id} is still running.\n'
            f'  Current output: {content_len} chars\n'
            f'  You can call await_task again to continue waiting.'
        )

    return f'Error: Unknown await_task action: {action}. Use list/wait/status.'
