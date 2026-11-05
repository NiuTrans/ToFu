"""Post-stream analysis: premature close / abort / normal exit decision.

Extracted 2026-07-31 ( slice 25) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs after the streaming-accumulator settle and BEFORE the per-round
gates. ``analyse_stream_result`` inspects the assistant message +
finish reason and decides whether this round was a premature stream
close (retry), an abort (break), or a normal exit (proceed to tool
dispatch).

The helper applies the decision's state mutations in place:

* ``rs.last_finish_reason`` — the possibly-rewritten finish reason.
* ``rs.abort_phase`` — stamped when an abort phase was detected.
* ``rs.exit_reason`` — stamped only on the 'break' action
  (``loop_exit_reason``).
* ``rs.last_stream_result`` — rebound only on the 'break' action when the
  analyser consumed a control declaration from the shared public message, so
  terminal provider evidence keeps the content the provider returned.

and returns ``(action, new_premature_retry_count)`` where action is
one of:

* ``'break'`` — caller must break out of the stream loop.
* ``'continue'`` — caller must continue (bounded premature-close retry).
* ``'program_continue'`` — caller must apply round budget gates, then
  continue to obtain the final message after a Responses program output.
* ``'proceed'`` — normal exit, fall through to the per-round gates.

The premature-close retry counter is returned (not mutated on ``rs``) because
the stream analyser uses it to enforce its failure-specific retry budget.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lib.llm.stream_result import ProviderStreamResult
from lib.log import get_logger
from lib.tasks_pkg.stream_handler.api import RecoveryAction, analyse_stream_result

logger = get_logger(__name__)


def apply_stream_decision(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
    premature_retry_count: int,
    messages: list[dict[str, Any]],
) -> tuple[str, int]:
    """Analyse the stream result and apply the decision's state mutations.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (read by the analyser).
    rs : RoundState
        Loop-state carrier (mutated: ``last_finish_reason``,
        ``abort_phase``, ``exit_reason``, ``last_stream_result``).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.
    premature_retry_count : int
        Current premature-close retry count (chassis-owned local).
    messages : list[dict[str, Any]]
        Working message list (read by the analyser).

    Returns
    -------
    tuple[str, int]
        ``(action, new_premature_retry_count)`` — action is 'break',
        'continue', 'program_continue', or 'proceed'.
    """
    # The provider result shares its message with the public assistant_msg;
    # the analyser may consume a control declaration from that message.
    provider_content = rs.assistant_msg.get('content')
    # Post-stream analysis: premature close, abort, normal exit
    stream_decision = analyse_stream_result(
        rs.assistant_msg, rs.last_finish_reason, task, tid, rs.model,
        round_num, premature_retry_count, messages,
        usage=rs.last_usage,
        stream_result=rs.last_stream_result,
    )
    new_count = stream_decision.premature_retry_count
    stream_result = rs.last_stream_result
    if (stream_decision.action is RecoveryAction.BREAK
            and isinstance(stream_result, ProviderStreamResult)
            and stream_result.message.get('content') != provider_content):
        rs.last_stream_result = replace(stream_result, message={
            **stream_result.message, 'content': provider_content})
    rs.last_finish_reason = stream_decision.last_finish_reason
    if stream_decision.abort_detected_phase:
        rs.abort_phase = stream_decision.abort_detected_phase
    if stream_decision.action is RecoveryAction.BREAK:
        rs.exit_reason = stream_decision.loop_exit_reason
        return 'break', new_count
    if stream_decision.action is RecoveryAction.CONTINUE:
        return 'continue', new_count
    if stream_decision.action is RecoveryAction.PROGRAM_CONTINUE:
        return 'program_continue', new_count
    return 'proceed', new_count
