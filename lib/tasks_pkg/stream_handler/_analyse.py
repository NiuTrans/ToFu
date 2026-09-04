"""Post-stream RESULT ANALYSIS — the ``analyse_stream_result`` classifier.

Extracted from the inner loop of ``orchestrator.run_task`` to isolate the
logic that inspects each LLM round's result and decides whether to retry
(premature close), break (normal finish / error / abort), or continue to
tool execution.
"""

import random

from lib.agent_core.events import EventType, Phase, build_event, emit_phase
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event, reset_task_text
from lib.tasks_pkg.assistant_messages import PARTIAL_STREAM_PREFILL_MARKER

from lib.tasks_pkg.stream_handler import _budget as retry_budget
from lib.tasks_pkg.stream_handler._audit import _maybe_audit_phase_scope
from lib.tasks_pkg.stream_handler._budget import (
    _CANNED_GREETING_RETRY_MAX,
    _EMPTY_STOP_RETRY_MAX,
    _NO_ACTIONABLE_RETRY_MAX,
    _PARTIAL_STREAM_RETRY_MAX,
    _PREMATURE_RETRY_MAX_CLASSIC,
    _PREMATURE_RETRY_MAX_ZERO_BYTE,
    _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
    _zero_byte_backoff_seconds,
)
from lib.tasks_pkg.stream_handler._canned_greeting import (
    is_canned_greeting_reply,
)

logger = get_logger(__name__)


_PARTIAL_STREAM_CONTINUATION_NUDGE = (
    '[SYSTEM: LOSSLESS STREAM CONTINUATION REQUIRED]\n'
    'The upstream response stream ended unexpectedly after a partial assistant '
    'reply. Continue exactly where that reply stopped. Output only the missing '
    'continuation: do not restart, summarize, or repeat the preserved prefix.'
)


def _stream_diagnostics(usage):
    """Normalize bounded provider/transport diagnostics for one round."""
    values = usage if isinstance(usage, dict) else {}
    network_route = values.get('_network_route') or {}
    if not isinstance(network_route, dict):
        network_route = {}
    return (
        values.get('trace_id', 'N/A'),
        values.get('resp_trace_id', ''),
        values.get('stream_elapsed_ms', 0),
        values.get('_stream_anomaly', False),
        values.get('_empty_stop', False),
        str(network_route.get('routeId') or 'unknown')[:160],
        str(network_route.get('routeMode') or 'unknown')[:24],
        str(values.get('_failure_stage') or '')[:80],
        bool(values.get('_semantic_progress_timeout')
             or values.get('_no_actionable_timeout')),
        values.get('_chunks_received'),
    )


def _diagnostic_seconds(value, fallback=0.0):
    """Normalize one untrusted transport duration for display/logging."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return max(0.0, float(fallback or 0))


def _diagnostic_milliseconds(value, fallback_seconds=0.0):
    """Normalize one typed millisecond duration, then fall back to seconds."""
    try:
        return max(0.0, float(value) / 1000)
    except (TypeError, ValueError, OverflowError):
        return _diagnostic_seconds(fallback_seconds)


def _emit_abnormal_retry_phase(
    task,
    *,
    model,
    elapsed_ms,
    route_id,
    route_mode,
    failure_stage,
    semantic_progress_timeout,
    semantic_stall_s,
    request_elapsed_s,
    is_zero_byte,
    retry_count,
    retry_cap,
    retry_bucket,
    backoff_seconds,
):
    """Publish one structured retry status without polluting transcript text."""
    if semantic_progress_timeout:
        detail_key = 'stream.phase.semanticProgressTimeoutRetry'
        detail = (
            f'{model} produced no new reasoning progress, assistant text, or '
            f'tool action for {semantic_stall_s:.1f}s '
            f'(request elapsed {request_elapsed_s:.1f}s); retrying on another '
            f'slot ({retry_count}/{retry_cap})…'
        )
    elif is_zero_byte:
        detail_key = 'stream.phase.emptyStreamRetry'
        detail = (
            f'{model} returned an empty stream over {route_id} after '
            f'{elapsed_ms / 1000:.1f}s; retrying '
            f'({retry_count}/{retry_cap})…'
        )
    else:
        route_suffix = {
            'direct': 'Direct',
            'proxy': 'Proxy',
            'env': 'Proxy',
            'desktop': 'Desktop',
        }.get(route_mode, 'Unknown')
        detail_key = 'stream.phase.streamInterruptedRetry' + route_suffix
        detail = (
            f'{model} stream ended over {route_id} after '
            f'{elapsed_ms / 1000:.1f}s; retrying on another slot '
            f'({retry_count}/{retry_cap})…'
        )
    emit_phase(
        task,
        Phase.RETRYING,
        attempt=retry_count,
        max=retry_cap,
        bucket=retry_bucket,
        backoff_s=round(backoff_seconds, 2),
        errorKind=(
            'semantic_progress_timeout' if semantic_progress_timeout
            else 'premature_close'
        ),
        routeId=route_id,
        routeMode=route_mode,
        failureStage=failure_stage,
        detail=detail,
        detailKey=detail_key,
        detailArgs={
            'model': model,
            'elapsed': round(
                semantic_stall_s if semantic_progress_timeout
                else elapsed_ms / 1000, 1),
            **({'requestElapsed': round(request_elapsed_s, 1)}
               if semantic_progress_timeout else {}),
            'attempt': retry_count,
            'max': retry_cap,
            'backoff': round(backoff_seconds, 1),
        },
    )


def _append_partial_stream_retry_context(messages, partial_content, model):
    """Preserve a cut response in model context for the next retry round.

    OpenAI-compatible models receive a trailing assistant prefill. Repeated
    cuts extend the same private prefill row byte-for-byte, avoiding the
    newline insertion used by generic same-role merging. Providers that reject
    assistant prefill receive the preserved assistant row followed by an
    explicit continuation nudge. Both modes keep ``task['content']`` untouched;
    the next round's deltas append to the visible prefix.
    """
    from lib.model_info import model_supports_assistant_prefill

    if model_supports_assistant_prefill(model):
        trailing_message = messages[-1] if messages else None
        if (isinstance(trailing_message, dict)
                and trailing_message.get('role') == 'assistant'
                and not trailing_message.get('tool_calls')
                and isinstance(trailing_message.get('content'), str)):
            # A task created by the manual Continue command can already end in
            # a capability-gated assistant prefill. Extend that row directly
            # too: appending a second assistant row would make the generic wire
            # sanitizer merge them with ``\n\n``, corrupting the exact prefix.
            trailing_message['content'] += partial_content
            trailing_message[PARTIAL_STREAM_PREFILL_MARKER] = True
        else:
            messages.append({
                'role': 'assistant',
                'content': partial_content,
                PARTIAL_STREAM_PREFILL_MARKER: True,
            })
        return 'assistant_prefill'

    messages.append({'role': 'assistant', 'content': partial_content})
    messages.append({
        'role': 'user',
        'content': _PARTIAL_STREAM_CONTINUATION_NUDGE,
        # Engine-authored control, not a new human objective. Provider
        # sanitization keeps the same role/text while turn/query policy treats
        # the carrier as transparent.
        '_isMeta': True,
    })
    return 'continuation_nudge'


def _handle_stream_anomaly(
    *,
    task,
    messages,
    result,
    round_content,
    premature_retry_count,
    tid,
    model,
    round_num,
    trace_id,
    empty_stop,
):
    """Continue a partial stream losslessly or settle an empty anomaly."""
    if round_content.strip():
        # This latch outlives the immediate continuation round. A later
        # failure must preserve the prefix instead of replaying completed work.
        task['_suppress_whole_turn_retry_to_preserve_partial'] = True
        if premature_retry_count < _PARTIAL_STREAM_RETRY_MAX:
            premature_retry_count += 1
            result['premature_retry_count'] = premature_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = premature_retry_count
            continuation_mode = _append_partial_stream_retry_context(
                messages, round_content, model)
            backoff_seconds = _zero_byte_backoff_seconds(
                premature_retry_count)
            logger.warning(
                '[%s] ⚠️ PARTIAL STREAM ERROR at round %d: gateway stream '
                'ended without terminal frames after %d content chars. '
                'Preserving the prefix and continuing losslessly via %s '
                '(%d/%d) after %.1fs backoff. M-TraceId=%s model=%s',
                tid, round_num, len(round_content), continuation_mode,
                premature_retry_count, _PARTIAL_STREAM_RETRY_MAX,
                backoff_seconds, trace_id, model,
            )
            emit_phase(
                task,
                Phase.RETRYING,
                attempt=premature_retry_count,
                max=_PARTIAL_STREAM_RETRY_MAX,
                bucket='partial_stream',
                backoff_s=round(backoff_seconds, 2),
                errorKind='premature_close',
                continuationMode=continuation_mode,
                detail=(
                    f'⚠️ Upstream stream broke after {len(round_content)} '
                    'characters; the partial reply is preserved. Continuing '
                    f'from its exact prefix ({premature_retry_count}/'
                    f'{_PARTIAL_STREAM_RETRY_MAX})…'
                ),
                detailKey='stream.phase.partialStreamRetry',
                detailArgs={
                    'chars': len(round_content),
                    'attempt': premature_retry_count,
                    'max': _PARTIAL_STREAM_RETRY_MAX,
                },
            )
            retry_budget._interruptible_sleep(backoff_seconds, task)
            result['action'] = 'continue'
            return result

        # Bounded continuations were exhausted. Keep the delivered prefix but
        # expose a terminal error; the latch above prevents destructive replay.
        result['action'] = 'break'
        result['last_finish_reason'] = 'premature_close'
        result['loop_exit_reason'] = (
            f'partial_stream_retries_exhausted_round_{round_num}'
        )
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'premature_close',
            detail=(
                'Upstream stream ended without finish markers after '
                f'{len(round_content)} content characters; bounded lossless '
                'continuation retries were exhausted '
                f'({premature_retry_count}/{_PARTIAL_STREAM_RETRY_MAX}). '
                f'M-TraceId={trace_id}'
            ),
            model=model,
            context=f'round-{round_num}',
            source='llm-stream',
            raw=(
                'bucket=partial_stream '
                f'attempts={premature_retry_count}/'
                f'{_PARTIAL_STREAM_RETRY_MAX} '
                f'content={len(round_content)}chars M-TraceId={trace_id}'
            ),
        )
        logger.error(
            '[%s] ⚠️ PARTIAL STREAM ERROR retries exhausted at round %d '
            '(%d/%d). Preserving %d content chars and settling '
            'finishReason=premature_close; whole-turn retry suppressed to '
            'prevent destructive replay. M-TraceId=%s model=%s',
            tid, round_num, premature_retry_count,
            _PARTIAL_STREAM_RETRY_MAX, len(round_content), trace_id, model,
        )
        return result

    result['action'] = 'break'
    result['last_finish_reason'] = 'abnormal_stop'
    result['loop_exit_reason'] = f'stream_anomaly_empty_round_{round_num}'
    from lib.error_envelope import make_envelope as _make_env
    task['error'] = _make_env(
        'abnormal_stop',
        detail=f'Stream ended without finish marker (M-TraceId: {trace_id})',
        model=model,
        context=f'round-{round_num}',
        source='llm-stream',
        raw=(
            f'has_content=False stream_anomaly=True empty_stop={empty_stop} '
            f'M-TraceId={trace_id}'
        ),
    )
    logger.warning(
        '[%s] ⚠️ Stream anomaly at round %d (no content). '
        'stream_anomaly=True empty_stop=%s M-TraceId=%s model=%s '
        'accumulated_content=%dchars Setting finishReason=abnormal_stop.',
        tid, round_num, empty_stop, trace_id, model,
        len(task.get('content') or ''),
    )
    return result


def _reset_round_to_base(task, round_num):
    """Reset the round's partial text/thinking to the round base stamped by
    ``stream_llm_response``, so a re-streamed retry never stacks on the
    poisoned attempt's tail. The discarded snapshot is recorded in the
    FloorRetry residue list so the shrink-convergent checkpoint/settle
    guards recognise it as our own discard (exact byte-match) and allow the
    overwrite. Same semantics as the truncated-tool-args retry path below
    (which keeps its inline copy deliberately untouched).
    """
    with task['content_lock']:
        _discarded_c = task['content']
        _discarded_t = task['thinking']
        _bc = task.get('_round_base_content')
        _bt = task.get('_round_base_thinking')
    _new_c = _discarded_c if _bc is None else _bc
    _new_t = _discarded_t if _bt is None else _bt
    _shrunk = (_new_c != _discarded_c or _new_t != _discarded_t)
    if _shrunk:
        _residue = task.setdefault('_floor_retry_residue', [])
        if len(_residue) < 8:
            _residue.append({'content': _discarded_c,
                             'thinking': _discarded_t})
        content_epoch = reset_task_text(
            task, content=_new_c, thinking=_new_t)
    else:
        content_epoch = int(task.get('_contentEpoch') or 0)
    append_event(task, build_event(
        EventType.DELTA_RESET, roundNum=round_num, discard=True,
        contentEpoch=content_epoch))


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


def _handle_empty_abnormal_stream(
    *,
    task,
    result,
    usage,
    tid,
    model,
    round_num,
    phase_retry_count,
    round_thinking,
    round_content,
    trace_id,
    resp_trace,
    stream_elapsed_ms,
    stream_anomaly,
    empty_stop,
    route_id,
    route_mode,
    failure_stage,
    semantic_progress_timeout,
    is_zero_byte,
):
    """Handle one empty abnormal stream, or return ``None`` if it is usable."""
    usage_values = usage if isinstance(usage, dict) else {}
    no_actionable_window_s = _diagnostic_milliseconds(
        usage_values.get('_semantic_idle_timeout_ms'),
        usage_values.get('_no_actionable_timeout_s'))
    semantic_stall_s = _diagnostic_milliseconds(
        usage_values.get('_semantic_progress_idle_ms'),
        _diagnostic_seconds(
            usage_values.get('_no_actionable_stall_elapsed_s'),
            no_actionable_window_s or stream_elapsed_ms / 1000))
    request_elapsed_s = _diagnostic_seconds(
        usage_values.get('_no_actionable_request_elapsed_s'),
        stream_elapsed_ms / 1000)
    try:
        reasoning_progress_chunks = max(
            0, int(usage_values.get(
                '_no_actionable_reasoning_chunks') or 0))
    except (TypeError, ValueError, OverflowError):
        reasoning_progress_chunks = 0
    try:
        reasoning_progress_chars = max(
            0, int(usage_values.get(
                '_no_actionable_reasoning_chars') or len(round_thinking)))
    except (TypeError, ValueError, OverflowError):
        reasoning_progress_chars = len(round_thinking)
    is_classic_premature = (
        not round_content.strip() and len(round_thinking) > 1000)
    is_semantic_timeout = (
        semantic_progress_timeout and not round_content.strip())
    is_anomaly_empty = (
        not round_content.strip()
        and stream_anomaly
        and (round_num > 0 or is_semantic_timeout)
        and not is_zero_byte
    )
    if not (is_classic_premature or is_anomaly_empty or is_zero_byte):
        return None

    abnormal_type = (
        'semantic_progress_timeout' if is_semantic_timeout
        else 'premature_close' if is_classic_premature
        else 'zero_byte' if is_zero_byte
        else 'stream_anomaly'
    )
    retry_cap = (
        _NO_ACTIONABLE_RETRY_MAX if is_semantic_timeout
        else _PREMATURE_RETRY_MAX_ZERO_BYTE if is_zero_byte
        else _PREMATURE_RETRY_MAX_CLASSIC
    )
    retry_bucket = (
        'semantic_progress_timeout' if is_semantic_timeout
        else 'zero_byte' if is_zero_byte
        else 'classic'
    )

    # Keep two phase-wide recovery opportunities, but only one alternate-slot
    # retry in an uninterrupted no-output streak. Real tool/text progress clears
    # this streak elsewhere, so later isolated failures still get a chance.
    no_actionable_streak = (
        retry_budget._no_actionable_retry_streak(task)
        if is_semantic_timeout else 0
    )
    streak_exhausted = (
        is_semantic_timeout
        and no_actionable_streak
        >= retry_budget._NO_ACTIONABLE_CONSECUTIVE_RETRY_MAX
    )

    if phase_retry_count < retry_cap and not streak_exhausted:
        phase_retry_count += 1
        result['premature_retry_count'] = phase_retry_count
        if is_semantic_timeout:
            no_actionable_streak = retry_budget._record_no_actionable_retry(task)
        if '_premature_retry_count_phase' in task:
            task['_premature_retry_count_phase'] = phase_retry_count
        backoff_s = (
            _zero_byte_backoff_seconds(phase_retry_count)
            if (is_zero_byte or is_classic_premature or is_semantic_timeout)
            else 0.0
        )

        # Avoid the just-failed pair once. Dispatch relaxes this hint only when
        # no same-model alternative exists, preserving strict-model authority.
        dispatch = (usage or {}).get('_dispatch') or {}
        key = dispatch.get('key')
        dispatch_model = dispatch.get('model') or model
        if key:
            task['_force_rotate_pair'] = (key, dispatch_model)
            logger.info(
                '[%s] %s retry: rotating away from slot %s:%s for the next '
                'dispatch attempt (route=%s stage=%s)',
                tid, retry_bucket, key, dispatch_model, route_id,
                failure_stage or 'unknown',
            )
        logger.warning(
            '[%s] ⚠️ ABNORMAL STOP detected at round %d (type=%s bucket=%s): '
            'thinking=%dchars content=%dchars, no tool_calls. '
            'stream_anomaly=%s empty_stop=%s M-TraceId=%s resp_trace=%s '
            'elapsed=%.1fs model=%s route=%s/%s stage=%s '
            'Retrying (%d/%d) after %.1fs backoff… '
            'The upstream stream ended before a usable result.',
            tid, round_num, abnormal_type, retry_bucket,
            len(round_thinking), len(round_content), stream_anomaly, empty_stop,
            trace_id, resp_trace or 'none', stream_elapsed_ms / 1000, model,
            route_mode, route_id, failure_stage or 'unknown',
            phase_retry_count, retry_cap, backoff_s,
        )
        _emit_abnormal_retry_phase(
            task,
            model=model,
            elapsed_ms=stream_elapsed_ms,
            route_id=route_id,
            route_mode=route_mode,
            failure_stage=failure_stage,
            semantic_progress_timeout=semantic_progress_timeout,
            semantic_stall_s=semantic_stall_s,
            request_elapsed_s=request_elapsed_s,
            is_zero_byte=is_zero_byte,
            retry_count=phase_retry_count,
            retry_cap=retry_cap,
            retry_bucket=retry_bucket,
            backoff_seconds=backoff_s,
        )
        if backoff_s > 0:
            retry_budget._interruptible_sleep(backoff_s, task)
        result['action'] = 'continue'
        return result

    # The deadline is authoritative even when a large reasoning body overlaps
    # the classic heuristic. Whole-turn replay is suppressed only for this
    # costly no-progress signature; manual Retry remains available.
    finish_reason = (
        'abnormal_stop' if is_semantic_timeout
        else 'premature_close' if is_classic_premature
        else 'abnormal_stop'
    )
    result['action'] = 'break'
    result['last_finish_reason'] = finish_reason
    result['loop_exit_reason'] = (
        f'{finish_reason}_retries_exhausted_round_{round_num}')
    error_extensions = {
        'failureStage': failure_stage,
        'routeId': route_id,
        'routeMode': route_mode,
    }
    if is_semantic_timeout:
        error_extensions['autoRetryExhausted'] = True
        retry_budget_detail = (
            f'phase={phase_retry_count}/{retry_cap}; '
            f'consecutive={no_actionable_streak}/'
            f'{retry_budget._NO_ACTIONABLE_CONSECUTIVE_RETRY_MAX}'
        )
    else:
        retry_budget_detail = f'phase={phase_retry_count}/{retry_cap}'

    from lib.error_envelope import make_envelope
    task['error'] = make_envelope(
        finish_reason,
        message=(
            f'⚠️ 模型连续 {semantic_stall_s:.1f} 秒没有新的推理进展、正文或工具动作\n'
            f'No new reasoning progress, assistant text, or tool action for '
            f'{semantic_stall_s:.1f}s'
            if is_semantic_timeout else ''
        ),
        hint=(
            '系统已自动尝试其他同模型槽位；继续自动重放收益很低，已停止避免长时间空耗。\n\n'
            'Alternate slots for the same model were tried automatically; '
            'replay stopped to avoid another long no-progress loop.'
            if is_semantic_timeout else None
        ),
        detail=(
            f'Retry budget exhausted ({retry_budget_detail}). '
            f'type={abnormal_type} bucket={retry_bucket} M-TraceId={trace_id} '
            f'request_elapsed={request_elapsed_s:.1f}s '
            f'last_progress_age={semantic_stall_s:.1f}s '
            f'reasoning={reasoning_progress_chars}chars/'
            f'{reasoning_progress_chunks}chunks'
        ),
        model=model,
        context=f'round-{round_num}',
        source='llm-stream',
        raw=(
            f'abnormal_type={abnormal_type} bucket={retry_bucket} '
            f'retry_budget={retry_budget_detail} '
            f'thinking={len(round_thinking)}chars '
            f'reasoning_progress={reasoning_progress_chars}chars/'
            f'{reasoning_progress_chunks}chunks '
            f'request_elapsed={request_elapsed_s:.1f}s '
            f'last_progress_age={semantic_stall_s:.1f}s '
            f'content={len(round_content)}chars'
        ),
        extensions=error_extensions,
    )
    logger.error(
        '[%s] ⚠️ ABNORMAL STOP retries exhausted at round %d '
        '(type=%s bucket=%s retry_budget=%s). '
        'thinking=%dchars, content=%dchars. stream_anomaly=%s empty_stop=%s '
        'M-TraceId=%s resp_trace=%s elapsed=%.1fs semantic_stall=%.1fs '
        'reasoning_progress=%dchars/%dchunks model=%s '
        'Setting finishReason=%s.',
        tid, round_num, abnormal_type, retry_bucket, retry_budget_detail,
        len(round_thinking), len(round_content), stream_anomaly, empty_stop,
        trace_id, resp_trace or 'none', stream_elapsed_ms / 1000,
        semantic_stall_s, reasoning_progress_chars,
        reasoning_progress_chunks, model,
        finish_reason,
    )
    return result


def analyse_stream_result(
    assistant_msg, last_finish_reason, task, tid, model,
    round_num, _premature_retry_count, messages, usage=None,
):
    """Return the break/continue/proceed decision for one streamed round.

    A task-owned phase counter overrides the legacy caller counter so retries
    remain bounded across Worker/Planner rounds. Zero-byte recovery also writes
    ``_force_rotate_pair`` for the next dispatch, while partial continuation
    preserves provider-compatible assistant history. The returned mapping owns
    the updated retry count, finish reason, loop-exit cause, and abort phase.
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

        # ── Detect PREMATURE STREAM CLOSE / ABNORMAL STOP ──
        # Two signatures:
        #   A) Classic premature close: no content, no tool_calls, large thinking (>1000)
        #   B) Stream anomaly + empty content: gateway/proxy severed connection so
        #      early that even thinking barely started (the mnbvo192q8u0zo pattern)
        round_thinking = assistant_msg.get('reasoning_content', '') or ''
        round_content = assistant_msg.get('content', '') or ''

        # Real SSE chunk count distinguishes a true zero-byte stream from a
        # later transport cut; the remaining fields enrich diagnosis/events.
        (_trace_id, _resp_trace, _stream_elapsed_ms, _stream_anomaly,
         _empty_stop, _route_id, _route_mode, _failure_stage,
         _semantic_progress_timeout, _chunks_received) = _stream_diagnostics(usage)

        # Determine if this round looks like an abnormal termination:
        #   - (A) No content + substantial thinking  (classic premature close)
        #   - (B) Stream anomaly flag + no content + at least 1 prior round
        #         (proxy killed connection before model could produce anything)
        # ── Zero-byte gateway anomaly (computed first, allowed on round 0) ──
        # The gateway opened the SSE connection and closed it before any
        # meaningful token came through.  No work was done, no tokens
        # were spent — retrying is essentially free, so we admit this
        # case on EVERY round including round 0.  This is the recurring
        # ``aws.claude-opus-4.7`` via sankuai gateway pattern documented
        # in the ``stream-retry-cap-split-by-signature`` memory.
        #
        # Detection: prefer the real ``_chunks_received`` from the LLM
        # client (0 = no SSE chunks at all → gateway hang, retry is
        # free regardless of how long we waited).  Fall back to the
        # legacy thinking-length + elapsed-time heuristic when the
        # client field isn't present.  The legacy bound originally
        # used ``< 15s`` but production logs show ~36 % of true
        # zero-byte gateway hangs took 15–40 s before the upstream
        # closed the socket, so we widen the bound to 60 s — still
        # less than the 5-minute read timeout, and still cheap to redo
        # because no tokens were actually generated.
        if _chunks_received is not None:
            _is_zero_byte = (
                not round_content.strip()
                and not round_thinking.strip()
                and _stream_anomaly
                and (
                    _chunks_received == 0
                    # Stub response: gateway returned protocol framing
                    # (role + stop chunks) but model generated nothing.
                    # prompt_tokens/completion_tokens are nonsensical.
                    # Same cost to retry as true zero-byte.
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

        # ── Empty-stop retry (model said finish_reason=stop with no
        #    content). Observed on GLM-5.1 (thinking-only response),
        #    MiniMax M2.5/M2.7, and Claude.  Cheap to retry once or
        #    twice; budget is shared with classic premature-close so
        #    a misbehaving turn can never burn more than 2 retries
        #    across both buckets. ──
        _is_empty_stop = (
            _empty_stop
            and not round_content.strip()
            and not _is_zero_byte
        )
        if (_is_empty_stop
                and _premature_retry_count < _EMPTY_STOP_RETRY_MAX):
            _premature_retry_count += 1
            result['premature_retry_count'] = _premature_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = _premature_retry_count
            _backoff_s = 0.5 + random.uniform(0.0, 0.5)
            logger.warning(
                '[%s] ⚠️ EMPTY_STOP detected at round %d: '
                'finish=stop content=0 thinking=%dchars '
                'M-TraceId=%s elapsed=%.1fs model=%s '
                'Retrying (%d/%d) after %.1fs backoff…',
                tid, round_num, len(round_thinking),
                _trace_id, _stream_elapsed_ms / 1000, model,
                _premature_retry_count, _EMPTY_STOP_RETRY_MAX, _backoff_s,
            )
            emit_phase(task, Phase.RETRYING,
                       attempt=_premature_retry_count,
                       max=_EMPTY_STOP_RETRY_MAX,
                       bucket='empty_stop',
                       backoff_s=round(_backoff_s, 2),
                       detail=(
                           f'⚠️ 模型空回复（{len(round_thinking)}字符思考但无正文），'
                           f'重试中 ({_premature_retry_count}/{_EMPTY_STOP_RETRY_MAX})…'
                       ))
            retry_budget._interruptible_sleep(_backoff_s, task)
            result['action'] = 'continue'
            return result

        # ── Canned-greeting upstream artifact (2026-07-28 Opus 5 incident) ──
        # The gateway's only Opus 5 request-id (a daily eval build) began
        # answering ANY request — including mid-tool-work continuations —
        # with an identical canned greeting and a CLEAN finish_reason=stop
        # (real M-TraceId, real usage). Every transport guard keys off
        # MISSING output, so this "successful" degenerate response ended
        # turns and was persisted over accumulated tool work (68+ events in
        # ~5h, see _canned_greeting.py). Detect by CONTENT + INCONGRUENCE
        # and retry like the other transient buckets — the failure was
        # intermittent (~50%/round), so a bounded retry recovers most
        # turns. Shares the per-phase counter (runaway-guard discipline).
        _is_canned_greeting = is_canned_greeting_reply(round_content, messages)
        if round_content.strip() and not _is_canned_greeting:
            retry_budget._clear_no_actionable_retry_streak(task)
        if (_is_canned_greeting
                and _premature_retry_count < _CANNED_GREETING_RETRY_MAX):
            _premature_retry_count += 1
            result['premature_retry_count'] = _premature_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = _premature_retry_count
            _backoff_s = 1.0 + random.uniform(0.0, 1.0)
            logger.warning(
                '[%s] ⚠️ CANNED GREETING detected at round %d: finish=stop '
                'content=%dchars (%r) — a greeting opener incongruent with '
                'the conversation tail. M-TraceId=%s elapsed=%.1fs model=%s '
                'Retrying (%d/%d) after %.1fs backoff…',
                tid, round_num, len(round_content), round_content[:40],
                _trace_id, _stream_elapsed_ms / 1000, model,
                _premature_retry_count, _CANNED_GREETING_RETRY_MAX,
                _backoff_s,
            )
            # Drop the poisoned text BEFORE re-streaming. This is the ONLY
            #   retry bucket whose discarded round HAS content (zero-byte /
            #   classic / empty-stop all require empty content), so it is also
            #   the only one that must reset the accumulators — otherwise each
            #   attempt's greeting concatenates onto the last (2026-08-02
            #   triple-greeting bug). ``discard=True`` tells the client
            #   reducer to clear WITHOUT the tool-round prose-capture guard:
            #   this round issued no tool calls, so there is no batch to
            #   stamp onto and the freeze guard would keep the text forever.
            content_epoch = reset_task_text(task)
            append_event(task, build_event(
                EventType.DELTA_RESET, roundNum=round_num, discard=True,
                contentEpoch=content_epoch))
            emit_phase(task, Phase.RETRYING,
                       attempt=_premature_retry_count,
                       max=_CANNED_GREETING_RETRY_MAX,
                       bucket='canned_greeting',
                       backoff_s=round(_backoff_s, 2),
                       detail=(
                           f'⚠️ 上游返回了与任务无关的模板问候（{len(round_content)}字符），'
                           f'重试中 ({_premature_retry_count}/{_CANNED_GREETING_RETRY_MAX})…'
                       ))
            retry_budget._interruptible_sleep(_backoff_s, task)
            result['action'] = 'continue'
            return result

        if _is_canned_greeting:
            # Budget exhausted — ACCEPT, never fabricate an error: a greeting
            # can be legitimate, and the persist-layer interception
            # (_maybe_preserve_accumulated_on_suspicion) rebuilds accumulated
            # narration when this overwrote real tool work. Loud + audited so
            # the upstream incident stays observable.
            logger.warning(
                '[%s] ⚠️ CANNED GREETING retries exhausted at round %d '
                '(%d/%d) — accepting the response. content=%r '
                'M-TraceId=%s model=%s',
                tid, round_num, _premature_retry_count,
                _CANNED_GREETING_RETRY_MAX, round_content[:60],
                _trace_id, model,
            )
            try:
                from lib.log import audit_log
                audit_log('canned_greeting_retries_exhausted',
                          task_id=task.get('id', ''),
                          conv=task.get('convId', ''),
                          round=round_num, model=model,
                          content=round_content[:60])
            except Exception as _ae:
                logger.debug('[%s] canned-greeting audit failed: %s', tid, _ae)

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
            if _premature_retry_count < _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX:
                _premature_retry_count += 1
                result['premature_retry_count'] = _premature_retry_count
                if '_premature_retry_count_phase' in task:
                    task['_premature_retry_count_phase'] = _premature_retry_count
                _backoff_s = _zero_byte_backoff_seconds(_premature_retry_count)
                logger.warning(
                    '[%s] ⚠️ TOOL_CALLS NO PAYLOAD at round %d: '
                    'finish_reason=%s but 0 tool call(s) assembled '
                    '(cause=%s) — the gateway reported tool calls it never '
                    'delivered. Retrying (%d/%d) after %.1fs backoff '
                    'instead of ending the turn on a preamble. '
                    'M-TraceId=%s model=%s',
                    tid, round_num, last_finish_reason, _void_cause,
                    _premature_retry_count, _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
                    _backoff_s, _trace_id, model,
                )
                _reset_round_to_base(task, round_num)
                emit_phase(task, Phase.RETRYING,
                           attempt=_premature_retry_count,
                           max=_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
                           bucket='tool_calls_no_payload',
                           backoff_s=round(_backoff_s, 2),
                           detail=(
                               f'⚠️ 网关声明了工具调用但载荷未送达，'
                               f'退避 {_backoff_s:.1f}s 后重试 '
                               f'({_premature_retry_count}/'
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
                        f'exhausted ({_premature_retry_count}/'
                        f'{_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX}). '
                        f'M-TraceId={_trace_id}'),
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=(f'bucket=tool_calls_no_payload cause={_void_cause} '
                     f'attempts={_premature_retry_count}/'
                     f'{_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX} '
                     f'M-TraceId={_trace_id}'),
            )
            logger.error(
                '[%s] ⚠️ TOOL_CALLS NO PAYLOAD retries exhausted at round %d '
                '(%d/%d). cause=%s M-TraceId=%s model=%s — settling '
                'finishReason=premature_close with error envelope.',
                tid, round_num, _premature_retry_count,
                _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX, _void_cause,
                _trace_id, model,
            )
            return result

        # ── Stream anomaly — with or without content ──
        # If the LLM client flagged a stream anomaly (_missing_done,
        # _missing_finish_reason, _empty_stop), the response is likely
        # truncated even if some content was produced.
        if _stream_anomaly:
            return _handle_stream_anomaly(
                task=task,
                messages=messages,
                result=result,
                round_content=round_content,
                premature_retry_count=_premature_retry_count,
                tid=tid,
                model=model,
                round_num=round_num,
                trace_id=_trace_id,
                empty_stop=_empty_stop,
            )

        # ── Todo-continuation enforcer (Rec 2) ──
        # The model is about to end its turn with a genuine final answer. If it
        # declared a structured checklist (task['_todos']) that still has
        # incomplete items, re-drive the loop with a reminder instead of
        # letting it stop — the productive-but-premature-stop case that the
        # zero-deliverable guard (INACTION) and suspicious-completion
        # (content-shape) both structurally miss. Only for a genuine content
        # stop; abort / error / anomaly paths above have already returned.
        _todo_max = retry_budget._todo_continuation_max()
        if round_content.strip():
            from lib.tools.todo import incomplete_todos, render_todo_list
            _todos = task.get('_todos') or []
            _incomplete = incomplete_todos(_todos)
            _actionable = [item for item in _incomplete
                           if item.get('status') != 'blocked']
            _nudges = int(task.get('_todo_continuation_count') or 0)
            if _actionable and _todo_max and _nudges < _todo_max:
                task['_todo_continuation_count'] = _nudges + 1
                from lib.tasks_pkg.assistant_messages import (
                    append_assistant_prose_message,
                )
                append_assistant_prose_message(
                    messages, assistant_msg, task=task)
                messages.append({
                    'role': 'user',
                    # Provider-wire user role, but engine-authored continuation
                    # control rather than a new human query/objective.
                    '_isMeta': True,
                    'content': (
                        '[SYSTEM: TODO CONTINUATION REQUIRED]\n'
                        f'You have {len(_incomplete)} incomplete checklist '
                        f'item(s):\n{render_todo_list(_todos)}\n\n'
                        'Do NOT end your turn yet. Continue working and complete '
                        'ALL items, updating the checklist with todo_write as you '
                        'go. If the scope changed, use todo_write operation='
                        '"replan" with a reason instead of silently deleting '
                        'unfinished items. If an item is genuinely impossible, '
                        'mark it blocked and explain the blocker; blocked work '
                        'settles explicitly as incomplete, never as success.'
                    ),
                })
                logger.info(
                    '[%s] 📋 Todo-continuation enforcer: %d incomplete item(s) '
                    'at stop — re-driving loop (nudge %d/%d) round=%d',
                    tid, len(_incomplete), _nudges + 1, _todo_max, round_num)
                emit_phase(task, Phase.TODO_CONTINUATION,
                           attempt=_nudges + 1,
                           max=_todo_max,
                           incomplete=len(_incomplete),
                           detail=(f'📋 检测到 {len(_incomplete)} 项待办未完成，'
                                   f'继续执行 ({_nudges + 1}/{_todo_max})…'))
                result['action'] = 'continue'
                return result
            if _incomplete:
                # The runaway budget is an automatic-continuation ceiling, not
                # a success escape hatch. Settle as an explicitly incomplete /
                # blocked turn so every client keeps Continue available and no
                # task with declared unfinished work is reported as completed.
                _only_blocked = not _actionable
                task['_todo_blocked'] = {
                    'reason': ('todo_items_blocked' if _only_blocked else
                               'todo_continuation_budget_exhausted'),
                    'incomplete': len(_incomplete),
                    'continuations': _nudges,
                    'max': _todo_max,
                    'todos': _incomplete,
                }
                logger.warning(
                    '[%s] 📋 Todo checklist cannot finish: nudges=%d/%d, '
                    'incomplete=%d, only_blocked=%s — settling as incomplete, '
                    'never success',
                    tid, _nudges, _todo_max, len(_incomplete), _only_blocked)
                result['action'] = 'break'
                result['last_finish_reason'] = 'incomplete'
                result['loop_exit_reason'] = (
                    f'todo_incomplete_budget_exhausted_round_{round_num}')
                return result

        # ── Intent-stall nudge () ──
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
        if _stall_nudges < 1:
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
                messages.append({
                    'role': 'user',
                    'content': _stall_text,
                    # The correction must not replace the task's real user
                    # intent or split a productive read sequence next round.
                    '_isMeta': True,
                })
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

        # Normal exit — model returned content without tool calls
        result['action'] = 'break'
        result['loop_exit_reason'] = f'no_tool_calls_round_{round_num}'

        # Defensive last resort: API reported finish_reason=tool_calls
        #   but no tool calls were assembled. The dedicated
        #   tool_calls_no_payload retry bucket above returns first, so this
        #   is only reachable if a future early-return bypasses it. Do NOT
        #   blame the phantom filter blindly — its drops carry their own
        #   'Filtering phantom' WARNINGs; when those are absent the GATEWAY
        #   simply never sent the payload. Normalize to 'stop' so the
        #   post-loop check in _finalize doesn't misinterpret this as
        #   "loop ended unexpectedly with pending tools".
        if last_finish_reason in ('tool_calls', 'tool_use'):
            logger.warning(
                '[%s] ⚠ finish_reason=%s but assistant_msg has 0 tool_calls '
                '(void cause=%s; dedicated retry bucket did not claim it). '
                'Normalizing to stop. model=%s round=%d',
                tid, last_finish_reason,
                (usage or {}).get('_tool_calls_void') or 'unknown',
                model, round_num,
            )
            result['last_finish_reason'] = 'stop'

        logger.debug(
            '[%s] Loop ending normally: model=%s returned text without '
            'tool_calls at round %d. finish_reason=%s content=%dchars',
            tid, model, round_num, result['last_finish_reason'],
            len(task.get('content') or ''),
        )
        return result

    # assistant_msg has tool_calls → but a premature close may have cut the
    # stream MID-ARGUMENTS, leaving tool calls whose accumulated JSON cannot
    # parse. Executing those would run tools on corrupt arguments (or on the
    # sanitizer's '{}' substitution). Validate BEFORE proceeding: unparseable
    # → retry the round transparently (classic-bucket budget), never execute.
    # A cut that left every arguments string parseable lost only the terminal
    # frames (JSON is self-delimiting) — proceeding is then safe. A malformed
    # frame is different: it may have contained another tool call or arguments,
    # so even apparently parseable calls must never execute from that attempt.
    _malformed_tool_stream = bool((usage or {}).get('_malformed_stream'))
    if (usage or {}).get('_missing_done') or _malformed_tool_stream:
        from lib.agent_loop import unparseable_tool_calls
        _bad_tcs = unparseable_tool_calls(assistant_msg)
        if _bad_tcs or _malformed_tool_stream:
            _trace_id = (usage or {}).get('trace_id', 'N/A')
            _bad_names = [(tc.get('function') or {}).get('name', '?')
                          for tc in _bad_tcs]
            if _malformed_tool_stream and not _bad_names:
                _bad_names = [
                    (tc.get('function') or {}).get('name', '?')
                    for tc in assistant_msg.get('tool_calls') or []
                ]
            _tool_stream_issue = (
                'malformed provider frame' if _malformed_tool_stream
                else 'truncated tool arguments'
            )
            if _premature_retry_count < _PREMATURE_RETRY_MAX_CLASSIC:
                _premature_retry_count += 1
                result['premature_retry_count'] = _premature_retry_count
                if '_premature_retry_count_phase' in task:
                    task['_premature_retry_count_phase'] = _premature_retry_count
                _backoff_s = _zero_byte_backoff_seconds(_premature_retry_count)
                logger.warning(
                    '[%s] ⚠️ UNTRUSTWORTHY TOOL STREAM at round %d: %s; '
                    '%d affected tool call(s) (%s). Retrying (%d/%d) '
                    'after %.1fs backoff instead of executing corrupt calls. '
                    'M-TraceId=%s model=%s',
                    tid, round_num, _tool_stream_issue, len(_bad_names),
                    _bad_names,
                    _premature_retry_count, _PREMATURE_RETRY_MAX_CLASSIC,
                    _backoff_s, _trace_id, model,
                )
                # Reset this round's partial text to the round base stamped by
                # stream_llm_response so the re-streamed attempt never stacks
                # on the poisoned one's tail. Record the discarded snapshot in
                # the FloorRetry residue list so the shrink-convergent
                # checkpoint/settle guards recognise it as our own discard
                # (exact byte-match) and allow the overwrite.
                with task['content_lock']:
                    _discarded_c = task['content']
                    _discarded_t = task['thinking']
                    _bc = task.get('_round_base_content')
                    _bt = task.get('_round_base_thinking')
                _new_c = _discarded_c if _bc is None else _bc
                _new_t = _discarded_t if _bt is None else _bt
                _shrunk = (_new_c != _discarded_c
                           or _new_t != _discarded_t)
                if _shrunk:
                    _residue = task.setdefault('_floor_retry_residue', [])
                    if len(_residue) < 8:
                        _residue.append({'content': _discarded_c,
                                         'thinking': _discarded_t})
                    content_epoch = reset_task_text(
                        task, content=_new_c, thinking=_new_t)
                else:
                    content_epoch = int(task.get('_contentEpoch') or 0)
                append_event(task, build_event(
                    EventType.DELTA_RESET, roundNum=round_num, discard=True,
                    contentEpoch=content_epoch))
                emit_phase(task, Phase.RETRYING,
                           attempt=_premature_retry_count,
                           max=_PREMATURE_RETRY_MAX_CLASSIC,
                           bucket=(
                               'malformed_tool_stream'
                               if _malformed_tool_stream
                               else 'truncated_tool_args'),
                           backoff_s=round(_backoff_s, 2),
                           detail=(
                               f'⚠️ 上游工具数据流不完整（{len(_bad_names)} 个调用），'
                               f'退避 {_backoff_s:.1f}s 后重试 '
                               f'({_premature_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC})…'
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
                        f'({_premature_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC}). '
                        f'M-TraceId={_trace_id}'),
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=(f'bucket=untrustworthy_tool_stream '
                     f'issue={_tool_stream_issue} bad_calls={_bad_names} '
                     f'attempts={_premature_retry_count}/'
                     f'{_PREMATURE_RETRY_MAX_CLASSIC} M-TraceId={_trace_id}'),
            )
            logger.error(
                '[%s] ⚠️ TRUNCATED TOOL CALL retries exhausted at round %d '
                '(%d/%d). bad_calls=%s M-TraceId=%s model=%s — settling '
                'finishReason=premature_close with error envelope.',
                tid, round_num, _premature_retry_count,
                _PREMATURE_RETRY_MAX_CLASSIC, _bad_names, _trace_id, model,
            )
            return result

    # A validated tool call is deliverable progress. A later no-actionable
    # incident is isolated and may use the remaining phase-wide recovery budget.
    retry_budget._clear_no_actionable_retry_streak(task)

    # assistant_msg has tool_calls → proceed to tool execution (or check budget)
    return result
