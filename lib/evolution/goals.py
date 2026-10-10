"""Goal intake and plan confirmation for the self-evolution module.

Responsibility: admit goals under their verifiable completion criteria and
persist path plans against an admitted goal. Entry points: ``admit_goal`` /
``save_plan`` / ``get_goal`` / ``get_plan`` / ``list_goals``. Dependencies:
``contracts`` (validation) and ``records`` (owner-scoped persistence). Goal
terminal status is derived from episode settlement in ``episodes.py``; the
stored status here only covers intake (draft/admitted/aborted), so no
two-write atomicity problem exists.
"""
from __future__ import annotations

from copy import deepcopy
import time

from .contracts import (
    EvolutionStateError,
    validate_goal,
    validate_plan,
)
from .records import (
    NAMESPACE_GOALS,
    NAMESPACE_PLANS,
    entity_key,
    get_record,
    get_versioned_record,
    list_records,
    put_record,
)

_ADMISSIBLE_STATUSES = ('draft', 'admitted')


def admit_goal(payload: dict, *, user_id, now: int | None = None) -> dict:
    """Validate and persist a goal; the stored status is always ``admitted``."""
    goal = validate_goal(deepcopy(payload))
    if goal.get('status') not in _ADMISSIBLE_STATUSES:
        raise EvolutionStateError(
            f"goal intake admits draft/admitted goals, not {goal.get('status')!r}")
    goal['status'] = 'admitted'
    goal.setdefault('created_at', int(now if now is not None else time.time()))
    key = entity_key(goal['goal_id'], user_id, kind='goal')
    existing, version = get_versioned_record(NAMESPACE_GOALS, key)
    if existing is not None and existing != goal:
        raise EvolutionStateError('admitted goals are immutable; use a new goal id')
    if existing is None:
        put_record(NAMESPACE_GOALS, key, goal, kind='goal', expected_version=version)
    return goal


def save_plan(payload: dict, *, user_id) -> dict:
    """Validate and persist a plan against an admitted goal of the same owner."""
    plan = validate_plan(deepcopy(payload))
    if get_record(NAMESPACE_GOALS,
                  entity_key(plan['goal_id'], user_id, kind='goal')) is None:
        raise EvolutionStateError('plan references an unknown goal for this owner')
    key = entity_key(plan['plan_id'], user_id, kind='plan')
    existing, version = get_versioned_record(NAMESPACE_PLANS, key)
    if existing and existing != plan:
        if existing['status'] != 'proposed':
            raise EvolutionStateError('confirmed/rejected plans are immutable')
        if {k: v for k, v in existing.items() if k != 'status'} != {k: v for k, v in plan.items() if k != 'status'}:
            raise EvolutionStateError('plan confirmation cannot change the proposed path')
    if existing != plan:
        put_record(NAMESPACE_PLANS, key, plan, kind='plan', expected_version=version)
    return plan


def get_goal(goal_id: str, *, user_id) -> dict | None:
    return get_record(NAMESPACE_GOALS, entity_key(goal_id, user_id, kind='goal'))


def get_plan(plan_id: str, *, user_id) -> dict | None:
    return get_record(NAMESPACE_PLANS, entity_key(plan_id, user_id, kind='plan'))


def list_goals(*, user_id, limit: int = 50) -> list[dict]:
    return list_records(NAMESPACE_GOALS, user_id, kind='goal', limit=limit)


def list_plans(*, user_id, goal_id: str | None = None,
               limit: int = 50) -> list[dict]:
    plans = list_records(NAMESPACE_PLANS, user_id, kind='plan', limit=limit)
    if goal_id:
        plans = [p for p in plans if p.get('goal_id') == goal_id]
    return plans
