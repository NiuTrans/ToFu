"""Interrupted-turn tool-history restoration.

Settled conversation history is reconstructed once from the owner-scoped,
turn-native transcript before task creation. This module only handles the
separate Continue/checkpoint case: replaying tool rounds from the interrupted
turn that has not yet settled into ordinary history.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.message_builder import inject_tool_history

logger = get_logger(__name__)


def inject_continue_tool_history(*, task, rs, messages, cfg, model, tid) -> int:
    """Continue-toolHistory injection + memory-prefetch eligibility drift guard.

    Extracted 2026-08-01 ( slice 36) from ``run_task`` (between
    context injection and resume-state hydration).

    1. ``inject_tool_history`` restores interrupted tool-call context from
       the continue checkpoint; returns the injected count.
    2. On a non-zero count: ``rs.tool_call_happened = True`` AND
       ``rs.tool_round_num = <count>`` — the offset keeps new roundNums
       from conflicting with the restored ones.
    3. Drift guard: the EARLY memory-prefetch spawn used
       ``len(cfg['toolHistory'])`` as its eligibility input (available
       before this call); if the actual injected count disagrees, WARN —
       inject_tool_history no longer derives its count from that key
       alone, so the spawn's skip decision may silently flip.

    Returns:
        The injected tool-call count (0 when nothing was restored).
    """
    _injected_tool_calls = inject_tool_history(messages, cfg, task, model)
    if _injected_tool_calls:
        rs.tool_call_happened = True
        rs.tool_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

    if bool(_injected_tool_calls) != bool(cfg.get('toolHistory') or []):
        logger.warning(
            '[%s] memory-prefetch eligibility drift: injected=%s but '
            'cfg[toolHistory]=%s — the early spawn used the latter; '
            'inject_tool_history no longer derives its count from that '
            'key alone', tid, _injected_tool_calls,
            len(cfg.get('toolHistory') or []))
    return _injected_tool_calls


__all__ = ['inject_continue_tool_history']
