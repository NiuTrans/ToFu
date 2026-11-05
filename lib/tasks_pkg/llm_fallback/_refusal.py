"""Content-filter outcomes: refusal detection and typed settlement envelopes.

A provider can finish an HTTP 200 stream with ``finish_reason:
content_filter`` — Claude's ``stop_reason: "refusal"`` (its ``stop_details``
ride ``usage['_model_refusal']``) or an OpenAI-compatible filter. Anthropic
documents that a refused response's partial output must be discarded and
that resending it to the same model usually refuses again, so ``_call``
retries the round once on the configured fallback model. The HTTP 450 gateway
block filters the content for every model and never switches models.

Both outcomes settle as ``truncated`` with a dedicated cause. The typed
envelope travels in ``task[CONTENT_FILTER_SETTLEMENT_KEY]`` rather than
``task['error']``: an error would turn the verdict into ``failed`` and the
OpenAI-compatible ``200 finish_reason=content_filter`` into a server error.

Entry points: ``is_model_refusal``, ``model_refusal_error``,
``model_refusal_envelope``, ``gateway_block_envelope``,
``stamp_content_filter_settlement``, ``_discard_refused_attempt`` and
``_settle_model_refusal``. Billing uses _usage; rollback uses round_retry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lib.error_envelope import make_envelope
from lib.llm.llm_errors import ModelRefusalError
from lib.log import get_logger
from lib.tasks_pkg.message_semantics import is_human_message
from lib.turn_verdict import (
    CONTENT_FILTER_SETTLEMENT_KEY,
    GATEWAY_CONTENT_FILTER_CAUSE,
    MODEL_REFUSAL_CAUSE,
)

from . import _usage

_REFUSAL_HINT = (
    '解决办法 / How to fix:\n'
    '• 同一模型通常会再次拒绝同一请求。请切换到其他模型后重试，或删除触发拒绝的内容'
    '（它可能来自工具输出或附件，而不只是你的消息）。\n\n'
    '• Sending the same request to the same model usually gets refused again. '
    'Switch to another model and retry, or remove the content that triggered '
    'the refusal (it can come from tool output or attachments, not only your '
    'message).'
)
_AUTOMATIC_REFUSAL_HINT = (
    '解决办法 / How to fix:\n'
    '• 这是系统自动发起的后续轮次（例如 swarm 汇总），被拒绝的不是你输入的内容。'
    '请切换到其他模型，然后让它继续。\n\n'
    '• This was an automatic follow-up turn (for example a swarm summary); '
    'nothing you typed was refused. Switch to another model and ask it to '
    'continue.'
)
_GATEWAY_HINT = (
    '解决办法 / How to fix:\n'
    '• 网关的内容策略会对所有模型拦截这段内容，切换模型无效。请删除或改写被拦截的内容'
    '（它可能来自工具输出、抓取的网页或附件）后重试。\n\n'
    '• The gateway content policy blocks this content for every model, so '
    'switching models will not help. Remove or rephrase the flagged content '
    '(it can come from tool output, fetched pages or attachments), then retry.'
)


def is_model_refusal(stream_result: Any) -> bool:
    """True for a verified provider finish whose terminal reason is a filter."""
    return (
        bool(getattr(stream_result, 'is_verified_complete', False))
        and getattr(stream_result, 'finish_reason', '') == 'content_filter'
    )


def model_refusal_error(model: str, usage: Any) -> ModelRefusalError:
    """Type a refused attempt, keeping Claude's bounded ``stop_details``."""
    details = usage.get('_model_refusal') if isinstance(usage, Mapping) else None
    return ModelRefusalError(model, details if isinstance(details, Mapping) else None)


def turn_input_is_automatic(messages: Sequence[Any] | None) -> bool:
    """Whether the latest turn input came from outside the human input box.

    Context carriers and inbox injections ride the user role without being
    the turn's input, so the newest remaining user message decides.
    """
    for message in reversed(list(messages or ())):
        if not isinstance(message, Mapping) or message.get('role') != 'user':
            continue
        if any(
            message.get(key)
            for key in ('_isMeta', '_contextComposer', '_isInboxInject')
        ):
            continue
        return not is_human_message(message)
    return False


def model_refusal_envelope(
    error: ModelRefusalError, *, automatic: bool, fallback_note: str = ''
) -> dict[str, Any]:
    """Typed ``content_filter`` envelope for a model's own refusal."""
    reason = f'category: {error.category}' if error.category else 'no category reported'
    parts = [f'{error.model or "The model"} declined the request ({reason}).']
    if fallback_note:
        parts.append(fallback_note)
    if error.explanation:
        parts.append(error.explanation)
    return make_envelope(
        'content_filter',
        detail=' '.join(parts),
        model=error.model,
        context='model-refusal',
        source='llm-stream',
        raw=str(error),
        title_key='err.k.content_filter.titleRefusal',
        hint=_AUTOMATIC_REFUSAL_HINT if automatic else _REFUSAL_HINT,
        hint_key=(
            'err.k.content_filter.hintRefusalAutomatic'
            if automatic
            else 'err.k.content_filter.hintRefusal'
        ),
    )


def gateway_block_envelope(error: BaseException, *, model: str) -> dict[str, Any]:
    """Typed ``content_filter`` envelope for an HTTP 450 gateway block."""
    return make_envelope(
        'content_filter',
        detail=str(error),
        model=model,
        context='gateway-content-filter',
        source='llm-stream',
        raw=str(error),
        hint=_GATEWAY_HINT,
        hint_key='err.k.content_filter.hintGateway',
    )


def stamp_content_filter_settlement(
    task: dict[str, Any], *, cause: str, envelope: Mapping[str, Any]
) -> None:
    """Record the typed outcome that ``lib.turn_lifecycle`` settles."""
    if cause not in (MODEL_REFUSAL_CAUSE, GATEWAY_CONTENT_FILTER_CAUSE):
        raise ValueError(f'unknown content-filter cause: {cause!r}')
    task[CONTENT_FILTER_SETTLEMENT_KEY] = {'cause': cause, 'error': dict(envelope)}


logger = get_logger(__name__)


def _discard_refused_attempt(
    task,
    *,
    round_num,
    model,
    usage,
    tag,
    on_tool_call_ready,
    accumulated_usage,
    api_rounds,
):
    """Bill a refused attempt, then retire everything it streamed.

    Anthropic bills mid-stream refusals and some categories, so the usage is
    a non-authoring round. Its partial text, thinking and early tool work are
    not an adopted response; Anthropic requires discarding them.
    """
    _usage.record_attempt_usage(
        task,
        usage,
        model,
        round_num + 1,
        accumulated_usage,
        api_rounds,
        tag=tag,
        response_authoring=False,
    )
    retire_tool_attempt = getattr(
        getattr(on_tool_call_ready, '__self__', None),
        'on_provider_attempt_restart',
        None,
    )
    if callable(retire_tool_attempt):
        retire_tool_attempt(reason='model refusal')
    # The round-base reset also records the discarded text as FloorRetry
    # residue, which the shrink-convergent checkpoint guard requires.
    from lib.tasks_pkg.round_retry import reset_round_to_base

    reset_round_to_base(task, round_num)


def _settle_model_refusal(
    task, error, *, messages, fallback_note, preset, thinking_enabled, round_num
):
    """Break the loop on a refusal no fallback can answer, with typed evidence."""
    envelope = model_refusal_envelope(
        error, automatic=turn_input_is_automatic(messages), fallback_note=fallback_note
    )
    stamp_content_filter_settlement(task, cause=MODEL_REFUSAL_CAUSE, envelope=envelope)
    logger.warning(
        '[%s] conv=%s 🚫 MODEL REFUSAL settled at round %d: model=%s category=%s — %s',
        task['id'][:8],
        task.get('convId', ''),
        round_num + 1,
        error.model,
        error.category or 'none',
        fallback_note,
    )
    return {
        'assistant_msg': {'role': 'assistant', 'content': ''},
        'finish_reason': 'content_filter',
        'usage': None,
        'model': error.model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': 'break',
        '_loop_exit_reason': f'model_refusal_round_{round_num}',
    }
