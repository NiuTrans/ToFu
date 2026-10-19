"""Maintenance reuses one owner-bound episode view without masking failures."""
from contextlib import contextmanager
from copy import deepcopy
import pytest
from lib.evolution import episodes, maintenance, records, goals
from lib.evolution.contracts import EvolutionStateError
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_system import _settle, USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def track_reads(store):
    query = store.query
    reads = []
    def observed(operation, payload):
        if operation == 'record.list' and payload['namespace'] == records.NAMESPACE_EPISODES:
            reads.append(deepcopy(payload))
        return query(operation, payload)
    store.query = observed
    return reads



def track_view_loads(monkeypatch):
    reader = episodes.recent_settled_episodes
    loads = []
    def observed(**kwargs):
        loads.append(kwargs)
        return reader(**kwargs)
    monkeypatch.setattr(episodes, 'recent_settled_episodes', observed)
    return loads


def test_view_is_lazy_isolated_and_fresh_for_the_next_cycle(store):
    _settle(store, 'g1', 'e1', 'success', tokens=123)
    reads = track_reads(store)
    with episodes.episode_read_window(user_id=USER) as reader:
        assert reads == []
        first = reader(user_id=USER, limit=200)
        first[0]['actuals']['tokens'] = 999
        _settle(store, 'g2', 'e2', 'failure')
        second = reader(user_id=USER, limit=200)
        assert len(second) == 1 and second[0]['actuals']['tokens'] == 123
        assert len(reads) == 1 and reads[0]['prefix'] == f'{USER}:'
        with pytest.raises(EvolutionStateError):reader(user_id=USER+1, limit=200)
    assert reader.closed and reader.rows is None
    with pytest.raises(EvolutionStateError):reader(user_id=USER, limit=200)
    with episodes.episode_read_window(user_id=USER) as fresh:
        assert len(fresh(user_id=USER, limit=200)) == 2
    assert len(reads) == 2


def test_nested_owner_views_cannot_replace_each_other(store):
    _settle(store, 'g1', 'e1', 'success', tokens=123)
    own = deepcopy(store.rows[records.NAMESPACE_EPISODES, f'{USER}:e1'])
    own['actuals']['tokens'] = 777
    store.rows[records.NAMESPACE_EPISODES, f'{USER+1}:e1'] = own
    with episodes.episode_read_window(user_id=USER) as outer:
        assert outer(user_id=USER, limit=200)[0]['actuals']['tokens'] == 123
        with episodes.episode_read_window(user_id=USER+1) as inner:
            assert inner(user_id=USER+1, limit=200)[0]['actuals']['tokens'] == 777
        assert outer(user_id=USER, limit=200)[0]['actuals']['tokens'] == 123


def test_exception_releases_retained_view(store):
    _settle(store, 'g1', 'e1', 'success')
    with pytest.raises(RuntimeError, match='cancelled'):
        with episodes.episode_read_window(user_id=USER) as reader:
            reader(user_id=USER, limit=200)
            raise RuntimeError('cancelled')
    assert reader.closed and reader.rows is None


@pytest.mark.parametrize('malformed', [False, True])
def test_failed_load_is_not_shared_as_empty_evidence(store, malformed, monkeypatch):
    loads = track_view_loads(monkeypatch)
    _settle(store, 'g1', 'e1', 'success')
    query = store.query
    attempts = []
    def flaky(operation, payload):
        if operation == 'record.list' and payload['namespace'] == records.NAMESPACE_EPISODES:
            attempts.append(payload)
            if len(attempts) == 1:
                if malformed:return None
                raise StorageError('database_integrity', 'injected first-read refusal')
        return query(operation, payload)
    store.query = flaky
    report = maintenance.run_maintenance_cycle(user_id=USER, include_refetch=False)
    assert not report['ok'] and not report['steps']['priors']['ok']
    assert report['steps']['hypotheses']['ok'] and report['steps']['monitoring']['ok']
    assert len(loads) == 2


def test_complete_cycle_preserves_results_with_one_episode_read(store, monkeypatch, record_property):
    for index, outcome in enumerate(('success','failure','success')):
        _settle(store, f'g{index}', f'e{index}', outcome)
    original_rows, original_versions = deepcopy(store.rows), deepcopy(store.versions)
    reads = track_view_loads(monkeypatch)
    actual = maintenance.run_maintenance_cycle(user_id=USER, include_refetch=False)
    assert actual['ok']
    cached_reads = len(reads)
    assert cached_reads == 1
    store.rows, store.versions = deepcopy(original_rows), deepcopy(original_versions)
    reads.clear()
    class FreshReader:
        def __call__(self, *, user_id, limit):
            return episodes.recent_settled_episodes(user_id=user_id, limit=limit)
        def read_goal(self, goal_id, *, user_id):
            return goals.get_goal(goal_id, user_id=user_id)
    @contextmanager
    def uncached(*, user_id):
        yield FreshReader()
    monkeypatch.setattr(maintenance, 'episode_read_window', uncached)
    baseline = maintenance.run_maintenance_cycle(user_id=USER, include_refetch=False)
    assert baseline['ok'] and baseline['steps'] == actual['steps']
    assert len(reads) > cached_reads
    record_property('uncached_episode_list_rpcs', len(reads))
    record_property('shared_episode_list_rpcs', cached_reads)


def test_selected_store_restart_keeps_cycle_view_and_next_view_is_fresh(storage, store, monkeypatch):
    _settle(store, 'g1', 'e1', 'success', tokens=123)
    original = deepcopy(store.rows[records.NAMESPACE_EPISODES, f'{USER}:e1'])
    supervisor, _ = storage
    monkeypatch.setattr(records, '_storage', lambda **kwargs: supervisor.client)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:e1', original, kind='episode')
    with episodes.episode_read_window(user_id=USER) as reader:
        assert [row['episode_id'] for row in reader(user_id=USER, limit=200)] == ['e1']
        supervisor.stop();supervisor.start()
        second = {**deepcopy(original), 'episode_id': 'e2',
            'settled_at': original['settled_at'] + 1, 'updated_at': original['settled_at'] + 1}
        records.put_record(records.NAMESPACE_EPISODES, f'{USER}:e2', second, kind='episode')
        assert [row['episode_id'] for row in reader(user_id=USER, limit=200)] == ['e1']
    with episodes.episode_read_window(user_id=USER) as fresh:
        assert [row['episode_id'] for row in fresh(user_id=USER, limit=200)] == ['e2', 'e1']
