"""Post-LLM streaming-accumulator settle: reconcile + readback + cache inject.

Extracted 2026-07-31 (pt_03f4cdf1 slice 24) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs after the per-round cache accounting and BEFORE the post-stream
analysis / tool dispatch. Three steps:

1. **Settle orphan early-announced rounds**
   (``stream_acc.reconcile_announced_rounds``): stream_chat re-runs
   the SSE stream on a transient mid-stream error while reusing the
   same on_tool_call_ready callback, so a tool call whose args
   streamed far enough on an EARLIER attempt already got a
   'searching' round + tool_start — but only the FINAL attempt's
   tool calls survive into assistant_msg. Any announced round whose
   tc_id isn't in the final message is orphaned at 'searching'
   forever (a permanently spinning tool row, live AND after reload).
   Reconciled here — the per-round complement of the task-end
   dangling sweep — BEFORE parse_tool_calls so the orphan never
   reaches the render/persist path unsettled.
2. **Read back the updated tool_round_num**: tool_start events
   emitted during streaming already consumed round numbers, so
   parse_tool_calls must start from the accumulator's counter when
   anything was announced.
3. **Inject pre-computed streaming tool results into the dedup
   cache** (``stream_acc.inject_into_cache``): execute_tool_pipeline
   will find these and skip re-execution. Logged at INFO when any
   hits landed.

All three steps are pure mutations — no control-flow crossings, no
event emission beyond what the accumulator itself performs.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def settle_stream_accumulator(
    stream_acc: Any,
    task: dict[str, Any],
    rs: Any,
    *,
    tid: str,
) -> None:
    """Reconcile orphan rounds + read back round counter + inject cache.

    Parameters
    ----------
    stream_acc : StreamingToolAccumulator
        This round's streaming tool accumulator.
    task : dict[str, Any]
        Live task dict (cache injection target).
    rs : RoundState
        Loop-state carrier (mutated: ``tool_round_num`` readback).
    tid : str
        8-char task id for logging.
    """
    # ★ Settle orphan early-announced rounds left by a discarded stream
    #   retry. stream_chat re-runs the SSE stream on a transient
    #   mid-stream error while reusing the same on_tool_call_ready
    #   callback, so a tool call whose args streamed far enough on an
    #   EARLIER attempt already got a 'searching' round + tool_start —
    #   but only the FINAL attempt's tool calls survive into
    #   assistant_msg. Any announced round whose tc_id isn't in the
    #   final message is orphaned at 'searching' forever (a permanently
    #   spinning tool row, live AND after reload). Reconcile here — the
    #   per-round complement of the task-end dangling sweep — BEFORE
    #   parse_tool_calls so the orphan never reaches the render/persist
    #   path unsettled.
    stream_acc.reconcile_announced_rounds(rs.assistant_msg)

    # ★ Read back updated tool_round_num from streaming accumulator
    #   (tool_start events emitted during streaming already consumed
    #   round numbers, so parse_tool_calls must start from here).
    if stream_acc.announced_tc_map:
        rs.tool_round_num = stream_acc.tool_round_num

    # ★ Inject pre-computed streaming tool results into dedup cache.
    #   execute_tool_pipeline will find these and skip re-execution.
    if stream_acc.submitted_count > 0:
        _prefetch_hits = stream_acc.inject_into_cache(task)
        if _prefetch_hits:
            logger.info('[%s] Streaming tool exec: %d results pre-computed '
                        'and injected into cache', tid, _prefetch_hits)
