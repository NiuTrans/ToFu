"""lib/scheduler/executor — Tool execution handlers for scheduler commands.

Facade-preserving package.  ``execute_scheduler_tool`` is the router that
dispatches scheduler tool calls to the appropriate handler:

  schedule_create / schedule_list / schedule_manage — handled inline here
      (thin wrappers over ``lib.scheduler.manager``)
  await_task   — delegated to ._await._execute_await_task
  timer_create — delegated to ._timer._execute_timer_create
  timer_manage — delegated to ._timer._execute_timer_manage

The public import path (``lib.scheduler.executor``) and every symbol
(``execute_scheduler_tool`` plus the ``_execute_*`` / ``_coerce_int_arg``
helpers) are preserved byte-identically for backwards compatibility.
"""

from lib.identity import PrincipalContext
from lib.log import get_logger
from lib.scheduler.cron import describe_cron, next_cron_run
from lib.scheduler.executor._await import _execute_await_task
from lib.scheduler.executor._common import _coerce_int_arg
from lib.scheduler.executor._timer import (
    _execute_timer_create,
    _execute_timer_manage,
)
from lib.scheduler.manager import get_scheduler

logger = get_logger(__name__)


def execute_scheduler_tool(fn_name, fn_args):
    """Execute a scheduler tool call. Returns string result for LLM."""
    mgr = get_scheduler()
    try:
        user_id = int(fn_args.get('_user_id'))
    except (TypeError, ValueError):
        return 'Error: Scheduler tools require an authenticated owner.'
    if user_id < 1:
        return 'Error: Scheduler tools require an authenticated owner.'
    principal = PrincipalContext.user(
        subject_id=f'scheduler-tool:user:{user_id}',
        owner_user_id=user_id,
        scopes={'agents:scheduler'},
    )

    if fn_name == 'schedule_create':
        try:
            task_type = fn_args.get('task_type', 'command')

            # For agent tasks, resolve 'current' conv_id from the calling context
            target_conv_id = fn_args.get('target_conv_id', '')
            source_conv_id = fn_args.get('_source_conv_id', '')  # injected by executor
            if target_conv_id == 'current' and source_conv_id:
                target_conv_id = source_conv_id

            create_kwargs = dict(
                principal=principal,
                name=fn_args['name'],
                schedule=fn_args['schedule'],
                command=fn_args['command'],
                task_type=task_type,
                description=fn_args.get('description', ''),
                max_runtime=fn_args.get('max_runtime', 300),
                condition_command=fn_args.get('condition_command', ''),
                condition_regex=fn_args.get('condition_regex', ''),
            )

            # Agent-specific fields
            if task_type == 'agent':
                if not target_conv_id:
                    return ('Error: target_conv_id is required for agent tasks. '
                            'Use "current" for this conversation.')
                create_kwargs.update(
                    target_conv_id=target_conv_id,
                    source_conv_id=source_conv_id,
                    tools_config=fn_args.get('tools_config'),
                    max_executions=fn_args.get('max_executions', 0),
                    expires_at=fn_args.get('expires_at', ''),
                )

            task = mgr.create_task(**create_kwargs)
            next_run = ''
            if not task['schedule'].startswith('once:'):
                nxt = next_cron_run(task['schedule'])
                next_run = f'\n  Next run: {nxt.strftime("%Y-%m-%d %H:%M")}' if nxt else ''

            result = (f'Task created successfully.\n'
                      f'  ID: {task["id"]}\n'
                      f'  Name: {task["name"]}\n'
                      f'  Schedule: {describe_cron(task["schedule"])}\n'
                      f'  Type: {task["task_type"]}\n'
                      f'  Instruction: {task["command"][:200]}'
                      f'{next_run}')

            if task_type == 'agent':
                result += (f'\n  Target conv: {target_conv_id[:12]}\n'
                           f'  Mode: Proactive Agent (poll->decide->execute)')
                max_exec = fn_args.get('max_executions', 0)
                if max_exec > 0:
                    result += f'\n  Max executions: {max_exec}'
                expires = fn_args.get('expires_at', '')
                if expires:
                    result += f'\n  Expires: {expires}'

            return result
        except ValueError as e:
            logger.warning('[Scheduler] schedule_create validation failed: %s', e, exc_info=True)
            return f'Error: {e}'

    elif fn_name == 'schedule_list':
        tasks = mgr.list_tasks(
            user_id=user_id,
            include_disabled=fn_args.get('include_disabled', False))
        if not tasks:
            return 'No scheduled tasks found. Use schedule_create to create one.'

        lines = [f'Scheduled Tasks ({len(tasks)}):']
        lines.append('-' * 60)
        for t in tasks:
            status = 'enabled' if t['enabled'] else 'disabled'
            last = t.get('last_status', 'never')

            is_agent = t['task_type'] == 'agent'
            type_label = 'Proactive Agent' if is_agent else t['task_type']

            lines.append(
                f'[{status}] [{t["id"]}] {t["name"]}\n'
                f'    Schedule: {t.get("schedule_human", t["schedule"])}\n'
                f'    Type: {type_label} | Runs: {t["run_count"]} | Fails: {t["fail_count"]}\n'
                f'    Last: {last} {t.get("last_run", "never")}\n'
                f'    Next: {t.get("next_run", "N/A")}\n'
                f'    Command: {t["command"][:100]}'
            )

            # Extra info for agent tasks
            if is_agent:
                lines.append(
                    f'    Polls: {t.get("poll_count", 0)} | '
                    f'Executions: {t.get("execution_count", 0)}'
                    f'{" / " + str(t["max_executions"]) if t.get("max_executions") else ""}\n'
                    f'    Last poll: {t.get("last_poll_decision", "none")} '
                    f'({t.get("last_poll_reason", "")[:80]})\n'
                    f'    Target conv: {t.get("target_conv_id", "?")[:12]}'
                )

            lines.append('')
        return '\n'.join(lines)

    elif fn_name == 'schedule_manage':
        action = fn_args['action']
        task_id = fn_args.get('task_id', '')

        if action == 'log':
            log = mgr.get_execution_log(
                user_id=user_id, limit=fn_args.get('limit', 20))
            if not log:
                return 'No execution log entries yet.'
            lines = ['Recent Execution Log:']
            for entry in reversed(log):
                status = 'OK' if entry['success'] else 'FAIL'
                lines.append(f'  [{status}] [{entry["time"]}] {entry["task_name"]}: {entry["result"][:200]}')
            return '\n'.join(lines)

        if not task_id:
            return 'Error: task_id is required for this action'

        if action == 'run':
            success, result = mgr.run_task_now(task_id, user_id=user_id)
            if success is None:
                return f'Error: Task {task_id} not found'
            status = 'OK' if success else 'FAIL'
            return f'[{status}] Task executed:\n{result[:5000]}'

        elif action == 'enable':
            enabled = mgr.toggle_task(
                task_id, user_id=user_id, enabled=True)
            return f'Task {task_id} enabled' if enabled is not None else 'Error: Task not found'

        elif action == 'disable':
            enabled = mgr.toggle_task(
                task_id, user_id=user_id, enabled=False)
            return f'Task {task_id} disabled' if enabled is not None else 'Error: Task not found'

        elif action == 'delete':
            deleted = mgr.delete_task(task_id, user_id=user_id)
            return (f'Task {task_id} deleted' if deleted
                    else 'Error: Task not found')

        elif action == 'update':
            updates = fn_args.get('updates', {})
            if not updates:
                return 'Error: No updates provided'
            changed = mgr.update_task(task_id, user_id=user_id, **updates)
            return (f'Task {task_id} updated: {", ".join(updates.keys())}'
                    if changed else 'Error: Task not found')

    elif fn_name == 'await_task':
        return _execute_await_task(fn_args, user_id=user_id)

    elif fn_name == 'timer_create':
        return _execute_timer_create(fn_args)

    elif fn_name == 'timer_manage':
        return _execute_timer_manage(fn_args)

    return f'Error: Unknown scheduler tool: {fn_name}'


__all__ = ['execute_scheduler_tool']
