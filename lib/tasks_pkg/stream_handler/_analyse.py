"""Post-stream classification order and authorized continuation policies.

Entry analyse_stream_result sequences cancellation, protocol recovery,
transport/text/tool recovery, and normal continuation. The public api module
adapts typed provider evidence and returns RecoveryDecision; this coordinator
neither dispatches tools nor introduces a second recovery state machine.
"""

from lib.agent_core.events import EventType, Phase, build_event, emit_phase
from lib.log import get_logger
from lib.tasks_pkg.manager import (
    append_event, begin_live_text_paragraph, reset_task_text,
)

from lib.tasks_pkg.stream_handler import _budget as retry_budget
from lib.tasks_pkg.stream_handler._audit import _maybe_audit_phase_scope
from lib.tasks_pkg.stream_handler._response_recovery import recover_response_artifacts
from lib.tasks_pkg.stream_handler._tool_recovery import (
    recover_incomplete_tool_stream, recover_missing_tool_payload,
)
from lib.tasks_pkg.stream_handler._transport_recovery import (
    _handle_empty_abnormal_stream, _handle_stream_anomaly, _stream_diagnostics,
)


logger = get_logger(__name__)


def _handle_pending_program_continuation(
    task,
    assistant_msg,
    messages,
    result,
    *,
    usage,
    tid,
    round_num,
):
    """Replay one opaque program result or terminate a protocol loop."""
    if not (usage or {}).get('_program_pending'):
        return None

    from lib.tasks_pkg.orchestrator._programmatic import (
        admit_program_continuation,
    )
    allowed, continuations, continuation_limit = (
        admit_program_continuation(task, assistant_msg))
    if allowed:
        replay = {
            'role': 'assistant',
            'content': assistant_msg.get('content') or '',
        }
        for field in (
            'reasoning_content', '_responses_items',
            '_anthropic_content_blocks',
        ):
            if assistant_msg.get(field):
                replay[field] = assistant_msg[field]
        messages.append(replay)
        logger.info(
            '[%s] Programmatic tool program completed without final message '
            'at round %d — replaying program_output and continuing (%d/%d)',
            tid, round_num, continuations, continuation_limit,
        )
        result['action'] = 'program_continue'
        return result

    logger.error(
        '[%s] Programmatic continuation exceeded %d rounds at round %d; '
        'ending to prevent a protocol loop',
        tid, continuation_limit, round_num,
    )
    result['action'] = 'break'
    result['last_finish_reason'] = 'abnormal_stop'
    result['loop_exit_reason'] = (
        f'program_continuation_exhausted_round_{round_num}')
    return result


def _handle_commentary_continuation(task, assistant_msg, messages, result, *,
                                   tid, round_num):
    """A completed Responses response can still be an intermediate update.

    Two continuations per task bound paid work independently of its wording,
    checklist, failed-tool history and the ordinary stream-recovery budget.
    """
    from lib.llm.responses_outbound._message_phase import is_commentary_only
    from lib.tasks_pkg.stream_handler._intent_stall import (
        _awaiting_human, known_external_blocker,
    )

    content = assistant_msg.get('content') or ''
    if (not is_commentary_only(assistant_msg)
            or _awaiting_human(task, assistant_msg, content)
            or known_external_blocker(task) is not None):
        return None
    limit = 2
    count = int(task.get('_commentary_continuation_count') or 0)
    if count >= limit:
        result.update(
            action='break', last_finish_reason='abnormal_stop',
            loop_exit_reason=f'commentary_continuation_exhausted_round_{round_num}')
        logger.warning('[%s] Commentary continuation exhausted (%d/%d)',
                       tid, count, limit)
        return result

    from lib.tasks_pkg.assistant_messages import append_assistant_prose_message
    from lib.tasks_pkg.segments import record_continuation_prose

    task['_commentary_continuation_count'] = count + 1
    append_assistant_prose_message(messages, assistant_msg, task=task)
    record_continuation_prose(
        task, llm_round=round_num, content=content,
        thinking=assistant_msg.get('reasoning_content') or '')
    # Commentary is intermediate work, never part of the final answer: the
    # record above becomes its only owner, so it renders once, at its round.
    content_epoch = reset_task_text(task)
    append_event(task, build_event(
        EventType.DELTA_RESET, roundNum=round_num, discard=True,
        contentEpoch=content_epoch))
    text = (
        '[SYSTEM: INTERMEDIATE RESPONSE]\n'
        'Your response was marked commentary, an intermediate progress update. '
        'Continue the current authorized task using the preserved context. '
        'When finished, provide the final answer. If essential user input or '
        'authorization is required, state the specific blocker in the final '
        'answer or use the available human-input tool. Do not repeat this update.'
    )
    from lib.tasks_pkg.context_composer import append_injection
    append_injection(
        messages, 'intermediate_response', text, task=task, round_num=round_num,
        action='continue', reason='intermediate_response')
    emit_phase(task, Phase.INTENT_STALL_NUDGE, attempt=count + 1, max=limit,
               detail='Model returned only a progress note — continuing '
                     '(%d/%d)…' % (count + 1, limit),
               detailKey='stream.phase.commentaryContinuationNudge',
               detailArgs={'attempt': count + 1, 'max': limit})
    result['action'] = 'continue'
    return result


def _handle_output_contract(task, assistant_msg, messages, result, *,
                            tid, round_num, round_content):
    """A delegated worker's invalid structured answer gets one corrective round.

    A second invalid answer settles normally; the scheduler records the
    violation beside the raw answer instead of re-running work that may
    already have had side effects.
    """
    count = int(task.get('_output_contract_retry_count') or 0)
    if count >= 1:
        return None
    from lib.swarm.spawn_contracts import parse_structured_answer
    value, error = parse_structured_answer(round_content, task['_invocationOutputSchema'])
    if value is not None:
        return None

    from lib.tasks_pkg.assistant_messages import append_assistant_prose_message
    from lib.tasks_pkg.segments import record_continuation_prose

    task['_output_contract_retry_count'] = count + 1
    append_assistant_prose_message(messages, assistant_msg, task=task)
    record_continuation_prose(
        task, llm_round=round_num, content=round_content,
        thinking=assistant_msg.get('reasoning_content') or '')
    # A prose-only round leaves its text in the accumulators. The corrected
    # object alone must become the deliverable; the vetoed answer survives in
    # the wire row and the continuation-prose record above.
    content_epoch = reset_task_text(task)
    append_event(task, build_event(
        EventType.DELTA_RESET, roundNum=round_num, discard=True,
        contentEpoch=content_epoch))
    text = (
        '[SYSTEM: RESULT CONTRACT]\n'
        f'Your final answer does not satisfy the delegated result contract: {error}\n'
        'Reply with ONLY the corrected JSON object that validates against the JSON '
        'Schema in your Result contract section: no prose, no code fence.'
    )
    from lib.tasks_pkg.context_composer import append_injection
    append_injection(
        messages, 'output_contract', text, task=task, round_num=round_num,
        action='continue', reason='output_contract')
    logger.info('[%s] Output contract violation at round %d; one corrective round: %s',
                tid, round_num, error)
    result['action'] = 'continue'
    return result


def _handle_intent_stall_nudge(task, assistant_msg, messages, result, *,
                               tid, model, round_num, round_content,
                               round_thinking):
    """Re-drive a prose-only stop after a failed tool round, once per task."""
    # The model's previous tool call was rejected/errored, and this round
    # is prose-only — it said what it would do and then stopped. Ground
    # truth: conv ms34yw0k74o2lq R18 ("Let me use explicit paths only."
    # after a blocked run_command). The task settled normally and the user
    # saw the conversation stop mid-thought.
    #
    # Four structural criteria, never wording — the ticket's A∧B pair
    # alone measured 60% false positives over 7 days (5 hand-backs, 4 VU
    # endings, 3 non-retryable), so C and D are load-bearing, not polish.
    # See docs/modules/task_engine.md and _intent_stall.py.
    #
    # ONE nudge per task: the counter is checked and bumped here, so a
    # model that stalls again after being nudged is allowed to stop (the
    # runaway guard — same discipline as the retry caps above).
    _stall_nudges = int(task.get('_intent_stall_nudge_count') or 0)
    if _stall_nudges >= 1:
        return None
    from lib.tasks_pkg.stream_handler._intent_stall import (
        NUDGE_TEXT as _stall_text,
        should_nudge_intent_stall as _should_stall_nudge,
    )
    _do_nudge, _stall_reason = _should_stall_nudge(
        task, assistant_msg, round_content)
    if _do_nudge:
        task['_intent_stall_nudge_count'] = _stall_nudges + 1
        from lib.tasks_pkg.assistant_messages import (
            append_assistant_prose_message,
        )
        append_assistant_prose_message(
            messages, assistant_msg, task=task)
        # The vetoed answer stays the provisional answer: the live text
        # keeps it, so a prose-only follow-up (often just an END_TURN
        # declaration) still delivers it. The record takes it over only if
        # a later tool round or retry discards the live text.
        from lib.tasks_pkg.segments import record_continuation_prose
        record_continuation_prose(
            task, llm_round=round_num, content=round_content,
            thinking=round_thinking)
        separators = begin_live_text_paragraph(task)
        if separators:
            append_event(task, build_event(EventType.DELTA, **separators))
        from lib.tasks_pkg.context_composer import append_injection
        append_injection(
            messages, 'intent_stall', _stall_text, task=task,
            round_num=round_num, action='continue', reason='intent_stall')
        # DISPLAY-ONLY sidecar accumulation — the in-timeline chip.
        # Unlike the peer / steer lanes this is emitted AT INJECTION
        # rather than deferred until the next LLM call confirms
        # consumption: those lanes defer because an abort must re-route
        # an undelivered HUMAN message to the durable queue (never
        # zero, never double). A nudge has no human author and nothing
        # to salvage — if the turn dies here the nudge is simply moot,
        # and the fact worth showing ('the system re-drove the model')
        # is true the moment we append it.
        from lib.tasks_pkg.stream_handler._intent_stall import (
            build_stall_nudge_record as _build_stall_record,
        )
        try:
            task.setdefault('_stallNudges', []).append(
                _build_stall_record(task, round_num))
        except Exception as _sn_e:  # a chip must never break the loop
            logger.warning('[%s] stall-nudge chip record failed: %s',
                           tid, _sn_e)
        logger.info(
            '[%s] ↻ Intent-stall nudge at round %d: previous tool '
            'round failed and this round was prose-only with no tool '
            'calls — re-driving once. model=%s content=%dchars',
            tid, round_num, model, len(round_content))
        emit_phase(task, Phase.INTENT_STALL_NUDGE,
                   attempt=_stall_nudges + 1,
                   max=1,
                   detail='↻ Previous tool call did not run — nudging the '
                          'model to continue…',
                   detailKey='stream.phase.intentStallNudge')
        result['action'] = 'continue'
        return result
    if _stall_reason not in ('prev_tool_ok', 'no_tool_rounds',
                             'no_content', 'has_tool_calls'):
        # Log only the INTERESTING skips (a stop that looked like a
        # stall but was deliberately left alone), so the criteria that
        # do the real work are observable in production.
        logger.debug(
            '[%s] intent-stall nudge skipped at round %d: %s',
            tid, round_num, _stall_reason)
    return None


def _analyse_tool_call_round(assistant_msg, last_finish_reason, task, tid, model,
                             round_num, premature_retry_count, messages, result, usage):
    """Decide a round that returned tool calls.

    An output-token limit continues the same reasoning, an incomplete tool
    stream is recovered before dispatch, and validated calls proceed.
    """
    if (last_finish_reason in ('length', 'max_tokens')
            and not task.get('aborted') and not task.get('error')):
        from lib.tasks_pkg.stream_handler._terminal_recovery import (
            _continue_from_output_limit,
        )
        return _continue_from_output_limit(
            assistant_msg, task, round_num=round_num, model=model,
            result=result, usage=usage, messages=messages)

    tool_stream_recovery = recover_incomplete_tool_stream(
        task=task, result=result, assistant_msg=assistant_msg, usage=usage,
        round_num=round_num, phase_retry_count=premature_retry_count,
        tid=tid, model=model)
    if tool_stream_recovery is not None:
        return tool_stream_recovery

    # A validated tool call is deliverable progress. A later no-actionable
    # incident is isolated and may use the remaining phase-wide recovery budget.
    retry_budget._clear_no_actionable_retry_streak(task)
    task.pop('_output_limit_recovery_count', None)

    # assistant_msg has tool_calls → proceed to tool execution (or check budget)
    return result


def analyse_stream_result(
    assistant_msg, last_finish_reason, task, tid, model,
    round_num, _premature_retry_count, messages, usage=None,
):
    """Return the break/continue/proceed decision for one streamed round.

    A task-owned phase counter overrides the legacy caller counter so retries
    remain bounded across Worker/Planner rounds. Zero-byte recovery also writes
    ``_force_rotate_pair`` for the next dispatch. Transport recovery never
    appends to ``messages``: a retry resends the request the provider already
    saw. The returned mapping owns the updated retry count, finish reason,
    loop-exit cause, and abort phase.
    """
    # ── Per-phase counter override ──
    # If the orchestrator has set ``task['_premature_retry_count_phase']``,
    # use it as the source of truth so the cap survives across rounds
    # within one phase.  Otherwise fall back to the legacy local counter
    # passed in by the caller.
    if '_premature_retry_count_phase' in task:
        _premature_retry_count = int(task.get('_premature_retry_count_phase') or 0)
        _maybe_audit_phase_scope()

    result = {
        'action': 'proceed',
        'loop_exit_reason': None,
        'abort_detected_phase': None,
        'premature_retry_count': _premature_retry_count,
        'last_finish_reason': last_finish_reason,
    }

    # ── Error finish reason → break ──
    if last_finish_reason == 'error':
        result['action'] = 'break'
        result['loop_exit_reason'] = f'finish_reason_error_round_{round_num}'
        logger.error(
            '[%s] ✕ Loop breaking due to finish_reason=error at round %d. '
            'error=%s content=%dchars',
            tid, round_num, task.get('error', 'none'),
            len(task.get('content') or ''),
        )
        return result

    # ── No tool calls returned ──
    if not assistant_msg.get('tool_calls'):
        # Check if abort happened mid-stream
        if task['aborted']:
            result['action'] = 'break'
            result['abort_detected_phase'] = f'post_stream_round_{round_num}'
            result['loop_exit_reason'] = f'aborted_post_stream_round_{round_num}'
            logger.debug(
                '[%s] Abort detected after LLM stream (round %d, model=%s). '
                'Model returned no tool_calls — likely interrupted mid-generation. '
                'content=%dchars',
                tid, round_num, model, len(task.get('content') or ''),
            )
            return result

        # Programmatic Tool Calling may complete its program in one Responses
        # item and deliver the final assistant message only in the following
        # response. Persist/replay the opaque program state and continue once;
        # treating this protocol-defined empty message as EMPTY_STOP would
        # discard the program result and retry the wrong request.
        program_result = _handle_pending_program_continuation(
            task,
            assistant_msg,
            messages,
            result,
            usage=usage,
            tid=tid,
            round_num=round_num,
        )
        if program_result is not None:
            # The opaque program state is protocol-level deliverable progress;
            # a later deadline is isolated rather than part of the prior streak.
            retry_budget._clear_no_actionable_retry_streak(task)
            return program_result


        from lib.tasks_pkg.stream_handler._router_silence import declare_silent_turn
        if (last_finish_reason in ('stop', 'end_turn') and not (usage or {}).get('_stream_anomaly')
                and declare_silent_turn(task, assistant_msg, messages, llm_round=round_num)):
            result['action'] = 'break'
            result['loop_exit_reason'] = f'deliberate_silence_round_{round_num}'
            return result

        from lib.tasks_pkg.stream_handler._terminal_recovery import (
            recover_abnormal_terminal,
        )
        terminal_result = recover_abnormal_terminal(
            assistant_msg, last_finish_reason, task,
            round_num=round_num, model=model, result=result, usage=usage,

            messages=messages,
        )
        if terminal_result is not None:
            return terminal_result
        # ── Detect PREMATURE STREAM CLOSE / ABNORMAL STOP ──
        # Two signatures:
        #   A) Classic premature close: no content, no tool_calls, large thinking (>1000)
        #   B) Stream anomaly + empty content: upstream closed before useful
        #      output, including heartbeat-only streams and short reasoning.
        # Both signatures can occur on the first round and must recover there.
        round_thinking = assistant_msg.get('reasoning_content', '') or ''
        round_content = assistant_msg.get('content', '') or ''

        # Real SSE chunk count distinguishes a true zero-byte stream from a
        # later transport cut; the remaining fields enrich diagnosis/events.
        (_trace_id, _resp_trace, _stream_elapsed_ms, _stream_anomaly,
         _empty_stop, _route_id, _route_mode, _failure_stage,
         _semantic_progress_timeout, _chunks_received) = _stream_diagnostics(usage)

        # Prefer observed chunk counts over the legacy elapsed/thinking
        # heuristic. Protocol-only empty replies share the zero-output
        # signature. Even empty attempts can incur prompt/cache charges;
        # the transport owner applies paced budgets and the tighter allowance
        # for long silent attempts.
        if _chunks_received is not None:
            _is_zero_byte = (
                not round_content.strip()
                and not round_thinking.strip()
                and _stream_anomaly
                and (
                    _chunks_received == 0
                    # Stub response: gateway returned protocol framing
                    # (role + stop chunks) but model generated nothing.
                    or (_empty_stop
                        and _chunks_received <= 5
                        and _stream_elapsed_ms < 60000)
                )
            )
        else:
            _is_zero_byte = (
                not round_content.strip()
                and _stream_anomaly
                and len(round_thinking) < 100
                and _stream_elapsed_ms < 60000
            )

        abnormal_result = _handle_empty_abnormal_stream(
            task=task,
            result=result,
            usage=usage,
            tid=tid,
            model=model,
            round_num=round_num,
            phase_retry_count=_premature_retry_count,
            round_thinking=round_thinking,
            round_content=round_content,
            trace_id=_trace_id,
            resp_trace=_resp_trace,
            stream_elapsed_ms=_stream_elapsed_ms,
            stream_anomaly=_stream_anomaly,
            empty_stop=_empty_stop,
            route_id=_route_id,
            route_mode=_route_mode,
            failure_stage=_failure_stage,
            semantic_progress_timeout=_semantic_progress_timeout,
            is_zero_byte=_is_zero_byte,
        )
        if abnormal_result is not None:
            return abnormal_result

        response_recovery = recover_response_artifacts(
            task=task, result=result, messages=messages, usage=usage,
            tid=tid, model=model,
            round_num=round_num, phase_retry_count=_premature_retry_count,
            round_thinking=round_thinking, round_content=round_content,
            trace_id=_trace_id, elapsed_ms=_stream_elapsed_ms,
            empty_stop=_empty_stop, is_zero_byte=_is_zero_byte)
        if response_recovery is not None:
            return response_recovery

        missing_payload_recovery = recover_missing_tool_payload(
            task=task, result=result, usage=usage,
            last_finish_reason=last_finish_reason, round_num=round_num,
            phase_retry_count=_premature_retry_count, tid=tid, model=model,
            trace_id=_trace_id)
        if missing_payload_recovery is not None:
            return missing_payload_recovery

        # ── Stream anomaly — with or without content ──
        # If the LLM client flagged a stream anomaly (_missing_done,
        # _missing_finish_reason, _empty_stop), the response is likely
        # truncated even if some content was produced.
        if _stream_anomaly:
            return _handle_stream_anomaly(
                task=task,
                result=result,
                round_content=round_content,
                premature_retry_count=_premature_retry_count,
                tid=tid,
                model=model,
                round_num=round_num,
                trace_id=_trace_id,
                empty_stop=_empty_stop,
            )

        # A transport-level stop ends one response, not necessarily the task.
        # Explicit provider commentary is stronger evidence than prose shape.
        # Aborts, errors, truncation and malformed streams retain precedence.
        if last_finish_reason == 'stop':
            commentary_result = _handle_commentary_continuation(
                task, assistant_msg, messages, result, tid=tid, round_num=round_num)
            if commentary_result is not None:
                return commentary_result

        # ── Intent-stall nudge () ──
        stall_result = _handle_intent_stall_nudge(
            task, assistant_msg, messages, result, tid=tid, model=model,
            round_num=round_num, round_content=round_content,
            round_thinking=round_thinking)
        if stall_result is not None:
            return stall_result

        if (last_finish_reason == 'stop'
                and isinstance(task.get('_invocationOutputSchema'), dict)):
            contract_result = _handle_output_contract(
                task, assistant_msg, messages, result, tid=tid,
                round_num=round_num, round_content=round_content)
            if contract_result is not None:
                return contract_result

        # Normal exit — model returned content without tool calls
        result['action'] = 'break'
        result['loop_exit_reason'] = f'no_tool_calls_round_{round_num}'

        # Missing tool payloads were already claimed by their recovery owner.
        logger.debug(
            '[%s] Loop ending normally: model=%s returned text without '
            'tool_calls at round %d. finish_reason=%s content=%dchars',
            tid, model, round_num, result['last_finish_reason'],
            len(task.get('content') or ''),
        )
        return result

    return _analyse_tool_call_round(
        assistant_msg, last_finish_reason, task, tid, model, round_num,
        _premature_retry_count, messages, result, usage)
