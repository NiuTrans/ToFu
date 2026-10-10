"""Accepted recovery reads bounded authority pages, never full transcripts."""
from unittest.mock import Mock

import pytest

from lib.conversation_sync import repository as sync_repository
from lib.storage.errors import StorageError
from lib.swarm.restart_recovery import read_recovery_turns
from tests.swarm.test_swarm_restart_continue import authority  # noqa: F401

pytestmark = pytest.mark.unit


def install_client(monkeypatch, client):
    repository_type = sync_repository.SidecarConversationSyncRepository
    monkeypatch.setattr(sync_repository, 'SidecarConversationSyncRepository',
        lambda **kwargs: repository_type(client_factory=lambda **kwargs: client))


class HistoryClient:
    def __init__(self, rows, *, capacity=64):
        self.rows = rows
        self.capacity = capacity
        self.sequence = 90
        self.calls = []

    def query(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        assert operation == 'turn.sync.page', 'Full history must not enter recovery'
        assert payload['conversation_id'] == 'conversation'
        assert payload['user_id'] == 7
        assert payload['lane_id'] == 'main'
        if payload['sync_sequence'] != self.sequence:
            return {'stale': True, 'syncSequence': self.sequence}
        if payload['limit'] > self.capacity:
            raise StorageError('database_unavailable', 'database_resource_exhausted', True)
        before = payload.get('before_ordinal', 10**9)
        rows = [row for row in self.rows if row['ordinal'] < before]
        selected = rows[-payload['limit']:]
        return {'turns': selected, 'syncSequence': self.sequence,
                'hasMore': len(rows) > len(selected),
                'nextBeforeOrdinal': selected[0]['ordinal'] if selected else None}


def history(count):
    return [{'turnId': str(n), 'ordinal': n, 'laneId': 'main',
             'status': 'completed', 'projection': {'toolRounds': ['large transcript']}}
            for n in range(1, count + 1)]


def test_continue_reads_one_latest_turn_even_with_long_history(monkeypatch):
    client = HistoryClient(history(5000))
    install_client(monkeypatch, client)
    assert read_recovery_turns('conversation', user_id=7,
        after_ordinal=5000, limit=1) == {'turns': []}
    assert len(client.calls) == 2
    assert all(payload['limit'] == 1 for _, payload in client.calls)
    page = read_recovery_turns('conversation', user_id=7, after_ordinal=4999, limit=1)
    assert [row['ordinal'] for row in page['turns']] == [5000]
    assert 'projection' not in page['turns'][0]


def test_detached_read_shrinks_pages_and_keeps_intervening_stop(monkeypatch):
    rows = history(100)
    rows[80]['status'] = 'cancelled'
    client = HistoryClient(rows, capacity=2)
    install_client(monkeypatch, client)
    page = read_recovery_turns('conversation', user_id=7, after_ordinal=75)
    assert [row['ordinal'] for row in page['turns']] == list(range(76, 101))
    assert any(row['status'] == 'cancelled' for row in page['turns'])
    assert all('projection' not in row for row in page['turns'])
    assert {payload['limit'] for _, payload in client.calls} >= {64, 32, 16, 8, 4, 2}
    assert len(client.calls) <= 20  # Once reduced, later pages keep that capacity.


def test_accepted_continue_keeps_its_lineage_without_full_history(request, monkeypatch):
    from lib.swarm import restart_recovery
    row, task, _, current = request.getfixturevalue('authority')
    task['convId'] = row['conv_id'] = 'conversation'
    client = HistoryClient(history(current['ordinal']))
    install_client(monkeypatch, client)
    monkeypatch.setattr(restart_recovery, 'read_recovery_turns', read_recovery_turns)
    assert restart_recovery.continue_recovery_allowed(row, task, user_id=7)
    assert len(client.calls) == 2
    client.rows.append({**history(1)[0], 'ordinal': current['ordinal'] + 1})
    assert not restart_recovery.continue_recovery_allowed(row, task, user_id=7)


def test_changed_head_discards_earlier_pages_before_resuming(monkeypatch):
    client = HistoryClient(history(100))
    original_query = client.query
    changed = False

    def query(operation, payload):
        nonlocal changed
        if 'before_ordinal' in payload and not changed:
            client.rows[-1]['status'] = 'cancelled'
            client.sequence += 1
            changed = True
        return original_query(operation, payload)

    client.query = query
    install_client(monkeypatch, client)
    page = read_recovery_turns('conversation', user_id=7, after_ordinal=1)
    assert len(page['turns']) == 99
    assert page['turns'][-1]['status'] == 'cancelled'
    assert len({row['ordinal'] for row in page['turns']}) == 99


def test_safety_bound_retains_truncation_evidence(monkeypatch):
    client = HistoryClient(history(2100))
    install_client(monkeypatch, client)
    page = read_recovery_turns('conversation', user_id=7, after_ordinal=1)
    assert len(page['turns']) == 2000


@pytest.mark.parametrize('result', [
    {'turns': 'invalid', 'syncSequence': 0},
    {'turns': history(1), 'syncSequence': 1},
    {'turns': [{'ordinal': True, 'laneId': 'main'}], 'syncSequence': 0},
    {'turns': [{'ordinal': 1, 'laneId': 'foreign'}], 'syncSequence': 0},
    {'turns': history(2)[::-1], 'syncSequence': 0},
    {'turns': history(1), 'syncSequence': 0, 'hasMore': True, 'nextBeforeOrdinal': 9},
])
def test_malformed_pages_never_grant_recovery(monkeypatch, result):
    install_client(monkeypatch, Mock(query=Mock(return_value=result)))
    with pytest.raises(StorageError) as raised:
        read_recovery_turns('conversation', user_id=7)
    assert raised.value.code == 'database_protocol_error'


def test_continuously_changing_head_stays_finite(monkeypatch):
    client = Mock()
    client.query.side_effect = [
        {'stale': True, 'syncSequence': head} for head in range(1, 8)]
    install_client(monkeypatch, client)
    with pytest.raises(StorageError) as raised:
        read_recovery_turns('conversation', user_id=7)
    assert raised.value.code == 'database_busy'
    assert client.query.call_count == 4


def test_irreducible_storage_pressure_preserves_typed_failure(monkeypatch):
    error = StorageError('database_unavailable', 'database_resource_exhausted', True)
    client = Mock(query=Mock(side_effect=error))
    install_client(monkeypatch, client)
    with pytest.raises(StorageError) as raised:
        read_recovery_turns('conversation', user_id=7, limit=1)
    assert raised.value is error
    assert client.query.call_count == 1


def test_missing_conversation_is_not_evidence_of_no_later_turn(monkeypatch):
    install_client(monkeypatch, Mock(query=Mock(return_value=None)))
    with pytest.raises(StorageError) as raised:
        read_recovery_turns('conversation', user_id=7, after_ordinal=50, limit=1)
    assert raised.value.code == 'database_not_found'
