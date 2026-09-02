"""Per-round financial budget gate and diagnostic logging.

Extracted 2026-07-31 ( slice 17) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

The budget check is evaluated after the LLM call and before tool dispatch:

* **max_budget_usd** (Claude Agent SDK parity): hard $ ceiling on
   accumulated cost. 0 / unset disables. On exceed: stamps
   ``finishReason='budget_exceeded'``, emits ROUND_END(reason='budget'),
   sets ``exit_reason``, and breaks.
* **per-round diagnostic**: INFO-level log of finish_reason / model /
   content-length / tool_calls count for every tool round.

The helper mutates ``rs`` (RoundState) in place and returns a bool:
``True`` when the caller should break out of the stream loop (the
financial budget fired), ``False`` when the round may proceed to tool
dispatch. All event emission is via ``append_event`` /
``build_event`` / ``EventType`` so the wire contract stays identical.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def check_task_resource_budget(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    cfg: dict[str, Any],
    messages: list[dict[str, Any]] | None = None,
) -> bool:
    """Warn once at the soft threshold; terminate before another API call."""
    from lib.task_budget import evaluate_task_budget

    decision = evaluate_task_budget(
        task, cfg, usage=rs.accumulated_usage,
        api_rounds=len(rs.api_rounds))
    warned = task.setdefault('_budgetWarnings', set())
    for reading in decision.warnings:
        if reading.name in warned:
            continue
        warned.add(reading.name)
        append_event(task, build_event(
            EventType.BUDGET_WARNING,
            limit=reading.name,
            used=reading.used,
            remaining=reading.remaining,
            hardLimit=reading.limit,
            unit=reading.unit,
        ))

    reading = decision.exceeded
    if reading is None:
        inject_api_round_finalization_reserve(
            task,
            decision,
            messages,
            round_num=round_num,
        )
        return False
    rs.last_finish_reason = 'budget_exceeded'
    rs.exit_reason = f'budget_exceeded_{reading.name}_round_{round_num}'
    from lib.error_envelope import make_envelope as _make_env
    envelope = _make_env(
        'budget_exceeded',
        detail=(f'{reading.name} used={reading.used:g} '
                f'limit={reading.limit:g} {reading.unit}'),
        model=rs.model,
        context='resource-budget-gate',
        source='orchestrator',
        raw=f'{reading.name}={reading.used:g}/{reading.limit:g}',
    )
    envelope['code'] = 'task_budget_exceeded'
    envelope['budget'] = {
        'limit': reading.name,
        'used': reading.used,
        'hardLimit': reading.limit,
        'remaining': reading.remaining,
        'unit': reading.unit,
        'remainingBudget': decision.remaining,
    }
    task['error'] = envelope
    return True


def inject_api_round_finalization_reserve(
    task: dict[str, Any],
    decision: Any,
    messages: list[dict[str, Any]] | None,
    *,
    round_num: int,
) -> bool:
    """Give the model one visible chance to finish before the hard round cap."""
    if messages is None or task.get('_apiRoundFinalizationReminder'):
        return False
    reading = next(
        (item for item in decision.readings if item.name == 'apiRounds'),
        None,
    )
    if reading is None:
        return False

    from lib.task_budget import api_round_finalization_reserve
    reserve = api_round_finalization_reserve(reading.limit)
    if reading.remaining <= 0 or reading.remaining > reserve:
        return False

    warned = task.setdefault('_budgetWarnings', set())
    if reading.name not in warned:
        warned.add(reading.name)
        append_event(task, build_event(
            EventType.BUDGET_WARNING,
            limit=reading.name,
            used=reading.used,
            remaining=reading.remaining,
            hardLimit=reading.limit,
            unit=reading.unit,
        ))

    used_rounds = int(reading.used)
    hard_limit = int(reading.limit)
    remaining_rounds = int(reading.remaining)
    messages.append({
        'role': 'user',
        'content': (
            '<system-reminder>\n'
            f'API-round budget: {used_rounds}/{hard_limit} used; '
            f'{remaining_rounds} remain. These remaining rounds are the '
            'finalization reserve. Stop broad discovery and repeated '
            'confirmation. Use known evidence to complete the requested '
            'implementation, research, or writing; run only decisive '
            'verification; then provide the final answer. If blocked, report '
            'the concrete blocker before the hard limit. Do not claim success '
            'without verification.\n'
            '</system-reminder>'
        ),
        '_isMeta': True,
    })
    task['_apiRoundFinalizationReminder'] = {
        'used': used_rounds,
        'hardLimit': hard_limit,
        'remaining': remaining_rounds,
        'round': round_num,
    }
    logger.warning(
        '[Budget] API-round finalization reserve entered: used=%d limit=%d '
        'remaining=%d round=%d',
        used_rounds,
        hard_limit,
        remaining_rounds,
        round_num,
    )
    return True


def check_round_gates(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
    cfg: dict[str, Any],
) -> bool:
    """Evaluate the per-round token/cost budget gate.

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
    cfg : dict[str, Any]
        Task config (read: ``maxBudgetUsd``).

    Returns
    -------
    bool
        ``True`` when the caller should ``break`` out of the stream
        loop (the budget fired), ``False`` to continue.
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
    try:
        _max_budget = float(cfg.get('maxBudgetUsd') or 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[BudgetGate] invalid maxBudgetUsd=%r: %s',
                     cfg.get('maxBudgetUsd'), exc)
        _max_budget = 0.0
    if _max_budget > 0:
        from lib.cost_estimator import check_budget, estimate_usage_cost
        _cost_before_gate = estimate_usage_cost(
            rs.accumulated_usage, rs.model,
            task.get('provider_id') or '')
        try:
            _soft_ratio = float(cfg.get('taskBudgetSoftRatio') or 0.8)
        except (TypeError, ValueError, OverflowError) as exc:
            logger.debug('[BudgetGate] invalid taskBudgetSoftRatio=%r: %s',
                         cfg.get('taskBudgetSoftRatio'), exc)
            _soft_ratio = 0.8
        _soft_ratio = max(0.1, min(0.99, _soft_ratio))
        _warned = task.setdefault('_budgetWarnings', set())
        if (_cost_before_gate < _max_budget
                and _cost_before_gate >= _max_budget * _soft_ratio
                and 'estimatedCostUsd' not in _warned):
            _warned.add('estimatedCostUsd')
            append_event(task, build_event(
                EventType.BUDGET_WARNING,
                limit='estimatedCostUsd',
                used=_cost_before_gate,
                remaining=max(0.0, _max_budget - _cost_before_gate),
                hardLimit=_max_budget,
                unit='usd',
            ))
        _exceeded, _cost, _reason = check_budget(
            task, rs.accumulated_usage, rs.model, _max_budget,
            provider_id=task.get('provider_id') or '',
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
            task['error']['code'] = 'task_budget_exceeded'
            from lib.task_budget import evaluate_task_budget
            _resource_remaining = evaluate_task_budget(
                task, cfg, usage=rs.accumulated_usage,
                api_rounds=len(rs.api_rounds)).remaining
            _cost_remaining = max(0.0, _max_budget - _cost)
            task['error']['budget'] = {
                'limit': 'estimatedCostUsd',
                'used': _cost,
                'hardLimit': _max_budget,
                'remaining': _cost_remaining,
                'unit': 'usd',
                'remainingBudget': {
                    **_resource_remaining,
                    'estimatedCostUsd': _cost_remaining,
                },
            }
            rs.exit_reason = f'budget_exceeded_round_{round_num}_${_cost:.4f}'
            append_event(task, build_event(EventType.ROUND_END,
                                           roundNum=round_num, reason='budget'))
            return True

    return False
