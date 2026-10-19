"""Retained listings use authority headers, never a possibly stale roster alone."""
from __future__ import annotations

import copy

import pytest

from lib.storage.errors import StorageError
from lib.swarm.retained_repository import (
    INDEX_NAMESPACE, NAMESPACE, RetainedAgentError, RetainedAgentRepository, scope_for,
)
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit


class HeaderRecordClient(RecordClient):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.fail_projection = False

    def query(self, operation, payload):
        self.reads.append((operation, copy.deepcopy(payload)))
        result = super().query(operation, payload)
        if operation == 'record.list' and payload.get('include_value') is False:
            return [{key: value for key, value in row.items() if key != 'value'}
                    for row in result]
        return result

    def command(self, operation, payload, command_id):
        if self.fail_projection and payload['namespace'] == INDEX_NAMESPACE:
            raise StorageError('database_unavailable', 'projection unavailable', retryable=False)
        return super().command(operation, payload, command_id)


@pytest.fixture
def owned():
    client = HeaderRecordClient()
    repository = RetainedAgentRepository(7, client=lambda **kwargs: client)
    return client, repository, scope_for(project_path='/workspace/repair')


def test_missing_create_projection_is_discovered_from_authority(owned):
    client, repository, scope = owned
    client.fail_projection = True
    with pytest.raises(StorageError):
        repository.create(scope, role='coder', name='Committed before crash')
    client.fail_projection = False
    # A new process has only the selected store, no in-memory dirty marker.
    restarted = RetainedAgentRepository(7, client=lambda **kwargs: client)
    listed = restarted.list(scope)
    assert [row['name'] for row in listed] == ['Committed before crash']
    key = repository._key(scope, listed[0]['id'])
    assert client.rows[INDEX_NAMESPACE, key]['value']['source_version'] == 1


def test_stale_update_projection_repairs_from_exact_current_identity(owned):
    client, repository, scope = owned
    agent = repository.create(scope, role='coder', name='Before')
    client.fail_projection = True
    with pytest.raises(StorageError):
        repository.change(scope, agent['id'], lambda current: {**current, 'name': 'After'})
    client.fail_projection = False
    assert repository.get(scope, agent['id'])[0]['name'] == 'After'
    listed = repository.list(scope)
    assert [row['name'] for row in listed] == ['After']
    assert listed[0]['source_version'] == 2


def test_deleted_authority_cannot_reappear_from_a_leftover_index(owned):
    client, repository, scope = owned
    agent = repository.create(scope, role='coder', name='Deleted')
    client.fail_projection = True
    with pytest.raises(StorageError):
        repository.delete(scope, agent['id'])
    client.fail_projection = False
    assert repository.get(scope, agent['id']) is None
    assert repository.list(scope) == []


def test_current_roster_does_not_hydrate_dialogue(owned):
    client, repository, scope = owned
    agent = repository.create(scope, role='coder', name='Large memory',
                              messages=[{'role': 'tool', 'content': 'x' * (512 * 1024)}])
    client.reads.clear()
    assert repository.list(scope)[0]['id'] == agent['id']
    authority_reads = [(operation, payload) for operation, payload in client.reads
                       if payload['namespace'] == NAMESPACE]
    assert authority_reads
    assert all(operation == 'record.list' and payload.get('include_value') is False
               for operation, payload in authority_reads)


def test_failed_optional_repair_returns_exact_authority_not_stale_metadata(owned):
    client, repository, scope = owned
    agent = repository.create(scope, role='coder', name='Before')
    client.fail_projection = True
    with pytest.raises(StorageError):
        repository.change(scope, agent['id'], lambda current: {**current, 'name': 'After'})
    assert repository.list(scope)[0]['name'] == 'After'
    client.fail_projection = False
    assert repository.list(scope)[0]['source_version'] == 2


def test_projection_cas_contention_cannot_reject_an_authoritative_list(owned, monkeypatch):
    client, repository, scope = owned
    agent = repository.create(scope, role='coder', name='Before')
    key = repository._key(scope, agent['id'])
    client.rows[NAMESPACE, key]['value']['name'] = 'After'
    client.rows[NAMESPACE, key]['version'] += 1
    command = client.command

    def contended(operation, payload, command_id):
        if payload['namespace'] == INDEX_NAMESPACE:
            raise StorageError('database_conflict', 'competing cache writer')
        return command(operation, payload, command_id)

    monkeypatch.setattr(client, 'command', contended)
    assert repository.list(scope)[0]['name'] == 'After'
    monkeypatch.setattr(client, 'command', command)
    assert repository.list(scope)[0]['source_version'] == 2


def test_repair_cannot_cross_owner_tenant_or_scope(owned):
    client, repository, scope = owned
    agent = repository.create(scope, role='coder', name='Owned')
    key = repository._key(scope, agent['id'])
    client.rows[INDEX_NAMESPACE, key]['value']['user_id'] = 8
    with pytest.raises(RetainedAgentError):
        repository.list(scope)
    assert RetainedAgentRepository(8, client=lambda **kwargs: client).list(scope) == []
    assert RetainedAgentRepository(7, tenant_id='other', client=lambda **kwargs: client).list(scope) == []
    assert repository.list(scope_for(conversation_id='other')) == []
