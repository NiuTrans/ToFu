"""Gate policy (R5): the gate's own thresholds as an evolvable artifact.

Responsibility: hold the active threshold set (regression margin, minimum
lift and group samples for hypothesis admission, monitor sample floor) and
apply activated ``budget_policy`` mutations to it. Entry points:
``get_thresholds`` / ``apply_gate_policy`` / ``meta_report``. Dependencies:
the gate (mutations history), the episodes store, the policy contract.

Defaults are the code constants every test already pins; an activated
mutation overrides them per owner, versioned and revertible like any other
mutation. ``meta_report`` is the meta-metric: per policy version, how many
mutations were proposed versus confirmed or activated, and the mean outcome
weight of episodes settled while it governed — the evidence for whether a
threshold change actually helped.
"""
from __future__ import annotations

import json
import time as time

from .contracts import EvolutionStateError as EvolutionStateError, validate_gate_policy as validate_gate_policy
from .episodes import list_episodes
from .gate import list_mutations
from .records import (
    NAMESPACE_GATE_POLICY as NAMESPACE_GATE_POLICY,
    entity_key as entity_key,
    get_record as get_record,
    put_record as put_record,
)

DEFAULTS = {
    'regression_margin': 0.1,
    'min_lift': 0.15,
    'min_group_samples': 3,
    'monitor_min_samples': 3,
}
_BOUNDS = {
    'regression_margin': (0.0, 0.5, float),
    'min_lift': (0.0, 1.0, float),
    'min_group_samples': (1, 10, int),
    'monitor_min_samples': (1, 10, int),
}
_OUTCOME_WEIGHT = {'success': 1.0, 'partial': 0.5, 'failure': 0.0}


def get_thresholds(*, user_id) -> dict:
    record = get_record(NAMESPACE_GATE_POLICY,
                        entity_key('active', user_id, kind='gate policy'))
    thresholds = dict(DEFAULTS)
    if record:
        for key in DEFAULTS:
            if key in record:
                thresholds[key] = record[key]
    return thresholds


def parse_policy_overrides(payload: dict) -> dict:
    """Validate a budget_policy mutation payload; raises on any violation."""
    raw = (payload or {}).get('policy')
    try:
        overrides = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        overrides = None
    if not isinstance(overrides, dict) or not overrides:
        raise EvolutionStateError(
            'budget_policy payload.policy must be a JSON object of overrides')
    cleaned = {}
    for key, value in overrides.items():
        if key not in _BOUNDS:
            raise EvolutionStateError(f'unknown gate policy key {key!r}')
        low, high, kind = _BOUNDS[key]
        if isinstance(value, bool) or not isinstance(
                value, (int, float)) or not low <= value <= high:
            raise EvolutionStateError(
                f'gate policy {key} must be within [{low}, {high}]')
        cleaned[key] = kind(value)
    return cleaned


def apply_gate_policy(mutation: dict, *, user_id,
                      now: int | None = None, expected_parent=None) -> dict:
    """Install an activated budget_policy mutation as the active thresholds."""
    from .policy_transitions import apply
    return apply(mutation,user_id=user_id,now=now,expected_parent=expected_parent)


def meta_report(*, user_id) -> dict:
    """Per gate-policy version: proposal throughput and governed outcomes."""
    policy_mutations = [m for m in list_mutations(user_id=user_id,
                                                  kind='budget_policy')]
    episodes = [e for e in list_episodes(user_id=user_id, limit=200)
                if e.get('status') in _OUTCOME_WEIGHT]
    rows = []
    for mutation in sorted(policy_mutations,
                           key=lambda m: int(m.get('version') or 0)):
        activated_at = int(mutation.get('updated_at') or 0)
        governed = [_OUTCOME_WEIGHT[e['status']] for e in episodes
                    if int(e.get('settled_at') or 0) >= activated_at]
        rows.append({
            'mutation_id': mutation['mutation_id'],
            'status': mutation['status'],
            'governed_episodes': len(governed),
            'governed_outcome_mean': (round(sum(governed) / len(governed), 4)
                                      if governed else None),
        })
    proposed = len(policy_mutations)
    advanced = sum(1 for m in policy_mutations
                   if m['status'] in ('active', 'rolled_back'))
    return {
        'policy_mutations': rows,
        'proposed': proposed,
        'advanced': advanced,
        'accepted_rate': (round(advanced / proposed, 4) if proposed else None),
    }
