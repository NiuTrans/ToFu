"""Consecutive tool-timeout circuit breaker (Phase 4b).

Extracted 2026-07-31 (pt_03f4cdf1 slice 21) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs after ``execute_tool_pipeline`` returns. The pipeline reports
whether THIS round's tool execution timed out; the breaker counts
CONSECUTIVE timeouts across rounds:

* **Timeout round**: increments ``rs.consecutive_tool_timeouts`` and
  logs a WARNING with the running count. When the count reaches
  ``max_consecutive_tool_timeouts`` (3 in production) the breaker
  FORCE-STOPs the task: logs an ERROR, stamps ``task['error']`` with
  a ``tool_timeout`` envelope (context='tool-loop'), sets
  ``rs.exit_reason``, and emits ROUND_END(reason='tool_timeout') —
  the ONLY exit path that previously skipped the round boundary, so
  this emission pairs the ROUND_START opened at this round's top
  (RENDER_CONTRACT Phase 3). The frontend reducer does not read
  ``reason`` (stream_reducer.js round_end case), so the
  'tool_timeout' value is wire-safe.
* **Successful round**: resets the counter to 0.

Returns True when the caller must break out of the stream loop
(force-stop fired), False when the round may proceed to the
checkpoint + round close.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def handle_tool_timeout_circuit_breaker(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
    tool_timed_out: bool,
    max_consecutive_tool_timeouts: int,
) -> bool:
    """Count consecutive tool timeouts; force-stop at the ceiling.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (mutated: ``error`` on force-stop).
    rs : RoundState
        Loop-state carrier (mutated: ``consecutive_tool_timeouts``,
        ``exit_reason``).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.
    tool_timed_out : bool
        Whether this round's tool execution timed out (from
        ``execute_tool_pipeline``).
    max_consecutive_tool_timeouts : int
        Force-stop ceiling (``_MAX_CONSECUTIVE_TOOL_TIMEOUTS`` = 3).

    Returns
    -------
    bool
        ``True`` when the caller must ``break`` (force-stop fired),
        ``False`` to proceed to the checkpoint + round close.
    """
    if not tool_timed_out:
        rs.consecutive_tool_timeouts = 0  # Reset on successful tool execution
        return False

    rs.consecutive_tool_timeouts += 1
    logger.warning(
        '[%s] conv=%s Tool timeout at round %d (%d/%d consecutive) model=%s',
        tid, task.get('convId', ''), round_num + 1, rs.consecutive_tool_timeouts,
        max_consecutive_tool_timeouts, rs.model)
    if rs.consecutive_tool_timeouts < max_consecutive_tool_timeouts:
        return False

    logger.error(
        '[%s] conv=%s ⚠️ FORCE STOP: %d consecutive tool timeouts — breaking loop to prevent runaway task. model=%s',
        tid, task.get('convId', ''), rs.consecutive_tool_timeouts, rs.model)
    from lib.error_envelope import make_envelope as _make_env
    task['error'] = _make_env(
        'tool_timeout',
        detail=f'{rs.consecutive_tool_timeouts} consecutive tool execution timeouts.',
        model=rs.model,
        context='tool-loop',
        source='orchestrator',
        raw=f'consecutive_tool_timeouts={rs.consecutive_tool_timeouts}',
    )
    rs.exit_reason = f'consecutive_tool_timeouts_{rs.consecutive_tool_timeouts}'
    # ★ RENDER_CONTRACT Phase 3: close THIS round's boundary —
    #   the FORCE-STOP break otherwise strands the ROUND_START
    #   emitted at this round's top with no pairing ROUND_END
    #   (the ONLY exit path that skipped it: budget x2 /
    #   aborted / tools all pair). The frontend reducer does
    #   not read `reason` (stream_reducer.js round_end case),
    #   so the new 'tool_timeout' value is wire-safe.
    append_event(task, build_event(
        EventType.ROUND_END,
        roundNum=round_num, reason='tool_timeout'))
    return True
