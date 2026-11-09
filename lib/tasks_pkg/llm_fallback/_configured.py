"""Configured fallback attempt and terminal failure policy.

Entry: call_configured_fallback. Uses request admission, shared transport,
usage, recovery and settlement owners; stamps the model decision before I/O."""

from lib import log
from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.llm import AbortedError
from lib.llm.conversion import ProtocolConversionError
from lib.llm.llm_errors import _ERR_BODY_LIMIT, ModelRefusalError
from lib.log import get_logger, task_log_tag
from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
from lib.tasks_pkg.manager import _events as task_events

from . import _errors, _pool, _recovery, _refusal, _requests, _retry, _transport, _usage

logger = get_logger(__name__)


def call_configured_fallback(
    task,
    body,
    original_model,
    round_num,
    max_tokens,
    tool_call_happened,
    tool_list,
    messages,
    preset,
    thinking_enabled,
    accumulated_usage,
    api_rounds,
    *,
    error,
    fallback_model,
    fallback_disabled,
    on_tool_call_ready=None,
):
    """Serve the admitted configured fallback or settle its actual failure."""
    from lib.tasks_pkg.message_input_receipts import MessageInputScopeError

    try:
        tid = task_log_tag(task['id'])
        e = error
        err_str = str(e)[:_ERR_BODY_LIMIT]
        # ── Fallback: switch to configured fallback model ──
        # Build a short, typed reason string from the original exception so
        # the UI can show *why* the fallback fired (kind + detail) instead
        # of an opaque "primary failed" message.
        from lib.error_envelope import from_exception as _from_exc

        _fb_envelope = _from_exc(
            e, model=original_model, context='fallback-trigger', source='llm-stream'
        )
        _fb_kind = _fb_envelope.get('kind', 'generic')
        _fb_detail = (_fb_envelope.get('detail') or err_str).strip()
        _fb_reason = f'{_fb_kind}: {_fb_detail}' if _fb_detail else _fb_kind

        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The done event already carries
        # fallbackModel / fallbackFrom / fallbackReason for the persistent badge.
        if isinstance(e, ModelRefusalError):
            task_events.append_event(
                task,
                build_phase(
                    Phase.RETRYING,
                    detail=(
                        f'⚠️ 模型 {original_model} 拒绝回答 — '
                        f'已自动改用 {fallback_model} 重新生成…'
                    ),
                    detailKey='stream.phase.refusalFallback',
                    detailArgs={'model': original_model, 'fallback': fallback_model},
                ),
            )
        else:
            task_events.append_event(
                task,
                build_phase(
                    Phase.RETRYING,
                    detail=(
                        f'⚠️ 模型 {original_model} 请求失败（{_fb_kind}）：'
                        f'{_fb_detail[:120]} — 已自动回退到 {fallback_model} 继续生成…'
                    ),
                ),
            )
        # Publish the decision before streaming so live and reloaded clients can
        # explain a long-running fallback. Clear the stamp if no reply is produced.
        task_events.append_event(
            task,
            build_event(
                EventType.MODEL_FALLBACK,
                fallbackModel=fallback_model,
                fallbackFrom=original_model,
                fallbackKind=_fb_kind,
                fallbackReason=_fb_reason[:300],
            ),
        )
        task['_fallback_model'] = fallback_model
        task['_fallback_from'] = original_model
        task['_fallback_reason'] = _fb_reason[:300]
        task['_fallback_kind'] = _fb_kind
        # A model fallback is a significant state change — record it in the
        # audit trail so the optimizer/operator can see WHICH model failed,
        # how often, and why (the analyzer already mines 'model_fallback').
        # The fallback itself is self-recovering, so the log line is WARNING
        # WITHOUT a traceback (the originating error was already logged with
        # exc_info just above); a traceback here would imply an unhandled bug.
        log.audit_log(
            'model_fallback',
            old=original_model,
            new=fallback_model,
            reason=_fb_reason[:200],
            kind=_fb_kind,
            tid=tid,
            conv=task.get('convId', ''),
        )
        logger.warning(
            '[%s] Model fallback: %s → %s (reason: %s)',
            tid,
            original_model,
            fallback_model,
            _fb_reason[:200],
        )

        fallback_body, _vision_fallback_from = _requests.prepare_fallback_body(
            task,
            body,
            original_model,
            fallback_model,
            max_tokens,
            tool_list,
            messages,
            round_num=round_num,
        )

        try:
            stream_result = _transport.stream_attempt(
                task,
                fallback_body,
                tag=f'R{round_num + 1}-FALLBACK',
                max_429_attempts=_retry._fallback_max_429_attempts(task),
            )
            assistant_msg, finish_reason, usage = stream_result

            if _refusal.is_model_refusal(stream_result):
                _refusal._discard_refused_attempt(
                    task,
                    round_num=round_num,
                    model=fallback_model,
                    usage=usage,
                    tag=f'R{round_num + 1}-FALLBACK-REFUSED',
                    on_tool_call_ready=None,
                    accumulated_usage=accumulated_usage,
                    api_rounds=api_rounds,
                )
                _errors._clear_failed_fallback_stamp(task)
                _primary_note = (
                    f'{original_model} declined first.'
                    if isinstance(e, ModelRefusalError)
                    else f'It was the fallback after {original_model} failed ({_fb_kind}).'
                )
                return _refusal._settle_model_refusal(
                    task,
                    _refusal.model_refusal_error(fallback_model, usage),
                    messages=messages,
                    fallback_note=_primary_note,
                    preset='medium',
                    thinking_enabled=True,
                    round_num=round_num,
                )

            if usage is not None and _retry._flag_empty_stop_for_retry(
                assistant_msg, finish_reason, task, round_num, usage
            ):
                logger.warning(
                    '[%s] ⚠️ Round-0 EMPTY STOP (fallback model=%s) — '
                    'flagging for empty_stop retry (NOT content_filter). '
                    'Will surface as abnormal_stop if it persists.',
                    tid,
                    fallback_model,
                )

            if finish_reason in ('length', 'max_tokens'):
                _fb_trace = (usage or {}).get('trace_id', 'N/A')
                _fb_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
                logger.warning(
                    '[%s] ⚠️ TRUNCATED at round %d (fallback model=%s): '
                    'finish_reason=%s M-TraceId=%s elapsed=%.1fs — '
                    'output token limit reached',
                    tid,
                    round_num,
                    fallback_model,
                    finish_reason,
                    _fb_trace,
                    _fb_elapsed / 1000,
                )

            # (fallback fields were stamped at the DECISION moment, before
            # the stream started — see above; nothing to re-stamp here.)
            _usage.record_attempt_usage(
                task,
                usage,
                fallback_model,
                round_num + 1,
                accumulated_usage,
                api_rounds,
                tag=f'R{round_num + 1}-FALLBACK',
            )

            _transport._log_stream_attempt_outcome(
                task,
                tid=tid,
                round_num=round_num + 1,
                model=fallback_model,
                stream_result=stream_result,
                label='FALLBACK',
                fallback_from=original_model,
            )

            # A refusal fallback stays on the fallback model for the rest of
            # this turn: the loop keeps rs.model on the fallback, so the next
            # round dispatches there directly instead of paying another
            # refused attempt and a fresh primary cache write.
            return {
                'assistant_msg': assistant_msg,
                'finish_reason': finish_reason,
                'usage': usage,
                'stream_result': stream_result,
                'model': fallback_model,
                'preset': 'medium',
                'thinking_enabled': True,
                '_loop_action': None,
                '_loop_exit_reason': None,
            }

        except Exception as e2:
            _errors.raise_if_scope_denied(
                task,
                e2,
                model=fallback_model,
                round_num=round_num,
                accumulated_usage=accumulated_usage,
                api_rounds=api_rounds,
            )
            if isinstance(e2, (EventDeliveryError, EventDeliveryRevoked)):
                _errors._clear_failed_fallback_stamp(task)
                raise
            if isinstance(e2, AbortedError):
                raise
            logger.error(
                '[%s] Configured fallback %s also failed: %s',
                tid,
                fallback_model,
                e2,
                exc_info=True,
            )
            # A fallback request has the same local recovery rights as the
            # primary. If compaction reveals a different upstream error,
            # continue with ordinary rescue policy.
            recovered, e2, fallback_body = _recovery.recover_oversized_request(
                task,
                fallback_body,
                fallback_model,
                round_num,
                tool_list,
                messages,
                'medium',
                True,
                accumulated_usage,
                api_rounds,
                on_tool_call_ready,
                e2,
                max_429_attempts=_retry._fallback_max_429_attempts(task),
                vision_fallback_from=_vision_fallback_from,
            )
            if recovered is not None:
                return recovered

            if (
                isinstance(e2, ProtocolConversionError)
                and e2.code == 'mandatory_continuity_unrepresentable'
                and not e2.provider_reached
            ):
                _errors._clear_failed_fallback_stamp(task)
                logger.warning(
                    '[%s] Fallback %s cannot represent native continuity; '
                    'preserving the primary failure from %s',
                    tid,
                    fallback_model,
                    original_model,
                )
                return _errors._settle_failed_fallback(
                    task,
                    e,
                    original_model=original_model,
                    fallback_model=fallback_model,
                    preset=preset,
                    thinking_enabled=thinking_enabled,
                    round_num=round_num,
                    blocked_error=e2,
                )

            # Availability failures may use the remaining pool. Gateway outages and
            # deterministic local/request defects cannot authorize model rotation.
            from lib.llm.llm_errors import (
                SubscriptionCredentialUnavailableError as _SCUE,
            )
            from lib.llm.llm_errors import (
                SubscriptionLoginRequiredError as _SLRE,
            )

            _rescue_blocked = _errors._is_local_request_preparation_error(
                e2
            ) and not isinstance(getattr(e2, '__cause__', None), (_SCUE, _SLRE))
            if (
                not fallback_disabled
                and not _errors._is_gateway_error(e2)
                and not _rescue_blocked
                and not _errors._is_request_payload_error(e2)
            ):
                _rescue = _pool._attempt_pool_rescue(
                    task,
                    fallback_body,
                    round_num,
                    max_tokens,
                    tool_list,
                    accumulated_usage,
                    api_rounds,
                    failed_models={original_model, fallback_model},
                    original_model=original_model,
                    cause_exc=e2,
                    preset='medium',
                    thinking_enabled=True,
                    on_tool_call_ready=on_tool_call_ready,
                )
                if _rescue is not None:
                    return _rescue
            # The configured fallback produced NOTHING — clear the
            # decision-time stamp. done/persist read these fields to claim a
            # fallback happened; claiming one that failed is a lie. (A
            # successful pool-rescue above re-stamps with its own values.)
            _errors._clear_failed_fallback_stamp(task)
            logger.warning(
                '[%s] 🛑 Both %s and fallback %s failed — settling normal '
                'DONE(error) (prior_tool_calls=%s)',
                tid,
                original_model,
                fallback_model,
                tool_call_happened,
            )
            return _errors._settle_failed_fallback(
                task,
                e2,
                original_model=original_model,
                fallback_model=fallback_model,
                preset='medium',
                thinking_enabled=True,
                round_num=round_num,
            )
    except MessageInputScopeError:
        _errors._clear_failed_fallback_stamp(task)
        raise
