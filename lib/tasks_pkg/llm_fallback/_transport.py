"""Provider attempts with input receipts and credential recovery.

Entry: stream_attempt. The stream manager owns I/O; message-input receipts own
physical ingress evidence. Outcome logging never promotes an incomplete stream."""

from lib.llm.stream_result import ensure_provider_stream_result
from lib.log import get_logger
from lib.tasks_pkg.manager._stream import stream_llm_response

from . import _credential_recovery

logger = get_logger(__name__)


def _stream_with_message_receipts(task, body, **kwargs):
    from lib.tasks_pkg.context_dependencies import observe_wire
    from lib.tasks_pkg.message_input_receipts import stream_with_receipts

    observe_wire(task, None)  # Only the physical attempt observer can replace this.
    return stream_with_receipts(stream_llm_response, task, body, **kwargs)


def stream_attempt(task, body, **kwargs):
    """Stream with credential recovery, physical-input receipts and typing."""
    return ensure_provider_stream_result(
        _credential_recovery.stream_with_credential_recovery(
            _stream_with_message_receipts, task, body, **kwargs
        )
    )


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
    context = f' {" ".join(context_parts)}' if context_parts else ''

    if stream_result.is_verified_complete:
        logger.info(
            '[%s] conv=%s ✓ %s round %d OK: stream_state=%s '
            'finish_reason=%s model=%s content=%dchars tool_calls=%d '
            'M-TraceId=%s elapsed=%.1fs%s',
            tid,
            task.get('convId', ''),
            label,
            round_num,
            stream_result.state.value,
            stream_result.finish_reason,
            model,
            content_len,
            tool_calls,
            trace_id,
            elapsed_s,
            context,
        )
        return

    logger.warning(
        '[%s] conv=%s ⚠ %s round %d UNUSABLE: stream_state=%s '
        'compatibility_finish_reason=%s model=%s content=%dchars tool_calls=%d '
        'M-TraceId=%s elapsed=%.1fs; recovery analysis pending%s',
        tid,
        task.get('convId', ''),
        label,
        round_num,
        stream_result.state.value,
        stream_result.finish_reason,
        model,
        content_len,
        tool_calls,
        trace_id,
        elapsed_s,
        context,
    )
