"""Last-resort pool dispatch after the caller permits model rotation.

Entry: _attempt_pool_rescue. Uses common transport, billing and decision
receipts; does not decide whether the primary failure authorizes rescue."""

from lib import log
from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.llm import AbortedError
from lib.log import get_logger, task_log_tag
from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
from lib.tasks_pkg.manager import _events as task_events

from . import _errors, _retry, _transport, _usage

logger = get_logger(__name__)


def _attempt_pool_rescue(
    task,
    body,
    round_num,
    max_tokens,
    tool_list,
    accumulated_usage,
    api_rounds,
    *,
    failed_models,
    original_model,
    cause_exc,
    preset,
    thinking_enabled,
    on_tool_call_ready=None,
):
    """Try a capable pool slot outside the failed-model set.

    The caller owns rotation policy. A failed capability probe cannot suppress
    dispatch; the existing dispatch retry budget owns the attempt. Successful
    rescue preserves original-primary attribution and records the serving model.
    """
    tid = task_log_tag(task['id'])
    failed_models = {m for m in (failed_models or set()) if m}
    rescue_prefer_model = _retry._get_pool_rescue_model(failed_models)

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
        logger.warning(
            '[%s] pool-rescue skipped — no capable slot beyond failed models %s',
            tid,
            sorted(failed_models),
        )
        return None

    # Classify the cause so the badge names WHY the rescue fired (the same
    # typed kind the normal fallback badge uses — 'permission', …).
    try:
        from lib.error_envelope import from_exception as _from_exc

        _rk_env = _from_exc(
            cause_exc, model=original_model, context='pool-rescue', source='llm-stream'
        )
        _rk_kind = _rk_env.get('kind', 'generic')
        _rk_detail = (_rk_env.get('detail') or str(cause_exc)).strip()
    except Exception as _ce:
        logger.debug('[%s] pool-rescue cause classify failed: %s', tid, _ce)
        _rk_kind, _rk_detail = 'generic', str(cause_exc)[:200]

    task_events.append_event(
        task,
        build_phase(
            Phase.RETRYING,
            detail=(
                f'⚠️ {original_model} 及其回退模型均不可用（{_rk_kind}）— '
                f'正在尝试池中其它可用模型…'
            ),
        ),
    )

    _tools_this = tool_list
    _rescue_body = dict(body)
    if _tools_this is not None:
        _rescue_body['tools'] = _tools_this
    # Same session-stable TTL latch rule as every rebuild path.
    _rescue_body['_task_id'] = task.get('id', '')

    try:
        stream_result = _transport.stream_attempt(
            task,
            _rescue_body,
            tag=f'R{round_num + 1}-RESCUE',
            on_tool_call_ready=on_tool_call_ready,
            pool_wide=True,
            exclude_models=failed_models,
            pool_prefer_model=rescue_prefer_model or None,
            max_429_attempts=_retry._fallback_max_429_attempts(task),
        )
        assistant_msg, finish_reason, usage = stream_result
    except Exception as e3:
        _errors.raise_if_scope_denied(
            task,
            e3,
            model=_rescue_body.get('model') or original_model,
            round_num=round_num,
            accumulated_usage=accumulated_usage,
            api_rounds=api_rounds,
        )
        if isinstance(e3, (EventDeliveryError, EventDeliveryRevoked)):
            raise
        if isinstance(e3, AbortedError):
            raise
        logger.warning('[%s] pool-rescue dispatch also failed: %s', tid, e3)
        return None

    _rescue_dispatch = (usage or {}).get('_dispatch') or {}
    _rescue_model = (
        _rescue_dispatch.get('logical_model')
        or _rescue_dispatch.get('model')
        or _rescue_body.get('model')
        or original_model
    )
    _rescue_from_model = _rescue_body.get('model') or original_model

    if usage is not None and _retry._flag_empty_stop_for_retry(
        assistant_msg, finish_reason, task, round_num, usage
    ):
        logger.warning(
            '[%s] ⚠️ Round-0 EMPTY STOP (pool-rescue model=%s) — '
            'flagging for empty_stop retry',
            tid,
            _rescue_model,
        )

    # Badge: keep the ORIGINAL primary as _fallback_from when an earlier hop
    # already recorded it (the rescue is the tail of the same chain).
    if not task.get('_fallback_from'):
        task['_fallback_from'] = original_model
    task['_fallback_model'] = _rescue_model
    task['_fallback_reason'] = (
        f'{_rk_kind}: {_rk_detail}' if _rk_detail else _rk_kind
    )[:300]
    task['_fallback_kind'] = _rk_kind
    if _rescue_model != _rescue_from_model:
        task_events.append_event(
            task,
            build_event(
                EventType.MODEL_FALLBACK,
                fallbackModel=_rescue_model,
                fallbackFrom=_rescue_from_model,
                fallbackKind='pool_rescue',
                fallbackReason=task['_fallback_reason'],
            ),
        )

    _usage.record_attempt_usage(
        task,
        usage,
        _rescue_model,
        round_num + 1,
        accumulated_usage,
        api_rounds,
        tag=f'R{round_num + 1}-RESCUE',
    )

    log.audit_log(
        'model_fallback',
        old=original_model,
        new=_rescue_model,
        reason=f'pool-rescue: {_rk_kind}: {_rk_detail[:160]}',
        kind=_rk_kind,
        tid=tid,
        conv=task.get('convId', ''),
    )
    _transport._log_stream_attempt_outcome(
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
