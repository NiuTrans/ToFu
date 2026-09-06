"""Core LLM-call-with-fallback entry point.

Streams one LLM round and transparently retries with the configured fallback
model when the primary model errors out; also drives same-model reactive
compaction when the provider rejects an oversized prompt or the local memory
guard cannot safely serialise the current derived payload. Dependencies are
bound from their concrete owner modules, so tests and callers have one
explicit injection seam.

The reactive-compaction retry state (``_reactive_compact_attempts`` /
``_REACTIVE_COMPACT_MAX_RETRIES``) is imported BY REFERENCE from
``._state`` — there is exactly one such dict in the process.
"""

from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.cgroup_guard import MemoryPressureError, approx_body_bytes
from lib.llm import AbortedError, build_body, model_supports_vision
from lib.llm.stream_result import ensure_provider_stream_result
from lib.llm_error_format import format_llm_error_for_user
from lib.llm_errors import _ERR_BODY_LIMIT
from lib.log import audit_log, get_logger
from lib.llm_dispatch.retry_i18n import display_model_name as _display_model_name
from lib.tasks_pkg.llm_fallback._retry import (
    _fallback_max_429_attempts,
    _flag_empty_stop_for_retry,
    _get_fallback_model,
    _get_pool_rescue_model,
)
from lib.tasks_pkg.llm_fallback._usage import (
    _emit_round_usage,
    project_usage_for_round_record,
)
from lib.tasks_pkg.manager._events import append_event
from lib.tasks_pkg.manager._stream import stream_llm_response

# Shared reactive-compaction state — imported by reference (never reassigned)
# so cleanup_reactive_compact_state mutates the SAME dict this module reads.
from lib.tasks_pkg.llm_fallback._state import (
    _reactive_compact_attempts,
    _REACTIVE_COMPACT_MAX_RETRIES,
)

logger = get_logger(__name__)


def _log_stream_attempt_outcome(
    task,
    *,
    tid,
    round_num,
    model,
    stream_result,
    label='LLM',
    fallback_from=None,
    failed_models=None,
):
    """Log transport completion without calling an unusable stream success."""
    assistant_msg = stream_result.message
    usage = stream_result.usage or {}
    content_len = len(assistant_msg.get('content', '') or '')
    tool_calls = len(assistant_msg.get('tool_calls', []) or [])
    trace_id = usage.get('trace_id', 'N/A')
    elapsed_s = (usage.get('stream_elapsed_ms', 0) or 0) / 1000
    context_parts = []
    if fallback_from:
        context_parts.append(f'fallback_from={fallback_from}')
    if failed_models:
        context_parts.append(f'failed_models={sorted(failed_models)}')
    context = f" {' '.join(context_parts)}" if context_parts else ''

    if stream_result.is_verified_complete:
        logger.info(
            '[%s] conv=%s ✓ %s round %d OK: stream_state=%s '
            'finish_reason=%s model=%s content=%dchars tool_calls=%d '
            'M-TraceId=%s elapsed=%.1fs%s',
            tid, task.get('convId', ''), label, round_num,
            stream_result.state.value, stream_result.finish_reason, model,
            content_len, tool_calls, trace_id, elapsed_s, context,
        )
        return

    logger.warning(
        '[%s] conv=%s ⚠ %s round %d UNUSABLE: stream_state=%s '
        'compatibility_finish_reason=%s model=%s content=%dchars tool_calls=%d '
        'M-TraceId=%s elapsed=%.1fs; recovery analysis pending%s',
        tid, task.get('convId', ''), label, round_num,
        stream_result.state.value, stream_result.finish_reason, model,
        content_len, tool_calls, trace_id, elapsed_s, context,
    )


def _is_gateway_error(exc: BaseException) -> bool:
    """True for the gateway/outage error class — RateLimitError raised with
    ``is_gateway=True`` (HTTP 502/503/504, plus vendor-transient outages
    wrapped in 4xx bodies). This class is WAITABLE, not proof the model is
    dead: the dispatcher already cycled within the pinned model's slots
    until the outage budget expired. It must NEVER trigger a model switch
    (owner directive 2026-08-18, strict mode)."""
    try:
        from lib.llm import RateLimitError as _RL
    except Exception as _imp:
        logger.debug('lib.llm import failed in gateway-error check: %s', _imp)
        return False
    return isinstance(exc, _RL) and bool(getattr(exc, 'is_gateway', False))


def _is_request_payload_error(exc: BaseException) -> bool:
    """True when changing models cannot repair the rejected request.

    These errors describe this request's wire shape or semantics, not model
    availability. Replaying the same payload on a configured fallback (and
    possibly stripping unsupported inputs such as images) only hides the
    producer defect and violates strict model selection.
    """
    try:
        from lib.llm import BadRequestError, RequestScopedError
    except Exception as import_error:
        logger.debug('lib.llm import failed in request-error check: %s',
                     import_error)
        return False
    return isinstance(exc, (BadRequestError, RequestScopedError))


def _is_local_request_preparation_error(exc: BaseException) -> bool:
    """True when dispatch proves no provider request was attempted."""
    try:
        from lib.llm import LocalRequestPreparationError
    except Exception as import_error:
        logger.debug('lib.llm import failed in local-prepare check: %s',
                     import_error)
        return False
    return isinstance(exc, LocalRequestPreparationError)


def _attempt_pool_rescue(task, body, round_num, max_tokens, tool_list,
                         accumulated_usage, api_rounds,
                         *, failed_models, original_model, cause_exc,
                         preset, thinking_enabled, on_tool_call_ready=None):
    """Last-resort pool-wide dispatch before a turn is allowed to die.

    Owner directive 2026-08-03: an error envelope may surface ONLY when every
    key in the pool is unavailable. The fallback chain (primary → configured
    fallback model) covers two models; when both fail — e.g. the fallback
    model's only key got a durable 401 while the rest of the pool is healthy
    (task 7ea8c25f, kimi-k3 pinned to one key_access cell whose AppId the
    vendor rejected) — the turn used to die with a "check your API keys"
    envelope even though other models could have completed it. This helper
    makes ONE more dispatch attempt across every remaining (key, model)
    slot: ``pool_wide=True`` stays non-strict, but softly prefers the configured
    default model before score-ranked catalogue alternatives.
    ``exclude_models`` skips the models already proven dead in this chain, so
    the rescue cannot re-enter their failure / 429 wall.

    Returns the standard OK result dict on rescue success, else ``None``
    (the caller then surfaces the original error envelope unchanged).
    """
    tid = task['id'][:8]
    failed_models = {m for m in (failed_models or set()) if m}
    rescue_prefer_model = _get_pool_rescue_model(failed_models)

    # ── Gate: is there anything healthy BEYOND the failed models? ──
    # A probe failure must not suppress the rescue attempt (the attempt is
    # bounded by dispatch_stream's own max_retries either way).
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        _disp = get_dispatcher()
        _has = _disp.has_capable_slots('text', exclude_models=failed_models)
    except Exception as _ge:
        logger.debug('[%s] pool-rescue gate probe failed: %s', tid, _ge)
        _has = True
    if not _has:
        logger.warning('[%s] pool-rescue skipped — no capable slot beyond '
                       'failed models %s', tid, sorted(failed_models))
        return None

    # Classify the cause so the badge names WHY the rescue fired (the same
    # typed kind the normal fallback badge uses — 'permission', …).
    try:
        from lib.error_envelope import from_exception as _from_exc
        _rk_env = _from_exc(cause_exc, model=original_model,
                            context='pool-rescue', source='llm-stream')
        _rk_kind = _rk_env.get('kind', 'generic')
        _rk_detail = (_rk_env.get('detail') or str(cause_exc)).strip()
    except Exception as _ce:
        logger.debug('[%s] pool-rescue cause classify failed: %s', tid, _ce)
        _rk_kind, _rk_detail = 'generic', str(cause_exc)[:200]

    append_event(task, build_phase(
        Phase.RETRYING,
        detail=(f'⚠️ {original_model} 及其回退模型均不可用（{_rk_kind}）— '
                f'正在尝试池中其它可用模型…'),
    ))

    _tools_this = tool_list
    _rescue_body = dict(body)
    if _tools_this is not None:
        _rescue_body['tools'] = _tools_this
    # Same session-stable TTL latch rule as every rebuild path.
    _rescue_body['_task_id'] = task.get('id', '')

    try:
        stream_result = ensure_provider_stream_result(stream_llm_response(
            task, _rescue_body, tag=f'R{round_num+1}-RESCUE',
            on_tool_call_ready=on_tool_call_ready,
            pool_wide=True, exclude_models=failed_models,
            pool_prefer_model=rescue_prefer_model or None,
            max_429_attempts=_fallback_max_429_attempts(task)))
        assistant_msg, finish_reason, usage = stream_result
    except Exception as e3:
        if isinstance(e3, AbortedError):
            raise
        if isinstance(e3, MemoryPressureError):
            return _finish_local_memory_pressure(
                task, e3, _rescue_body.get('model') or original_model,
                preset, thinking_enabled, round_num)
        logger.warning('[%s] pool-rescue dispatch also failed: %s', tid, e3)
        return None

    _rescue_model = ((usage or {}).get('_dispatch') or {}).get('model') \
        or _rescue_body.get('model') or original_model
    _rescue_from_model = _rescue_body.get('model') or original_model

    if usage is not None and _flag_empty_stop_for_retry(
            assistant_msg, finish_reason, task, round_num, usage):
        logger.warning('[%s] ⚠️ Round-0 EMPTY STOP (pool-rescue model=%s) — '
                       'flagging for empty_stop retry', tid, _rescue_model)

    # Badge: keep the ORIGINAL primary as _fallback_from when an earlier hop
    # already recorded it (the rescue is the tail of the same chain).
    if not task.get('_fallback_from'):
        task['_fallback_from'] = original_model
    task['_fallback_model'] = _rescue_model
    task['_fallback_reason'] = (
        f'{_rk_kind}: {_rk_detail}' if _rk_detail else _rk_kind)[:300]
    task['_fallback_kind'] = _rk_kind
    if _rescue_model != _rescue_from_model:
        append_event(task, build_event(
            EventType.MODEL_FALLBACK,
            fallbackModel=_rescue_model,
            fallbackFrom=_rescue_from_model,
            fallbackKind='pool_rescue',
            fallbackReason=task['_fallback_reason'],
        ))

    # Honest accounting — identical to the primary/fallback success paths.
    if usage:
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                accumulated_usage[k] = accumulated_usage.get(k, 0) + v
        api_rounds.append({'round': round_num + 1, 'model': _rescue_model,
                           'usage': project_usage_for_round_record(usage),
                           'tag': f'R{round_num+1}-RESCUE'})
        _emit_round_usage(task, round_num + 1, _rescue_model, usage,
                          tag=f'R{round_num+1}-RESCUE')
        for _bill in (usage.get('_extra_billing_rounds') or []):
            _bu = _bill.get('usage') or {}
            for k, v in _bu.items():
                if isinstance(v, (int, float)):
                    accumulated_usage[k] = accumulated_usage.get(k, 0) + v
            api_rounds.append({
                'round': round_num + 1,
                'model': _bill.get('model') or _rescue_model,
                'usage': project_usage_for_round_record(_bu),
                'tag': _bill.get('tag') or f'R{round_num+1}-RESCUE-DISCARDED',
                'responseAuthoring': False,
            })
            _emit_round_usage(task, round_num + 1,
                              _bill.get('model') or _rescue_model, _bu,
                              tag=_bill.get('tag') or f'R{round_num+1}-RESCUE-DISCARDED',
                              response_authoring=False)

    audit_log('model_fallback', old=original_model, new=_rescue_model,
              reason=f'pool-rescue: {_rk_kind}: {_rk_detail[:160]}',
              kind=_rk_kind, tid=tid, conv=task.get('convId', ''))
    _log_stream_attempt_outcome(
        task,
        tid=tid,
        round_num=round_num + 1,
        model=_rescue_model,
        stream_result=stream_result,
        label='POOL-RESCUE',
        fallback_from=original_model,
        failed_models=failed_models,
    )

    return {
        'assistant_msg': assistant_msg,
        'finish_reason': finish_reason,
        'usage': usage,
        'stream_result': stream_result,
        'model': _rescue_model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': None,
        '_loop_exit_reason': None,
    }


def _learn_context_limit_from_overflow(task, model, error, tid):
    """Best-effort learning for provider-reported context-window overflows."""
    try:
        from lib.context_limits import learn_shrink_from_error
        from lib.tasks_pkg.compaction._tokens import (
            _get_context_limit,
            _parse_context_overflow,
        )
        reported, stated_max = _parse_context_overflow(str(error))
        prior_limit = _get_context_limit(task)
        learned_info = learn_shrink_from_error(
            task.get('provider_id') or '', model, reported,
            preset_limit=prior_limit, stated_max=stated_max)
        if learned_info:
            append_event(task, build_phase(
                Phase.RETRYING,
                detail=(
                    f'⚙️ Auto-detected smaller context window for {model}: '
                    f'{learned_info["new_limit"]:,} tokens '
                    f'(was {learned_info["old_limit"]:,})'
                ),
            ))
    except Exception as learn_error:
        logger.debug('[%s] context_limits shrink-learn failed: %s',
                     tid, learn_error)


def _prepare_reactive_retry_body(task, body, model, messages, tool_list,
                                 preset, thinking_enabled, cause, tid,
                                 *, vision_fallback_from=''):
    """Compact derived context and rebuild one same-model retry body.

    Compaction/body rebuilding are recovery mechanics. If either mechanic has
    its own defect, keep the original typed failure so it can be surfaced or
    handled by normal policy instead of replacing it with a new FATAL.
    """
    try:
        from lib.tasks_pkg.compaction.api import reactive_compact
        byte_target = None
        if isinstance(cause, MemoryPressureError):
            # Make the recovery load-bearing: halve the derived body, bounded
            # at 1 MiB, instead of hoping token compaction happens to reduce a
            # request whose model context may already be perfectly valid.
            byte_target = max(1 << 20, approx_body_bytes(body) // 2)
        reactive_compact(
            messages, task=task, error_text=str(cause),
            byte_target=byte_target)
        retry_body = build_body(
            model, messages,
            max_tokens=task.get('config', {}).get('maxTokens', 128000),
            temperature=body.get('temperature', 1.0),
            thinking_enabled=thinking_enabled,
            preset=preset,
            tools=tool_list,
            response_format=body.get('response_format'),
            stream=True,
            vision_fallback_from=vision_fallback_from,
        )
    except Exception as prep_error:
        logger.error(
            '[%s] Reactive request recovery could not prepare a smaller body; '
            'preserving original %s: %s',
            tid, type(cause).__name__, prep_error, exc_info=True)
        return None

    # Preserve the session-stable TTL latch key after a body rebuild.
    retry_body['_task_id'] = task.get('id', '')
    return retry_body


def _record_reactive_usage(task, usage, model, round_num,
                           accumulated_usage, api_rounds):
    """Account for a successful reactive retry and discarded sub-rounds."""
    if not usage:
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            accumulated_usage[key] = accumulated_usage.get(key, 0) + value
    api_rounds.append({
        'round': round_num + 1,
        'model': model,
        'usage': project_usage_for_round_record(usage),
        'tag': f'R{round_num+1}-REACTIVE',
    })
    _emit_round_usage(
        task, round_num + 1, model, usage, tag=f'R{round_num+1}-REACTIVE')
    for billed_round in (usage.get('_extra_billing_rounds') or []):
        billed_usage = billed_round.get('usage') or {}
        for key, value in billed_usage.items():
            if isinstance(value, (int, float)):
                accumulated_usage[key] = accumulated_usage.get(key, 0) + value
        tag = (billed_round.get('tag')
               or f'R{round_num+1}-REACTIVE-DISCARDED')
        billed_model = billed_round.get('model') or model
        api_rounds.append({
            'round': round_num + 1,
            'model': billed_model,
            'usage': project_usage_for_round_record(billed_usage),
            'tag': tag,
            'responseAuthoring': False,
        })
        _emit_round_usage(
            task, round_num + 1, billed_model, billed_usage, tag=tag,
            response_authoring=False)


def _attempt_request_payload_recovery(
        task, body, model, round_num, tool_list, messages, preset,
        thinking_enabled, accumulated_usage, api_rounds,
        on_tool_call_ready, cause, *, max_429_attempts=None,
        vision_fallback_from=''):
    """Shrink a locally-derived request and retry the same model once.

    Returns ``(success_result, next_error, retry_body)``. ``next_error`` is
    the reactive retry's actual exception, so downstream policy never
    misclassifies it as the original size/pressure failure.
    """
    tid = task['id'][:8]
    task_id = task.get('id', '')
    attempts = _reactive_compact_attempts.get(task_id, 0)
    memory_pressure = isinstance(cause, MemoryPressureError)

    if not memory_pressure:
        _learn_context_limit_from_overflow(task, model, cause, tid)

    if attempts >= _REACTIVE_COMPACT_MAX_RETRIES:
        logger.error(
            '[%s] Reactive request recovery exhausted (%d/%d) for %s',
            tid, attempts, _REACTIVE_COMPACT_MAX_RETRIES,
            type(cause).__name__)
        return None, cause, None

    _reactive_compact_attempts[task_id] = attempts + 1
    cause_label = ('local memory headroom' if memory_pressure
                   else 'provider context limit')
    logger.warning(
        '[%s] ⚡ REACTIVE COMPACT at round %d (attempt %d/%d): %s for '
        'model=%s — reducing the derived payload and retrying the same model',
        tid, round_num, attempts + 1, _REACTIVE_COMPACT_MAX_RETRIES,
        cause_label, model)

    retry_body = _prepare_reactive_retry_body(
        task, body, model, messages, tool_list, preset, thinking_enabled,
        cause, tid, vision_fallback_from=vision_fallback_from)
    if retry_body is None:
        return None, cause, None

    if memory_pressure:
        phase = build_phase(
            Phase.RETRYING,
            detail='⚡ 本地内存余量不足，已缩减请求上下文后重试…',
            detailKey='stream.phase.compactingWindow',
        )
    else:
        phase = build_phase(
            Phase.RETRYING,
            detail=(f'⚡ 上下文超长，已自动压缩 '
                    f'(reactive compact {attempts + 1}/'
                    f'{_REACTIVE_COMPACT_MAX_RETRIES})…'),
            detailKey='stream.phase.reactiveCompact',
            detailArgs={
                'attempt': attempts + 1,
                'max': _REACTIVE_COMPACT_MAX_RETRIES,
            },
        )
    # This event belongs to authoritative task state; preserve its existing
    # fail-closed behavior rather than hiding a durable-write failure.
    append_event(task, phase)

    try:
        stream_result = ensure_provider_stream_result(stream_llm_response(
            task, retry_body, tag=f'R{round_num+1}-REACTIVE',
            on_tool_call_ready=on_tool_call_ready,
            max_429_attempts=max_429_attempts))
        assistant_msg, finish_reason, usage = stream_result
    except Exception as retry_error:
        if isinstance(retry_error, AbortedError):
            return None, retry_error, retry_body
        logger.error('[%s] Reactive compact retry also failed: %s',
                     tid, retry_error, exc_info=True)
        return None, retry_error, retry_body

    _record_reactive_usage(
        task, usage, model, round_num, accumulated_usage, api_rounds)
    return ({
        'assistant_msg': assistant_msg,
        'finish_reason': finish_reason,
        'usage': usage,
        'stream_result': stream_result,
        'model': model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': None,
        '_loop_exit_reason': None,
    }, None, retry_body)


def _finish_local_memory_pressure(task, error, model, preset,
                                  thinking_enabled, round_num):
    """Settle genuine local pressure without a useless model/pool switch."""
    # A fallback/rescue may have been announced at decision time but produced
    # no reply before the shared local guard refused its payload. Never persist
    # a model-switch badge for a switch that did not successfully generate.
    _clear_failed_fallback_stamp(task)
    task['error'] = format_llm_error_for_user(
        error, model=model, context='local-memory-pressure',
        source='llm-stream')
    logger.warning(
        '[%s] Local request headroom is still insufficient after compaction; '
        'settling as retryable server_busy without switching models',
        task['id'][:8])
    return {
        'assistant_msg': {'role': 'assistant', 'content': ''},
        'finish_reason': 'error',
        'usage': None,
        'model': model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': 'break',
        '_loop_exit_reason': f'local_memory_pressure_round_{round_num}',
    }


def _clear_failed_fallback_stamp(task):
    """Remove decision-time fallback metadata when no fallback reply exists."""
    for key in ('_fallback_model', '_fallback_from',
                '_fallback_reason', '_fallback_kind'):
        task.pop(key, None)


def _settle_failed_fallback(task, error, *, original_model, fallback_model,
                            preset, thinking_enabled, round_num):
    """Return a normal loop break carrying a typed terminal fallback error.

    A configured fallback is already an inner recovery attempt. Once it (and
    any bounded pool rescue) fails, raising into a separate fatal path makes
    client settlement depend on exception plumbing and permits whole-turn
    retries to reset the same wait budget. Normal loop finalization guarantees
    one ``done`` event with ``done.error``; ``autoRetryExhausted`` keeps the
    bounded failure bounded while preserving manual Retry.
    """
    envelope = format_llm_error_for_user(
        error,
        model=fallback_model,
        context=f'both-failed ({original_model}→{fallback_model})',
        source='llm-fallback',
    )
    envelope.update({
        'autoRetryExhausted': True,
        'fallbackFailed': True,
        'fallbackFrom': original_model,
        'fallbackModel': fallback_model,
    })
    for field in ('attempts', 'limit',
                  'credential_delivery_anomaly_attempts',
                  'credential_delivery_anomaly_limit'):
        value = getattr(error, field, None)
        if value is not None:
            envelope[field] = value
    task['error'] = envelope
    return {
        'assistant_msg': {'role': 'assistant', 'content': ''},
        'finish_reason': 'error',
        'usage': None,
        'model': fallback_model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': 'break',
        '_loop_exit_reason': f'both_models_failed_round_{round_num}',
    }


def _llm_call_with_fallback(task, body, model, round_num, max_tokens,
                             tool_call_happened, tool_list,
                             messages, preset, thinking_enabled,
                             accumulated_usage, api_rounds,
                             on_tool_call_ready=None):
    """Make an LLM call with automatic fallback to Opus on failure.

    Streams the LLM response for the current round.  If the primary model
    fails, transparently falls back to Claude Opus 4 (medium preset) and
    retries once.  Detects content-filter blocks (empty first-round
    responses) and output-token truncation, logging at appropriate levels.

    Parameters
    ----------
    task : dict
        Live task dict — mutated in-place (content, _fallback_model, etc.).
    body : dict
        Pre-built request body for the primary LLM call.
    model : str
        Current model identifier.
    round_num : int
        Zero-based loop iteration index.
    max_tokens : int
        Max output tokens (for truncation logging).
    tool_call_happened : bool
        Whether any tool call executed in prior rounds.
    tool_list : list | None
        Tool definitions list (needed if fallback must rebuild body).
    messages : list
        Conversation messages (needed if fallback rebuilds body).
    preset : str
        Current preset name.
    thinking_enabled : bool
        Whether extended thinking is active.
    accumulated_usage : dict
        Mutable usage accumulator — updated in-place.
    api_rounds : list
        Mutable per-round usage list — appended in-place.

    Returns
    -------
    dict with keys:
        assistant_msg    – The parsed assistant message dict.
        finish_reason    – Finish reason string from the API.
        usage            – Raw usage dict from the response (or None).
        model            – Model actually used (may differ if fallback fired).
        preset           – Preset actually used.
        thinking_enabled – Thinking flag actually used.
        _loop_action     – 'break' if caller must break the loop, else None.
        _loop_exit_reason – Set when _loop_action == 'break'.

    Raises
    ------
    Exception
        Re-raised when both primary and fallback models fail and no prior
        tool calls exist (unrecoverable first-round error).
    lib.llm.AbortedError
        Never caught — propagates directly to signal user abort.
    """
    tid = task['id'][:8]
    _FALLBACK_MODEL = _get_fallback_model(task)
    # Distinguish "admin never configured a fallback" from "this request
    # explicitly opted out" so the surfaced error envelope names the
    # actual cause (context='fallback-disabled') instead of an opaque
    # 'no-fallback'.  Headless callers who set disableModelFallback need
    # this to understand why a transient primary error wasn't masked.
    _fb_disabled_by_request = False
    try:
        _fb_disabled_by_request = bool((task.get('config') or {}).get('disableModelFallback'))
    except Exception as _e:
        logger.debug('[%s] disableModelFallback flag read failed: %s', tid, _e)
    _no_fb_context = ('fallback-disabled' if _fb_disabled_by_request
                      else 'no-fallback')

    # ── Primary model call ──
    try:
        stream_result = ensure_provider_stream_result(stream_llm_response(
            task, body, tag=f'R{round_num+1}',
            on_tool_call_ready=on_tool_call_ready))
        assistant_msg, finish_reason, usage = stream_result
        last_finish_reason = finish_reason

        # Round-0 empty stop → flag for the empty_stop/zero_byte RETRY bucket,
        # NOT a terminal content_filter. A genuine policy block is HTTP 450
        # (ContentFilterError, handled below and terminal); a plain empty stop
        # is a transient gateway artifact (proven by debug/repro_conv_empty_stop.py).
        # When the stream layer already flagged an anomaly, analyse_stream_result
        # retries it unchanged; when it slipped through unflagged (whitespace-only
        # body, or a zero-chunk clean [DONE]), the helper sets the flags so the
        # retry bucket still fires. Only after retries are exhausted does it
        # surface as abnormal_stop.
        if usage is not None and _flag_empty_stop_for_retry(
                assistant_msg, finish_reason, task, round_num, usage):
            logger.warning('[%s] ⚠️ Round-0 EMPTY STOP (model=%s) — flagging for '
                           'empty_stop retry (NOT content_filter; a real policy '
                           'block would be HTTP 450). Will retry then surface as '
                           'abnormal_stop if it persists.', tid, model)

        # Log output-token truncation so operators can tune max_tokens
        if finish_reason in ('length', 'max_tokens'):
            _trunc_content_len = len(assistant_msg.get('content', ''))
            _trunc_tool_calls = len(assistant_msg.get('tool_calls', []))
            _u_trace = (usage or {}).get('trace_id', 'N/A')
            _u_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
            logger.warning('[%s] ⚠️ TRUNCATED at round %d: finish_reason=%s '
                           'content=%dchars tool_calls=%d model=%s max_tokens=%s '
                           'M-TraceId=%s elapsed=%.1fs — '
                           'output token limit reached',
                           tid, round_num, finish_reason, _trunc_content_len,
                           _trunc_tool_calls, model, max_tokens,
                           _u_trace, _u_elapsed / 1000)

        if usage:
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    accumulated_usage[k] = accumulated_usage.get(k, 0) + v
            api_rounds.append({'round': round_num + 1, 'model': model,
                               'usage': project_usage_for_round_record(usage),
                               'tag': f'R{round_num+1}'})
            _emit_round_usage(task, round_num + 1, model, usage, tag=f'R{round_num+1}')
            # HONEST ACCOUNTING: bill every DISCARDED FloorRetry attempt the
            #   gateway processed. Each was a real request the provider charged
            #   for — hiding them made cost popover / wallet debit / daily
            #   report under-report by ~9%~50% per triggered round. See
            #   lib.tasks_pkg.floor_retry docstring for the full story.
            _extra = (usage or {}).get('_extra_billing_rounds') or []
            for _bill in _extra:
                _bill_usage = _bill.get('usage') or {}
                for k, v in _bill_usage.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({
                    'round': round_num + 1,
                    'model': _bill.get('model') or model,
                    'usage': project_usage_for_round_record(_bill_usage),
                    'tag': _bill.get('tag') or f'R{round_num+1}-DISCARDED',
                    'responseAuthoring': False,
                })
                _emit_round_usage(task, round_num + 1,
                                  _bill.get('model') or model,
                                  _bill_usage,
                                  tag=_bill.get('tag') or f'R{round_num+1}-DISCARDED',
                                  response_authoring=False)
            if _extra:
                logger.warning('[%s] conv=%s billed %d discarded FloorRetry '
                               'attempt(s) into api_rounds/accumulated_usage — '
                               'cost popover now matches gateway bill',
                               tid, task.get('convId', ''), len(_extra))

        _log_stream_attempt_outcome(
            task,
            tid=tid,
            round_num=round_num + 1,
            model=model,
            stream_result=stream_result,
        )

        return {
            'assistant_msg': assistant_msg,
            'finish_reason': last_finish_reason,
            'usage': usage,
            'stream_result': stream_result,
            'model': model,
            'preset': preset,
            'thinking_enabled': thinking_enabled,
            '_loop_action': None,
            '_loop_exit_reason': None,
        }

    except Exception as e:
        # AbortedError must escape — never fallback/retry on user abort
        from lib.llm import ContentFilterError, PromptTooLongError
        if isinstance(e, AbortedError):
            logger.debug('[%s] ✋ AbortedError at round %d — stopping immediately', tid, round_num)
            raise

        # Derived-payload failures are recoverable locally. Repeatedly compact
        # within the small shared retry budget, preserving the latest typed
        # error from each retry. Local memory pressure is never evidence that
        # another model/provider will help, so a persistent instance settles as
        # server_busy instead of replaying the same body across the pool.
        while isinstance(e, (PromptTooLongError, MemoryPressureError)):
            _prior_error = e
            _recovered, e, _retry_body = _attempt_request_payload_recovery(
                task, body, model, round_num, tool_list, messages, preset,
                thinking_enabled, accumulated_usage, api_rounds,
                on_tool_call_ready, e)
            if _retry_body is not None:
                body = _retry_body
            if _recovered is not None:
                return _recovered
            if isinstance(e, AbortedError):
                raise e
            if e is _prior_error:
                break

        if isinstance(e, MemoryPressureError):
            return _finish_local_memory_pressure(
                task, e, model, preset, thinking_enabled, round_num)

        # InvalidImageError — image content rejected (too large, corrupt, etc.)
        # Fallback to another model won't help (same image = same rejection).
        from lib.llm import InvalidImageError
        if isinstance(e, InvalidImageError):
            err_str = str(e)[:300]
            logger.warning('[%s] 🖼️ INVALID_IMAGE at round %d model=%s: %s',
                           tid, round_num, model, err_str)
            from lib.error_envelope import make_envelope as _make_env
            if 'many-image' in err_str.lower():
                _hint_cn = '过多大图。同时发送 5 张以上图片时，每张需小于 2000×2000像素。请压缩或删除部分图片。'
                _hint_en = ('Too many large images. When sending 5+ images, each must be '
                            'under 2000×2000 pixels. Please resize or remove some images.')
                _hint_key = 'err.k.invalid_image.hintMany'
            else:
                _hint_cn = '会话中某张图片超过了 API 大小限制。请使用更小的图片或删除过大的图片。'
                _hint_en = ('One or more images in this conversation exceed the API size '
                            'limit. Please use a smaller image or remove the oversized image.')
                _hint_key = 'err.k.invalid_image.hintSize'
            envelope = _make_env(
                'invalid_image',
                # Legacy bilingual hint stays byte-identical for headless
                # clients; the keyed variant lets the frontend localize.
                hint=f'解决办法 / How to fix:\n• {_hint_cn}\n\n• {_hint_en}',
                hint_key=_hint_key,
                detail=err_str,
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=str(e),
            )
            task['error'] = envelope
            return {
                'assistant_msg': {'role': 'assistant', 'content': ''},
                'finish_reason': 'error',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'break',
                '_loop_exit_reason': f'invalid_image_round_{round_num}',
            }

        # ContentFilterError (HTTP 450) — content policy violation.
        # Fallback to another model won't help (same content = same filter).
        # Return content_filter finish_reason so orchestrator shows the right message.
        if isinstance(e, ContentFilterError):
            err_str = str(e)[:_ERR_BODY_LIMIT]
            logger.warning('[%s] 🚫 CONTENT_FILTER (HTTP 450) at round %d model=%s: %s',
                           tid, round_num, model, err_str, exc_info=True)
            return {
                'assistant_msg': {'role': 'assistant', 'content': ''},
                'finish_reason': 'content_filter',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'break',
                '_loop_exit_reason': f'content_filter_http450_round_{round_num}',
            }

        original_model = model
        err_str = str(e)[:_ERR_BODY_LIMIT]
        logger.error('[%s] conv=%s LLM call failed at round %d (model=%s): %s '
                     '(check M-TraceId in preceding debug logs for gateway coordination)',
                     tid, task.get('convId', ''), round_num + 1, model, err_str, exc_info=True)

        # Gateway/outage class — WAITABLE, never a model-switch trigger
        #   (owner directive 2026-08-18, strict mode). A 502-class error is
        #   the backend being sick, not this model being dead; the
        #   dispatcher already waited it out cycling WITHIN the pinned
        #   model's slots (strict_model=True, pair-level rotation only).
        #   Switching to the configured fallback — or rescuing onto an
        #   arbitrary pool slot — here is exactly the silent model swap the
        #   user forbade: they interrupt and switch models THEMSELVES.
        #   Surface the upstream_error envelope (retryable) instead.
        #   NOTE (owner directive 2026-08-20): the dispatcher's outage
        #   budget is DISABLED by default (TOFU_GATEWAY_OUTAGE_BUDGET_S=0)
        #   — it waits out a gateway storm indefinitely and only the user's
        #   abort interrupts it — so this branch fires only when ops
        #   re-enable the bounded give-up (or a non-dispatch path raises
        #   the class).
        if _is_gateway_error(e):
            _gw_status = int(getattr(e, 'status_code', 0) or 0)
            _gw_label = _display_model_name(model)
            logger.warning(
                '[%s] 🛑 Gateway outage (HTTP %s) outlasted the dispatch wait '
                'budget on model=%s — NOT switching models (strict mode); '
                'surfacing upstream_error envelope instead',
                tid, _gw_status or '?', model)
            append_event(task, build_phase(
                Phase.RETRYING,
                detail=(f'⛔ 后端网关故障持续未恢复——模型保持 {_gw_label}，'
                        f'未自动切换。可稍后重试，或中断后手动切换模型。'),
                detailKey='stream.phase.gatewayOutageFinal',
                detailArgs={'model': _gw_label, 'status': _gw_status},
            ))
            _gateway_error_envelope = format_llm_error_for_user(
                e, model=model, context='gateway-outage', source='llm-stream')
            if tool_call_happened:
                task['error'] = _gateway_error_envelope
                return {
                    'assistant_msg': {'role': 'assistant', 'content': ''},
                    'finish_reason': 'error', 'usage': None,
                    'model': model, 'preset': preset,
                    'thinking_enabled': thinking_enabled,
                    '_loop_action': 'break',
                    '_loop_exit_reason': f'gateway_outage_round_{round_num}',
                }
            try:
                e._user_message = _gateway_error_envelope  # type: ignore[attr-defined]
            except Exception as _attr_err:
                logger.debug('[%s] Could not attach _user_message: %s',
                             tid, _attr_err)
            raise

        # Local request construction failed before provider ingress. This is
        # our defect, not model availability; rotating models only hides the
        # cause and spends unrelated capacity.
        if _is_local_request_preparation_error(e):
            logger.error(
                '[%s] Local request preparation failed on model=%s at %s — '
                'NOT switching models or attempting pool rescue',
                tid, model, getattr(e, 'stage', 'request_prepare'))
            try:
                e._user_message = format_llm_error_for_user(  # type: ignore[attr-defined]
                    e, model=model, context='request-prepare',
                    source='llm-stream')
            except Exception as attribute_error:
                logger.debug('[%s] Could not attach _user_message: %s',
                             tid, attribute_error)
            raise

        # Deterministic request rejection — never switch models or attempt a
        # pool-wide rescue. A 400/404/422 says the payload/route is invalid;
        # it is not evidence that the selected model is dead. In particular,
        # a locally malformed tool schema must remain visible as our defect
        # instead of silently degrading a Kimi vision request onto text-only
        # GLM.
        if _is_request_payload_error(e):
            logger.warning(
                '[%s] Request-scoped rejection on model=%s — NOT switching '
                'models or attempting pool rescue: %s', tid, model, err_str)
            request_error_envelope = format_llm_error_for_user(
                e, model=model, context='request-rejected',
                source='llm-stream')
            if tool_call_happened:
                task['error'] = request_error_envelope
                return {
                    'assistant_msg': {'role': 'assistant', 'content': ''},
                    'finish_reason': 'error', 'usage': None,
                    'model': model, 'preset': preset,
                    'thinking_enabled': thinking_enabled,
                    '_loop_action': 'break',
                    '_loop_exit_reason':
                        f'request_rejected_round_{round_num}',
                }
            try:
                e._user_message = request_error_envelope  # type: ignore[attr-defined]
            except Exception as attribute_error:
                logger.debug('[%s] Could not attach _user_message: %s',
                             tid, attribute_error)
            raise

        # If already on the fallback model, or no fallback configured —
        # try the pool-wide last resort before giving up (owner directive
        # 2026-08-03: an error surfaces ONLY when every key is unavailable).
        # An explicit per-request opt-out (disableModelFallback) skips it.
        if not _FALLBACK_MODEL or model == _FALLBACK_MODEL:
            if not _fb_disabled_by_request:
                _rescue = _attempt_pool_rescue(
                    task, body, round_num, max_tokens, tool_list,
                    accumulated_usage, api_rounds,
                    failed_models={model}, original_model=original_model,
                    cause_exc=e, preset=preset,
                    thinking_enabled=thinking_enabled,
                    on_tool_call_ready=on_tool_call_ready)
                if _rescue is not None:
                    return _rescue
            if tool_call_happened:
                _user_err = format_llm_error_for_user(
                    e, model=model,
                    context=(_no_fb_context if not _FALLBACK_MODEL else 'on-fallback-model'),
                    source='llm-stream')
                task['error'] = _user_err
                logger.warning('[%s] 🛑 Fallback model error with prior tool calls — giving up: %s',
                               tid, err_str, exc_info=True)
                # ``content`` must be a string — the typed envelope is
                # carried separately on task['error'] / done.error.  The
                # assistant bubble shows the empty string; the frontend
                # renders the error envelope as a typed error block.
                return {
                    'assistant_msg': {'role': 'assistant', 'content': ''},
                    'finish_reason': 'error', 'usage': None,
                    'model': model, 'preset': preset, 'thinking_enabled': thinking_enabled,
                    '_loop_action': 'break',
                    '_loop_exit_reason': f'opus_error_with_tool_calls_round_{round_num}',
                }
            # No fallback / already on fallback and no prior tool calls —
            # stash the typed envelope on the exception so the top-level
            # FATAL handler in orchestrator can surface actionable text
            # without losing the exception type (subclasses may have
            # non-trivial __init__ signatures).
            try:
                e._user_message = format_llm_error_for_user(  # type: ignore[attr-defined]
                    e, model=model,
                    context=(_no_fb_context if not _FALLBACK_MODEL else 'on-fallback-model'),
                    source='llm-stream')
            except Exception as _attr_err:
                logger.debug('[%s] Could not attach _user_message: %s', tid, _attr_err)
            raise

        # ── Fallback: switch to configured fallback model ──
        # Build a short, typed reason string from the original exception so
        # the UI can show *why* the fallback fired (kind + detail) instead
        # of an opaque "primary failed" message.
        from lib.error_envelope import from_exception as _from_exc
        _fb_envelope = _from_exc(
            e, model=original_model,
            context='fallback-trigger', source='llm-stream')
        _fb_kind = _fb_envelope.get('kind', 'generic')
        _fb_detail = (_fb_envelope.get('detail') or err_str).strip()
        _fb_reason = f'{_fb_kind}: {_fb_detail}' if _fb_detail else _fb_kind

        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The done event already carries
        # fallbackModel / fallbackFrom / fallbackReason for the persistent badge.
        append_event(task, build_phase(
            Phase.RETRYING,
            detail=(f'⚠️ 模型 {original_model} 请求失败（{_fb_kind}）：'
                    f'{_fb_detail[:120]} — 已自动回退到 {_FALLBACK_MODEL} 继续生成…'),
        ))
        # EARLY notification, at the DECISION MOMENT — before the fallback
        #   stream starts. A fallback generation can run for minutes; the
        #   transient phase line is cleared the moment fallback content
        #   starts streaming, so without a STRUCTURED event + early task
        #   stamps the user has no indication the model changed for the
        #   whole generation, and a cold reload mid-fallback cannot repaint
        #   the banner (build_fresh_state_snapshot reads these fields).
        #   Cleared again below if the fallback itself fails.
        append_event(task, build_event(
            EventType.MODEL_FALLBACK,
            fallbackModel=_FALLBACK_MODEL,
            fallbackFrom=original_model,
            fallbackKind=_fb_kind,
            fallbackReason=_fb_reason[:300],
        ))
        task['_fallback_model'] = _FALLBACK_MODEL
        task['_fallback_from'] = original_model
        task['_fallback_reason'] = _fb_reason[:300]
        task['_fallback_kind'] = _fb_kind
        # A model fallback is a significant state change — record it in the
        # audit trail so the optimizer/operator can see WHICH model failed,
        # how often, and why (the analyzer already mines 'model_fallback').
        # The fallback itself is self-recovering, so the log line is WARNING
        # WITHOUT a traceback (the originating error was already logged with
        # exc_info just above); a traceback here would imply an unhandled bug.
        audit_log('model_fallback', old=original_model, new=_FALLBACK_MODEL,
                  reason=_fb_reason[:200], kind=_fb_kind, tid=tid,
                  conv=task.get('convId', ''))
        logger.warning('[%s] Model fallback: %s → %s (reason: %s)',
                       tid, original_model, _FALLBACK_MODEL,
                       _fb_reason[:200])

        _vision_fallback_from = (
            original_model
            if (model_supports_vision(original_model)
                and not model_supports_vision(_FALLBACK_MODEL))
            else ''
        )
        fallback_body = build_body(
            _FALLBACK_MODEL, messages,
            max_tokens=max_tokens,
            temperature=1.0,
            thinking_enabled=True,
            preset='opus',
            thinking_depth='medium',
            tools=tool_list,
            response_format=body.get('response_format'),
            stream=True,
            vision_fallback_from=_vision_fallback_from,
        )
        # Preserve the session-stable TTL latch key on the fallback body
        #   too (see reactive-compact rebuild above). The fallback model is a
        #   different cache namespace anyway, but a stable TTL decision keeps
        #   the fallback model's OWN prefix reusable across its rounds.
        fallback_body['_task_id'] = task.get('id', '')

        try:
            stream_result = ensure_provider_stream_result(
                stream_llm_response(
                    task, fallback_body, tag=f'R{round_num+1}-FALLBACK',
                    max_429_attempts=_fallback_max_429_attempts(task)))
            assistant_msg, finish_reason, usage = stream_result
            last_finish_reason = finish_reason

            if usage is not None and _flag_empty_stop_for_retry(
                    assistant_msg, finish_reason, task, round_num, usage):
                logger.warning('[%s] ⚠️ Round-0 EMPTY STOP (fallback model=%s) — '
                               'flagging for empty_stop retry (NOT content_filter). '
                               'Will surface as abnormal_stop if it persists.',
                               tid, _FALLBACK_MODEL)

            if finish_reason in ('length', 'max_tokens'):
                _fb_trace = (usage or {}).get('trace_id', 'N/A')
                _fb_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
                logger.warning('[%s] ⚠️ TRUNCATED at round %d (fallback model=%s): '
                               'finish_reason=%s M-TraceId=%s elapsed=%.1fs — '
                               'output token limit reached',
                               tid, round_num, _FALLBACK_MODEL, finish_reason,
                               _fb_trace, _fb_elapsed / 1000)

            # (fallback fields were stamped at the DECISION moment, before
            # the stream started — see above; nothing to re-stamp here.)
            if usage:
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({
                    'round': round_num + 1,
                    'model': _FALLBACK_MODEL,
                    'usage': project_usage_for_round_record(usage),
                    'tag': f'R{round_num+1}-FALLBACK',
                })
                _emit_round_usage(task, round_num + 1, _FALLBACK_MODEL, usage,
                                   tag=f'R{round_num+1}-FALLBACK')
                # Honest accounting (same as primary path)
                for _bill in (usage.get('_extra_billing_rounds') or []):
                    _bu = _bill.get('usage') or {}
                    for k, v in _bu.items():
                        if isinstance(v, (int, float)):
                            accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                    api_rounds.append({
                        'round': round_num + 1,
                        'model': _bill.get('model') or _FALLBACK_MODEL,
                        'usage': project_usage_for_round_record(_bu),
                        'tag': _bill.get('tag') or f'R{round_num+1}-FALLBACK-DISCARDED',
                        'responseAuthoring': False,
                    })
                    _emit_round_usage(task, round_num + 1,
                                      _bill.get('model') or _FALLBACK_MODEL,
                                      _bu,
                                      tag=_bill.get('tag') or
                                      f'R{round_num+1}-FALLBACK-DISCARDED',
                                      response_authoring=False)

            _log_stream_attempt_outcome(
                task,
                tid=tid,
                round_num=round_num + 1,
                model=_FALLBACK_MODEL,
                stream_result=stream_result,
                label='FALLBACK',
                fallback_from=original_model,
            )

            return {
                'assistant_msg': assistant_msg,
                'finish_reason': last_finish_reason,
                'usage': usage,
                'stream_result': stream_result,
                'model': _FALLBACK_MODEL,
                'preset': 'medium',
                'thinking_enabled': True,
                '_loop_action': None,
                '_loop_exit_reason': None,
            }

        except Exception as e2:
            if isinstance(e2, AbortedError):
                raise
            logger.error('[%s] Opus fallback also failed: %s', tid, e2,
                         exc_info=True)
            # A fallback request has the same local recovery rights as the
            # primary. In particular, cgroup pressure must not trigger a
            # pool-wide replay of the same payload. If compaction reveals a
            # different upstream error, continue with ordinary rescue policy.
            while isinstance(e2, (PromptTooLongError, MemoryPressureError)):
                _prior_fallback_error = e2
                _recovered, e2, _retry_body = _attempt_request_payload_recovery(
                    task, fallback_body, _FALLBACK_MODEL, round_num,
                    tool_list, messages, 'medium', True,
                    accumulated_usage, api_rounds, on_tool_call_ready, e2,
                    max_429_attempts=_fallback_max_429_attempts(task),
                    vision_fallback_from=_vision_fallback_from)
                if _retry_body is not None:
                    fallback_body = _retry_body
                if _recovered is not None:
                    return _recovered
                if isinstance(e2, AbortedError):
                    raise e2
                if e2 is _prior_fallback_error:
                    break
            if isinstance(e2, MemoryPressureError):
                _clear_failed_fallback_stamp(task)
                return _finish_local_memory_pressure(
                    task, e2, _FALLBACK_MODEL, 'medium', True, round_num)

            # Pool-wide last resort (owner directive 2026-08-03) — the
            #   configured fallback dying must not kill the turn while the
            #   pool still has healthy (key, model) slots. EXCEPTION (owner
            #   directive 2026-08-18, strict mode): a GATEWAY/outage class
            #   failure is waitable, not proof any model is dead — the
            #   rescue's arbitrary-pool-model pick is the silent switch the
            #   user forbade. Fall through to the honest error envelope.
            if not _fb_disabled_by_request and not _is_gateway_error(e2):
                _rescue = _attempt_pool_rescue(
                    task, fallback_body, round_num, max_tokens, tool_list,
                    accumulated_usage, api_rounds,
                    failed_models={original_model, _FALLBACK_MODEL},
                    original_model=original_model,
                    cause_exc=e2, preset='medium',
                    thinking_enabled=True,
                    on_tool_call_ready=on_tool_call_ready)
                if _rescue is not None:
                    return _rescue
            # The configured fallback produced NOTHING — clear the
            # decision-time stamp. done/persist read these fields to claim a
            # fallback happened; claiming one that failed is a lie. (A
            # successful pool-rescue above re-stamps with its own values.)
            _clear_failed_fallback_stamp(task)
            logger.warning(
                '[%s] 🛑 Both %s and fallback %s failed — settling normal '
                'DONE(error) (prior_tool_calls=%s)',
                tid, original_model, _FALLBACK_MODEL, tool_call_happened)
            return _settle_failed_fallback(
                task,
                e2,
                original_model=original_model,
                fallback_model=_FALLBACK_MODEL,
                preset='medium',
                thinking_enabled=True,
                round_num=round_num,
            )
