"""Interrupted-turn tool-history restoration.

Settled conversation history is reconstructed once from the owner-scoped,
turn-native transcript before task creation. This module only handles the
separate Continue/checkpoint case: replaying tool rounds from the interrupted
turn that has not yet settled into ordinary history.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.message_builder import inject_tool_history, prepare_tool_history

logger = get_logger(__name__)


def prepare_continue_tool_history(*, task, cfg, model):
    """Validate and detach Continue history before expensive task startup."""
    return prepare_tool_history(cfg, task, model)


def inject_continue_tool_history(
    *, task, rs, messages, cfg, model, tid, prepared_history=None,
) -> int:
    """Continue-toolHistory injection + memory-prefetch eligibility drift guard.

    Extracted 2026-08-01 ( slice 36) from ``run_task`` (between
    context injection and resume-state hydration).

    1. ``inject_tool_history`` restores interrupted tool-call context from
       the continue checkpoint; returns the injected count.
    2. On a non-zero count: ``rs.tool_call_happened = True`` AND
       ``rs.tool_round_num = <count>`` — the offset keeps new roundNums
       from conflicting with the restored ones.
    3. The normal orchestrator passes a prepared history and uses its exact
       call count for memory-prefetch eligibility. The legacy direct-call path
       retains a drift warning for callers that still estimate from raw cfg.

    Returns:
        The injected tool-call count (0 when nothing was restored).
    """
    if prepared_history is None:
        _injected_tool_calls = inject_tool_history(
            messages, cfg, task, model)
    else:
        _injected_tool_calls = inject_tool_history(
            messages, cfg, task, model,
            prepared_history=prepared_history,
        )
    if _injected_tool_calls:
        rs.tool_call_happened = True
        rs.tool_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

    if (prepared_history is None
            and bool(_injected_tool_calls) != bool(cfg.get('toolHistory') or [])):
        logger.warning(
            '[%s] memory-prefetch eligibility drift: injected=%s but '
            'cfg[toolHistory]=%s — this legacy caller may have used the '
            'unvalidated envelope instead of the prepared call count',
            tid, _injected_tool_calls,
            len(cfg.get('toolHistory') or []))
    return _injected_tool_calls


__all__ = [
    'inject_continue_tool_history',
    'prepare_continue_tool_history',
]
