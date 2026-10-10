"""Friction miner: struggle without failure becomes frontier-visible."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settle(store, episode_id, task_id, *, status='success'):
    from lib import evolution
    from lib.evolution import records
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': f'g-{episode_id}',
        'statement': 'goal', 'domain': 'code', 'exploratory': True,
        'criteria': [],
        'budget_envelope': {'max_tokens': 100, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 9},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id,
        'goal_id': f'g-{episode_id}', 'plan_id': 'p', 'selected_path_id': 'a',
        'exploratory': True, 'task_id': task_id, 'status': status,
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 3},
        'stage_receipts': [],
        'verdict': {'outcome': status, 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def _call(call_id, name, args):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': args}}


def _task(calls_and_results):
    """Build a task message list from (call_id, name, args, result) rows."""
    messages = []
    for call_id, name, args, result in calls_and_results:
        messages.append({'role': 'assistant',
                         'tool_calls': [_call(call_id, name, args)]})
        messages.append({'role': 'tool', 'tool_call_id': call_id,
                         'content': result})
    return {'messages': messages}


def test_truncation_followed_by_identical_retry_is_mined(store):
    from lib import evolution
    _settle(store, 'e1', 't1')
    task = _task([
        ('c1', 'grep_search', '{"pattern": "foo"}',
         '… (output truncated at 12000 chars or 30 matches)'),
        ('c2', 'grep_search', '{"pattern": "foo"}',
         '… (output truncated at 12000 chars or 30 matches)'),
    ])
    found = evolution.mine_friction(user_id=USER, loader=lambda *a, **k: task)
    assert [f['signal'] for f in found] == ['truncation_retry']
    observations = evolution.list_observations(user_id=USER, kind='friction')
    assert len(observations) == 1
    record = observations[0]
    assert record['subject'] == {'type': 'tool', 'id': 'grep_search'}
    assert record['lineage'] == ['e1', 't1']
    assert record['source'] == {'actor': 'miner'}


def test_identical_call_loop_is_mined(store):
    from lib import evolution
    _settle(store, 'e1', 't1')
    task = _task([
        ('c1', 'read_files', '{"path": "a.py"}', 'content v1'),
        ('c2', 'read_files', '{"path": "a.py"}', 'content v1'),
        ('c3', 'read_files', '{"path": "a.py"}', 'content v1'),
    ])
    found = evolution.mine_friction(user_id=USER, loader=lambda *a, **k: task)
    assert [f['signal'] for f in found] == ['repeat_loop']
    assert '3 times with identical arguments' in found[0]['statement']


def test_clean_task_yields_no_friction(store):
    from lib import evolution
    _settle(store, 'e1', 't1')
    task = _task([
        ('c1', 'grep_search', '{"pattern": "foo"}', 'match 1'),
        ('c2', 'grep_search', '{"pattern": "bar"}', 'match 2'),
    ])
    assert evolution.mine_friction(user_id=USER,
                                   loader=lambda *a, **k: task) == []
    assert evolution.list_observations(user_id=USER) == []


def test_narrowed_retry_is_not_friction(store):
    from lib import evolution
    _settle(store, 'e1', 't1')
    task = _task([
        ('c1', 'grep_search', '{"pattern": "foo"}',
         '… (output truncated at 12000 chars or 30 matches)'),
        ('c2', 'grep_search', '{"pattern": "foo", "max_results": 5}',
         'match 1'),
    ])
    assert evolution.mine_friction(user_id=USER,
                                   loader=lambda *a, **k: task) == []


def test_mining_is_idempotent(store):
    from lib import evolution
    _settle(store, 'e1', 't1')
    task = _task([
        ('c1', 'grep_search', '{"pattern": "foo"}', 'truncated'),
        ('c2', 'grep_search', '{"pattern": "foo"}', 'truncated'),
    ])
    first = evolution.mine_friction(user_id=USER, loader=lambda *a, **k: task)
    second = evolution.mine_friction(user_id=USER, loader=lambda *a, **k: task)
    assert len(first) == len(second) == 1
    assert len(evolution.list_observations(user_id=USER)) == 1


def test_unloadable_task_never_aborts_the_pass(store):
    from lib import evolution
    _settle(store, 'e1', 't-missing')
    _settle(store, 'e2', 't2')

    def loader(task_id, *, user_id):
        if task_id == 't-missing':
            raise RuntimeError('task evicted')
        return _task([('c1', 'grep_search', '{"p": 1}', 'truncated'),
                      ('c2', 'grep_search', '{"p": 1}', 'truncated')])

    found = evolution.mine_friction(user_id=USER, loader=loader)
    assert [f['episode_id'] for f in found] == ['e2']


def test_friction_clusters_surface_in_frontier_digest(store):
    from lib import evolution
    _settle(store, 'e1', 't1')
    _settle(store, 'e2', 't2')
    task = _task([
        ('c1', 'grep_search', '{"pattern": "foo"}', 'truncated'),
        ('c2', 'grep_search', '{"pattern": "foo"}', 'truncated'),
    ])
    evolution.mine_friction(user_id=USER, loader=lambda *a, **k: task)
    digest = evolution.frontier_digest(user_id=USER)
    assert len(digest['friction']) == 1
    cluster = digest['friction'][0]
    assert cluster['kind'] == 'friction'
    assert cluster['subject'] == {'type': 'tool', 'id': 'grep_search'}
    assert cluster['count'] == 2  # two episodes, one signature
