"""Failure classification and terminal fallback settlement.

Entries: typed non-rotatable predicates, failed-stamp cleanup and
_settle_failed_fallback. Error-envelope and turn contracts own public semantics."""

from lib.agent_core.events import Phase, build_phase
from lib.llm.conversion import ProtocolConversionError
from lib.llm.llm_error_format import format_llm_error_for_user
from lib.llm.llm_errors import _ERR_BODY_LIMIT, ModelRefusalError
from lib.log import get_logger
from lib.tasks_pkg.manager import _events as task_events
from lib.turn_verdict import GATEWAY_CONTENT_FILTER_CAUSE

from . import _refusal, _usage

logger = get_logger(__name__)


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
        logger.debug('lib.llm import failed in request-error check: %s', import_error)
        return False
    return isinstance(exc, (BadRequestError, RequestScopedError))


def _is_local_request_preparation_error(exc: BaseException) -> bool:
    """True when dispatch proves no provider request was attempted."""
    try:
        from lib.llm import LocalRequestPreparationError
    except Exception as import_error:
        logger.debug('lib.llm import failed in local-prepare check: %s', import_error)
        return False
    return isinstance(exc, LocalRequestPreparationError)


def _clear_failed_fallback_stamp(task):
    """Remove decision-time fallback metadata when no fallback reply exists."""
    for key in (
        '_fallback_model',
        '_fallback_from',
        '_fallback_reason',
        '_fallback_kind',
    ):
        task.pop(key, None)


def _settle_failed_fallback(
    task,
    error,
    *,
    original_model,
    fallback_model,
    preset,
    thinking_enabled,
    round_num,
    blocked_error: ProtocolConversionError | None = None,
):
    """Return a normal loop break carrying a typed terminal fallback error.

    A configured fallback is already an inner recovery attempt. Once it (and
    any bounded pool rescue) fails, raising into a separate fatal path makes
    client settlement depend on exception plumbing and permits whole-turn
    retries to reset the same wait budget. Normal loop finalization guarantees
    one ``done`` event with ``done.error``; ``autoRetryExhausted`` keeps the
    bounded failure bounded while preserving manual Retry.
    A continuity-blocked fallback reports the primary error and policy.
    """
    envelope = format_llm_error_for_user(
        error,
        model=original_model if blocked_error is not None else fallback_model,
        context=(
            f'fallback-unrepresentable ({original_model}→{fallback_model})'
            if blocked_error is not None
            else f'both-failed ({original_model}→{fallback_model})'
        ),
        source='llm-fallback',
    )
    envelope.update(
        {
            'autoRetryExhausted': True,
            'fallbackFailed': True,
            'fallbackFrom': original_model,
            'fallbackModel': fallback_model,
        }
    )
    if blocked_error is not None:
        # The destination rejected native continuity before provider ingress.
        # Keep the primary availability failure actionable without pretending
        # the fallback generated a reply or erasing provider-bound history.
        envelope.update(
            {
                'fallbackDetail': str(blocked_error),
                'fallbackFailureStage': blocked_error.stage,
                'fallbackProviderReached': False,
            }
        )
    for field in (
        'attempts',
        'limit',
        'credential_delivery_anomaly_attempts',
        'credential_delivery_anomaly_limit',
    ):
        value = getattr(error, field, None)
        if value is not None:
            envelope[field] = value
    task['error'] = envelope
    return {
        'assistant_msg': {'role': 'assistant', 'content': ''},
        'finish_reason': 'error',
        'usage': None,
        'model': original_model if blocked_error is not None else fallback_model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': 'break',
        '_loop_exit_reason': (
            f'fallback_unrepresentable_round_{round_num}'
            if blocked_error is not None
            else f'both_models_failed_round_{round_num}'
        ),
    }


def settle_content_rejection(task, e, *, model, round_num, preset, thinking_enabled):
    """Settle invalid images or HTTP 450 without authorizing a model switch."""
    from lib.llm import ContentFilterError

    tid = task['id'][:8]
    # InvalidImageError — image content rejected (too large, corrupt, etc.)
    # Fallback to another model won't help (same image = same rejection).
    from lib.llm import InvalidImageError

    if isinstance(e, InvalidImageError):
        err_str = str(e)[:300]
        logger.warning(
            '[%s] 🖼️ INVALID_IMAGE at round %d model=%s: %s',
            tid,
            round_num,
            model,
            err_str,
        )
        from lib.error_envelope import make_envelope as _make_env

        if 'many-image' in err_str.lower():
            _hint_cn = '过多大图。同时发送 5 张以上图片时，每张需小于 2000×2000像素。请压缩或删除部分图片。'
            _hint_en = (
                'Too many large images. When sending 5+ images, each must be '
                'under 2000×2000 pixels. Please resize or remove some images.'
            )
            _hint_key = 'err.k.invalid_image.hintMany'
        else:
            _hint_cn = (
                '会话中某张图片超过了 API 大小限制。请使用更小的图片或删除过大的图片。'
            )
            _hint_en = (
                'One or more images in this conversation exceed the API size '
                'limit. Please use a smaller image or remove the oversized image.'
            )
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
    # A ModelRefusalError is the model's own refusal and falls through.
    if isinstance(e, ContentFilterError) and not isinstance(e, ModelRefusalError):
        err_str = str(e)[:_ERR_BODY_LIMIT]
        logger.warning(
            '[%s] 🚫 CONTENT_FILTER (HTTP 450) at round %d model=%s: %s',
            tid,
            round_num,
            model,
            err_str,
            exc_info=True,
        )
        _refusal.stamp_content_filter_settlement(
            task,
            cause=GATEWAY_CONTENT_FILTER_CAUSE,
            envelope=_refusal.gateway_block_envelope(e, model=model),
        )
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

    return None


def settle_non_rotatable_primary_error(
    task, e, *, model, round_num, tool_call_happened, preset, thinking_enabled
):
    """Settle or raise gateway/payload defects; return None for availability.

    Subscription credential outages are model-scoped availability. Local
    request defects and gateway outages cannot authorize model rotation.
    """
    tid = task['id'][:8]
    err_str = str(e)[:_ERR_BODY_LIMIT]
    # A gateway outage cannot authorize model rotation. Keep the pinned
    # model and surface the dispatcher's actual wait/abort outcome.
    if _is_gateway_error(e):
        _gw_status = int(getattr(e, 'status_code', 0) or 0)
        _gw_label = model
        logger.warning(
            '[%s] 🛑 Gateway outage (HTTP %s) outlasted the dispatch wait '
            'budget on model=%s — NOT switching models (strict mode); '
            'surfacing upstream_error envelope instead',
            tid,
            _gw_status or '?',
            model,
        )
        task_events.append_event(
            task,
            build_phase(
                Phase.RETRYING,
                detail=(
                    f'⛔ 后端网关故障持续未恢复——模型保持 {_gw_label}，'
                    f'未自动切换。可稍后重试，或中断后手动切换模型。'
                ),
                detailKey='stream.phase.gatewayOutageFinal',
                detailArgs={'model': _gw_label, 'status': _gw_status},
            ),
        )
        _gateway_error_envelope = format_llm_error_for_user(
            e, model=model, context='gateway-outage', source='llm-stream'
        )
        if tool_call_happened:
            task['error'] = _gateway_error_envelope
            return {
                'assistant_msg': {'role': 'assistant', 'content': ''},
                'finish_reason': 'error',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'break',
                '_loop_exit_reason': f'gateway_outage_round_{round_num}',
            }
        try:
            e._user_message = _gateway_error_envelope  # type: ignore[attr-defined]
        except Exception as _attr_err:
            logger.debug('[%s] Could not attach _user_message: %s', tid, _attr_err)
        raise e

    # Local payload defects are non-rotatable. A typed subscription-source
    # outage, after same-credential recovery, is model-scoped availability.
    if _is_local_request_preparation_error(e):
        from lib.llm.llm_errors import (
            SubscriptionCredentialUnavailableError,
            SubscriptionLoginRequiredError,
        )

        _cred_cause = e.__cause__
        _credential_unavailable = isinstance(
            _cred_cause,
            (SubscriptionCredentialUnavailableError, SubscriptionLoginRequiredError),
        )
        if not _credential_unavailable:
            logger.error(
                '[%s] Local request preparation failed on model=%s at %s — '
                'NOT switching models or attempting pool rescue',
                tid,
                model,
                getattr(e, 'stage', 'request_prepare'),
            )
            try:
                e._user_message = format_llm_error_for_user(  # type: ignore[attr-defined]
                    e, model=model, context='request-prepare', source='llm-stream'
                )
            except Exception as attribute_error:
                logger.debug(
                    '[%s] Could not attach _user_message: %s', tid, attribute_error
                )
            raise e
        logger.warning(
            '[%s] Subscription credential unavailable on model=%s '
            '(reason=%s) — rotating like a primary-model failure',
            tid,
            model,
            getattr(_cred_cause, 'reason', type(_cred_cause).__name__),
        )

    # Deterministic request rejection — never switch models or attempt a
    # pool-wide rescue. A 400/404/422 says the payload/route is invalid;
    # it is not evidence that the selected model is dead. In particular,
    # a locally malformed tool schema must remain visible as our defect
    # instead of silently degrading a Kimi vision request onto text-only
    # GLM.
    if _is_request_payload_error(e):
        logger.warning(
            '[%s] Request-scoped rejection on model=%s — NOT switching '
            'models or attempting pool rescue: %s',
            tid,
            model,
            err_str,
        )
        request_error_envelope = format_llm_error_for_user(
            e, model=model, context='request-rejected', source='llm-stream'
        )
        if tool_call_happened:
            task['error'] = request_error_envelope
            return {
                'assistant_msg': {'role': 'assistant', 'content': ''},
                'finish_reason': 'error',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'break',
                '_loop_exit_reason': f'request_rejected_round_{round_num}',
            }
        try:
            e._user_message = request_error_envelope  # type: ignore[attr-defined]
        except Exception as attribute_error:
            logger.debug(
                '[%s] Could not attach _user_message: %s', tid, attribute_error
            )
        raise e

    return None


def raise_if_scope_denied(
    task, error, *, model, round_num, accumulated_usage, api_rounds
):
    """Record known paid work, then propagate lost input authority unchanged."""
    from lib.tasks_pkg.message_input_receipts import MessageInputScopeError

    if not isinstance(error, MessageInputScopeError):
        return
    _usage.record_scope_denied_usage(
        getattr(error, 'usage', None), model, round_num, accumulated_usage, api_rounds
    )
    error._user_message = format_llm_error_for_user(
        error, model=model, context='request-rejected', source='llm-stream'
    )
    raise error
