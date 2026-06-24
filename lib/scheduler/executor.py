"""lib/scheduler/executor.py — Tool execution handlers for scheduler commands."""

import time as _time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.scheduler.cron import describe_cron, next_cron_run
from lib.scheduler.manager import get_scheduler

logger = get_logger(__name__)


def _coerce_int_arg(name, raw, default):
    """Coerce an LLM-supplied tool arg to int, with warning on fallback.

    LLM tool-call arguments sometimes arrive as strings (e.g. ``"60"``)
    even for parameters declared as integers in the tool schema. Rather
    than raising ``TypeError`` deep inside the call chain, we coerce
    here and fall back to *default* with a warning if the value can't
    be parsed.
    """
    if isinstance(raw, bool):
        # bool is a subclass of int — reject explicitly to avoid
        # silently mapping True→1 / False→0 for numeric params.
        logger.warning('[Timer] Boolean %s=%r passed as numeric — '
                       'coerced to default %d', name, raw, default)
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as _e:
        logger.warning('[Timer] Non-integer %s=%r — coerced to default %d '
                       '(reason: %s)', name, raw, default, _e)
        return default


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
        tasks = mgr.list_tasks(include_disabled=fn_args.get('include_disabled', False))
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
            log = mgr.get_execution_log(limit=fn_args.get('limit', 20))
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
            success, result = mgr.run_task_now(task_id)
            if success is None:
                return f'Error: Task {task_id} not found'
            status = 'OK' if success else 'FAIL'
            return f'[{status}] Task executed:\n{result[:5000]}'

        elif action == 'enable':
            enabled = mgr.toggle_task(task_id, enabled=True)
            return f'Task {task_id} enabled' if enabled is not None else 'Error: Task not found'

        elif action == 'disable':
            enabled = mgr.toggle_task(task_id, enabled=False)
            return f'Task {task_id} disabled' if enabled is not None else 'Error: Task not found'

        elif action == 'delete':
            mgr.delete_task(task_id)
            return f'Task {task_id} deleted'

        elif action == 'update':
            updates = fn_args.get('updates', {})
            if not updates:
                return 'Error: No updates provided'
            mgr.update_task(task_id, **updates)
            return f'Task {task_id} updated: {", ".join(updates.keys())}'

    elif fn_name == 'await_task':
        return _execute_await_task(fn_args)

    elif fn_name == 'timer_create':
        return _execute_timer_create(fn_args)

    elif fn_name == 'timer_manage':
        return _execute_timer_manage(fn_args)

    return f'Error: Unknown scheduler tool: {fn_name}'


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
        with tasks_lock:
            t = tasks.get(task_id)
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
        with tasks_lock:
            t = tasks.get(task_id)
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
            with tasks_lock:
                t = tasks.get(task_id)
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
        with tasks_lock:
            t = tasks.get(task_id)
        content_len = len(t.get('content', '')) if t else 0
        logger.warning('[AwaitTask] Timeout after %ds waiting for task %s (output=%d chars)',
                       timeout, task_id, content_len)
        return (
            f'Timeout after {timeout}s: task {task_id} is still running.\n'
            f'  Current output: {content_len} chars\n'
            f'  You can call await_task again to continue waiting.'
        )

    return f'Error: Unknown await_task action: {action}. Use list/wait/status.'


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
        return 'Error: Both check_instruction and continuation_message are required.'

    conv_id = fn_args.get('_source_conv_id', '')
    if not conv_id:
        return 'Error: Could not determine conversation ID. Timer must be created within a conversation.'

    parent_task = fn_args.get('_parent_task')
    round_num = fn_args.get('_tool_round_num')  # SSE roundNum for this tool call
    tc_id = fn_args.get('_tool_call_id', '')  # SSE toolCallId for collision-proof matching

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

    # Coerce numeric LLM-supplied args defensively — timer schema declares
    # them as integers but some models pass them as strings.
    _poll_interval_arg = _coerce_int_arg(
        'poll_interval', fn_args.get('poll_interval', 60), 60)
    _max_polls_arg = _coerce_int_arg(
        'max_polls', fn_args.get('max_polls', 120), 120)

    try:
        timer = create_timer(
            conv_id=conv_id,
            check_instruction=check_instruction,
            continuation_message=continuation_message,
            poll_interval=_poll_interval_arg,
            max_polls=_max_polls_arg,
            check_command=fn_args.get('check_command', ''),
            tools_config=_poll_tools_config,
            source_task_id=fn_args.get('_source_task_id', ''),
        )
        timer_id = timer['id']
        poll_interval = timer['poll_interval']
        max_polls = timer['max_polls']

        logger.info('[Timer:%s] Inline blocking poll started (interval=%ds, max=%d)',
                    timer_id, poll_interval, max_polls)

        # ── Helper: update the toolRound entry in task['toolRounds'] ──
        # so that SSE state snapshots include _timerPolls for reconnection.
        def _attach_poll_to_round(poll_entry):
            """Append a poll entry to the toolRound's _timerPolls list."""
            if not parent_task:
                return
            for sr in parent_task.get('toolRounds', []):
                if sr.get('roundNum') == round_num:
                    if '_timerPolls' not in sr:
                        sr['_timerPolls'] = []
                    sr['_timerPolls'].append(poll_entry)
                    sr['_timerTimerId'] = timer_id
                    break

        # Emit initial status so frontend shows "watching…".  We attach the
        # full check instruction + command and the poll cadence to the round
        # so the UI can explain WHAT is being verified and HOW — not just a
        # bare "watching" line.
        _check_cmd_full = timer.get('check_command', '') or ''
        if parent_task and round_num is not None:
            for sr in parent_task.get('toolRounds', []):
                if sr.get('roundNum') == round_num:
                    sr['_timerCheckInstruction'] = check_instruction
                    sr['_timerCheckCommand'] = _check_cmd_full
                    sr['_timerPollInterval'] = poll_interval
                    sr['_timerMaxPolls'] = max_polls
                    break
            _started_poll = {
                'pollNum': 0,
                'decision': 'started',
                'reason': f'Timer created — polling every {poll_interval}s (max {max_polls})',
                'tokensUsed': 0,
                'timerId': timer_id,
                'ts': int(_time.time() * 1000),
            }
            _attach_poll_to_round(_started_poll)
            append_event(parent_task, build_event(
                EventType.TIMER_POLL_CHECK,
                roundNum=round_num,
                toolCallId=tc_id,
                timerId=timer_id,
                pollNum=0,
                decision='started',
                reason=f'Timer created — polling every {poll_interval}s (max {max_polls})',
                checkInstruction=check_instruction[:600],
                checkCommand=_check_cmd_full[:400],
                pollInterval=poll_interval,
                maxPolls=max_polls,
                nextPollTs=int((_time.time() + poll_interval) * 1000),
            ))

        poll_count = 0
        while True:
            # ── Check abort ──
            if parent_task and parent_task.get('aborted'):
                logger.info('[Timer:%s] Parent task aborted — cancelling timer', timer_id)
                from lib.scheduler.timer import cancel_timer
                cancel_timer(timer_id)
                return f'Timer {timer_id} cancelled: task was aborted by the user.'

            # ── Sleep ──
            _time.sleep(poll_interval)

            # ── Check abort again after sleep ──
            if parent_task and parent_task.get('aborted'):
                logger.info('[Timer:%s] Parent task aborted after sleep — cancelling', timer_id)
                from lib.scheduler.timer import cancel_timer
                cancel_timer(timer_id)
                return f'Timer {timer_id} cancelled: task was aborted by the user.'

            # ── Max polls check ──
            poll_count += 1
            if max_polls > 0 and poll_count > max_polls:
                logger.info('[Timer:%s] Max polls (%d) exhausted', timer_id, max_polls)
                _mark_exhausted(timer_id)
                return (
                    f'Timer {timer_id} exhausted after {poll_count - 1} polls.\n'
                    f'Conditions were never met within the poll limit.\n'
                    f'Continuation message was: {continuation_message[:200]}'
                )

            # Stable per-poll id so this exact check is locatable across the
            # log file, the DB row, and the UI (matches the background loop).
            poll_id = f'{timer_id}.p{poll_count}'
            # ── Run poll ──
            try:
                (ready, reason, tokens_used, skipped, parse_error, cmd_output,
                 poll_model, tool_trace, raw_content) = poll_timer(timer_id)
            except Exception as e:
                logger.error('[Timer:%s] Poll %s error: %s', timer_id, poll_id, e, exc_info=True)
                _record_poll(timer_id, 'error', str(e)[:200], 0, poll_id=poll_id,
                             raw_output=str(e)[:2000])
                _increment_poll_count(timer_id, 'error', str(e)[:200])
                # Emit error event
                if parent_task and round_num is not None:
                    _err_poll = {
                        'pollNum': poll_count,
                        'pollId': poll_id,
                        'decision': 'error',
                        'reason': f'Poll error: {str(e)[:100]}',
                        'rawContent': str(e)[:2000],
                        'tokensUsed': 0,
                        'timerId': timer_id,
                        'ts': int(_time.time() * 1000),
                    }
                    _attach_poll_to_round(_err_poll)
                    append_event(parent_task, build_event(
                        EventType.TIMER_POLL_CHECK,
                        roundNum=round_num,
                        toolCallId=tc_id,
                        timerId=timer_id,
                        pollNum=poll_count,
                        pollId=poll_id,
                        decision='error',
                        reason=f'Poll error: {str(e)[:100]}',
                        rawContent=str(e)[:2000],
                    ))
                continue

            # Skipped polls (unchanged command output) — no LLM call,
            # no DB record, but emit lightweight heartbeat so the UI
            # can show "waiting… output unchanged (N consecutive skips)"
            # instead of appearing frozen.
            if skipped:
                logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)',
                             timer_id, poll_count)
                if parent_task and round_num is not None:
                    _now_ms = int(_time.time() * 1000)
                    # Attach skip metadata directly to the toolRound so state
                    # snapshots (page refresh, reconnect) can reconstruct the UI.
                    for sr in parent_task.get('toolRounds', []):
                        if sr.get('roundNum') == round_num:
                            sr['_timerSkipCount'] = sr.get('_timerSkipCount', 0) + 1
                            sr['_timerLastSkipTs'] = _now_ms
                            sr['_timerLastSkipPollNum'] = poll_count
                            sr['_timerTimerId'] = timer_id
                            break
                    append_event(parent_task, build_event(
                        EventType.TIMER_POLL_CHECK,
                        roundNum=round_num,
                        toolCallId=tc_id,
                        timerId=timer_id,
                        pollNum=poll_count,
                        decision='skipped',
                        reason='check_command output unchanged — LLM call skipped',
                        nextPollTs=int((_time.time() + poll_interval) * 1000),
                    ))
                continue

            decision = 'ready' if ready else ('parse_error' if parse_error else 'wait')
            # Persist the raw LLM output only when diagnostically useful (a
            # malformed decision) — a clean wait/ready needs no raw dump.
            _raw_to_store = raw_content if parse_error else ''
            _record_poll(timer_id, decision, reason, tokens_used, cmd_output, poll_model,
                         poll_id=poll_id, raw_output=_raw_to_store)
            _increment_poll_count(timer_id, decision, reason)

            logger.info('[Timer:%s] Poll %s: %s — %s (tokens=%d, model=%s)',
                        timer_id, poll_id, decision, reason[:80], tokens_used,
                        poll_model or '?')

            # ── Emit SSE event for each poll check ──
            #   Carry the (truncated) check_command output so the UI can show
            #   the evidence behind the decision, plus the next-poll timestamp
            #   so it can render a "next check in Ns" countdown.  reason is
            #   sent fuller (400 chars) since the UI now offers expand.
            _cmd_snippet = (cmd_output or '')[:1200]
            # Surface the raw LLM output to the UI only when the decision could
            # not be parsed — that is exactly when the model's actual text is
            # what the user needs to see (and it's also persisted in the DB).
            _raw_snippet = (raw_content or '')[:2000] if parse_error else ''
            if parent_task and round_num is not None:
                _poll_entry = {
                    'pollNum': poll_count,
                    'pollId': poll_id,
                    'decision': decision,
                    'reason': reason[:400],
                    'tokensUsed': tokens_used,
                    'timerId': timer_id,
                    'cmdOutput': _cmd_snippet,
                    'parseError': parse_error,
                    'rawContent': _raw_snippet,
                    'model': poll_model,
                    'toolTrace': tool_trace,
                    'ts': int(_time.time() * 1000),
                }
                _attach_poll_to_round(_poll_entry)
                # Mark the round as triggered if ready
                if ready:
                    for sr in parent_task.get('toolRounds', []):
                        if sr.get('roundNum') == round_num:
                            sr['_timerTriggered'] = True
                            sr['status'] = 'done'
                            break
                append_event(parent_task, build_event(
                    EventType.TIMER_POLL_CHECK,
                    roundNum=round_num,
                    toolCallId=tc_id,
                    timerId=timer_id,
                    pollNum=poll_count,
                    pollId=poll_id,
                    decision=decision,
                    reason=reason[:400],
                    tokensUsed=tokens_used,
                    cmdOutput=_cmd_snippet,
                    parseError=parse_error,
                    rawContent=_raw_snippet,
                    model=poll_model,
                    toolTrace=tool_trace,
                    nextPollTs=int((_time.time() + poll_interval) * 1000),
                ))

            if ready:
                logger.info('[Timer:%s] Conditions met at poll #%d - returning result',
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
                    f'Timer {timer_id} triggered after {poll_count} polls.\n'
                    f'Detection result: {reason}\n\n'
                    f'The conditions you were watching for have been met.\n'
                    f'Original continuation message: {continuation_message}\n\n'
                    f'Please proceed with the continuation instructions above.'
                )

    except Exception as e:
        logger.error('[Timer] timer_create failed: %s', e, exc_info=True)
        return f'Error: Failed to create timer: {e}'


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
            return 'No timers found. Use timer_create to create one.'

        lines = [f'Timer Watchers ({len(timers)}):']
        lines.append('-' * 50)
        for t in timers:
            lines.append(
                f'[{t["status"]}] [{t["id"]}]\n'
                f'    Conv: {t["conv_id"][:12]}...\n'
                f'    Polls: {t["poll_count"]} / {t["max_polls"]}\n'
                f'    Interval: {t["poll_interval"]}s\n'
                f'    Last poll: {t.get("last_poll_decision", "-")} '
                f'({t.get("last_poll_reason", "")[:60]})\n'
                f'    Check: {t["check_instruction"][:100]}\n'
                f'    Created: {t["created_at"]}'
            )
            lines.append('')
        return '\n'.join(lines)

    if not timer_id:
        return 'Error: timer_id is required for this action.'

    if action == 'cancel':
        cancel_timer(timer_id)
        return f'Timer {timer_id} cancelled.'

    elif action == 'status':
        timer = get_timer(timer_id)
        if not timer:
            return f'Error: Timer {timer_id} not found.'
        result = (
            f'Timer {timer_id}\n'
            f'  Status: {timer["status"]}\n'
            f'  Conv: {timer["conv_id"][:12]}\n'
            f'  Polls: {timer["poll_count"]} / {timer["max_polls"]}\n'
            f'  Interval: {timer["poll_interval"]}s\n'
            f'  Last poll: {timer.get("last_poll_at", "never")} '
            f'({timer.get("last_poll_decision", "-")})\n'
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
            return f'No poll log entries for timer {timer_id}.'
        lines = [f'Poll Log for {timer_id} (newest first):']
        for entry in log:
            lines.append(
                f'  [{entry["decision"].upper()}] {entry["poll_time"]} -- '
                f'{entry.get("reason", "")[:80]} (tokens: {entry.get("tokens_used", 0)})'
            )
        return '\n'.join(lines)

    return f'Error: Unknown timer_manage action: {action}. Use cancel/status/list/log.'


__all__ = ['execute_scheduler_tool']
