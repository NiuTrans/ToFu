"""Macro crystallization: recurrence in winning tasks becomes gate evidence."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _success_episode(store, episode_id, task_id):
    from lib import evolution
    from lib.evolution import records
    if evolution.get_goal('g', user_id=USER) is None:
        evolution.admit_goal({
            'schema_version': 1, 'goal_id': 'g', 'statement': 'goal',
            'domain': 'code', 'exploratory': False,
            'criteria': [{'criterion_id': 'c', 'kind': 'test_command',
                          'params': {'command': 'true'}}],
            'budget_envelope': {'max_tokens': 10,
                                'max_wall_clock_seconds': 10,
                                'max_tool_calls': 10},
            'status': 'draft', 'created_at': 1}, user_id=USER)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': 'g',
        'plan_id': 'p', 'selected_path_id': 'a', 'exploratory': False,
        'task_id': task_id, 'status': 'success',
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 1},
        'verdict': {'outcome': 'success', 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def _tasks(*sequences):
    return {f'task-{i}': {'toolRounds': [{'tool_calls': [
        {'name': name} for name in seq]}]} for i, seq in enumerate(sequences)}


def test_repeated_winning_sequence_crystallizes_once(store):
    from lib import evolution
    _success_episode(store, 'e1', 'task-0')
    _success_episode(store, 'e2', 'task-1')
    _success_episode(store, 'e3', 'task-2')  # one-off sequence: ignored
    tasks = _tasks(('search', 'fetch', 'summarize'),
                   ('search', 'fetch', 'summarize'),
                   ('search',))
    loader = lambda task_id, *, user_id: tasks.get(task_id)
    proposals = evolution.propose_macro_crystallizations(
        user_id=USER, loader=loader, now=100)
    assert len(proposals) == 1
    macro = proposals[0]
    assert macro['kind'] == 'macro_tool'
    assert macro['payload']['sequence'] == ['search', 'fetch', 'summarize']
    assert set(macro['evidence_episode_ids']) == {'e1', 'e2'}
    again = evolution.propose_macro_crystallizations(
        user_id=USER, loader=loader, now=101)
    assert again == []  # already pending, never re-proposed


def test_failures_and_missing_tasks_contribute_nothing(store):
    from lib import evolution
    from lib.evolution import records
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:e9', {
        'schema_version': 1, 'episode_id': 'e9', 'goal_id': 'g',
        'plan_id': 'p', 'selected_path_id': 'a', 'exploratory': False,
        'task_id': 'task-x', 'status': 'failure',
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 1},
        'verdict': {'outcome': 'failure', 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')
    loader = lambda task_id, *, user_id: {
        'toolRounds': [{'tool_calls': [{'name': 'a'}, {'name': 'b'}]}]}
    assert evolution.propose_macro_crystallizations(
        user_id=USER, loader=loader) == []
