"""Self-evolution goal intake and episode settlement behavior.

The store fixture mirrors the research-records seam: a controlled Sidecar
record.* replacement, no network, no real storage. What is pinned here is the
module's failure posture and learning-rights discipline, not private helpers.
"""
from __future__ import annotations

from copy import deepcopy
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
        'schema_version': 1,
        'goal_id': 'goal-1',
        'statement': 'Reproduce the published result within 10% error.',
        'domain': 'research',
        'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': 'pytest -q'}}],
        'budget_envelope': {'max_tokens': 200000, 'max_wall_clock_seconds': 7200,
                            'max_tool_calls': 200},
        'status': 'draft',
        'created_at': 1700000000,
    }
    goal.update(overrides)
    return goal


def _plan(status='confirmed', **overrides):
    plan = {
        'schema_version': 1,
        'plan_id': 'plan-1',
        'goal_id': 'goal-1',
        'plan_revision': 1,
        'candidates': [{
            'path_id': 'path-a',
            'title': 'Adapt the reference implementation',
            'stages': [{'kind': 'execute', 'detail': 'port then evaluate'}],
            'resource_refs': [],
            'cost_estimate': {
                'expected_tokens': 50000, 'expected_wall_clock_seconds': 1800,
                'expected_tool_calls': 60, 'failure_rate_prior': 0.3,
                'expected_information_gain': 0.7, 'expected_utility': 0.6,
                'priors_source': 'measured_history'},
            'utility_over_cost': 1.4,
        }],
        'selected_path_id': 'path-a',
        'selection_rationale': 'highest utility over cost',
        'requires_confirmation': True,
        'status': status,
        'created_at': 1700000001,
    }
    plan.update(overrides)
    return plan


def _learning(**overrides):
    learning = {
        'resource_weight_deltas': [],
        'cost_actual_vs_estimate': {'tokens_ratio': 0.94, 'wall_clock_ratio': 0.92},
        'strategy_outcome': None,
        'capability_gap': None,
    }
    learning.update(overrides)
    return learning


def _opened(store, *, exploratory=False):
    from lib import evolution
    evolution.admit_goal(_goal(exploratory=exploratory,
                               criteria=[] if exploratory else _goal()['criteria']),
                         user_id=USER)
    evolution.save_plan(_plan(), user_id=USER)
    return evolution.open_episode(goal_id='goal-1', plan_id='plan-1',
                                  selected_path_id='path-a', task_id='task-1',
                                  user_id=USER, now=1700000002)


def test_admit_open_settle_roundtrip_validates_against_contracts(store):
    from lib import evolution
    episode = _opened(store)
    assert episode['status'] == 'running'
    settled = evolution.settle_episode(
        episode['episode_id'], user_id=USER, status='success',
        actuals={'tokens': 47000, 'wall_clock_seconds': 1650, 'tool_calls': 55},
        stage_receipts=[{'stage': 'execute', 'status': 'done'}],
        verdict={'outcome': 'success', 'assessed_by': 'checker',
                 'criterion_results': [{'criterion_id': 'c1', 'outcome': 'pass'}]},
        learning=_learning(strategy_outcome={'distilled_template_id': 'tmpl-1',
                                             'anti_pattern': None}),
        now=1700001002)
    assert settled['settled_at'] == 1700001002
    schema = json.loads((CONTRACTS / 'evolution_episode_v1.schema.json')
                        .read_text(encoding='utf-8'))
    jsonschema.validate(evolution.get_episode(episode['episode_id'], user_id=USER),
                        schema)
    assert evolution.get_goal('goal-1', user_id=USER)['status'] == 'admitted'


def test_intake_refuses_invalid_goal_without_writing(store):
    from lib import evolution
    with pytest.raises(evolution.EvolutionContractError):
        evolution.admit_goal(_goal(criteria=[]), user_id=USER)
    assert store.writes == 0


def test_open_requires_confirmed_plan_and_known_candidate(store):
    from lib import evolution
    evolution.admit_goal(_goal(), user_id=USER)
    evolution.save_plan(_plan(status='proposed'), user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.open_episode(goal_id='goal-1', plan_id='plan-1',
                               selected_path_id='path-a', task_id='task-1',
                               user_id=USER)
    evolution.save_plan(_plan(), user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.open_episode(goal_id='goal-1', plan_id='plan-1',
                               selected_path_id='path-missing', task_id='task-1',
                               user_id=USER)


def test_plan_cannot_attach_to_another_owners_goal(store):
    from lib import evolution
    evolution.admit_goal(_goal(), user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.save_plan(_plan(), user_id=99)


def test_exploratory_episode_cannot_mint_strategy_or_capability(store):
    from lib import evolution
    episode = _opened(store, exploratory=True)
    assert episode['exploratory'] is True
    with pytest.raises(evolution.EvolutionStateError):
        evolution.settle_episode(
            episode['episode_id'], user_id=USER, status='failure',
            actuals={'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
            verdict={'outcome': 'failure', 'assessed_by': 'checker',
                     'criterion_results': []},
            learning=_learning(capability_gap={
                'gap_id': 'gap-1', 'summary': 'no tool', 'proposed_action': 'add one'}))
    assert evolution.get_episode(episode['episode_id'], user_id=USER)['status'] == 'running'
    settled = evolution.settle_episode(
        episode['episode_id'], user_id=USER, status='failure',
        actuals={'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
        verdict={'outcome': 'failure', 'assessed_by': 'checker',
                 'criterion_results': []},
        learning=_learning())
    assert settled['status'] == 'failure'


def test_verdict_must_agree_with_terminal_status(store):
    from lib import evolution
    episode = _opened(store)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.settle_episode(
            episode['episode_id'], user_id=USER, status='success',
            actuals={'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 0},
            verdict={'outcome': 'failure', 'assessed_by': 'checker',
                     'criterion_results': []},
            learning=_learning())
    assert evolution.get_episode(episode['episode_id'], user_id=USER)['status'] == 'running'


def test_terminal_episode_never_resettles(store):
    from lib import evolution
    episode = _opened(store)
    evolution.settle_episode(
        episode['episode_id'], user_id=USER, status='aborted',
        actuals={'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 0},
        learning=_learning())
    with pytest.raises(evolution.EvolutionStateError):
        evolution.settle_episode(
            episode['episode_id'], user_id=USER, status='aborted',
            actuals={'tokens': 2, 'wall_clock_seconds': 2, 'tool_calls': 0},
            learning=_learning())


@pytest.mark.parametrize('complete', [False, True])
def test_reconciliation_confirms_exact_terminal_bytes_without_rewriting(store, complete):
    from lib import evolution
    episode = _opened(store)
    arguments = dict(user_id=USER, status='aborted', actuals_complete=complete,
                     actuals={'tokens': 2, 'wall_clock_seconds': 1, 'tool_calls': 0})
    settled = evolution.settle_episode(episode['episode_id'], **arguments, now=1700000010)
    writes = store.writes
    assert evolution.settle_episode(episode['episode_id'], **arguments,
                                    reconcile=True, now=1700000200) == settled
    assert store.writes == writes
    with pytest.raises(evolution.EvolutionStateError, match='already terminal'):
        evolution.settle_episode(episode['episode_id'], **arguments)
    with pytest.raises(evolution.EvolutionStateError, match='unknown episode'):
        evolution.settle_episode(episode['episode_id'], **{**arguments, 'user_id': USER + 1},
                                 reconcile=True)
    with pytest.raises(evolution.EvolutionStateError, match='conflicts'):
        evolution.settle_episode(episode['episode_id'],
                                 **{**arguments, 'actuals_complete': not complete}, reconcile=True)
    assert store.writes == writes


@pytest.mark.parametrize('changed', ['actuals', 'verdict', 'stage_receipts', 'learning'])
def test_reconciliation_rejects_changed_settlement_intent(store, changed):
    from lib import evolution
    episode = _opened(store)
    arguments = dict(user_id=USER, status='success',
                     actuals={'tokens': 2, 'wall_clock_seconds': 1, 'tool_calls': 0},
                     verdict={'outcome': 'success', 'assessed_by': 'checker',
                              'criterion_results': [{'criterion_id': 'c1', 'outcome': 'pass'}]})
    settled = evolution.settle_episode(episode['episode_id'], **arguments)
    proposed = deepcopy(arguments)
    if changed == 'actuals':
        proposed['actuals']['tokens'] += 1
    elif changed == 'verdict':
        proposed['verdict']['criterion_results'][0]['evidence_ref'] = 'Changed evidence'
    elif changed == 'stage_receipts':
        proposed['stage_receipts'] = [{'stage': 'execute', 'status': 'done'}]
    else:
        proposed['learning'] = _learning()
    writes = store.writes
    with pytest.raises(evolution.EvolutionStateError, match='conflicts'):
        evolution.settle_episode(episode['episode_id'], **proposed, reconcile=True)
    assert evolution.get_episode(episode['episode_id'], user_id=USER) == settled
    assert store.writes == writes


def test_transient_write_failure_retries_and_persistent_failure_leaves_running(store):
    from lib import evolution
    episode = _opened(store)
    store.failures_left = 1
    settled = evolution.settle_episode(
        episode['episode_id'], user_id=USER, status='aborted',
        actuals={'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 0},
        learning=_learning())
    assert settled['status'] == 'aborted'

    episode = _opened(store)
    store.permanent_failure = True
    with pytest.raises(RuntimeError):
        evolution.settle_episode(
            episode['episode_id'], user_id=USER, status='aborted',
            actuals={'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 0},
            learning=_learning())
    store.permanent_failure = False
    assert evolution.get_episode(episode['episode_id'], user_id=USER)['status'] == 'running'


def test_episode_listing_is_owner_scoped_and_bounded(store):
    from lib import evolution
    mine = _opened(store)
    evolution.admit_goal(_goal(goal_id='goal-other'), user_id=99)
    assert [e['episode_id'] for e in evolution.list_episodes(user_id=USER)] == [mine['episode_id']]
    assert evolution.list_episodes(user_id=99) == []
    with pytest.raises(Exception):
        evolution.list_episodes(user_id=None)
