"""Restart recovery reads retained swarm sessions through bounded storage reads.

The 2026-10-26 restart lost every resumable swarm: one startup query carried
all retained sessions with every checkpoint, exceeded the 64 MiB stream bound,
was retried as transient load shedding and was then reported as "no sessions".
"""
from __future__ import annotations

import pytest

from lib.storage.errors import StorageError
from lib.swarm import persistence
from lib.swarm.integration import _rehydrate

pytestmark = pytest.mark.unit


def _agent(agent_id: str) -> dict:
    return {'agent_id': agent_id, 'role': 'coder', 'objective': agent_id,
            'status': 'running', 'messages': [{'role': 'user', 'content': agent_id}],
            'result': {}, 'rounds_used': 1, 'delivered': False, 'updated_at': 5}


def _header(swarm_key: str) -> dict:
    return {'swarm_key': swarm_key, 'conv_id': swarm_key, 'task_id': f'task-{swarm_key}',
            'status': 'running', 'specs': [{'id': 'a1'}], 'config': {'user_id': 7},
            'created_at': 1, 'updated_at': 2}


class _PagedStorage:
    """Storage double that speaks only the bounded swarm read protocol."""

    def __init__(self, sessions: dict[str, list[dict]], *, page_size: int = 1):
        self.sessions = sessions
        self.page_size = page_size
        self.failures: dict[tuple[str, str], Exception] = {}
        self.calls: list[tuple[str, dict]] = []

    def query(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        key = payload.get('swarm_key', '')
        if (operation, key) in self.failures:
            raise self.failures[(operation, key)]
        if operation == 'swarm.resumable.list':
            assert payload == {}
            return [{'swarm_key': swarm_key, 'updated_at': 2} for swarm_key in self.sessions]
        if operation == 'swarm.session.get':
            assert payload == {'swarm_key': key, 'include_agents': False}
            return _header(key) if key in self.sessions else None
        if operation == 'swarm.agents.page':
            after = payload.get('after_agent_id', '')
            remaining = [agent for agent in self.sessions.get(key, []) if agent['agent_id'] > after]
            page = remaining[:self.page_size]
            return {'agents': page,
                    'next_after_agent_id': page[-1]['agent_id'] if len(remaining) > len(page) else None}
        raise AssertionError(f'unbounded swarm read {operation}')


@pytest.fixture
def storage(monkeypatch):
    double = _PagedStorage({})
    monkeypatch.setattr(persistence, '_storage', lambda **_kwargs: double)
    return double


@pytest.fixture
def live(monkeypatch):
    sessions: dict[str, dict] = {}
    resumed: list[dict] = []

    def rehydrate(session):
        resumed.append(session)
        sessions[session['swarm_key']] = session
        return True

    monkeypatch.setattr(_rehydrate, '_get_session', sessions.get)
    monkeypatch.setattr(_rehydrate, '_rehydrate_one', rehydrate)
    return resumed


def test_session_larger_than_one_response_is_assembled_from_agent_pages(storage):
    storage.sessions['conv'] = [_agent('a1'), _agent('a2'), _agent('a3')]
    storage.page_size = 2

    session = persistence.load_session('conv', required=True)

    assert session == {**_header('conv'),
                       'agents': [_agent('a1'), _agent('a2'), _agent('a3')]}
    assert [call for call in storage.calls if call[0] == 'swarm.agents.page'] == [
        ('swarm.agents.page', {'swarm_key': 'conv'}),
        ('swarm.agents.page', {'swarm_key': 'conv', 'after_agent_id': 'a2'}),
    ]
    assert persistence.load_session('missing') is None


def test_page_without_progress_is_a_protocol_failure_not_a_loop(storage, monkeypatch):
    storage.sessions['conv'] = [_agent('a1')]
    bounded_read = storage.query

    def stalled(operation, payload):
        if operation != 'swarm.agents.page':
            return bounded_read(operation, payload)
        storage.calls.append((operation, dict(payload)))
        return {'agents': [], 'next_after_agent_id': 'a1'}

    monkeypatch.setattr(storage, 'query', stalled)

    with pytest.raises(StorageError) as failure:
        persistence.load_session('conv', required=True)

    assert failure.value.code == 'database_protocol_error'
    assert len([call for call in storage.calls if call[0] == 'swarm.agents.page']) == 1


def test_startup_resumes_every_loadable_session_independently(storage, live):
    storage.sessions.update({'broken': [_agent('a1')], 'healthy': [_agent('a1')]})
    storage.failures[('swarm.session.get', 'broken')] = StorageError(
        'database_integrity', 'swarm resumable index has no session')

    assert _rehydrate.rehydrate_swarms_on_startup() == 1

    assert live == [{**_header('healthy'), 'agents': [_agent('a1')]}]


def test_startup_reports_unreadable_resumable_index_instead_of_zero_sessions(storage, live):
    unavailable = StorageError('database_unavailable', 'database_resource_exhausted', True, 500)
    storage.failures[('swarm.resumable.list', '')] = unavailable

    with pytest.raises(StorageError) as failure:
        _rehydrate.rehydrate_swarms_on_startup()

    assert failure.value is unavailable
    assert live == []


def test_transient_session_failure_requests_a_retry_after_resuming_the_rest(storage, live):
    storage.sessions.update({'flaky': [_agent('a1')], 'healthy': [_agent('a1')]})
    unavailable = StorageError('database_unavailable', 'authority restarting', True, 10)
    storage.failures[('swarm.agents.page', 'flaky')] = unavailable

    with pytest.raises(StorageError) as failure:
        _rehydrate.rehydrate_swarms_on_startup()
    assert failure.value is unavailable
    assert [session['swarm_key'] for session in live] == ['healthy']

    del storage.failures[('swarm.agents.page', 'flaky')]
    assert _rehydrate.rehydrate_swarms_on_startup() == 1
    assert [session['swarm_key'] for session in live] == ['healthy', 'flaky']
