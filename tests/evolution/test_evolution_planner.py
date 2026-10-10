"""Planner: measured costs enter plans, prose costs cannot; ranking is arithmetic."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[2] / 'contracts'
USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _goal(**overrides):
    goal = {
        'schema_version': 1, 'goal_id': 'goal-1',
        'statement': 'Reproduce the transformer baseline within error bounds',
        'domain': 'research', 'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': 'pytest -q'}}],
        'budget_envelope': {'max_tokens': 100000, 'max_wall_clock_seconds': 3600,
                            'max_tool_calls': 100},
        'status': 'draft', 'created_at': 1700000000,
    }
    goal.update(overrides)
    return goal


def _candidate(path_id, *, utility=0.5, gain=0.5, title='Do the thing'):
    return {
        'path_id': path_id, 'title': title,
        'stages': [{'kind': 'execute', 'detail': 'run then evaluate'}],
        'expected_utility': utility, 'expected_information_gain': gain,
    }


def test_build_plan_fills_measured_cost_and_ranks_by_utility_over_cost(store):
    from lib import evolution
    evolution.admit_goal(_goal(), user_id=USER)
    plan = evolution.build_plan(evolution.get_goal('goal-1', user_id=USER), [
        _candidate('cheap', utility=0.5),
        _candidate('fancy', utility=0.9),
    ], user_id=USER, now=1700000002)
    schema = json.loads((CONTRACTS / 'evolution_plan_v1.schema.json')
                        .read_text(encoding='utf-8'))
    jsonschema.validate(plan, schema)
    assert plan['status'] == 'proposed'
    assert plan['requires_confirmation'] is True
    # Empty history → exploratory_flat full-envelope cost for every candidate,
    # so the higher-utility one must win the same denominator.
    assert plan['selected_path_id'] == 'fancy'
    assert plan['candidates'][0]['cost_estimate']['priors_source'] == 'exploratory_flat'
    assert plan['candidates'][0]['cost_estimate']['expected_tokens'] == 100000


def test_information_gain_breaks_utility_ties(store):
    from lib import evolution
    evolution.admit_goal(_goal(), user_id=USER)
    goal = evolution.get_goal('goal-1', user_id=USER)
    plan = evolution.build_plan(goal, [
        _candidate('shallow', utility=0.6, gain=0.1),
        _candidate('instructive', utility=0.6, gain=0.9),
    ], user_id=USER, persist=False)
    assert plan['selected_path_id'] == 'instructive'
    gains = {c['path_id']: c['utility_over_cost']
             for c in plan['candidates']}
    assert gains['instructive'] > gains['shallow']


def test_build_plan_rejects_prose_costs_and_duplicate_ids(store):
    from lib import evolution
    evolution.admit_goal(_goal(), user_id=USER)
    goal = evolution.get_goal('goal-1', user_id=USER)
    tainted = _candidate('a')
    tainted['cost_estimate'] = {'expected_tokens': 1}  # caller cost is discarded
    plan = evolution.build_plan(goal, [tainted], user_id=USER, persist=False)
    estimate = plan['candidates'][0]['cost_estimate']
    assert estimate['expected_tokens'] == 100000  # envelope, not the prose 1
    with pytest.raises(evolution.EvolutionStateError):
        evolution.build_plan(goal, [_candidate('x'), _candidate('x')],
                             user_id=USER, persist=False)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.build_plan(goal, [], user_id=USER, persist=False)


def test_suggest_candidates_replays_winning_local_paths(store):
    from lib import evolution
    from lib.evolution import records
    evolution.admit_goal(_goal(), user_id=USER)
    plan = evolution.build_plan(evolution.get_goal('goal-1', user_id=USER),
                                [_candidate('path-a', utility=0.6)],
                                user_id=USER)
    evolution.save_plan({**plan, 'status': 'confirmed'}, user_id=USER)
    episode = evolution.open_episode(goal_id='goal-1', plan_id=plan['plan_id'],
                                     selected_path_id='path-a', task_id='task-1',
                                     user_id=USER)
    evolution.settle_episode(
        episode['episode_id'], user_id=USER, status='success',
        actuals={'tokens': 100, 'wall_clock_seconds': 10, 'tool_calls': 1},
        verdict={'outcome': 'success', 'assessed_by': 'checker',
                 'criterion_results': []},
        learning={'resource_weight_deltas': [], 'cost_actual_vs_estimate': None,
                  'strategy_outcome': None, 'capability_gap': None})
    candidates = evolution.suggest_candidates(
        evolution.get_goal('goal-1', user_id=USER), user_id=USER)
    kinds = {c['path_id'] for c in candidates}
    assert f"replay-{episode['episode_id']}" in kinds
    assert 'baseline' in kinds
    replay = next(c for c in candidates if c['path_id'].startswith('replay-'))
    assert replay['expected_utility'] == 0.7
    assert replay['stages'] == [{'kind': 'execute', 'detail': 'run then evaluate'}]
    baseline = next(c for c in candidates if c['path_id'] == 'baseline')
    assert baseline['resource_refs'][0]['indexer'] == 'history'


def test_suggest_candidates_ignores_foreign_history(store):
    from lib import evolution
    evolution.admit_goal(_goal(), user_id=99)
    candidates = evolution.suggest_candidates(_goal(), user_id=USER)
    assert [c['path_id'] for c in candidates] == ['baseline']
    assert candidates[0]['resource_refs'] == []


def test_zero_information_gain_remains_zero_in_path_selection(store):
    from lib.evolution.planner import build_plan
    plan = build_plan(_goal(), [_candidate('zero', gain=0),
        _candidate('small', gain=0.1)], user_id=USER, persist=False)
    assert plan['selected_path_id'] == 'small'
    zero = next(row for row in plan['candidates'] if row['path_id'] == 'zero')
    assert zero['cost_estimate']['expected_information_gain'] == 0
    assert zero['utility_over_cost'] == 0.25


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -float('inf'), True, 10**400])
def test_invalid_proposer_scores_use_defaults_and_cannot_win_by_inflation(store, invalid):
    from lib.evolution.planner import build_plan
    plan = build_plan(_goal(), [_candidate('invalid', utility=invalid, gain=invalid),
        _candidate('valid', utility=0.6, gain=0.6)], user_id=USER, persist=False)
    assert plan['selected_path_id'] == 'valid'
    candidate = next(row for row in plan['candidates'] if row['path_id'] == 'invalid')
    assert candidate['cost_estimate']['expected_utility'] == 0.5
    assert candidate['cost_estimate']['expected_information_gain'] == 0.5
    json.dumps(plan, allow_nan=False)


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -float('inf'), -1, True, 10**400])
def test_invalid_policy_scores_fall_back_to_arithmetic_without_breaking_plan(store, invalid):
    from lib.evolution.planner import build_plan
    candidates = [_candidate('lower', utility=0.3), _candidate('higher', utility=0.8)]
    plan = build_plan(_goal(), candidates, user_id=USER, persist=False,
        policy=lambda candidate, context: invalid)
    assert plan['selected_path_id'] == 'higher'
    assert [row['utility_over_cost'] for row in plan['candidates']] == [0.6, 0.225]
    json.dumps(plan, allow_nan=False)


def test_finite_policy_zero_and_override_remain_valid(store):
    from lib.evolution.planner import build_plan
    plan = build_plan(_goal(), [_candidate('ordinary', utility=0.9),
        _candidate('policy', utility=0.1)], user_id=USER, persist=False,
        policy=lambda candidate, context: 0 if candidate['path_id'] == 'ordinary' else 0.2)
    assert plan['selected_path_id'] == 'policy'
    assert [row['utility_over_cost'] for row in plan['candidates']] == [0.2, 0]


def test_invalid_utility_preserves_admitted_self_model_default(store, monkeypatch):
    from lib.evolution import planner
    monkeypatch.setattr(planner, 'predict_success', lambda *args, **kwargs:
        {'success_probability': 0.2})
    plan = planner.build_plan(_goal(), [_candidate('invalid', utility=float('nan')),
        _candidate('explicit', utility=0.3)], user_id=USER, persist=False)
    assert plan['selected_path_id'] == 'explicit'
    invalid = next(row for row in plan['candidates'] if row['path_id'] == 'invalid')
    assert invalid['cost_estimate']['expected_utility'] == 0.2
    assert invalid['cost_estimate']['failure_rate_prior'] == 0.8
