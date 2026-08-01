"""Per-round gates: budget ceiling + tool-rounds ceiling + per-round diagnostic.

Extracted 2026-07-31 (pt_03f4cdf1 slice 17) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

The three gates are evaluated in strict order after the LLM call and
before tool dispatch:

1. **max_budget_usd** (Claude Agent SDK parity): hard $ ceiling on
   accumulated cost. 0 / unset disables. On exceed: stamps
   ``finishReason='budget_exceeded'``, emits ROUND_END(reason='budget'),
   sets ``exit_reason``, and breaks.
2. **tool_rounds_exhausted**: safety ceiling on tool call rounds. On
   exceed: stamps ``finishReason='tool_rounds_exhausted'``, emits
   ROUND_END(reason='budget'), sets ``exit_reason``, and breaks.
3. **per-round diagnostic**: INFO-level log of finish_reason / model /
   content-length / tool_calls count for every tool round.

The helper mutates ``rs`` (RoundState) in place and returns a bool:
``True`` when the caller should break out of the stream loop (one of
the budget gates fired), ``False`` when the round may proceed to tool
dispatch. All event emission is via ``append_event`` /
``build_event`` / ``EventType`` so the wire contract stays identical.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def check_round_gates(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
    max_tool_rounds: int,
    cfg: dict[str, Any],
) -> bool:
    """Evaluate per-round budget + tool-rounds gates.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (mutated: ``error`` may be set on gate fire).
    rs : RoundState
        Loop-state carrier (mutated: ``last_finish_reason``,
        ``exit_reason`` may be set).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.
    max_tool_rounds : int
        Configured tool-round ceiling.
    cfg : dict[str, Any]
        Task config (read: ``maxBudgetUsd``).

    Returns
    -------
    bool
        ``True`` when the caller should ``break`` out of the stream
        loop (a gate fired), ``False`` to continue.
    """
    # ── Per-round diagnostic: log finish_reason for every tool round ──
    _round_content = len((rs.assistant_msg or {}).get('content', '') or '')
    _round_tcs = len((rs.assistant_msg or {}).get('tool_calls', []))
    logger.info('[%s] conv=%s Round %d result: finish_reason=%s model=%s '
                'content=%dchars tool_calls=%d → proceeding to tool execution',
                tid, task.get('convId', ''), round_num + 1,
                rs.last_finish_reason, rs.model,
                _round_content, _round_tcs)

    # ── max_budget_usd gate (Claude Agent SDK parity) ──
    # Hard $ ceiling on accumulated cost.  0 / unset disables.
    _max_budget = float(cfg.get('maxBudgetUsd') or 0.0)
    if _max_budget > 0:
        from lib.cost_estimator import check_budget
        _exceeded, _cost, _reason = check_budget(
            task, rs.accumulated_usage, rs.model, _max_budget,
            round_num=round_num,
        )
        if _exceeded:
            rs.last_finish_reason = 'budget_exceeded'
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                'budget_exceeded',
                detail=_reason,
                model=rs.model,
                context='budget-gate',
                source='orchestrator',
                raw=f'cost_usd={_cost:.6f} max={_max_budget:.6f}',
            )
            rs.exit_reason = f'budget_exceeded_round_{round_num}_${_cost:.4f}'
            append_event(task, build_event(EventType.ROUND_END,
                                           roundNum=round_num, reason='budget'))
            return True

    # ── Tool round budget check ──
    if round_num >= max_tool_rounds:
        # Safety ceiling: tool round budget exhausted
        rs.last_finish_reason = 'tool_rounds_exhausted'
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'tool_rounds_exhausted',
            detail=f'Tool call limit reached ({max_tool_rounds} rounds).',
            model=rs.model,
            context='tool-budget',
            source='orchestrator',
            raw=f'max_tool_rounds={max_tool_rounds}',
        )
        logger.warning('[Task %s] conv=%s ⚠️ Tool rounds exhausted at round %d/%d',
                       task['id'][:8], task.get('convId', ''),
                       round_num+1, max_tool_rounds)
        rs.exit_reason = f'tool_rounds_exhausted_{round_num}'
        append_event(task, build_event(EventType.ROUND_END,
                                       roundNum=round_num, reason='budget'))
        return True

    return False
