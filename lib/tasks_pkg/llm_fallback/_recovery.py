"""Same-model recovery for provider context overflow and rejected state.

Entries: recover_oversized_request, recover_rejected_encrypted_state. Compaction and mandatory request admission
share the normal context owners; _state owns the per-task recovery budget."""

from lib import llm
from lib.agent_core.events import Phase, build_phase
from lib.llm import AbortedError
from lib.log import get_logger
from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
from lib.tasks_pkg.manager import _events as task_events

from . import _errors, _state, _transport, _usage

logger = get_logger(__name__)


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
            task.get('provider_id') or '',
            model,
            reported,
            preset_limit=prior_limit,
            stated_max=stated_max,
        )
        if learned_info:
            task_events.append_event(
                task,
                build_phase(
                    Phase.RETRYING,
                    detail=(
                        f'⚙️ Auto-detected smaller context window for {model}: '
                        f'{learned_info["new_limit"]:,} tokens '
                        f'(was {learned_info["old_limit"]:,})'
                    ),
                ),
            )
    except Exception as learn_error:
        logger.debug('[%s] context_limits shrink-learn failed: %s', tid, learn_error)


def _prepare_reactive_retry_body(
    task,
    body,
    model,
    messages,
    tool_list,
    preset,
    thinking_enabled,
    cause,
    tid,
    *,
    vision_fallback_from='',
):
    """Compact derived context and rebuild one same-model retry body.

    Compaction/body rebuilding are recovery mechanics. If either mechanic has
    its own defect, keep the original typed failure so it can be surfaced or
    handled by normal policy instead of replacing it with a new FATAL.
    """
    try:
        from lib.tasks_pkg.compaction.api import reactive_compact

        compacted = reactive_compact(messages, task=task, error_text=str(cause))
        if task.get('aborted'):
            return None
        if compacted:
            from lib.tasks_pkg.compaction._pipeline import (
                recompose_context_after_compaction,
            )

            # Provider-overflow recovery may run L2 and remove managed policy
            # tails. Use the same restoration owner as normal admission.
            recompose_context_after_compaction(messages, task)

        if task.get('aborted'):
            return None
    except Exception as prep_error:
        logger.error(
            '[%s] Reactive compaction could not prepare a smaller projection; '
            'preserving original %s: %s',
            tid,
            type(cause).__name__,
            prep_error,
            exc_info=True,
        )
        return None

    # Required acquisition is outside best-effort compaction recovery. Let its
    # failure terminate this dispatch; returning None would allow a fallback
    # model to rebuild the now-compacted messages without current requirements.
    from lib.tasks_pkg.context_composer.turn_requirements import (
        refresh_turn_requirements,
    )

    refresh_turn_requirements(messages, task=task, model=model)
    try:
        from lib.tasks_pkg.context_composer.checklist import restore_checklist_context

        # Reactive recovery bypasses ordinary round preparation and can drop
        # the checklist even during LLM-free head truncation. Restore before
        # body accounting and selected-route token admission.
        restore_checklist_context(messages, task=task, model=model)
        retry_body = _rebuild_same_request(
            task, body, model, messages, tool_list, preset, thinking_enabled,
            vision_fallback_from=vision_fallback_from)
    except Exception as prep_error:
        logger.error(
            '[%s] Reactive request recovery could not prepare a smaller body; '
            'preserving original %s: %s',
            tid,
            type(cause).__name__,
            prep_error,
            exc_info=True,
        )
        return None

    from lib.tasks_pkg.compaction.api import settle_compaction_request_size
    settle_compaction_request_size(messages, task, tool_schema=tool_list)
    return retry_body


def _rebuild_same_request(task, body, model, messages, tool_list, preset,
                          thinking_enabled, *, vision_fallback_from=''):
    """Rebuild ``body`` from changed ``messages`` for the same destination."""
    from lib.tasks_pkg.model_images import task_image_reuse
    same_model = body.get('model') == model
    profile = (
        body.get('_route_inference_profile', body.get('_inference_profile'))
        if same_model
        else None
    )
    retry_body = llm.build_body(
        model,
        messages,
        reasoning_intent=body.get('_reasoning_intent'),
        max_tokens=task.get('config', {}).get('maxTokens', 128000),
        inference_profile=profile,
        # Keep an already-compiled same-model direct call compatible;
        # pending task intent and changed destinations still require binding.
        defer_reasoning=bool(body.get('_reasoning_pending')) or not same_model,
        temperature=body.get('temperature', 1.0),
        thinking_enabled=thinking_enabled,
        preset=preset,
        # Recovery rebuilds the same request, not a new effort choice.
        # Claude's canonical effort is already resolved; other families
        # retain the raw user depth rather than re-mapping a wire rung.
        thinking_depth=(
            body.get('effort') or (task.get('config') or {}).get('thinkingDepth')
        ),
        tools=tool_list,
        response_format=body.get('response_format'),
        stream=True,
        vision_fallback_from=vision_fallback_from,
        image_reuse=task_image_reuse(task, model=model),
        image_user_id=task.get('_userId'),
    )
    from ._request_metadata import preserve_rebuilt_request_context
    return preserve_rebuilt_request_context(body, retry_body, task=task)


def _record_reactive_usage(
    task, usage, model, round_num, accumulated_usage, api_rounds
):
    """Invalidate the pre-compaction sample before adopting retry usage."""
    if not usage:
        return
    from lib.tasks_pkg.compaction._calibration import discard_dispatch_sample

    discard_dispatch_sample(task)
    _usage.record_attempt_usage(
        task,
        usage,
        model,
        round_num + 1,
        accumulated_usage,
        api_rounds,
        tag=f'R{round_num + 1}-REACTIVE',
    )


def _attempt_request_payload_recovery(
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
    cause,
    *,
    max_429_attempts=None,
    vision_fallback_from='',
):
    """Shrink a locally-derived request and retry the same model once.

    Returns ``(success_result, next_error, retry_body)``. ``next_error`` is
    the reactive retry's actual exception, so downstream policy never
    misclassifies it as the original size failure.
    """
    tid = task['id'][:8]
    task_id = task.get('id', '')
    attempts = _state._reactive_compact_attempts.get(task_id, 0)

    _learn_context_limit_from_overflow(task, model, cause, tid)

    if attempts >= _state._REACTIVE_COMPACT_MAX_RETRIES:
        logger.error(
            '[%s] Reactive request recovery exhausted (%d/%d) for %s',
            tid,
            attempts,
            _state._REACTIVE_COMPACT_MAX_RETRIES,
            type(cause).__name__,
        )
        return None, cause, None

    _state._reactive_compact_attempts[task_id] = attempts + 1
    logger.warning(
        '[%s] ⚡ REACTIVE COMPACT at round %d (attempt %d/%d): provider '
        'context limit for model=%s — reducing the derived payload and '
        'retrying the same model',
        tid,
        round_num,
        attempts + 1,
        _state._REACTIVE_COMPACT_MAX_RETRIES,
        model,
    )

    retry_body = _prepare_reactive_retry_body(
        task,
        body,
        model,
        messages,
        tool_list,
        preset,
        thinking_enabled,
        cause,
        tid,
        vision_fallback_from=vision_fallback_from,
    )
    if retry_body is None:
        return None, cause, None
    from ._request_metadata import attach_rebuilt_request_metadata
    attach_rebuilt_request_metadata(
        task, body, retry_body, messages, tool_list, round_num=round_num, tid=tid)

    phase = build_phase(
        Phase.RETRYING,
        detail=(
            f'⚡ 上下文超长，已自动压缩 '
            f'(reactive compact {attempts + 1}/'
            f'{_state._REACTIVE_COMPACT_MAX_RETRIES})…'
        ),
        detailKey='stream.phase.reactiveCompact',
        detailArgs={
            'attempt': attempts + 1,
            'max': _state._REACTIVE_COMPACT_MAX_RETRIES,
        },
    )
    # This event belongs to authoritative task state; preserve its existing
    # fail-closed behavior rather than hiding a durable-write failure.
    task_events.append_event(task, phase)

    try:
        stream_result = _transport.stream_attempt(
            task,
            retry_body,
            tag=f'R{round_num + 1}-REACTIVE',
            on_tool_call_ready=on_tool_call_ready,
            max_429_attempts=max_429_attempts,
        )
        assistant_msg, finish_reason, usage = stream_result
    except Exception as retry_error:
        _errors.raise_if_scope_denied(
            task,
            retry_error,
            model=model,
            round_num=round_num,
            accumulated_usage=accumulated_usage,
            api_rounds=api_rounds,
        )
        if isinstance(retry_error, (EventDeliveryError, EventDeliveryRevoked)):
            raise
        if isinstance(retry_error, AbortedError):
            return None, retry_error, retry_body
        logger.error(
            '[%s] Reactive compact retry also failed: %s',
            tid,
            retry_error,
            exc_info=True,
        )
        return None, retry_error, retry_body

    _record_reactive_usage(task, usage, model, round_num, accumulated_usage, api_rounds)
    return (
        {
            'assistant_msg': assistant_msg,
            'finish_reason': finish_reason,
            'usage': usage,
            'stream_result': stream_result,
            'model': model,
            'preset': preset,
            'thinking_enabled': thinking_enabled,
            '_loop_action': None,
            '_loop_exit_reason': None,
        },
        None,
        retry_body,
    )


def recover_oversized_request(
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
    error,
    *,
    max_429_attempts=None,
    vision_fallback_from='',
):
    """Retry oversized input within the existing shared recovery budget."""
    from lib.llm import PromptTooLongError

    while isinstance(error, PromptTooLongError):
        prior_error = error
        recovered, error, retry_body = _attempt_request_payload_recovery(
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
            error,
            max_429_attempts=max_429_attempts,
            vision_fallback_from=vision_fallback_from,
        )
        if retry_body is not None:
            body = retry_body
        if recovered is not None:
            return recovered, None, body
        if isinstance(error, AbortedError):
            raise error
        if error is prior_error:
            break
    return None, error, body


def recover_rejected_encrypted_state(
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
    error,
):
    """Retry once without the encrypted reasoning the provider refused.

    Replayed reasoning decrypts only on the upstream account that minted it,
    and a gateway may serve one Deployment from several accounts, so routing
    alone cannot guarantee it. The rejection proves the request was never run.
    The live ``messages`` lose that reasoning in place, so later rounds do not
    resend it; compaction state stays and a second rejection surfaces. Returns
    ``(success_result, next_error, body)`` like ``recover_oversized_request``.
    """
    from lib.llm import InvalidEncryptedContentError
    from lib.llm.responses_replay import retire_rejected_reasoning_state

    if not isinstance(error, InvalidEncryptedContentError):
        return None, error, body
    tid = task['id'][:8]
    retired = retire_rejected_reasoning_state(messages)
    if not retired:
        return None, error, body
    logger.warning(
        '[%s] Responses encrypted state rejected at round %d (model=%s) — '
        'retired %d encrypted reasoning item(s), retrying the same model once',
        tid, round_num, model, retired)
    try:
        retry_body = _rebuild_same_request(
            task, body, model, messages, tool_list, preset, thinking_enabled)
    except Exception as prep_error:
        logger.error(
            '[%s] Encrypted-state recovery could not rebuild the request; '
            'preserving original %s: %s',
            tid, type(error).__name__, prep_error, exc_info=True)
        return None, error, body
    from ._request_metadata import attach_rebuilt_request_metadata
    attach_rebuilt_request_metadata(
        task, body, retry_body, messages, tool_list, round_num=round_num, tid=tid)
    task_events.append_event(task, build_phase(
        Phase.RETRYING,
        detail='🔁 上游无法解密已保存的推理状态，已移除后重试…',
    ))
    try:
        stream_result = _transport.stream_attempt(
            task, retry_body, tag=f'R{round_num + 1}-STATE',
            on_tool_call_ready=on_tool_call_ready)
        assistant_msg, finish_reason, usage = stream_result
    except Exception as retry_error:
        _errors.raise_if_scope_denied(
            task, retry_error, model=model, round_num=round_num,
            accumulated_usage=accumulated_usage, api_rounds=api_rounds)
        if isinstance(retry_error, (EventDeliveryError, EventDeliveryRevoked, AbortedError)):
            raise
        logger.error('[%s] Encrypted-state retry also failed: %s',
                     tid, retry_error, exc_info=True)
        return None, retry_error, retry_body
    _usage.record_attempt_usage(
        task, usage, model, round_num + 1, accumulated_usage, api_rounds,
        tag=f'R{round_num + 1}-STATE')
    return (
        {
            'assistant_msg': assistant_msg,
            'finish_reason': finish_reason,
            'usage': usage,
            'stream_result': stream_result,
            'model': model,
            'preset': preset,
            'thinking_enabled': thinking_enabled,
            '_loop_action': None,
            '_loop_exit_reason': None,
        },
        None,
        retry_body,
    )
