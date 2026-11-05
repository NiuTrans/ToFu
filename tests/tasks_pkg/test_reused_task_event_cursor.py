"""A task identity reused by a later physical attempt continues its durable stream.

Project-dispatch Workers resume in place under one deterministic workspace task
id (``project-worker:<assignmentId>``), so the successor's carrier is born with
an id whose ``storage_events`` rows the interrupted predecessor already owns.
Minting from zero collided with those rows ("event sequence has a conflicting
payload", a permanent refusal): every later event was refused as "Earlier task
event is not durable", the resumed Worker aborted before its first round and its
terminal debt was retried forever. Registration reads the durable cursor before
the carrier can emit; a transient read waits, a permanent refusal fails the start.
"""
from __future__ import annotations

import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


class _Client:
    """The real storage client; ``event.latest`` may first raise ``failures``."""

    def __init__(self, client, failures=()):
        self._client = client
        self._failures = list(failures)
        self.queries = []

    def query(self, operation, payload, **options):
        self.queries.append(operation)
        if operation == 'event.latest' and self._failures:
            raise self._failures.pop(0)
        return self._client.query(operation, payload, **options)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def identities():
    created = []
    yield created
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import discard_task
    for task_id in created:
        discard_task(task_id)
        get_storage_client(write=True).command(
            'record.delete', {'namespace': 'task_results', 'key': task_id},
            f'test-delete-task-result:{task_id}')


def _client(monkeypatch, failures=()):
    import lib.storage as storage
    client = _Client(storage.get_storage_client(write=True), failures)
    monkeypatch.setattr(storage, 'get_storage_client', lambda **_options: client)
    return client


def _predecessor_stream(identities, rows=3):
    from lib.tasks_pkg.event_log import append_persistent_event
    task_id = 'project-worker:' + str(uuid.uuid4())
    identities.append(task_id)
    for sequence in range(rows):
        append_persistent_event(task_id, sequence, {
            'type': 'phase', 'attemptId': 'predecessor', 'n': sequence})
    return task_id


def _resume(task_id):
    from lib.tasks_pkg.manager import create_task
    return create_task('', [{'role': 'user', 'content': 'continue'}], {
        'model': 'resume-model', '_projectDispatchWorkspaceTaskId': task_id,
    }, user_id=1, supersede=False)


def _emit(task_id):
    """Mint through the runtime and commit the exact frame before visibility."""
    from lib.tasks_pkg.event_log import append_persistent_event
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    failures = []
    event = {'type': 'phase', 'attemptId': 'successor'}

    def persist(sequence):
        try:
            append_persistent_event(task_id, sequence, event)
        except Exception as error:
            failures.append(error)
            raise

    sequence = chat_task_runtime.append_event(
        task_id, event, before_push=persist, deliver_push=False)
    return sequence, failures


def test_resumed_identity_continues_after_its_predecessor_rows(identities):
    from lib.tasks_pkg.event_log import read_events
    task_id = _predecessor_stream(identities)

    task = _resume(task_id)
    sequence, failures = _emit(task_id)

    assert task['id'] == task_id
    assert failures == []
    assert sequence == 3
    rows = read_events(task_id, raise_on_error=True)
    assert [row['event_id'] for row in rows] == [0, 1, 2, 3]
    assert [row['payload']['attemptId'] for row in rows] == [
        'predecessor', 'predecessor', 'predecessor', 'successor']


def test_fresh_identity_pays_no_cursor_read(identities, monkeypatch):
    from lib.tasks_pkg.manager import create_task
    client = _client(monkeypatch)

    task = create_task('', [{'role': 'user', 'content': 'hi'}],
                       {'model': 'fresh-model'}, user_id=1, supersede=False)
    identities.append(task['id'])

    assert 'event.latest' not in client.queries
    assert task['_eventNextSeq'] == 0


def test_transient_cursor_read_waits_instead_of_minting_into_owned_rows(
        identities, monkeypatch):
    from lib.storage.errors import StorageError
    task_id = _predecessor_stream(identities)
    client = _client(monkeypatch, failures=[
        StorageError('database_unavailable', 'authority restarting', True)])

    _resume(task_id)
    sequence, failures = _emit(task_id)

    assert client.queries.count('event.latest') == 2
    assert failures == [] and sequence == 3


def test_permanent_cursor_refusal_fails_start_without_a_carrier(
        identities, monkeypatch):
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    task_id = _predecessor_stream(identities)
    _client(monkeypatch, failures=[
        StorageError('database_protocol_error', 'cursor read refused')])

    with pytest.raises(StorageError, match='cursor read refused'):
        _resume(task_id)

    assert chat_task_runtime.get(task_id) is None
