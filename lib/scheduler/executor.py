"""lib/scheduler/executor.py — Tool execution handlers for scheduler commands."""

import time as _time

from lib.log import get_logger
from lib.scheduler.cron import describe_cron, next_cron_run
from lib.scheduler.manager import get_scheduler

logger = get_logger(__name__)


def execute_scheduler_tool(fn_name, fn_args):
    """Execute a scheduler tool call. Returns string result for LLM."""
    mgr = get_scheduler()

    if fn_name == 'schedule_create':
        try:
            task_type = fn_args.get('task_type', 'command')

            # For agent tasks, resolve 'current' conv_id from the calling context
            target_conv_id = fn_args.get('target_conv_id', '')
            source_conv_id = fn_args.get('_source_conv_id', '')  # injected by executor
            if target_conv_id == 'current' and source_conv_id:
                target_conv_id = source_conv_id

            create_kwargs = dict(
                name=fn_args['name'],
                schedule=fn_args['schedule'],
                command=fn_args['command'],
                task_type=task_type,
                description=fn_args.get('description', ''),
                max_runtime=fn_args.get('max_runtime', 300),
            )

            # Agent-specific fields
            if task_type == 'agent':
                if not target_conv_id:
                    return ('❌ target_conv_id is required for agent tasks. '
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

            result = (f'✅ Task created successfully!\n'
                      f'  ID: {task["id"]}\n'
                      f'  Name: {task["name"]}\n'
                      f'  Schedule: {describe_cron(task["schedule"])}\n'
                      f'  Type: {task["task_type"]}\n'
                      f'  Instruction: {task["command"][:200]}'
                      f'{next_run}')

            if task_type == 'agent':
                result += (f'\n  Target conv: {target_conv_id[:12]}\n'
                           f'  Mode: 🤖 Proactive Agent (poll→decide→execute)')
                max_exec = fn_args.get('max_executions', 0)
                if max_exec > 0:
                    result += f'\n  Max executions: {max_exec}'
                expires = fn_args.get('expires_at', '')
                if expires:
                    result += f'\n  Expires: {expires}'

            return result
        except ValueError as e:
            logger.warning('[Scheduler] schedule_create validation failed: %s', e, exc_info=True)
            return f'❌ {e}'

    elif fn_name == 'schedule_list':
        tasks = mgr.list_tasks(include_disabled=fn_args.get('include_disabled', False))
        if not tasks:
            return '📋 No scheduled tasks found. Use schedule_create to create one.'

        lines = [f'📋 Scheduled Tasks ({len(tasks)}):']
        lines.append('─' * 60)
        for t in tasks:
            status = '🟢' if t['enabled'] else '🔴'
            last = t.get('last_status', 'never')
            last_icon = {'ok': '✅', 'failed': '❌', 'never': '⏳'}.get(last, '❓')

            is_agent = t['task_type'] == 'agent'
            type_label = '🤖 Proactive Agent' if is_agent else t['task_type']

            lines.append(
                f'{status} [{t["id"]}] {t["name"]}\n'
                f'    Schedule: {t.get("schedule_human", t["schedule"])}\n'
                f'    Type: {type_label} | Runs: {t["run_count"]} | Fails: {t["fail_count"]}\n'
                f'    Last: {last_icon} {t.get("last_run", "never")}\n'
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
            log = mgr.get_execution_log(limit=fn_args.get('limit', 20))
            if not log:
                return '📋 No execution log entries yet.'
            lines = ['📋 Recent Execution Log:']
            for entry in reversed(log):
                icon = '✅' if entry['success'] else '❌'
                lines.append(f'  {icon} [{entry["time"]}] {entry["task_name"]}: {entry["result"][:200]}')
            return '\n'.join(lines)

        if not task_id:
            return '❌ task_id is required for this action'

        if action == 'run':
            success, result = mgr.run_task_now(task_id)
            if success is None:
                return f'❌ Task {task_id} not found'
            icon = '✅' if success else '❌'
            return f'{icon} Task executed:\n{result[:5000]}'

        elif action == 'enable':
            enabled = mgr.toggle_task(task_id, enabled=True)
            return f'✅ Task {task_id} enabled' if enabled is not None else '❌ Task not found'

        elif action == 'disable':
            enabled = mgr.toggle_task(task_id, enabled=False)
            return f'✅ Task {task_id} disabled' if enabled is not None else '❌ Task not found'

        elif action == 'delete':
            mgr.delete_task(task_id)
            return f'🗑️ Task {task_id} deleted'

        elif action == 'update':
            updates = fn_args.get('updates', {})
            if not updates:
                return '❌ No updates provided'
            mgr.update_task(task_id, **updates)
            return f'✅ Task {task_id} updated: {", ".join(updates.keys())}'

    elif fn_name == 'await_task':
        return _execute_await_task(fn_args)

    elif fn_name == 'timer_create':
        return _execute_timer_create(fn_args)

    elif fn_name == 'timer_manage':
        return _execute_timer_manage(fn_args)

    return f'❌ Unknown scheduler tool: {fn_name}'


def _execute_await_task(fn_args):
    """Handle await_task tool — list/wait/status for cross-conversation tasks."""
    from lib.tasks_pkg.manager import tasks, tasks_lock

    action = fn_args.get('action', 'list')

    if action == 'list':
        with tasks_lock:
            running = [
                {
                    'task_id': t['id'],
                    'conv_id': t.get('convId', '?'),
                    'status': t['status'],
                    'elapsed': round(_time.time() - t.get('created_at', _time.time())),
                    'content_len': len(t.get('content', '')),
                }
                for t in tasks.values()
                if t.get('status') == 'running'
            ]
        if not running:
            return '📋 No tasks currently running. All conversations are idle.'
        lines = [f'📋 Currently Running Tasks ({len(running)}):']
        lines.append('─' * 50)
        for r in running:
            lines.append(
                f'  🔄 Task: {r["task_id"][:12]}…\n'
                f'     Conversation: {r["conv_id"][:12]}…\n'
                f'     Running for: {r["elapsed"]}s\n'
                f'     Output so far: {r["content_len"]} chars'
            )
            lines.append('')
        return '\n'.join(lines)

    task_id = fn_args.get('task_id', '')
    if not task_id:
        return '❌ task_id is required for wait/status actions. Use action="list" to discover running tasks.'

    if action == 'status':
        with tasks_lock:
            t = tasks.get(task_id)
        if not t:
            return f'❌ Task {task_id} not found (may have already been cleaned up).'
        elapsed = round(_time.time() - t.get('created_at', _time.time()))
        return (
            f'📊 Task Status:\n'
            f'  ID: {t["id"]}\n'
            f'  Conversation: {t.get("convId", "?")}\n'
            f'  Status: {t["status"]}\n'
            f'  Running for: {elapsed}s\n'
            f'  Output: {len(t.get("content", ""))} chars\n'
            f'  Error: {t.get("error") or "none"}'
        )

    if action == 'wait':
        timeout = min(fn_args.get('timeout', 600), 3600)
        poll_interval = max(fn_args.get('poll_interval', 5), 2)
        deadline = _time.time() + timeout
        parent_task = fn_args.get('_parent_task')  # injected by tool_dispatch

        # First check if it exists
        with tasks_lock:
            t = tasks.get(task_id)
        if not t:
            return f'❌ Task {task_id} not found. It may have already finished.'

        if t.get('status') != 'running':
            return (
                f'✅ Task {task_id} already finished.\n'
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
                return '⛔ Wait cancelled — your task was aborted by the user.'
            with tasks_lock:
                t = tasks.get(task_id)
            if not t:
                logger.info('[AwaitTask] Task %s completed and cleaned up', task_id)
                return f'✅ Task {task_id} has completed and been cleaned up.'
            if t.get('status') != 'running':
                elapsed = round(_time.time() - t.get('created_at', _time.time()))
                snippet = t.get('content', '')[-500:] if t.get('content') else '(empty)'
                logger.info('[AwaitTask] Task %s finished with status=%s after %ds',
                            task_id, t['status'], elapsed)
                return (
                    f'✅ Task {task_id} finished!\n'
                    f'  Status: {t["status"]}\n'
                    f'  Total time: {elapsed}s\n'
                    f'  Final output ({len(t.get("content", ""))} chars, last 500):\n'
                    f'  {snippet}\n'
                    f'  Error: {t.get("error") or "none"}'
                )

        # Timeout
        with tasks_lock:
            t = tasks.get(task_id)
        content_len = len(t.get('content', '')) if t else 0
        logger.warning('[AwaitTask] Timeout after %ds waiting for task %s (output=%d chars)',
                       timeout, task_id, content_len)
        return (
            f'⏰ Timeout after {timeout}s — task {task_id} is still running.\n'
            f'  Current output: {content_len} chars\n'
            f'  You can call await_task again to continue waiting.'
        )

    return f'❌ Unknown await_task action: {action}. Use list/wait/status.'


def _execute_timer_create(fn_args):
    """Handle timer_create tool — blocking inline poll with SSE events.

    Unlike the old fire-and-forget approach, this blocks the tool call
    and polls inline.  Each poll emits a ``timer_poll_check`` SSE event
    so the frontend can render collapsible check rounds.  When conditions
    are met the final result is returned as the tool result and the LLM
    continues its tool loop normally.
    """
    from lib.scheduler.timer import (
        _increment_poll_count,
        _mark_exhausted,
        _record_poll,
        create_timer,
        poll_timer,
    )
    from lib.tasks_pkg.manager import append_event

    check_instruction = fn_args.get('check_instruction', '')
    continuation_message = fn_args.get('continuation_message', '')
    if not check_instruction or not continuation_message:
        return '❌ Both check_instruction and continuation_message are required.'

    conv_id = fn_args.get('_source_conv_id', '')
    if not conv_id:
        return '❌ Could not determine conversation ID. Timer must be created within a conversation.'

    parent_task = fn_args.get('_parent_task')
    round_num = fn_args.get('_tool_round_num')  # SSE roundNum for this tool call

    # ── Capture parent task's tool config so the timer poll can rebuild tools ──
    _parent_cfg = parent_task.get('config', {}) if parent_task else {}
    _poll_tools_config = {
        'projectPath': _parent_cfg.get('projectPath', ''),
        'searchMode': _parent_cfg.get('searchMode', 'multi'),
        'fetchEnabled': _parent_cfg.get('fetchEnabled', True),
        'codeExecEnabled': _parent_cfg.get('codeExecEnabled', False),
        'browserEnabled': _parent_cfg.get('browserEnabled', False),
        'imageGenEnabled': _parent_cfg.get('imageGenEnabled', False),
    }

    try:
        timer = create_timer(
            conv_id=conv_id,
            check_instruction=check_instruction,
            continuation_message=continuation_message,
            poll_interval=fn_args.get('poll_interval', 60),
            max_polls=fn_args.get('max_polls', 120),
            check_command=fn_args.get('check_command', ''),
            tools_config=_poll_tools_config,
            source_task_id=fn_args.get('_source_task_id', ''),
        )
        timer_id = timer['id']
        poll_interval = timer['poll_interval']
        max_polls = timer['max_polls']

        logger.info('[Timer:%s] Inline blocking poll started (interval=%ds, max=%d)',
                    timer_id, poll_interval, max_polls)

        # Emit initial status so frontend shows "watching…"
        if parent_task and round_num is not None:
            append_event(parent_task, {
                'type': 'timer_poll_check',
                'roundNum': round_num,
                'timerId': timer_id,
                'pollNum': 0,
                'decision': 'started',
                'reason': f'Timer created — polling every {poll_interval}s (max {max_polls})',
                'checkCommand': (timer.get('check_command', '') or '')[:100],
            })

        poll_count = 0
        while True:
            # ── Check abort ──
            if parent_task and parent_task.get('aborted'):
                logger.info('[Timer:%s] Parent task aborted — cancelling timer', timer_id)
                from lib.scheduler.timer import cancel_timer
                cancel_timer(timer_id)
                return f'⛔ Timer {timer_id} cancelled — task was aborted by the user.'

            # ── Sleep ──
            _time.sleep(poll_interval)

            # ── Check abort again after sleep ──
            if parent_task and parent_task.get('aborted'):
                logger.info('[Timer:%s] Parent task aborted after sleep — cancelling', timer_id)
                from lib.scheduler.timer import cancel_timer
                cancel_timer(timer_id)
                return f'⛔ Timer {timer_id} cancelled — task was aborted by the user.'

            # ── Max polls check ──
            poll_count += 1
            if max_polls > 0 and poll_count > max_polls:
                logger.info('[Timer:%s] Max polls (%d) exhausted', timer_id, max_polls)
                _mark_exhausted(timer_id)
                return (
                    f'⏰ Timer {timer_id} exhausted after {poll_count - 1} polls.\n'
                    f'Conditions were never met within the poll limit.\n'
                    f'Continuation message was: {continuation_message[:200]}'
                )

            # ── Run poll ──
            try:
                ready, reason, tokens_used, skipped = poll_timer(timer_id)
            except Exception as e:
                logger.error('[Timer:%s] Poll error: %s', timer_id, e, exc_info=True)
                _record_poll(timer_id, 'error', str(e)[:200], 0)
                _increment_poll_count(timer_id, 'error', str(e)[:200])
                # Emit error event
                if parent_task and round_num is not None:
                    append_event(parent_task, {
                        'type': 'timer_poll_check',
                        'roundNum': round_num,
                        'timerId': timer_id,
                        'pollNum': poll_count,
                        'decision': 'error',
                        'reason': f'Poll error: {str(e)[:100]}',
                    })
                continue

            # Skipped polls (unchanged command output) — no LLM call,
            # no DB record, no SSE event — silently wait.
            if skipped:
                logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)',
                             timer_id, poll_count)
                continue

            decision = 'ready' if ready else 'wait'
            _record_poll(timer_id, decision, reason, tokens_used)
            _increment_poll_count(timer_id, decision, reason)

            logger.info('[Timer:%s] Poll #%d: %s — %s (tokens=%d)',
                        timer_id, poll_count, decision, reason[:80], tokens_used)

            # ── Emit SSE event for each poll check ──
            if parent_task and round_num is not None:
                append_event(parent_task, {
                    'type': 'timer_poll_check',
                    'roundNum': round_num,
                    'timerId': timer_id,
                    'pollNum': poll_count,
                    'decision': decision,
                    'reason': reason[:200],
                    'tokensUsed': tokens_used,
                })

            if ready:
                logger.info('[Timer:%s] ✅ Conditions met at poll #%d — returning result',
                            timer_id, poll_count)
                # Mark timer as triggered in DB
                from datetime import datetime

                from lib.database import DOMAIN_SYSTEM, get_thread_db
                sysdb = get_thread_db(DOMAIN_SYSTEM)
                now_iso = datetime.now().isoformat()
                sysdb.execute(
                    "UPDATE timer_watchers SET status='triggered', triggered_at=?, updated_at=? WHERE id=?",
                    [now_iso, now_iso, timer_id]
                )
                sysdb.commit()

                # Clean up command output cache
                from lib.scheduler.timer import _cmd_outputs_lock, _last_cmd_outputs
                with _cmd_outputs_lock:
                    _last_cmd_outputs.pop(timer_id, None)

                # Return the result as the tool call output —
                # the LLM continues its loop as if this was a normal tool result
                return (
                    f'✅ Timer {timer_id} triggered after {poll_count} polls!\n'
                    f'Detection result: {reason}\n\n'
                    f'The conditions you were watching for have been met.\n'
                    f'Original continuation message: {continuation_message}\n\n'
                    f'Please proceed with the continuation instructions above.'
                )

    except Exception as e:
        logger.error('[Timer] timer_create failed: %s', e, exc_info=True)
        return f'❌ Failed to create timer: {e}'


def _execute_timer_manage(fn_args):
    """Handle timer_manage tool — cancel, status, list, log."""
    from lib.scheduler.timer import (
        cancel_timer,
        get_timer,
        get_timer_poll_log,
        list_active_timers,
    )

    action = fn_args.get('action', '')
    timer_id = fn_args.get('timer_id', '')

    if action == 'list':
        timers = list_active_timers()
        if not timers:
            return '⏱️ No timers found. Use timer_create to create one.'

        lines = [f'⏱️ Timer Watchers ({len(timers)}):']
        lines.append('─' * 50)
        for t in timers:
            status_icon = {
                'active': '🟢', 'triggered': '⏰',
                'cancelled': '🔴', 'exhausted': '⚪',
            }.get(t['status'], '❓')
            lines.append(
                f'{status_icon} [{t["id"]}] status={t["status"]}\n'
                f'    Conv: {t["conv_id"][:12]}…\n'
                f'    Polls: {t["poll_count"]} / {t["max_polls"]}\n'
                f'    Interval: {t["poll_interval"]}s\n'
                f'    Last poll: {t.get("last_poll_decision", "—")} '
                f'({t.get("last_poll_reason", "")[:60]})\n'
                f'    Check: {t["check_instruction"][:100]}\n'
                f'    Created: {t["created_at"]}'
            )
            lines.append('')
        return '\n'.join(lines)

    if not timer_id:
        return '❌ timer_id is required for this action.'

    if action == 'cancel':
        cancel_timer(timer_id)
        return f'🔴 Timer {timer_id} cancelled.'

    elif action == 'status':
        timer = get_timer(timer_id)
        if not timer:
            return f'❌ Timer {timer_id} not found.'
        status_icon = {
            'active': '🟢', 'triggered': '⏰',
            'cancelled': '🔴', 'exhausted': '⚪',
        }.get(timer['status'], '❓')
        result = (
            f'{status_icon} Timer {timer_id}\n'
            f'  Status: {timer["status"]}\n'
            f'  Conv: {timer["conv_id"][:12]}\n'
            f'  Polls: {timer["poll_count"]} / {timer["max_polls"]}\n'
            f'  Interval: {timer["poll_interval"]}s\n'
            f'  Last poll: {timer.get("last_poll_at", "never")} '
            f'({timer.get("last_poll_decision", "—")})\n'
            f'  Reason: {timer.get("last_poll_reason", "")[:100]}\n'
            f'  Check: {timer["check_instruction"][:200]}\n'
            f'  Command: {timer.get("check_command", "(none)")[:100] or "(none)"}\n'
            f'  Continuation: {timer["continuation_message"][:200]}\n'
            f'  Created: {timer["created_at"]}'
        )
        if timer.get('triggered_at'):
            result += f'\n  Triggered: {timer["triggered_at"]}'
        if timer.get('execution_task_id'):
            result += f'\n  Exec task: {timer["execution_task_id"]}'
        return result

    elif action == 'log':
        log = get_timer_poll_log(timer_id, limit=fn_args.get('limit', 20))
        if not log:
            return f'⏱️ No poll log entries for timer {timer_id}.'
        lines = [f'⏱️ Poll Log for {timer_id} (newest first):']
        for entry in log:
            icon = '✅' if entry['decision'] == 'ready' else '⏳' if entry['decision'] == 'wait' else '❌'
            lines.append(
                f'  {icon} {entry["poll_time"]} — {entry["decision"].upper()} — '
                f'{entry.get("reason", "")[:80]} (tokens: {entry.get("tokens_used", 0)})'
            )
        return '\n'.join(lines)

    return f'❌ Unknown timer_manage action: {action}. Use cancel/status/list/log.'


__all__ = ['execute_scheduler_tool']
