"""Per-round LLM call with automatic fallback + deferred-inbox flush.

Extracted 2026-07-31 ( slice 26) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs after the messages-snapshot emission + body build, inside the
stream loop. Steps:

1. **LLM call with fallback** (``_llm_call_with_fallback``): streams
   the LLM response, automatically falling back (e.g. to Opus) on
   dispatch failure. The streaming tool accumulator's
   ``on_tool_call_ready`` callback pre-executes read-only tools while
   the model is still generating.
2. **State writeback**: assistant_msg / finish_reason / usage /
   model / preset / thinking_enabled land on ``rs`` (usage keeps the
   previous value when the result carries none).
3. **Deferred peer + steer inbox flush**
   (``flush_deferred_peer_and_steer`` — slice 12): only a verified
   provider completion (not a local failure dictionary or loop break)
   confirms consumption of messages injected earlier this round. The
   flush emits the confirm chips + dedups the durable queue rows.
4. **Early model surface**: ``task['model']`` is stamped AS SOON as
   the resolved model is known (was only set at finalization), so
   per-round telemetry emitted during tool dispatch records the real
   model instead of an empty string.
5. **Loop action**: when the fallback layer asks for a break
   (``_loop_action == 'break'``), the exit reason is stamped and the
   helper returns 'break'.

**Abort semantics**: ``AbortedError`` is caught here, logs the
user-abort line, stamps ``rs.exit_reason = 'user_abort'``, and
returns 'break'. Every other exception re-raises so the outer
fatal-handling path (handle_task_fatal) sees it unchanged.

Returns 'break' when the caller must break out of the stream loop,
'proceed' otherwise.
"""

from __future__ import annotations

from typing import Any, Callable

from lib.log import get_logger
from lib.llm import AbortedError
from lib.llm.stream_result import (
    ProviderStreamResult,
    ensure_provider_stream_result,
)
from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback
from lib.tasks_pkg.orchestrator._deferred_inbox_flush import (
    confirm_consumed_project_context,
    flush_deferred_peer_and_steer,
)

logger = get_logger(__name__)


def run_llm_call_with_fallback(
    task: dict[str, Any],
    rs: Any,
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_list: list[dict[str, Any]],
    stream_acc: Any,
    *,
    round_num: int,
    tid: str,
    max_tokens: int,
    on_request_usage: Callable[[Any], None] | None = None,
    on_admission_denied: Callable[[Any], None] | None = None,
) -> str:
    """Execute this round's LLM call with fallback + deferred flush.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (mutated: ``model`` early-surface).
    rs : RoundState
        Loop-state carrier (mutated: ``assistant_msg``,
        ``last_finish_reason``, ``last_usage``, ``model``, ``preset``,
        ``thinking_enabled``, ``exit_reason``).
    body : dict[str, Any]
        The built request body (from ``build_body``).
    messages : list[dict[str, Any]]
        Working message list.
    tool_list : list[dict[str, Any]]
        Assembled tool schema.
    stream_acc : StreamingToolAccumulator
        This round's streaming tool accumulator (its
        ``on_tool_call_ready`` is threaded into the call).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.
    max_tokens : int
        Token ceiling for the call.

    on_request_usage : Callable | None
        Receives this request's usage, including None/empty on local failure;
        prior paid last_usage remains intact but cannot certify this request.
    on_admission_denied : Callable | None
        Receives a typed, account-bound refusal before the current request
        receives a dispatch lease; it is not a usage mapping or replay right.
    Returns
    -------
    str
        'break' when the caller must break out of the stream loop
        (fallback-requested break or user abort), 'proceed' otherwise.
    """
    # LLM call with automatic fallback to Opus on failure
    try:
        llm_result = _llm_call_with_fallback(
            task, body, rs.model, round_num, max_tokens,
            rs.tool_call_happened, tool_list,
            messages, rs.preset, rs.thinking_enabled,
            rs.accumulated_usage, rs.api_rounds,
            on_tool_call_ready=stream_acc.on_tool_call_ready,
        )
        rs.assistant_msg = llm_result['assistant_msg']
        rs.last_finish_reason = llm_result['finish_reason']
        rs.last_usage = llm_result['usage'] or rs.last_usage

        if on_request_usage is not None:
            on_request_usage(llm_result['usage'])
        raw_stream_result = llm_result.get('stream_result')
        rs.last_stream_result = (
            ensure_provider_stream_result(raw_stream_result)
            if raw_stream_result is not None
            else ProviderStreamResult.from_legacy(
                rs.assistant_msg, rs.last_finish_reason, llm_result['usage'])
        )
        rs.model = llm_result['model']
        rs.preset = llm_result['preset']
        rs.thinking_enabled = llm_result['thinking_enabled']

        # Returning a dictionary is not success: the fallback layer also
        # returns local/rejected/exhausted failures with a loop break. Require
        # the provider result carried by successful primary/fallback/rescue
        # paths, and its closed completion verdict. The synthetic legacy
        # writeback above is for downstream compatibility, not delivery proof.
        provider_consumed = (
            raw_stream_result is not None
            and rs.last_stream_result.is_verified_complete
            and llm_result['_loop_action'] != 'break'
        )

        # Narrative cursor advancement is an acknowledgement of model
        # consumption, not request construction.  Only the first successful
        # provider return can confirm the pending Project Context page.
        if provider_consumed:
            confirm_consumed_project_context(task, tid=tid)

        # ── Flush DEFERRED peer + steer inbox ( slice 12) ──
        #   Only verified completion confirms consumption of the peer,
        #   steer and background-command messages injected earlier this
        #   round. Failed/unusable attempts leave them pending. The helper emits
        #   the PEER_INBOX_INJECT / USER_STEER_INJECT chips, records
        #   display-only sidecars on the task, and de-dups the
        #   durable message_queue rows so dispatch_next_queued can't
        #   later re-dispatch them as a redundant fresh turn.
        #   Preserve recoverability; consumption and storage ACK are not atomic.
        if provider_consumed:
            flush_deferred_peer_and_steer(task, round_num=round_num, tid=tid)

        # Surface the resolved model on the task AS SOON as it's known
        # (was only set at task finalization), so per-round telemetry
        # emitted during tool dispatch — e.g. report_hallucinated's
        # `tool_hallucinated` audit — records the real model instead of
        # an empty string and the optimizer can cluster by model.
        if rs.model:
            task['model'] = rs.model

        if llm_result['_loop_action'] == 'break':
            rs.exit_reason = llm_result['_loop_exit_reason']
            return 'break'
        return 'proceed'
    except Exception as e:
        from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
        if isinstance(e, (AbortedError, EventDeliveryCancelled)):
            if task.get('_executionLimitReason'):
                from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
                if isinstance(e, ModelBudgetAdmissionDenied) and on_admission_denied is not None:
                    on_admission_denied(e)
                rs.last_finish_reason = 'error'
                rs.exit_reason = task['_executionLimitReason']
                return 'break'
            logger.info('[%s] ✋ User abort caught at round %d', tid, round_num)
            task['aborted'] = True
            abort_event = task.get('abort_event')
            if abort_event is not None:
                abort_event.set()
            rs.last_finish_reason = 'aborted'
            rs.exit_reason = 'user_abort'
            return 'break'
        raise
