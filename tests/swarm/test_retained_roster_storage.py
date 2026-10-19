"""Real selected-authority writes remain discoverable across roster publication loss."""
import json

import pytest

from lib.storage import get_storage_client
from lib.storage.errors import StorageError
from lib.swarm.retained_repository import NAMESPACE, RetainedAgentRepository, scope_for

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def test_committed_identity_survives_lost_roster_publication(chat_sidecar, monkeypatch):
    scope = scope_for(conversation_id='roster-crash-window', requested='conversation')
    writer = RetainedAgentRepository(1)
    original_sync = writer._sync_index

    def unavailable(*args, **kwargs):
        raise StorageError('database_unavailable', 'projection unavailable', retryable=False)

    monkeypatch.setattr(writer, '_sync_index', unavailable)
    with pytest.raises(StorageError):
        writer.create(scope, role='coder', name='Durable before projection')
    restarted = RetainedAgentRepository(1)
    created = restarted.list(scope)
    assert [row['name'] for row in created] == ['Durable before projection']
    agent_id = created[0]['id']
    with pytest.raises(StorageError):
        writer.change(scope, agent_id, lambda current: {**current, 'name': 'Committed update'})
    assert restarted.list(scope)[0]['name'] == 'Committed update'
    assert RetainedAgentRepository(2).list(scope) == []
    monkeypatch.setattr(writer, '_sync_index', original_sync)
    writer.delete(scope, agent_id)
    assert restarted.list(scope) == []


def test_header_enumeration_does_not_transfer_retained_dialogue(chat_sidecar):
    repository = RetainedAgentRepository(1)
    scope = scope_for(conversation_id='large-roster-header', requested='conversation')
    dialogue = 'x' * (9 * 1024 * 1024)
    created = repository.create(scope, role='coder', name='Large memory',
                                messages=[{'role': 'assistant', 'content': dialogue}])
    headers = get_storage_client().query('record.list', {
        'namespace': NAMESPACE, 'prefix': repository._prefix(scope),
        'limit': 1000, 'include_value': False,
    })
    encoded_bytes = len(json.dumps(headers).encode())
    assert len(headers) == 1 and set(headers[0]) == {'key', 'version', 'updated_at_ms'}
    assert encoded_bytes < 512
    listed = RetainedAgentRepository(1).list(scope)
    assert listed[0]['id'] == created['id'] and listed[0]['message_count'] == 1
    assert 'messages' not in listed[0]
    print(f'roster authority metadata={encoded_bytes} bytes; retained dialogue={len(dialogue)} bytes')
