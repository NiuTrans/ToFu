"""Provider-neutral abnormal terminal recovery at the post-stream boundary.

Call ``recover_abnormal_terminal`` after cancellation and protocol-defined
program continuation, before legacy stream anomaly buckets. This owner only
chooses a loop directive: it never dispatches tools or rewinds settled history.
Provider-specific text normalization must run first and may explicitly mark an
unresolved control sequence in usage; this module does not parse model prose.
"""

from lib.agent_core.events import Phase, emit_phase

ABNORMAL_TERMINAL_RECOVERY_MAX = 2
OUTPUT_LIMIT_FINISH_REASONS = {'length', 'max_tokens'}
OUTPUT_LIMIT_RECOVERY_NOTICE = (
    '[SYSTEM: OUTPUT LIMIT RECOVERY]\n'
    'Your last response reached the output-token limit. Its generated '
    'analysis and text are preserved above; no tool call from it ran. '
    'Resume directly from that work instead of restarting or repeating it. '
    'Provide the requested deliverable, preserving the original task '
    'requirements and required output format; if more work is needed and '
    'a native tool is available, issue one complete call with its schema. '
    'Do not replace requested code or structured output with a short status '
    'note, and do not repeat operations that already have receipts.'
)
OUTPUT_LIMIT_EXHAUSTED_MESSAGE = (
    'Model repeatedly reached the output-token limit without '
    'a usable answer or a complete tool call.'
)


def abnormal_terminal_kind(assistant_msg, finish_reason, task, usage=None):
    """Classify missing terminal output, excluding real tools and cancellation."""
    if task.get('aborted') or task.get('error'):
        return None

    if finish_reason in OUTPUT_LIMIT_FINISH_REASONS:
        # The provider proved this response was cut, so its trailing prose is
        # not a complete question. Genuine pending human work still wins;
        # projected tool calls never become new human-wait evidence here.
        from lib.tasks_pkg.stream_handler._intent_stall import (
            _awaiting_human, parse_end_turn_reason)
        content = assistant_msg.get('content') or ''
        awaiting_evidence = dict(assistant_msg, tool_calls=[])
        if _awaiting_human(task, awaiting_evidence,
                           content if parse_end_turn_reason(content) is not None else ''):
            return None
        return 'output_limit'
    from lib.tasks_pkg.stream_handler._intent_stall import _awaiting_human
    if _awaiting_human(task, assistant_msg, assistant_msg.get('content') or ''):
        return None
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
    if kind == 'output_limit':
        return _continue_from_output_limit(
            assistant_msg, task, round_num=round_num, model=model,
            result=result, usage=usage, messages=messages)
    attempts = max(
        int(task.get('_abnormal_terminal_recovery_count') or 0),
        int(result.get('premature_retry_count') or 0),
    )
    decision = dict(result)
    if attempts < ABNORMAL_TERMINAL_RECOVERY_MAX:
        attempts += 1
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
            if kind != 'unresolved_tool_marker':
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


def _continue_from_output_limit(assistant_msg, task, *, round_num, model,
                                result, usage, messages):
    """Resume from generated analysis; never execute a cut tool call."""
    decision = dict(result)
    task['_suppress_whole_turn_retry_to_preserve_partial'] = True
    attempts = int(task.get('_output_limit_recovery_count') or 0)
    if attempts >= ABNORMAL_TERMINAL_RECOVERY_MAX:
        if (assistant_msg.get('content') or '').strip():
            decision.update(action='break', last_finish_reason='length',
                            loop_exit_reason=f'output_limit_round_{round_num}')
            return decision
        from lib.error_envelope import make_envelope
        task['error'] = make_envelope(
            'abnormal_stop',
            message=OUTPUT_LIMIT_EXHAUSTED_MESSAGE,
            detail=('The cut response and completed tool results were preserved. '
                    'The existing two-attempt terminal recovery budget is exhausted.'),
            model=model, context=f'round-{round_num}', source='llm-stream',
            raw=(f'bucket=output_limit attempts={attempts}/'
                 f'{ABNORMAL_TERMINAL_RECOVERY_MAX} '
                 f"trace_id={str((usage or {}).get('trace_id') or '')[:160]}"),
        )
        decision.update(action='break', last_finish_reason='abnormal_stop',
                        loop_exit_reason=f'output_limit_exhausted_round_{round_num}')
        return decision

    attempts += 1
    task['_output_limit_recovery_count'] = attempts
    cut_calls = assistant_msg.get('tool_calls') or []
    # Tool items in a protocol sidecar are just as unverified as their projected
    # tool_calls. Keep only text/reasoning so a converter cannot resurrect one.
    cut = dict(assistant_msg)
    cut.pop('tool_calls', None)
    # A provider can project a partial tool item only in its opaque sidecar,
    # leaving tool_calls empty. Text, reasoning and the non-wire origin marker
    # remain bound; executable or hosted-operation items do not.
    # A replay-order witness also covers the now-withdrawn tool calls. Retire
    # it explicitly so checked conversion uses the ordinary sidecar ordering.
    responses_replay_types = {'reasoning', 'message', 'tofu_state_origin'}
    cut['_responses_items'] = [
        item for item in assistant_msg.get('_responses_items') or []
        if isinstance(item, dict) and item.get('type') in responses_replay_types
    ]
    cut['_anthropic_content_blocks'] = [
        block for block in assistant_msg.get('_anthropic_content_blocks') or []
        if isinstance(block, dict)
        and block.get('type') in ('text', 'thinking', 'redacted_thinking')
    ]
    from lib.tasks_pkg.assistant_messages import append_assistant_prose_message
    append_assistant_prose_message(messages, cut, task=task)
    names = ', '.join(
        str((call.get('function') or {}).get('name') or 'unknown')
        for call in cut_calls if isinstance(call, dict)
    ) or 'none'
    text = OUTPUT_LIMIT_RECOVERY_NOTICE
    if cut_calls:
        text += (f'\nThe cut response proposed incomplete tool call(s): {names}. '
                 'Their arguments were not executed or replayed; issue any still '
                 'needed operation afresh with complete arguments.')
    if any(item.get('type') not in responses_replay_types
           for item in assistant_msg.get('_responses_items') or []
           if isinstance(item, dict)):
        text += ('\nThe cut response contained an incomplete Responses item. '
                 'It was not executed or replayed; issue any needed operation afresh.')
    if any(block.get('type') not in ('text', 'thinking', 'redacted_thinking')
           for block in assistant_msg.get('_anthropic_content_blocks') or []
           if isinstance(block, dict)):
        text += ('\nThe cut response contained an incomplete Anthropic tool item. '
                 'It was not executed or replayed; issue any needed operation afresh.')
    from lib.tasks_pkg.context_composer import append_injection
    append_injection(messages, 'output_limit_recovery', text, task=task,
                     round_num=round_num, action='continue', reason='output_limit')
    emit_phase(task, Phase.RETRYING, attempt=attempts,
               max=ABNORMAL_TERMINAL_RECOVERY_MAX, bucket='output_limit',
               detail=(f'Output limit reached; resuming from the preserved response '
                       f'({attempts}/{ABNORMAL_TERMINAL_RECOVERY_MAX}).'))
    decision['action'] = 'continue'
    return decision
