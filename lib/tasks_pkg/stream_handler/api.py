"""Stable typed post-stream decision service.

Callers depend on this module for the round classifier. Retry budgets and
diagnostic helpers remain owned by their focused implementation modules.  The
implementation's mutable mapping is accepted only here and converted to one
validated :class:`RecoveryDecision` before it enters the orchestrator.
"""

from lib.llm.stream_result import (
    ProviderStreamResult,
    ProviderStreamState,
    ensure_provider_stream_result,
)
from lib.log import get_logger
from lib.tasks_pkg.stream_handler._analyse import (
    analyse_stream_result as _analyse_stream_result,
)
from lib.tasks_pkg.stream_handler._decision import (
    RecoveryAction,
    RecoveryDecision,
)


logger = get_logger(__name__)


_LEGACY_STREAM_SEMANTIC_KEYS = (
    '_stream_state',
    '_stream_anomaly',
    '_missing_done',
    '_missing_finish_reason',
    '_malformed_stream',
    '_malformed_frames',
    '_semantic_progress_timeout',
    '_semantic_idle_timeout_ms',
    '_no_actionable_timeout',
    '_no_actionable_timeout_s',
    '_empty_stop',
    '_tool_calls_void',
)


def _compatibility_usage_from_typed_stream(
    stream_result: ProviderStreamResult,
) -> dict:
    """Derive legacy analyser flags from the typed semantic authority.

    The large policy analyser is intentionally migrated behind this boundary.
    Until its implementation consumes ``ProviderStreamResult`` directly, its
    old diagnostic flags are outputs of the closed state—not a second source
    of truth that can disagree with it.
    """
    source = stream_result.usage if isinstance(stream_result.usage, dict) else {}
    values = dict(source)
    evidence = stream_result.evidence
    for key in _LEGACY_STREAM_SEMANTIC_KEYS:
        values.pop(key, None)

    state = stream_result.state
    values['_stream_state'] = state.value
    if state is ProviderStreamState.CLIENT_ABORTED:
        return values

    # ``[DONE]`` and finish-frame diagnostics remain useful to legacy readers,
    # but they are derived from the typed evidence. In particular, a provider
    # finish without ``[DONE]`` is still verified completion and therefore does
    # not acquire ``_stream_anomaly`` below.
    if not stream_result.saw_done:
        values['_missing_done'] = True
    if not stream_result.saw_finish_reason:
        values['_missing_finish_reason'] = True
    if state is not ProviderStreamState.PROVIDER_FINISHED:
        values['_stream_anomaly'] = True
    if state is ProviderStreamState.MALFORMED_STREAM:
        values['_malformed_stream'] = True
        values['_malformed_frames'] = max(
            1, stream_result.malformed_frame_count)
    elif state in {
            ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT,
            ProviderStreamState.NO_ACTIONABLE_OUTPUT}:
        values['_no_actionable_timeout'] = True
        if state is ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT:
            values['_semantic_progress_timeout'] = True
        values['_semantic_idle_timeout_ms'] = (
            evidence.semantic_idle_timeout_ms)
        values['_no_actionable_timeout_s'] = round(
            evidence.semantic_idle_timeout_ms / 1000, 3)
    elif state is ProviderStreamState.EMPTY_RESPONSE:
        values['_empty_stop'] = True
    elif state is ProviderStreamState.TOOL_PAYLOAD_MISSING:
        values['_tool_calls_void'] = (
            evidence.tool_payload_missing_cause or 'gateway_no_payload')
    return values


def _retry_count(task, fallback_count) -> int:
    value = (
        task.get('_premature_retry_count_phase')
        if '_premature_retry_count_phase' in task
        else fallback_count
    )
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _closed_state_terminal_decision(
    stream_result: ProviderStreamResult,
    task,
    *,
    tid,
    model,
    round_num,
    retry_count,
) -> RecoveryDecision | None:
    """Stop before tool dispatch for non-executable closed stream states."""
    if stream_result.state is ProviderStreamState.CLIENT_ABORTED:
        return RecoveryDecision(
            action=RecoveryAction.BREAK,
            loop_exit_reason=f'client_aborted_stream_round_{round_num}',
            abort_detected_phase=f'post_stream_round_{round_num}',
            premature_retry_count=retry_count,
            last_finish_reason='aborted',
            stream_state=stream_result.state,
        )
    if stream_result.state is not ProviderStreamState.UNKNOWN:
        return None

    # UNKNOWN has no proof that the visible tool-call subset is complete. It
    # must fail before the shared runner can execute any of those calls.
    if not task.get('error'):
        from lib.error_envelope import make_envelope
        task['error'] = make_envelope(
            'internal',
            detail=(
                'Provider stream ended without a recognized semantic state; '
                'tool execution and successful settlement were suppressed.'
            ),
            model=model,
            context=f'round-{round_num}',
            source='llm-stream',
            raw='provider_stream_state=unknown',
        )
    logger.error(
        '[%s] Provider stream state is UNKNOWN at round %d; breaking before '
        'tool dispatch (model=%s)', tid, round_num, model)
    return RecoveryDecision(
        action=RecoveryAction.BREAK,
        loop_exit_reason=f'unknown_stream_state_round_{round_num}',
        abort_detected_phase=None,
        premature_retry_count=retry_count,
        last_finish_reason='error',
        stream_state=stream_result.state,
    )


def analyse_stream_result(
    assistant_msg,
    last_finish_reason,
    task,
    tid,
    model,
    round_num,
    _premature_retry_count,
    messages,
    usage=None,
    stream_result=None,
) -> RecoveryDecision:
    """Return a closed recovery decision for one provider stream result.

    ``stream_result`` is authoritative on the root execution path.  The
    historical arguments remain for external callers and are adapted once at
    this boundary.
    """
    typed_stream = (
        ensure_provider_stream_result(stream_result)
        if stream_result is not None
        else ProviderStreamResult.from_legacy(
            assistant_msg, last_finish_reason, usage)
    )
    closed_terminal = _closed_state_terminal_decision(
        typed_stream,
        task,
        tid=tid,
        model=model,
        round_num=round_num,
        retry_count=_retry_count(task, _premature_retry_count),
    )
    if closed_terminal is not None:
        return closed_terminal

    effective_usage = _compatibility_usage_from_typed_stream(typed_stream)
    raw_decision = _analyse_stream_result(
        typed_stream.message,
        typed_stream.finish_reason,
        task,
        tid,
        model,
        round_num,
        _premature_retry_count,
        messages,
        usage=effective_usage,
    )
    return RecoveryDecision.from_mapping(
        raw_decision,
        stream_state=typed_stream.state,
    )

__all__ = ['RecoveryAction', 'RecoveryDecision', 'analyse_stream_result']
