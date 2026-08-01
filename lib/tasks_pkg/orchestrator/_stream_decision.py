"""Post-stream analysis: premature close / abort / normal exit decision.

Extracted 2026-07-31 (pt_03f4cdf1 slice 25) from
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

and returns ``(action, new_premature_retry_count)`` where action is
one of:

* ``'break'`` — caller must break out of the stream loop.
* ``'continue'`` — caller must continue (premature-close retry: the
  round is re-run; the premature_retry_count ceiling expansion lives
  in the returned count).
* ``'proceed'`` — normal exit, fall through to the per-round gates.

The premature-close retry counter is returned (not mutated on rs)
because it is a chassis-owned plain local in run_task — the
WHILE-loop ceiling ``max_tool_rounds + _premature_retry_count`` reads
it directly.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.stream_handler import analyse_stream_result

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
        ``abort_phase``, ``exit_reason``).
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
        ``(action, new_premature_retry_count)`` — action is 'break' /
        'continue' / 'proceed'.
    """
    # ★ Post-stream analysis: premature close, abort, normal exit
    stream_decision = analyse_stream_result(
        rs.assistant_msg, rs.last_finish_reason, task, tid, rs.model,
        round_num, premature_retry_count, messages,
        usage=rs.last_usage,
    )
    new_count = stream_decision['premature_retry_count']
    rs.last_finish_reason = stream_decision['last_finish_reason']
    if stream_decision['abort_detected_phase']:
        rs.abort_phase = stream_decision['abort_detected_phase']
    if stream_decision['action'] == 'break':
        rs.exit_reason = stream_decision['loop_exit_reason']
        return 'break', new_count
    if stream_decision['action'] == 'continue':
        return 'continue', new_count
    return 'proceed', new_count
