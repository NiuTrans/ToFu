"""Research business receipts require exact selected-authority write confirmation."""
from copy import deepcopy
import pytest
from lib.research import records, runtime
from lib.storage.errors import StorageError
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.fixture
def research_task(tmp_path, monkeypatch, storage):
    supervisor, _ = storage
    monkeypatch.setattr(records, '_storage', lambda **kwargs: supervisor.client)
    task = runtime._new_research_task(runtime._research_task_id(), user_id=12,
        direction='Acknowledged progress', workdir=str(tmp_path), lang='en')
    try:
        yield task, supervisor.client
    finally:
        runtime._research_runtime.remove_owned(task['task_id'], user_id=12)


@pytest.mark.parametrize('namespace', [records.NAMESPACE, records.INDEX_NAMESPACE])
def test_success_shaped_ack_without_write_cannot_advance_checkpoint(research_task, monkeypatch, namespace):
    task, client = research_task
    prior = records.persist_record(task)
    command = client.command
    def refuse(name, args, command_id):
        if args['namespace'] == namespace:
            return {'version': 999}
        return command(name, args, command_id)
    monkeypatch.setattr(client, 'command', refuse)
    task['status'] = 'running'
    with pytest.raises(StorageError, match='not exactly acknowledged'):
        records.persist_record(task)
    assert task['_durable_research_record'] == prior


@pytest.mark.parametrize('namespace', [records.NAMESPACE, records.INDEX_NAMESPACE])
def test_lost_ack_after_commit_recovers_without_rewriting(research_task, monkeypatch, namespace):
    task, client = research_task
    command = client.command
    calls = []
    def lost_ack(name, args, command_id):
        result = command(name, args, command_id)
        calls.append(args['namespace'])
        if args['namespace'] == namespace:
            raise StorageError('database_unavailable', 'response lost after durable commit')
        return result
    monkeypatch.setattr(client, 'command', lost_ack)
    saved = records.persist_record(task)
    assert task['_durable_research_record'] == saved
    assert calls == [records.NAMESPACE, records.INDEX_NAMESPACE]
    assert records.load_record(task['task_id'], user_id=12, internal=True) == saved


def test_readback_distinguishes_boolean_from_numeric_value(research_task, monkeypatch):
    task, client = research_task
    query = client.query
    def altered(name, args):
        row = query(name, args)
        if name == 'record.get' and args['namespace'] == records.NAMESPACE and row:
            row = deepcopy(row)
            row['value']['schema_version'] = True
        return row
    monkeypatch.setattr(client, 'query', altered)
    with pytest.raises(StorageError, match='not exactly acknowledged'):
        records.persist_record(task)
    assert '_durable_research_record' not in task


@pytest.mark.parametrize('reason', ['execution_token_budget_exhausted', 'execution_deadline_exceeded'])
def test_terminal_limit_reason_survives_registry_loss_in_record_and_index(research_task, monkeypatch, reason):
    from lib.evolution import records as record_store
    task, client = research_task
    monkeypatch.setattr(record_store, '_storage', lambda **kwargs: client)
    task.update(status='error', error={'message': reason}, _executionLimitReason=reason)
    records.persist_record(task)
    runtime._research_runtime.remove_owned(task['task_id'], user_id=12)
    detail = records.load_record(task['task_id'], user_id=12)
    summary = next(row for row in records.list_records(user_id=12) if row['task_id'] == task['task_id'])
    assert detail['execution_limit_reason'] == summary['execution_limit_reason'] == reason
    assert detail['status'] == summary['status'] == 'error'
    assert detail['error'] == summary['error'] == reason


@pytest.mark.parametrize('reason', [True, '', 'x' * 81])
def test_invalid_execution_limit_reason_cannot_replace_snapshot(research_task, reason):
    task, _ = research_task
    before = records.persist_record(task)
    task['_executionLimitReason'] = reason
    with pytest.raises(ValueError, match='limit reason'):
        records.persist_record(task)
    assert records.load_record(task['task_id'], user_id=12, internal=True) == before
