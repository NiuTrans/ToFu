"""Provider-neutral abnormal terminal recovery at the post-stream boundary.

Call ``recover_abnormal_terminal`` after cancellation and protocol-defined
program continuation, before legacy stream anomaly buckets. This owner only
chooses a loop directive: it never dispatches tools or rewinds settled history.
Provider-specific text normalization must run first and may explicitly mark an
unresolved control sequence in usage; this module does not parse model prose.
"""

from lib.agent_core.events import Phase, emit_phase

ABNORMAL_TERMINAL_RECOVERY_MAX = 2


def abnormal_terminal_kind(assistant_msg, finish_reason, task, usage=None):
    """Classify missing terminal output, excluding real tools and cancellation."""
    if task.get('aborted') or task.get('error'):
        return None

    from lib.tasks_pkg.stream_handler._intent_stall import _awaiting_human
    if _awaiting_human(task, assistant_msg, assistant_msg.get('content') or ''):
        return None
    # Reasoning-channel blowout (TB2.1 v8 regex-chess): four consecutive
    # rounds hit the completion ceiling with 200K+ reasoning chars, ZERO
    # content and no tool call — the loop just re-fed the truncated message
    # and the model blew out again, burning 90% of the trial budget. This
    # kind is detected before the stop/tool-calls gate below (which
    # deliberately excludes 'length') and gets a reasoning-degrade recovery.
    if (finish_reason == 'length'
            and not (assistant_msg.get('content') or '').strip()
            and not assistant_msg.get('tool_calls')
            and ((assistant_msg.get('reasoning_content') or '').strip()
                 or (usage or {}).get('_reasoning_chars', 0) > 0)):
        return 'reasoning_blowout'
    if ((usage or {}).get('_semantic_progress_timeout')
            or (usage or {}).get('_no_actionable_timeout')):
        return None
    # A real transport cut belongs to the lossless stream policy. The
    # fallback also flags clean empty stops as anomalies, distinguished by
    # _empty_stop; only those enter this terminal-response budget.
    if (usage or {}).get('_stream_anomaly') and not (usage or {}).get('_empty_stop'):
        return None
    if assistant_msg.get('tool_calls'):
        return None
    if finish_reason not in ('stop', 'tool_calls', 'tool_use'):
        return None
    if (usage or {}).get('_unresolved_tool_marker'):
        return 'unresolved_tool_marker'
    if finish_reason in ('tool_calls', 'tool_use'):
        return 'tool_calls_no_payload'
    if not (assistant_msg.get('content') or '').strip():
        return 'empty_stop'
    return None


def recover_abnormal_terminal(assistant_msg, finish_reason, task, *,
                              round_num, model, result, usage=None, messages=None):
    """Return a retry/failure directive or None; at most two retries per task.

    The counter deliberately does not use phase/round retry counters, which can
    reset after tools. Keep the original messages, tool receipts and accumulated
    prose untouched. Suppress whole-turn regeneration even on exhaustion: that
    recovery path could repeat already executed non-idempotent operations.
    """
    from lib.llm.terminal_tool_text import classify_longcat_tool_text

    profile = (task.get('config') or {}).get('harnessProfile', 'auto')
    if classify_longcat_tool_text(
            assistant_msg.get('content'), profile=profile, model=model,
            has_native_tool_calls=bool(assistant_msg.get('tool_calls'))):
        usage = dict(usage or {}, _unresolved_tool_marker=True)
    kind = abnormal_terminal_kind(assistant_msg, finish_reason, task, usage)
    if kind is None:
        return None
    task['_suppress_whole_turn_retry_to_preserve_partial'] = True
    if kind == 'reasoning_blowout':
        attempts = int(task.get('_reasoning_blowout_recovery_count') or 0)
    else:
        attempts = max(
            int(task.get('_abnormal_terminal_recovery_count') or 0),
            int(result.get('premature_retry_count') or 0),
        )
    decision = dict(result)
    if attempts < ABNORMAL_TERMINAL_RECOVERY_MAX:
        attempts += 1
        if kind == 'reasoning_blowout':
            task['_reasoning_blowout_recovery_count'] = attempts
            # The next round's body compiles with thinking off — the cheapest
            # legal rung on cannot-disable models, disabled elsewhere — so
            # the budget buys the answer instead of more analysis.
            task['_reasoning_blowout_degrade_next'] = True
        else:
            task['_abnormal_terminal_recovery_count'] = attempts

        decision['premature_retry_count'] = attempts
        if '_premature_retry_count_phase' in task:
            task['_premature_retry_count_phase'] = attempts
        if messages is not None:
            text = (
                '[SYSTEM: TOOL PROTOCOL RECOVERY]\n'
                'Your last response contained an unexecuted tool-call packet. '
                'No operation from that text was executed. Continue from the '
                'preserved tool results and reissue the intended next operation '
                'using the provided native tool schema, not XML tags. Do not '
                'repeat operations that already have execution receipts.'
            )
            if kind == 'reasoning_blowout':
                text = (
                    '[SYSTEM: REASONING BUDGET RECOVERY]\n'
                    'Your last response spent the entire output budget on '
                    'analysis and returned no text and no tool call. Reply '
                    'now with the requested deliverable, preserving the original '
                    'task requirements and required output format. If another '
                    'operation is necessary and a native tool is available, '
                    'use its schema. State a blocker only if you cannot complete '
                    'the task. Do not replace requested code or structured output '
                    'with a short status message, repeat analysis, or repeat '
                    'operations with execution receipts.'
                )
            elif kind != 'unresolved_tool_marker':
                text = (
                    '[SYSTEM: TERMINAL RESPONSE RECOVERY]\n'
                    'Your last response ended without usable assistant text '
                    'or a tool call. Continue the current authorized task from '
                    'the preserved tool results. If more work is needed, use '
                    'the native tool schema. Otherwise provide a final answer, '
                    'or state the specific blocker or required user input. '
                    'Do not repeat operations with execution receipts.'
                )
            from lib.tasks_pkg.context_composer import append_injection
            append_injection(
                messages, 'terminal_recovery', text, task=task,
                round_num=round_num, action='continue', reason=kind)
        emit_phase(
            task, Phase.RETRYING, attempt=attempts,
            max=ABNORMAL_TERMINAL_RECOVERY_MAX, bucket=kind,
            detail=(f'Model ended without a usable terminal response; '
                    f'continuing from preserved tool results '
                    f'({attempts}/{ABNORMAL_TERMINAL_RECOVERY_MAX}).'),
        )
        decision['action'] = 'continue'
        return decision

    from lib.error_envelope import make_envelope
    task['error'] = make_envelope(
        'abnormal_stop',
        message=(
            'Model repeatedly returned no usable terminal response '
            f'({kind}); bounded recovery exhausted.'),
        detail=('Model repeatedly ended without a usable terminal response; '
                'the two per-task recovery attempts are exhausted. '
                'Completed tool operations and their results were preserved.'),
        model=model, context=f'round-{round_num}', source='llm-stream',
        raw=(f'bucket={kind} attempts={attempts}/{ABNORMAL_TERMINAL_RECOVERY_MAX} '
             f"trace_id={str((usage or {}).get('trace_id') or '')[:160]}"),
    )
    decision.update(
        action='break', last_finish_reason='abnormal_stop',
        loop_exit_reason=f'abnormal_terminal_retries_exhausted_round_{round_num}',
    )
    return decision
