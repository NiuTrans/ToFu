"""Transport interruption recovery and its bounded diagnostic projection.

The analyser calls _handle_empty_abnormal_stream or _handle_stream_anomaly
after cancellation/protocol recovery. Every interrupted attempt is recovered by
resending the unchanged request: empty retries keep route/counter evidence, and
a cut partial reply is rewound to its round base instead of being continued.
Budgets remain in _budget, typed stream authority in api, and
lifecycle/dispatch in the root loop.
"""
from lib.agent_core.events import Phase, emit_phase
from lib.log import get_logger
from lib.tasks_pkg.round_retry import reset_round_to_base
from lib.tasks_pkg.stream_handler import _budget as retry_budget
from lib.tasks_pkg.stream_handler._budget import (
    _NO_ACTIONABLE_RETRY_MAX,
    _PREMATURE_RETRY_MAX_CLASSIC,
    _PREMATURE_RETRY_MAX_ZERO_BYTE,
    _PREMATURE_RETRY_MAX_ZERO_BYTE_STALLED,
    _ZERO_BYTE_STALL_THRESHOLD_MS,
    _zero_byte_backoff_seconds,
)

logger = get_logger(__name__)


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


def _handle_stream_anomaly(
    *,
    task,
    result,
    round_content,
    premature_retry_count,
    tid,
    model,
    round_num,
    trace_id,
    empty_stop,
):
    """Rewind and resend a cut partial reply, or settle an empty anomaly."""
    if round_content.strip():
        if premature_retry_count < _PREMATURE_RETRY_MAX_CLASSIC:
            premature_retry_count += 1
            result['premature_retry_count'] = premature_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = premature_retry_count
            backoff_seconds = _zero_byte_backoff_seconds(
                premature_retry_count)
            logger.warning(
                '[%s] ⚠️ PARTIAL STREAM ERROR at round %d: gateway stream '
                'ended without terminal frames after %d content chars. '
                'Rewinding the round and resending the unchanged request '
                '(%d/%d) after %.1fs backoff. M-TraceId=%s model=%s',
                tid, round_num, len(round_content), premature_retry_count,
                _PREMATURE_RETRY_MAX_CLASSIC, backoff_seconds, trace_id, model,
            )
            # No provider continues a cut reply from a bare trailing assistant
            # row: Claude 4.6+ and Gemini 3.6+ reject it, and OpenAI-style
            # APIs answer it as a new message. A continuation prompt would
            # stay in the working context. Leaving ``messages`` untouched
            # resends the exact request whose prompt the provider has cached.
            reset_round_to_base(task, round_num)
            emit_phase(
                task,
                Phase.RETRYING,
                attempt=premature_retry_count,
                max=_PREMATURE_RETRY_MAX_CLASSIC,
                bucket='partial_stream',
                backoff_s=round(backoff_seconds, 2),
                errorKind='premature_close',
                detail=(
                    f'⚠️ Upstream stream broke after {len(round_content)} '
                    'characters; discarding the partial reply and resending '
                    f'the same request ({premature_retry_count}/'
                    f'{_PREMATURE_RETRY_MAX_CLASSIC})…'
                ),
                detailKey='stream.phase.partialStreamRetry',
                detailArgs={
                    'chars': len(round_content),
                    'attempt': premature_retry_count,
                    'max': _PREMATURE_RETRY_MAX_CLASSIC,
                },
            )
            retry_budget._interruptible_sleep(backoff_seconds, task)
            result['action'] = 'continue'
            return result

        # The last attempt's partial stays visible; a resume rolls it back.
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
                f'{len(round_content)} content characters; bounded '
                'rewind-and-resend retries were exhausted '
                f'({premature_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC}). '
                f'M-TraceId={trace_id}'
            ),
            model=model,
            context=f'round-{round_num}',
            source='llm-stream',
            raw=(
                'bucket=partial_stream '
                f'attempts={premature_retry_count}/'
                f'{_PREMATURE_RETRY_MAX_CLASSIC} '
                f'content={len(round_content)}chars M-TraceId={trace_id}'
            ),
        )
        logger.error(
            '[%s] ⚠️ PARTIAL STREAM ERROR retries exhausted at round %d '
            '(%d/%d). Keeping %d content chars visible and settling '
            'finishReason=premature_close. M-TraceId=%s model=%s',
            tid, round_num, premature_retry_count,
            _PREMATURE_RETRY_MAX_CLASSIC, len(round_content), trace_id, model,
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
        and not is_zero_byte
    )
    # A zero-byte attempt that already spent at least 60s of the transport
    # idle window is an upstream stall, not the fast gateway close the large
    # budget was sized for. Each replay costs most of a window plus
    # prompt-cache traffic, so it gets the small alternate-slot budget
    # instead of 16 slow replays.
    is_zero_byte_stalled = (
        is_zero_byte and stream_elapsed_ms >= _ZERO_BYTE_STALL_THRESHOLD_MS
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
        else _PREMATURE_RETRY_MAX_ZERO_BYTE_STALLED if is_zero_byte_stalled
        else _PREMATURE_RETRY_MAX_ZERO_BYTE if is_zero_byte
        else _PREMATURE_RETRY_MAX_CLASSIC
    )
    retry_bucket = (
        'semantic_progress_timeout' if is_semantic_timeout
        else 'zero_byte_stalled' if is_zero_byte_stalled
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
        # Heartbeats and short reasoning prefixes are transport activity too.
        # Their EOF needs the same pacing as a zero-byte/long-reasoning cut;
        # neither SSE count nor round number proves an immediate retry is safe.
        backoff_s = _zero_byte_backoff_seconds(phase_retry_count)

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
    if is_semantic_timeout or is_zero_byte_stalled:
        # A whole-turn replay would only buy another full idle window per
        # stall, so stop unattended replay; manual Retry remains available.
        error_extensions['autoRetryExhausted'] = True
        retry_budget_detail = f'phase={phase_retry_count}/{retry_cap}'
        if is_semantic_timeout:
            retry_budget_detail += (
                f'; consecutive={no_actionable_streak}/'
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
            if is_semantic_timeout
            else '⚠️ 上游长时间没有返回任何流数据\n'
                 'Upstream returned no stream data for an extended period'
            if is_zero_byte_stalled else ''
        ),
        hint=(
            '系统已自动尝试其他同模型槽位；继续自动重放收益很低，已停止避免长时间空耗。\n\n'
            'Alternate slots for the same model were tried automatically; '
            'replay stopped to avoid another long no-progress loop.'
            if is_semantic_timeout
            else '已自动尝试其他同模型槽位；上游持续无数据时自动重放收益很低，'
                 '已停止避免长时间空耗。\n\n'
                 'Alternate slots for the same model were tried automatically; '
                 'replay stopped to avoid another long silent stall.'
            if is_zero_byte_stalled else None
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
