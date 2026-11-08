"""Round dispatch policy. Entry: _llm_call_with_fallback.

Coordinates request admission, stream adoption, reactive recovery and model
fallback through concrete owners; callers retain task, messages and ledger."""

from lib.llm import AbortedError
from lib.llm.llm_error_format import format_llm_error_for_user
from lib.llm.llm_errors import _ERR_BODY_LIMIT, ModelRefusalError
from lib.log import get_logger
from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked

from . import (
    _configured,
    _errors,
    _pool,
    _recovery,
    _refusal,
    _retry,
    _transport,
    _usage,
)

logger = get_logger(__name__)


def _call_primary(
    task,
    body,
    model,
    round_num,
    max_tokens,
    preset,
    thinking_enabled,
    accumulated_usage,
    api_rounds,
    on_tool_call_ready,
):
    """Adopt a provider response or bill and retire a refused attempt."""
    tid = task['id'][:8]
    stream_result = _transport.stream_attempt(
        task, body, tag=f'R{round_num + 1}', on_tool_call_ready=on_tool_call_ready
    )
    assistant_msg, finish_reason, usage = stream_result

    if _refusal.is_model_refusal(stream_result):
        _refusal._discard_refused_attempt(
            task,
            round_num=round_num,
            model=model,
            usage=usage,
            tag=f'R{round_num + 1}-REFUSED',
            on_tool_call_ready=on_tool_call_ready,
            accumulated_usage=accumulated_usage,
            api_rounds=api_rounds,
        )
        # Typed like any primary failure so the configured-fallback path
        # below owns the retry; same-model resends usually refuse again.
        raise _refusal.model_refusal_error(model, usage)

    # An empty stop enters the ordinary retry bucket; only a verified
    # provider refusal or HTTP 450 can settle as a content filter.
    if usage is not None and _retry._flag_empty_stop_for_retry(
        assistant_msg, finish_reason, task, round_num, usage
    ):
        logger.warning(
            '[%s] ⚠️ Round-0 EMPTY STOP (model=%s) — flagging for '
            'empty_stop retry (NOT content_filter; a real policy '
            'block would be HTTP 450). Will retry then surface as '
            'abnormal_stop if it persists.',
            tid,
            model,
        )

    # Log output-token truncation so operators can tune max_tokens
    if finish_reason in ('length', 'max_tokens'):
        _trunc_content_len = len(assistant_msg.get('content', ''))
        _trunc_tool_calls = len(assistant_msg.get('tool_calls', []))
        _u_trace = (usage or {}).get('trace_id', 'N/A')
        _u_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
        logger.warning(
            '[%s] ⚠️ TRUNCATED at round %d: finish_reason=%s '
            'content=%dchars tool_calls=%d model=%s max_tokens=%s '
            'M-TraceId=%s elapsed=%.1fs — '
            'output token limit reached',
            tid,
            round_num,
            finish_reason,
            _trunc_content_len,
            _trunc_tool_calls,
            model,
            max_tokens,
            _u_trace,
            _u_elapsed / 1000,
        )

    _usage.record_attempt_usage(
        task,
        usage,
        model,
        round_num + 1,
        accumulated_usage,
        api_rounds,
        tag=f'R{round_num + 1}',
    )
    if usage and usage.get('_extra_billing_rounds'):
        logger.warning(
            '[%s] conv=%s billed %d discarded FloorRetry '
            'attempt(s) into api_rounds/accumulated_usage — '
            'cost popover now matches gateway bill',
            tid,
            task.get('convId', ''),
            len(usage['_extra_billing_rounds']),
        )

    _transport._log_stream_attempt_outcome(
        task,
        tid=tid,
        round_num=round_num + 1,
        model=model,
        stream_result=stream_result,
    )

    return {
        'assistant_msg': assistant_msg,
        'finish_reason': finish_reason,
        'usage': usage,
        'stream_result': stream_result,
        'model': model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': None,
        '_loop_exit_reason': None,
    }


def _llm_call_with_fallback(
    task,
    body,
    model,
    round_num,
    max_tokens,
    tool_call_happened,
    tool_list,
    messages,
    preset,
    thinking_enabled,
    accumulated_usage,
    api_rounds,
    on_tool_call_ready=None,
):
    """Dispatch one round, recover oversized input, and apply fallback policy.

    The supplied task, messages and billing ledger remain caller-owned. Local
    admission/durability failures and cancellation never become model failures.
    Results retain the loop's model/preset and typed settlement contract.
    """
    tid = task['id'][:8]
    fallback_model = _retry._get_fallback_model(task)
    # An explicit opt-out also forbids pool rescue and has its own error context.
    fallback_disabled = False
    try:
        fallback_disabled = bool((task.get('config') or {}).get('disableModelFallback'))
    except Exception as _e:
        logger.debug('[%s] disableModelFallback flag read failed: %s', tid, _e)
    no_fallback_context = 'fallback-disabled' if fallback_disabled else 'no-fallback'

    # ── Primary model call ──
    try:
        return _call_primary(
            task,
            body,
            model,
            round_num,
            max_tokens,
            preset,
            thinking_enabled,
            accumulated_usage,
            api_rounds,
            on_tool_call_ready,
        )

    except Exception as e:
        if isinstance(e, (EventDeliveryError, EventDeliveryRevoked)):
            # Retain the original local durability cause; a route cannot repair it.
            raise
        _errors.raise_if_scope_denied(
            task,
            e,
            model=model,
            round_num=round_num,
            accumulated_usage=accumulated_usage,
            api_rounds=api_rounds,
        )
        # AbortedError must escape — never fallback/retry on user abort

        if isinstance(e, AbortedError):
            logger.debug(
                '[%s] ✋ AbortedError at round %d — stopping immediately',
                tid,
                round_num,
            )
            raise

        # Derived-payload failures are recoverable locally. Repeatedly compact
        # within the small shared retry budget, preserving the latest typed
        # error from each retry.
        recovered, e, body = _recovery.recover_oversized_request(
            task,
            body,
            model,
            round_num,
            tool_list,
            messages,
            preset,
            thinking_enabled,
            accumulated_usage,
            api_rounds,
            on_tool_call_ready,
            e,
        )
        if recovered is not None:
            return recovered
        recovered, e, body = _recovery.recover_rejected_encrypted_state(
            task,
            body,
            model,
            round_num,
            tool_list,
            messages,
            preset,
            thinking_enabled,
            accumulated_usage,
            api_rounds,
            on_tool_call_ready,
            e,
        )
        if recovered is not None:
            return recovered

        settled = _errors.settle_content_rejection(
            task,
            e,
            model=model,
            round_num=round_num,
            preset=preset,
            thinking_enabled=thinking_enabled,
        )
        if settled is not None:
            return settled

        original_model = model
        err_str = str(e)[:_ERR_BODY_LIMIT]
        if isinstance(e, ModelRefusalError):
            logger.warning(
                '[%s] conv=%s 🚫 MODEL REFUSAL at round %d (model=%s category=%s): %s',
                tid,
                task.get('convId', ''),
                round_num + 1,
                model,
                e.category or 'none',
                err_str,
            )
        else:
            logger.error(
                '[%s] conv=%s LLM call failed at round %d (model=%s): %s '
                '(check M-TraceId in preceding debug logs for gateway coordination)',
                tid,
                task.get('convId', ''),
                round_num + 1,
                model,
                err_str,
                exc_info=True,
            )

        settled = _errors.settle_non_rotatable_primary_error(
            task,
            e,
            model=model,
            round_num=round_num,
            tool_call_happened=tool_call_happened,
            preset=preset,
            thinking_enabled=thinking_enabled,
        )
        if settled is not None:
            return settled

        # A refusal is a content decision, not unavailability: without a
        # distinct configured fallback it settles typed, never pool-rescued
        # onto an arbitrary model.
        if isinstance(e, ModelRefusalError) and (
            not fallback_model or model == fallback_model
        ):
            if fallback_disabled:
                _refusal_note = 'Model fallback is disabled for this request.'
            elif not fallback_model:
                _refusal_note = 'No fallback model is configured.'
            else:
                _refusal_note = f'{model} is already the configured fallback model.'
            return _refusal._settle_model_refusal(
                task,
                e,
                messages=messages,
                fallback_note=_refusal_note,
                preset=preset,
                thinking_enabled=thinking_enabled,
                round_num=round_num,
            )

        # If already on the fallback model, or no fallback configured —
        # try the pool-wide last resort before giving up (owner directive
        # 2026-08-03: an error surfaces ONLY when every key is unavailable).
        # An explicit per-request opt-out (disableModelFallback) skips it.
        if not fallback_model or model == fallback_model:
            if not fallback_disabled:
                _rescue = _pool._attempt_pool_rescue(
                    task,
                    body,
                    round_num,
                    max_tokens,
                    tool_list,
                    accumulated_usage,
                    api_rounds,
                    failed_models={model},
                    original_model=original_model,
                    cause_exc=e,
                    preset=preset,
                    thinking_enabled=thinking_enabled,
                    on_tool_call_ready=on_tool_call_ready,
                )
                if _rescue is not None:
                    return _rescue
            if tool_call_happened:
                _user_err = format_llm_error_for_user(
                    e,
                    model=model,
                    context=(
                        no_fallback_context
                        if not fallback_model
                        else 'on-fallback-model'
                    ),
                    source='llm-stream',
                )
                task['error'] = _user_err
                logger.warning(
                    '[%s] 🛑 Fallback model error with prior tool calls — giving up: %s',
                    tid,
                    err_str,
                    exc_info=True,
                )
                # ``content`` must be a string — the typed envelope is
                # carried separately on task['error'] / done.error.  The
                # assistant bubble shows the empty string; the frontend
                # renders the error envelope as a typed error block.
                return {
                    'assistant_msg': {'role': 'assistant', 'content': ''},
                    'finish_reason': 'error',
                    'usage': None,
                    'model': model,
                    'preset': preset,
                    'thinking_enabled': thinking_enabled,
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
                    e,
                    model=model,
                    context=(
                        no_fallback_context
                        if not fallback_model
                        else 'on-fallback-model'
                    ),
                    source='llm-stream',
                )
            except Exception as _attr_err:
                logger.debug('[%s] Could not attach _user_message: %s', tid, _attr_err)
            raise e

        return _configured.call_configured_fallback(
            task,
            body,
            model,
            round_num,
            max_tokens,
            tool_call_happened,
            tool_list,
            messages,
            preset,
            thinking_enabled,
            accumulated_usage,
            api_rounds,
            error=e,
            fallback_model=fallback_model,
            fallback_disabled=fallback_disabled,
            on_tool_call_ready=on_tool_call_ready,
        )
