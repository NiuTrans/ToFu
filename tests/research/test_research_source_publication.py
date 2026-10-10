"""Workspace publication, not prepared nodes, authorizes source consumption."""
from copy import deepcopy

import pytest

from lib.storage.errors import StorageError
from lib.research.workspace import save_workspace, load_workspace
from lib.research.source_index import ReceiptSourceIndex, NAMESPACE, empty_reference
from lib.research.persistence import research_direction_hash
from tests.research.test_research_history import draft_with_runs, DIRECTION
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def draft():
    value = draft_with_runs(1)
    value['runs'][0]['tool_receipts'][0].update(source_task_id='source-task', call_id='call')
    return value


def repository(client):
    return ReceiptSourceIndex(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en')


def test_refused_workspace_cas_leaves_prepared_sources_unconsumed(storage, monkeypatch):
    supervisor, _ = storage; client = supervisor.client; original = client.command
    def command(operation, payload, key):
        if operation == 'research.workspace.put':
            raise StorageError('database_unavailable', 'injected refusal before publication')
        return original(operation, payload, key)
    monkeypatch.setattr(client, 'command', command)
    with pytest.raises(StorageError): save_workspace(DIRECTION, 'en', draft(), expected_revision=0, user_id=1)
    repo = repository(client)
    assert client.query('record.list', {'namespace': NAMESPACE, 'prefix': repo.prefix, 'limit': 2})
    current = load_workspace(DIRECTION, 'en', user_id=1)
    reference = current.get('history', {}).get('source_index', empty_reference())
    assert not repo.replay(reference, draft()['runs'][0]['tool_receipts'])
    monkeypatch.setattr(client, 'command', original)
    saved = save_workspace(DIRECTION, 'en', draft(), expected_revision=0, user_id=1)
    assert saved['history']['source_index']['count'] == 1


@pytest.mark.parametrize('ack', ['lost', 'wrong-document', 'wrong-revision'])
def test_committed_source_root_requires_exact_ack_or_readback(storage, monkeypatch, ack):
    supervisor, _ = storage; client = supervisor.client; original = client.command
    def command(operation, payload, key):
        result = original(operation, payload, key)
        if operation == 'research.workspace.put':
            if ack == 'lost': raise StorageError('database_timeout', 'injected lost publication ACK')
            result = deepcopy(result)
            if ack == 'wrong-document': result['workspace']['history']['source_index'] = empty_reference()
            else: result['revision'] = 999
        return result
    monkeypatch.setattr(client, 'command', command)
    saved = save_workspace(DIRECTION, 'en', draft(), expected_revision=0, user_id=1)
    assert saved['revision'] == 1
    assert saved['history']['source_index']['count'] == 1
    assert saved == load_workspace(DIRECTION, 'en', user_id=1)


@pytest.mark.parametrize('result', [None, {'revision': 1, 'workspace': {}}, {'revision': True, 'workspace': {}}])
def test_malformed_ack_without_publication_cannot_return_a_workspace(storage, monkeypatch, result):
    supervisor, _ = storage; client = supervisor.client; original = client.command
    def command(operation, payload, key):
        if operation == 'research.workspace.put': return deepcopy(result)
        return original(operation, payload, key)
    monkeypatch.setattr(client, 'command', command)
    with pytest.raises(StorageError, match='not exactly acknowledged'):
        save_workspace(DIRECTION, 'en', draft(), expected_revision=0, user_id=1)
    assert load_workspace(DIRECTION, 'en', user_id=1)['revision'] == 0
