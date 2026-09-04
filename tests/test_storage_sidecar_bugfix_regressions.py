"""Regression pins for the four storage-layer bug fixes.

These tests exercise the exact corruption/CAS/clock boundaries that the fixes
close.  The SQLite-session tests run without a sidecar process (fast and
deterministic); the record-CAS concurrency test goes through a real sidecar so
it also discriminates the PostgreSQL advisory-lock fix when
``TOFU_STORAGE_TEST_POSTGRES=1``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit


@pytest.fixture
def session(tmp_path):
    """A real SQLiteSession over an initialized temp authority (no process)."""
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import initialize_schema

    connection = sqlite3.connect(
        str(tmp_path / 'sidecar.db'), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        sess = SQLiteSession(connection)
        initialize_schema(sess)
        yield sess
    finally:
        connection.close()


_BACKENDS = ['sqlite']
if os.environ.get('TOFU_STORAGE_TEST_POSTGRES') == '1':
    _BACKENDS.append('postgres')


@pytest.fixture(params=_BACKENDS)
def storage(request, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_RPC_CAPACITY', '8')
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend=request.param, startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def test_pre_turn_transcript_survives_delete_and_restore(session):
    """Delete/restore must not corrupt the only durable pre-turn transcript.

    SQLite stores ``_dump`` output as a BLOB in the JSONDOC column, so a
    pre-turn ``messages_json`` reads back as ``bytes``.  ``str(b'[...]')`` used
    to turn that into the literal ``"b'[...]'"`` string, destroying the
    transcript on a recoverable delete/restore round-trip.
    """
    from lib.storage_sidecar.operations_pkg._common import _dump, _load
    from lib.storage_sidecar.operations_pkg._conversations import (
        _conversation_delete,
        _conversation_get,
        _conversation_list,
        _conversation_restore,
    )

    archived = [
        {'role': 'user', 'content': 'pre-turn question'},
        {'role': 'assistant', 'content': 'pre-turn answer'},
    ]
    session.execute(
        'INSERT INTO storage_conversations('
        'id,user_id,title,messages_json,created_at_ms,updated_at_ms,'
        'settings_json,msg_count,search_text,rev) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        ('legacy-conv', 1, 'Legacy', _dump(archived), 1000, 2000,
         _dump({}), len(archived), '', 3),
    )

    deleted = _conversation_delete(
        session, {'conv_id': 'legacy-conv', 'user_id': 1})
    assert deleted['deleted'] is True and deleted['recoverable'] is True

    trash = session.fetch_one(
        'SELECT messages_json, msg_count FROM storage_conversation_trash '
        'WHERE conversation_id=? AND user_id=?',
        ('legacy-conv', 1),
    )
    assert _load(trash['messages_json']) == archived
    assert trash['msg_count'] == len(archived)

    restored = _conversation_restore(
        session, {'conv_id': 'legacy-conv', 'user_id': 1})
    assert restored['restored'] is True

    stored = session.fetch_one(
        'SELECT msg_count FROM storage_conversations WHERE id=? AND user_id=?',
        ('legacy-conv', 1),
    )
    assert stored['msg_count'] == len(archived), (
        'restore must preserve the archived msg_count for the sidebar')

    document = _conversation_get(
        session, {'conv_id': 'legacy-conv', 'user_id': 1})
    assert document['messages'] == archived
    assert document['metadata']['msg_count'] == len(archived)

    listed = _conversation_list(
        session, {'user_id': 1, 'include_messages': False})
    counts = {
        item['metadata']['id']: item['metadata']['msg_count']
        for item in listed
    }
    assert counts['legacy-conv'] == len(archived)


def test_compacted_pre_turn_transcript_survives_delete_and_restore(session):
    """Private archive references stay lossless across trash lifecycle."""
    import orjson

    from lib.storage_sidecar.operations_pkg._common import _dump
    from lib.storage_sidecar.operations_pkg._conversations import (
        _conversation_delete,
        _conversation_get,
        _conversation_restore,
    )
    from lib.storage_sidecar.projection_codec import (
        STORAGE_PROJECTION_CODEC_KEY,
        encode_projection_sequence_for_storage,
    )

    repeated = 'archived tool result ' * 2_000
    archived = [{
        'role': 'assistant',
        'content': 'done',
        'segments': [{
            'type': 'tool_use',
            'id': 'call-archive',
            'input': {'path': 'large.txt'},
            'result': {'content': repeated, 'isError': False},
        }],
        'toolRounds': [{
            'toolCallId': 'call-archive',
            'toolArgs': {'path': 'large.txt'},
            'toolContent': repeated,
        }],
    }]
    compacted = orjson.dumps(
        encode_projection_sequence_for_storage(archived),
        option=orjson.OPT_SORT_KEYS,
    )
    assert STORAGE_PROJECTION_CODEC_KEY.encode() in compacted

    session.execute(
        'INSERT INTO storage_conversations('
        'id,user_id,title,messages_json,created_at_ms,updated_at_ms,'
        'settings_json,msg_count,search_text,rev) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        ('compacted-legacy', 1, 'Compacted', compacted, 1000, 2000,
         _dump({}), 1, '', 3),
    )

    deleted = _conversation_delete(
        session, {'conv_id': 'compacted-legacy', 'user_id': 1})
    assert deleted['deleted'] is True
    trash = session.fetch_one(
        'SELECT messages_json FROM storage_conversation_trash '
        'WHERE conversation_id=? AND user_id=?',
        ('compacted-legacy', 1),
    )
    assert trash['messages_json'] == compacted

    restored = _conversation_restore(
        session, {'conv_id': 'compacted-legacy', 'user_id': 1})
    assert restored['restored'] is True
    active = session.fetch_one(
        'SELECT messages_json FROM storage_conversations '
        'WHERE id=? AND user_id=?',
        ('compacted-legacy', 1),
    )
    assert active['messages_json'] == compacted

    document = _conversation_get(
        session, {'conv_id': 'compacted-legacy', 'user_id': 1})
    assert document['messages'] == archived
    assert document['metadata']['msg_count'] == 1


def test_worker_job_now_rejects_far_future_clock():
    """A caller clock far ahead of the authority must not strand a job."""
    from lib.storage_sidecar.operations_pkg._worker_jobs import (
        _MAX_CLOCK_MS,
        _MAX_CLOCK_SKEW_MS,
        _job_now,
    )

    server_now = int(time.time() * 1000)
    assert _job_now({'now_ms': server_now}) == server_now
    assert _job_now({'now_ms': server_now + _MAX_CLOCK_SKEW_MS}) == (
        server_now + _MAX_CLOCK_SKEW_MS)

    with pytest.raises(StorageError) as raised:
        _job_now({'now_ms': _MAX_CLOCK_MS})
    assert raised.value.code == 'database_protocol_error'


def test_worker_job_enqueue_clock_validation(session):
    """``worker_job.enqueue`` accepts a sane clock and rejects an absurd one."""
    from lib.storage_sidecar.operations_pkg._worker_jobs import (
        _worker_job_enqueue,
    )

    result = _worker_job_enqueue(session, {
        'task_id': 'normal-job', 'user_id': 1, 'tenant_id': '',
        'task_kind': 'conversation-attempt', 'idempotency_key': 'k-normal',
        'payload': {}, 'now_ms': int(time.time() * 1000),
    })
    assert result['created'] is True
    assert result['job']['status'] == 'queued'

    with pytest.raises(StorageError) as raised:
        _worker_job_enqueue(session, {
            'task_id': 'stranded-job', 'user_id': 1, 'tenant_id': '',
            'task_kind': 'conversation-attempt', 'idempotency_key': 'k-absurd',
            'payload': {}, 'now_ms': 9_223_372_036_854_775_000,
        })
    assert raised.value.code == 'database_protocol_error'


def test_record_cas_acquires_per_key_lock_before_version_read():
    """Record and guarded task CAS acquire their authority locks before reads."""
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )
    from lib.storage_sidecar.operations_pkg._records import (
        _record_put,
        _task_results_checkpoint,
    )

    class _RecordingSession:
        backend = 'sqlite'

        def __init__(self):
            self.events = []

        def lock_key(self, namespace, key):
            self.events.append(('lock', namespace, key))

        def fetch_one(self, sql, params=()):
            self.events.append(('read', sql))
            return None

        def execute(self, sql, params=()):
            self.events.append(('write', sql))
            return 1

    session = _RecordingSession()
    _record_put(session, {'namespace': 'ns', 'key': 'k', 'value': {'a': 1}})
    lock_index = next(
        i for i, event in enumerate(session.events) if event[0] == 'lock')
    read_index = next(
        i for i, event in enumerate(session.events) if event[0] == 'read')
    assert session.events[lock_index] == ('lock', 'record', 'ns:k')
    assert lock_index < read_index

    checkpoint_session = _RecordingSession()
    _task_results_checkpoint(checkpoint_session, {
        'key': 'k', 'value': {'status': 'running'}, 'expected_version': 0,
    })
    lock_index = next(
        i for i, event in enumerate(checkpoint_session.events)
        if event[0] == 'lock')
    read_index = next(
        i for i, event in enumerate(checkpoint_session.events)
        if event[0] == 'read')
    assert checkpoint_session.events[lock_index] == (
        'lock', 'task_result', 'k')
    assert lock_index < read_index

    guarded_session = _RecordingSession()

    def fetch_guarded(sql, params=()):
        del params
        guarded_session.events.append(('read', sql))
        return {'present': 1} if 'storage_conversations' in sql else None

    guarded_session.fetch_one = fetch_guarded
    _task_results_checkpoint(guarded_session, {
        'key': 'guarded-k',
        'value': {
            'task_id': 'guarded-k',
            'conv_id': 'parent-k',
            'user_id': 7,
            'status': 'running',
        },
        'expected_version': 0,
        'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
        'require_parent': True,
    })
    assert guarded_session.events[0] == (
        'lock', 'conversation', '7:parent-k')
    assert guarded_session.events[1][0] == 'read'
    assert 'storage_conversations' in guarded_session.events[1][1]
    assert guarded_session.events[2] == (
        'lock', 'task_result', 'guarded-k')
    assert guarded_session.events[3][0] == 'read'
    assert 'storage_records' in guarded_session.events[3][1]


def test_concurrent_record_put_same_expected_version_single_winner(storage):
    """Two competing CAS writers on the same key produce exactly one winner."""
    barrier = threading.Barrier(2)
    outcomes = []
    outcomes_lock = threading.Lock()

    def put(marker):
        barrier.wait()
        try:
            result = storage.client.command(
                'record.put',
                {
                    'namespace': 'race', 'key': 'cas', 'value': marker,
                    'expected_version': 0,
                },
                f'race-{marker}',
            )
            with outcomes_lock:
                outcomes.append(('ok', result['version']))
        except StorageError as exc:
            with outcomes_lock:
                outcomes.append(('conflict', exc.code))

    threads = [
        threading.Thread(target=put, args=(marker,))
        for marker in ('a', 'b')
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [
        ('conflict', 'database_conflict'),
        ('ok', 1),
    ]
