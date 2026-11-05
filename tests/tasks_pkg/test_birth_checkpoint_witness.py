"""A just-minted task id needs no durable-row witness read at birth.

``create_task`` mints a uuid4 task id that no durable row can carry yet, so
the birth checkpoint's compare-and-swap expectation is version 0 by
construction. ``task_results.checkpoint`` (SQL authority and native TofuDB
alike) writes version 1 when no row exists and ``expected_version`` is 0,
and reports ``database_conflict`` otherwise; the conflict path then reads the
witness and retries. A fresh registration therefore issues no
``task_results.replay_get``, a concurrent writer is still recovered through
the conflict, and a prepared (restorable) task id keeps its witness read.
"""
from __future__ import annotations

import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


class _RecordingClient:
    """The real storage client, recording operation names in call order."""

    def __init__(self, client, *, before_first_checkpoint=None):
        self._client = client
        self._before_first_checkpoint = before_first_checkpoint
        self.operations = []

    def query(self, operation, payload, **options):
        self.operations.append(operation)
        return self._client.query(operation, payload, **options)

    def command(self, operation, payload, command_id, **options):
        self.operations.append(operation)
        if operation == 'task_results.checkpoint' and self._before_first_checkpoint:
            hook, self._before_first_checkpoint = self._before_first_checkpoint, None
            hook(self._client, payload)
        return self._client.command(operation, payload, command_id, **options)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def conversation():
    from tests._seed import delete_conversation, seed_conversation
    conv_id = f'birth-witness-{uuid.uuid4().hex[:10]}'
    seed_conversation(conv_id, messages=[
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': 'hello', 'timestamp': 2},
    ], title='birth witness')
    created = []
    yield conv_id, created
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import discard_task
    for task_id in created:
        discard_task(task_id, conv_id)
        get_storage_client(write=True).command(
            'record.delete', {'namespace': 'task_results', 'key': task_id},
            f'test-delete-task-result:{task_id}')
    delete_conversation(conv_id)


def _record(monkeypatch, **hooks):
    import lib.storage as storage
    recorder = _RecordingClient(storage.get_storage_client(write=True), **hooks)
    monkeypatch.setattr(storage, 'get_storage_client', lambda **_options: recorder)
    return recorder


def _task_result_operations(recorder):
    return [op for op in recorder.operations if op.startswith('task_results.')]


def _row(task_id):
    from lib.storage import get_storage_client
    return get_storage_client().query(
        'record.get', {'namespace': 'task_results', 'key': task_id})


def _create(conv_id, created):
    from lib.tasks_pkg.manager import create_task
    task = create_task(conv_id, [{'role': 'user', 'content': 'hi'}],
                       {'model': 'birth-model', 'projectEnabled': False},
                       user_id=1, supersede=False)
    created.append(task['id'])
    return task


def test_fresh_registration_writes_its_birth_row_without_a_witness_read(
        conversation, monkeypatch):
    conv_id, created = conversation
    recorder = _record(monkeypatch)
    task = _create(conv_id, created)

    assert _task_result_operations(recorder) == ['task_results.checkpoint']
    row = _row(task['id'])
    assert row['version'] == 1
    assert row['value']['status'] == 'pending' and row['value']['conv_id'] == conv_id


def test_concurrent_writer_at_birth_is_recovered_through_the_cas_conflict(
        conversation, monkeypatch):
    conv_id, created = conversation

    def concurrent_writer(client, payload):
        client.command('task_results.checkpoint', {
            **payload, 'value': {**payload['value'], 'content': 'concurrent writer'},
        }, f'test-concurrent-birth:{payload["key"]}')

    recorder = _record(monkeypatch, before_first_checkpoint=concurrent_writer)
    task = _create(conv_id, created)

    assert _task_result_operations(recorder) == [
        'task_results.checkpoint', 'task_results.replay_get',
        'task_results.checkpoint']
    row = _row(task['id'])
    assert row['version'] == 2
    assert row['value']['content'] == '' and row['value']['status'] == 'pending'


def test_prepared_task_identity_keeps_its_witness_read(monkeypatch):
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.manager._registry import _create_chat_task
    task_id = 'prepared-' + uuid.uuid4().hex + uuid.uuid4().hex[:16]
    recorder = _record(monkeypatch)
    try:
        _create_chat_task('', [{'role': 'user', 'content': 'hi'}],
                          {'model': 'birth-model', 'userId': 1},
                          user_id=1, supersede=False, prepared_task_id=task_id)
        assert _task_result_operations(recorder)[:2] == [
            'task_results.replay_get', 'task_results.checkpoint']
    finally:
        discard_task(task_id)
        from lib.storage import get_storage_client
        get_storage_client(write=True).command(
            'record.delete', {'namespace': 'task_results', 'key': task_id},
            f'test-delete-task-result:{task_id}')
