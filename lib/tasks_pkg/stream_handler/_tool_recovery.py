"""Refuse incomplete tool occurrences and recover their provisional text.

Entries recover_missing_tool_payload and recover_incomplete_tool_stream run
before dispatch and use the existing _budget policy. Both share one round-base
rollback through manager.reset_task_text, retaining discard evidence and the
client content epoch. This module never executes or settles a tool.
"""
from lib.agent_core.events import Phase, emit_phase
from lib.log import get_logger
from lib.tasks_pkg.round_retry import reset_round_to_base
from lib.tasks_pkg.stream_handler import _budget as retry_budget
from lib.tasks_pkg.stream_handler._budget import (
    _PREMATURE_RETRY_MAX_CLASSIC, _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
    _zero_byte_backoff_seconds,
)

logger = get_logger(__name__)


def recover_missing_tool_payload(*, task, result, usage, last_finish_reason,
                                 round_num, phase_retry_count, tid, model,
                                 trace_id):
    """Retry an announced but absent payload; exhaustion remains a failure."""
    # ── Tool-calls finish WITHOUT payload (2026-08-06 kimi-k3/sankuai
    #    incident, conv msh3qeplzneph5 R3) ──
    # The wire reported finish_reason=tool_calls yet zero tool calls
    # were assembled: the gateway lost the model's tool-call deltas
    # UPSTREAM (usage['_tool_calls_void']=='gateway_no_payload'), or our
    # own phantom filter dropped every entry ('filtered' — its
    # per-entry WARNINGs are then in the log). Normalizing this to
    # 'stop' ends the turn mid-work and delivers a PREAMBLE as if it
    # were the conclusion ("说了要去查,然后死了"), so retry
    # transparently like the other transport-lying buckets. Shares the
    # per-phase counter; the cap is the low one because the poisoned
    # round DID bill prompt + completion tokens.
    if last_finish_reason in ('tool_calls', 'tool_use'):
        _void_cause = (usage or {}).get('_tool_calls_void') or 'unknown'
        if phase_retry_count < _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX:
            phase_retry_count += 1
            result['premature_retry_count'] = phase_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = phase_retry_count
            _backoff_s = _zero_byte_backoff_seconds(phase_retry_count)
            logger.warning(
                '[%s] ⚠️ TOOL_CALLS NO PAYLOAD at round %d: '
                'finish_reason=%s but 0 tool call(s) assembled '
                '(cause=%s) — the gateway reported tool calls it never '
                'delivered. Retrying (%d/%d) after %.1fs backoff '
                'instead of ending the turn on a preamble. '
                'M-TraceId=%s model=%s',
                tid, round_num, last_finish_reason, _void_cause,
                phase_retry_count, _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
                _backoff_s, trace_id, model,
            )
            reset_round_to_base(task, round_num)
            emit_phase(task, Phase.RETRYING,
                       attempt=phase_retry_count,
                       max=_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
                       bucket='tool_calls_no_payload',
                       backoff_s=round(_backoff_s, 2),
                       detail=(
                           f'⚠️ 网关声明了工具调用但载荷未送达，'
                           f'退避 {_backoff_s:.1f}s 后重试 '
                           f'({phase_retry_count}/'
                           f'{_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX})…'
                       ))
            retry_budget._interruptible_sleep(_backoff_s, task)
            result['action'] = 'continue'
            return result
        # Budget exhausted — honest terminal error (same shape as the
        # truncated-tool-args exhaustion: the turn-level auto-retry may
        # still re-run the whole turn from pristine input). Never
        # deliver the preamble as if the turn concluded normally.
        result['action'] = 'break'
        result['last_finish_reason'] = 'premature_close'
        result['loop_exit_reason'] = (
            f'tool_calls_no_payload_retries_exhausted_round_{round_num}'
        )
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'premature_close',
            detail=(f'Gateway repeatedly reported finish_reason='
                    f'{last_finish_reason} without delivering any '
                    f'tool_call payload (cause={_void_cause}); retries '
                    f'exhausted ({phase_retry_count}/'
                    f'{_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX}). '
                    f'M-TraceId={trace_id}'),
            model=model,
            context=f'round-{round_num}',
            source='llm-stream',
            raw=(f'bucket=tool_calls_no_payload cause={_void_cause} '
                 f'attempts={phase_retry_count}/'
                 f'{_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX} '
                 f'M-TraceId={trace_id}'),
        )
        logger.error(
            '[%s] ⚠️ TOOL_CALLS NO PAYLOAD retries exhausted at round %d '
            '(%d/%d). cause=%s M-TraceId=%s model=%s — settling '
            'finishReason=premature_close with error envelope.',
            tid, round_num, phase_retry_count,
            _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX, _void_cause,
            trace_id, model,
        )
        return result

    return None


def recover_incomplete_tool_stream(*, task, result, assistant_msg, usage,
                                   round_num, phase_retry_count, tid, model):
    """A parseable prefix is insufficient without provider completion."""
    # assistant_msg has tool_calls → but a premature close may have cut the
    # stream MID-ARGUMENTS, leaving tool calls whose accumulated JSON cannot
    # parse. Executing those would run tools on corrupt arguments (or on the
    # sanitizer's '{}' substitution). Validate BEFORE proceeding: unparseable
    # → retry the round transparently (classic-bucket budget), never execute.
    # Parseable arguments prove only a complete prefix, never a complete
    # provider response. The public adapter derives _stream_anomaly from the
    # typed stream authority; even a salvage hint cannot override that verdict.
    # An observed provider finish without [DONE] remains executable.
    _malformed_tool_stream = bool((usage or {}).get('_malformed_stream'))
    _incomplete_tool_stream = bool((usage or {}).get('_stream_anomaly'))
    if (usage or {}).get('_missing_done') or _incomplete_tool_stream:
        from lib.agent_loop.agent_loop import unparseable_tool_calls
        _bad_tcs = unparseable_tool_calls(assistant_msg)
        if _bad_tcs or _incomplete_tool_stream:
            trace_id = (usage or {}).get('trace_id', 'N/A')
            _bad_names = [(tc.get('function') or {}).get('name', '?')
                          for tc in _bad_tcs]
            if not _bad_names:
                _bad_names = [
                    (tc.get('function') or {}).get('name', '?')
                    for tc in assistant_msg.get('tool_calls') or []
                ]
            _tool_stream_issue = (
                'malformed provider frame' if _malformed_tool_stream
                else 'truncated tool arguments' if _bad_tcs
                else 'missing provider completion'
            )
            _tool_stream_bucket = (
                'malformed_tool_stream' if _malformed_tool_stream
                else 'truncated_tool_args' if _bad_tcs
                else 'incomplete_tool_stream')
            if phase_retry_count < _PREMATURE_RETRY_MAX_CLASSIC:
                phase_retry_count += 1
                result['premature_retry_count'] = phase_retry_count
                if '_premature_retry_count_phase' in task:
                    task['_premature_retry_count_phase'] = phase_retry_count
                _backoff_s = _zero_byte_backoff_seconds(phase_retry_count)
                logger.warning(
                    '[%s] ⚠️ UNTRUSTWORTHY TOOL STREAM at round %d: %s; '
                    '%d affected tool call(s) (%s). Retrying (%d/%d) '
                    'after %.1fs backoff instead of executing corrupt calls. '
                    'M-TraceId=%s model=%s',
                    tid, round_num, _tool_stream_issue, len(_bad_names),
                    _bad_names,
                    phase_retry_count, _PREMATURE_RETRY_MAX_CLASSIC,
                    _backoff_s, trace_id, model,
                )
                # Reset this round's partial text to the round base stamped by
                # stream_llm_response so the re-streamed attempt never stacks
                # on the poisoned one's tail. Record the discarded snapshot in
                # the FloorRetry residue list so the shrink-convergent
                # checkpoint/settle guards recognise it as our own discard
                # (exact byte-match) and allow the overwrite.
                reset_round_to_base(task, round_num)
                emit_phase(task, Phase.RETRYING,
                           attempt=phase_retry_count,
                           max=_PREMATURE_RETRY_MAX_CLASSIC,
                           bucket=_tool_stream_bucket,
                           backoff_s=round(_backoff_s, 2),
                           detail=(
                               f'⚠️ 上游工具数据流不完整（{len(_bad_names)} 个调用），'
                               f'退避 {_backoff_s:.1f}s 后重试 '
                               f'({phase_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC})…'
                           ))
                retry_budget._interruptible_sleep(_backoff_s, task)
                result['action'] = 'continue'
                return result
            # Budget exhausted — honest terminal error (same shape as the
            # classic premature-close exhaustion: the turn-level auto-retry
            # may still re-run the whole turn from pristine input).
            result['action'] = 'break'
            result['last_finish_reason'] = 'premature_close'
            result['loop_exit_reason'] = (
                f'truncated_tool_args_retries_exhausted_round_{round_num}'
            )
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                'premature_close',
                detail=(f'Provider tool stream repeatedly arrived incomplete '
                        f'({_tool_stream_issue}; calls={_bad_names}); '
                        f'retries exhausted '
                        f'({phase_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC}). '
                        f'M-TraceId={trace_id}'),
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=(f'bucket=untrustworthy_tool_stream '
                     f'issue={_tool_stream_issue} bad_calls={_bad_names} '
                     f'attempts={phase_retry_count}/'
                     f'{_PREMATURE_RETRY_MAX_CLASSIC} M-TraceId={trace_id}'),
            )
            logger.error(
                '[%s] ⚠️ TRUNCATED TOOL CALL retries exhausted at round %d '
                '(%d/%d). bad_calls=%s M-TraceId=%s model=%s — settling '
                'finishReason=premature_close with error envelope.',
                tid, round_num, phase_retry_count,
                _PREMATURE_RETRY_MAX_CLASSIC, _bad_names, trace_id, model,
            )
            return result

    return None
