"""Abort-before-tools gate: skip tool execution when the task was aborted.

Extracted 2026-07-31 (pt_03f4cdf1 slice 19) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs after the tool-call prelude (live-tail assistant/tool_call
message already appended) and before ``parse_tool_calls`` / the tool
execution pipeline. When ``task['aborted']`` is set:

1. Stamps ``rs.abort_phase`` / ``rs.exit_reason``.
2. **Removes the trailing tool_calls message** just appended by the
   prelude — skipping tool execution while leaving it would create
   orphaned tool_use blocks without matching tool_result, which the
   gateway rejects with HTTP 400 on the next turn when
   server_message_store replays the full history. If the popped
   message carried prose content alongside the tool_calls, the
   content is re-appended as a plain assistant message so the user's
   visible reply is preserved.
3. Emits ROUND_END(reason='aborted') so the round boundary opened at
   this round's top is paired (RENDER_CONTRACT Phase 3).

Returns True when the caller must break out of the stream loop,
False when the round may proceed to tool execution.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def handle_abort_before_tools(
    task: dict[str, Any],
    rs: Any,
    messages: list[dict[str, Any]],
    *,
    round_num: int,
    tid: str,
) -> bool:
    """Skip tool execution when the task was aborted mid-round.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (read: ``aborted``).
    rs : RoundState
        Loop-state carrier (mutated: ``abort_phase``, ``exit_reason``).
    messages : list[dict[str, Any]]
        Working message list — the trailing tool_calls message is
        popped (and prose content re-appended) on the abort path.
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.

    Returns
    -------
    bool
        ``True`` when the caller must ``break`` (abort handled),
        ``False`` to proceed to tool execution.
    """
    if not task['aborted']:
        return False

    rs.abort_phase = f'before_tool_exec_round_{round_num}'
    rs.exit_reason = f'aborted_before_tools_round_{round_num}'
    # ★ Remove the assistant message with tool_calls that we just
    #   appended — since we're skipping tool execution, leaving it
    #   creates orphaned tool_use blocks without matching tool_result.
    #   This causes HTTP 400 on the next turn when server_message_store
    #   replays the full message history.
    if messages and messages[-1].get('tool_calls'):
        _popped = messages.pop()
        logger.info('[%s] Removed trailing tool_calls message (abort) — '
                    'prevents orphaned tool_use in stored history', tid)
        # If it had content alongside tool_calls, keep just the content
        if _popped.get('content'):
            messages.append({'role': 'assistant', 'content': _popped['content']})
            logger.debug('[%s] Re-added assistant content without tool_calls', tid)
    logger.info('[%s] Task aborted before tool execution at round %d — '
                'skipping all tools', tid, round_num)
    append_event(task, build_event(EventType.ROUND_END,
                                   roundNum=round_num, reason='aborted'))
    return True
