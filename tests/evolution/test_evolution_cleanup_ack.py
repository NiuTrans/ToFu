"""Working-context deletion survives transient/lost ACKs without losing originals."""
import pytest
from lib.evolution import records, stage_inputs
from lib.storage.errors import StorageError
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['before_commit', 'after_commit', 'false_ack', 'permanent', 'exhausted'])
def test_working_delete_requires_absence_and_bounded_transient_retry(storage, monkeypatch, failure):
    supervisor, _ = storage
    client = supervisor.client
    monkeypatch.setattr(records, '_storage', lambda **kwargs: client)
    delays = []
    monkeypatch.setattr(records, '_sleep', delays.append)
    namespace = stage_inputs.WORKING_PARTS_NAMESPACE
    key = '12:cleanup-owned-frame'
    client.command('record.put', {'namespace': namespace, 'key': key, 'value': {'frame': 'temporary'}}, 'seed-frame')
    original_namespace = stage_inputs.NAMESPACE
    client.command('record.put', {'namespace': original_namespace, 'key': key, 'value': {'original': 'retain'}}, 'seed-original')
    command = client.command
    attempts = []
    def injected(name, args, command_id):
        if name != 'record.delete':
            return command(name, args, command_id)
        attempts.append(command_id)
        if failure == 'false_ack':
            return {'deleted': True}
        if failure == 'exhausted':
            raise StorageError('database_busy', 'persistent transient', retryable=True)
        if failure == 'permanent':
            raise StorageError('database_integrity', 'durable refusal')
        if len(attempts) == 1:
            if failure == 'after_commit':
                command(name, args, command_id)
            raise StorageError('database_busy', 'injected transient', retryable=True)
        return command(name, args, command_id)
    monkeypatch.setattr(client, 'command', injected)
    if failure in ('false_ack', 'permanent', 'exhausted'):
        with pytest.raises((StorageError, stage_inputs.EvolutionStateError)):
            stage_inputs._delete_working_record(namespace, key)
        assert records.get_record(namespace, key) == {'frame': 'temporary'}
        assert len(attempts) == (3 if failure == 'exhausted' else 1)
        assert delays == ([1.0, 2.0] if failure == 'exhausted' else [])
    else:
        stage_inputs._delete_working_record(namespace, key)
        assert records.get_record(namespace, key) is None
        assert len(attempts) == (2 if failure == 'before_commit' else 1)
        assert len(set(attempts)) == 1
        assert delays == ([1.0] if failure == 'before_commit' else [])
    assert records.get_record(original_namespace, key) == {'original': 'retain'}
