"""lib/scheduler/executor/_timer.py — timer_create / timer_manage tool handlers.

Implements the blocking inline poll loop for ``timer_create`` (emitting
``timer_poll_check`` SSE events each poll) and the ``timer_manage`` tool
(cancel / status / list / log).
"""

import time as _time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.scheduler.executor._common import _coerce_int_arg

logger = get_logger(__name__)


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
    condition_command = fn_args.get('condition_command', '')
    # A pure-code timer needs only a predicate + continuation (no LLM
    # instruction). Otherwise an LLM/hybrid timer needs a check_instruction.
    if not continuation_message:
        return 'Error: continuation_message is required.'
    if not check_instruction and not condition_command:
        return ('Error: provide check_instruction (LLM/hybrid) and/or '
                'condition_command (pure-code predicate).')

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
            condition_command=fn_args.get('condition_command', ''),
            condition_regex=fn_args.get('condition_regex', ''),
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
        _cond_kind = timer.get('condition_kind', 'llm') or 'llm'
        _cond_cmd = timer.get('condition_command', '') or ''
        if parent_task and round_num is not None:
            for sr in parent_task.get('toolRounds', []):
                if sr.get('roundNum') == round_num:
                    sr['_timerCheckInstruction'] = check_instruction
                    sr['_timerCheckCommand'] = _check_cmd_full
                    sr['_timerConditionKind'] = _cond_kind
                    sr['_timerConditionCommand'] = _cond_cmd
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
                checkInstruction=check_instruction[:4000],
                checkCommand=_check_cmd_full[:400],
                conditionKind=_cond_kind,
                conditionCommand=_cond_cmd[:400],
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
            # A hybrid timer can AUTO-PROMOTE to pure `code` mid-run once its
            # predicate has agreed with the LLM enough times (see
            # reconcile_and_decide). Re-read the CURRENT kind each poll so the
            # UI badge flips hybrid→command-based when that happens, instead of
            # showing the stale creation-time kind forever.
            _cur_kind = _cond_kind
            try:
                from lib.scheduler.timer import get_timer as _get_timer
                _cur_row = _get_timer(timer_id)
                if _cur_row and _cur_row.get('condition_kind'):
                    _cur_kind = _cur_row['condition_kind']
            except Exception as _ke:
                logger.debug('[Timer:%s] current-kind read failed: %s', timer_id, _ke)
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
                # Keep the toolRound's kind in sync with a mid-run promotion so
                # a page refresh / reconnect (which rebuilds from the snapshot)
                # shows the CURRENT kind, not the creation-time one.
                for sr in parent_task.get('toolRounds', []):
                    if sr.get('roundNum') == round_num:
                        sr['_timerConditionKind'] = _cur_kind
                        break
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
                    conditionKind=_cur_kind,
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

                # Clean up command output cache + reconcile audit stash
                from lib.scheduler.timer import _cmd_outputs_lock, _last_cmd_outputs
                with _cmd_outputs_lock:
                    _last_cmd_outputs.pop(timer_id, None)
                from lib.scheduler.timer import _reconcile_audit, _reconcile_audit_lock
                with _reconcile_audit_lock:
                    _reconcile_audit.pop(timer_id, None)

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
