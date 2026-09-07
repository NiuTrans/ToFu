"""Durable worker claims survive replica loss and reject stale writers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import sqlite3
import uuid

import pytest

from lib.storage.errors import StorageError


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _client(*, write=False):
    from lib.storage import get_storage_client

    return get_storage_client(write=write)


def _command(operation, payload, prefix):
    return _client(write=True).command(
        operation, payload, f'{prefix}:{uuid.uuid4().hex}')


def _enqueue(
    task_id, *, user_id=41, key=None, payload=None, now_ms=1_000,
    task_kind='conversation-attempt',
):
    return _command('worker_job.enqueue', {
        'task_id': task_id,
        'user_id': user_id,
        'tenant_id': 'tenant-a',
        'task_kind': task_kind,
        'idempotency_key': key or f'key:{task_id}',
        'payload': payload or {'conversationId': f'conv:{task_id}'},
        'now_ms': now_ms,
    }, f'enqueue:{task_id}')


def _claim(worker_id, *, now_ms, task_kinds=None):
    request = {
        'worker_id': worker_id,
        'now_ms': now_ms,
        'lease_ms': 60_000,
        'task_kinds': task_kinds or ['conversation-attempt'],
    }
    return _command('worker_job.claim_next', request, f'claim:{worker_id}')


def test_enqueue_is_semantically_idempotent_and_owner_scoped():
    first = _enqueue('job-idempotent', key='stable-key', payload={'value': 1})
    assert first['created'] is True

    replay = _enqueue(
        'another-client-task-id', key='stable-key', payload={'value': 1})
    assert replay['created'] is False
    assert replay['job']['taskId'] == 'job-idempotent'
    assert _client().query('worker_job.get', {
        'task_id': 'job-idempotent', 'user_id': 42,
    }) is None

    with pytest.raises(StorageError) as raised:
        _enqueue('changed-request', key='stable-key', payload={'value': 2})
    assert raised.value.code == 'database_conflict'
    _command('worker_job.request_cancel', {
        'task_id': 'job-idempotent',
        'user_id': 41,
        'now_ms': 1_001,
    }, 'cleanup-idempotent')


def test_claim_requires_explicit_supported_kinds():
    _enqueue('job-unsupported-kind', task_kind='future-unsupported-kind')

    with pytest.raises(StorageError) as raised:
        _command('worker_job.claim_next', {
            'worker_id': 'replica-a/no-kind-filter',
            'now_ms': 1_001,
            'lease_ms': 60_000,
        }, 'claim-without-kinds')
    assert raised.value.code == 'database_protocol_error'

    assert _claim(
        'replica-a/supported-only',
        now_ms=1_002,
        task_kinds=['conversation-attempt'],
    ) is None
    _command('worker_job.request_cancel', {
        'task_id': 'job-unsupported-kind',
        'user_id': 41,
        'now_ms': 1_003,
    }, 'cleanup-unsupported-kind')


def test_expired_lease_is_reclaimed_with_a_new_fence():
    _enqueue('job-takeover')
    first = _claim('replica-a/worker-1', now_ms=1_000)
    assert first['fencingToken'] == 1
    assert first['attempt'] == 1
    assert _claim('replica-b/worker-1', now_ms=60_999) is None

    second = _claim('replica-b/worker-1', now_ms=61_000)
    assert second['taskId'] == 'job-takeover'
    assert second['fencingToken'] == 2
    assert second['attempt'] == 2

    stale_heartbeat = _command('worker_job.heartbeat', {
        'task_id': 'job-takeover',
        'worker_id': 'replica-a/worker-1',
        'fencing_token': 1,
        'now_ms': 61_001,
        'lease_ms': 60_000,
    }, 'stale-heartbeat')
    assert stale_heartbeat == {'ok': False, 'error': 'stale_fence'}

    stale_terminal = _command('worker_job.complete', {
        'task_id': 'job-takeover',
        'worker_id': 'replica-a/worker-1',
        'fencing_token': 1,
        'now_ms': 61_001,
        'terminal_status': 'succeeded',
        'result_ref': 'record:stale',
    }, 'stale-terminal')
    assert stale_terminal['ok'] is False

    terminal = _command('worker_job.complete', {
        'task_id': 'job-takeover',
        'worker_id': 'replica-b/worker-1',
        'fencing_token': 2,
        'now_ms': 61_002,
        'terminal_status': 'succeeded',
        'result_ref': 'record:authoritative',
        'replay_cursor': 19,
    }, 'fresh-terminal')
    assert terminal['ok'] is True
    assert terminal['job']['status'] == 'succeeded'
    assert terminal['job']['resultRef'] == 'record:authoritative'
    assert terminal['job']['replayCursor'] == 19
    assert _claim('replica-c/worker-1', now_ms=200_000) is None


def test_heartbeat_extends_lease_and_advances_cursor_monotonically():
    _enqueue('job-heartbeat')
    claimed = _claim('replica-a/worker-heartbeat', now_ms=1_000)
    assert claimed['leaseDeadlineMs'] == 61_000

    heartbeat = _command('worker_job.heartbeat', {
        'task_id': 'job-heartbeat',
        'worker_id': 'replica-a/worker-heartbeat',
        'fencing_token': claimed['fencingToken'],
        'now_ms': 21_000,
        'lease_ms': 60_000,
        'replay_cursor': 12,
    }, 'heartbeat')
    assert heartbeat['ok'] is True
    assert heartbeat['job']['leaseDeadlineMs'] == 81_000
    assert heartbeat['job']['replayCursor'] == 12

    lower_cursor = _command('worker_job.heartbeat', {
        'task_id': 'job-heartbeat',
        'worker_id': 'replica-a/worker-heartbeat',
        'fencing_token': claimed['fencingToken'],
        'now_ms': 22_000,
        'lease_ms': 60_000,
        'replay_cursor': 4,
    }, 'heartbeat-lower-cursor')
    assert lower_cursor['job']['replayCursor'] == 12
    assert _claim('replica-b/worker-heartbeat', now_ms=81_999) is None
    reclaimed = _claim('replica-b/worker-heartbeat', now_ms=82_000)
    assert reclaimed['attempt'] == 2
    terminal = _command('worker_job.complete', {
        'task_id': 'job-heartbeat',
        'worker_id': 'replica-b/worker-heartbeat',
        'fencing_token': reclaimed['fencingToken'],
        'now_ms': 82_001,
        'terminal_status': 'succeeded',
        'replay_cursor': 12,
    }, 'cleanup-heartbeat')
    assert terminal['ok'] is True


def test_expired_worker_cannot_settle_before_takeover():
    _enqueue('job-expired-settlement')
    claimed = _claim('replica-a/expired', now_ms=1_000)
    expired = _command('worker_job.complete', {
        'task_id': 'job-expired-settlement',
        'worker_id': 'replica-a/expired',
        'fencing_token': claimed['fencingToken'],
        'now_ms': 61_000,
        'terminal_status': 'succeeded',
    }, 'expired-complete')
    assert expired['ok'] is False

    reclaimed = _claim('replica-b/expired', now_ms=61_000)
    assert reclaimed['fencingToken'] == claimed['fencingToken'] + 1
    terminal = _command('worker_job.complete', {
        'task_id': 'job-expired-settlement',
        'worker_id': 'replica-b/expired',
        'fencing_token': reclaimed['fencingToken'],
        'now_ms': 61_001,
        'terminal_status': 'succeeded',
    }, 'reclaimed-complete')
    assert terminal['ok'] is True


def test_cancel_is_durable_for_queued_and_running_jobs():
    _enqueue('job-cancel-queued')
    queued_cancel = _command('worker_job.request_cancel', {
        'task_id': 'job-cancel-queued',
        'user_id': 41,
        'now_ms': 2_000,
        'reason': 'owner changed direction',
    }, 'cancel-queued')
    assert queued_cancel['job']['status'] == 'cancelled'
    assert _claim('replica-a/cancel', now_ms=3_000) is None

    _enqueue('job-cancel-running')
    claimed = _claim('replica-a/cancel', now_ms=3_000)
    running_cancel = _command('worker_job.request_cancel', {
        'task_id': 'job-cancel-running',
        'user_id': 41,
        'now_ms': 4_000,
        'reason': 'stop now',
    }, 'cancel-running')
    assert running_cancel['job']['status'] == 'running'
    state = _client().query('worker_job.claim_state', {
        'task_id': 'job-cancel-running',
        'worker_id': 'replica-a/cancel',
        'fencing_token': claimed['fencingToken'],
        'now_ms': 4_001,
    })
    assert state['cancelSequence'] == 1
    assert state['cancelReason'] == 'stop now'

    refused_success = _command('worker_job.complete', {
        'task_id': 'job-cancel-running',
        'worker_id': 'replica-a/cancel',
        'fencing_token': claimed['fencingToken'],
        'now_ms': 4_002,
        'terminal_status': 'succeeded',
    }, 'complete-after-cancel')
    assert refused_success['ok'] is False
    cancelled = _command('worker_job.complete', {
        'task_id': 'job-cancel-running',
        'worker_id': 'replica-a/cancel',
        'fencing_token': claimed['fencingToken'],
        'now_ms': 4_003,
        'terminal_status': 'cancelled',
    }, 'acknowledge-cancel')
    assert cancelled['job']['status'] == 'cancelled'


def test_concurrent_claimers_receive_distinct_jobs():
    for index in range(8):
        _enqueue(f'job-concurrent-{index}', now_ms=10_000)

    def claim(index):
        return _claim(f'replica-{index}/worker', now_ms=10_001)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(claim, range(8)))
    assert len({job['taskId'] for job in claimed}) == 8
    assert {job['fencingToken'] for job in claimed} == {1}


def test_postgres_claim_adapter_uses_skip_locked():
    if importlib.util.find_spec('psycopg') is None:
        pytest.skip('psycopg is not installed in this local environment')

    from lib.storage_sidecar.adapters.postgres import PostgresSession

    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchone(self):
            return {'task_id': 'candidate'}

    class Connection:
        def cursor(self):
            return Cursor()

    session = PostgresSession(Connection())
    row = session.fetch_one_for_update_skip_locked(
        'SELECT task_id FROM storage_worker_jobs WHERE status=? LIMIT 1',
        ('queued',),
    )
    assert row == {'task_id': 'candidate'}
    assert executed == [(
        'SELECT task_id FROM storage_worker_jobs WHERE status=%s '
        'LIMIT 1 FOR UPDATE SKIP LOCKED',
        ('queued',),
    )]


def test_worker_job_registry_marks_live_cas_operations_unreceipted():
    from lib.storage_sidecar.operation_domains import REGISTRY_VERSION
    from lib.storage_sidecar.operation_domains.worker_jobs import OPERATIONS

    assert REGISTRY_VERSION == 38
    assert OPERATIONS['worker_job.enqueue'].receipt_required is True
    assert OPERATIONS['worker_job.request_cancel'].receipt_required is True
    assert OPERATIONS['worker_job.complete'].receipt_required is True
    assert OPERATIONS['worker_job.claim_next'].receipt_required is False
    assert OPERATIONS['worker_job.heartbeat'].receipt_required is False


def test_schema_25_migrates_to_worker_job_authority(tmp_path):
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema

    connection = sqlite3.connect(tmp_path / 'schema-v25.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '25'))

    initialize_schema(SQLiteSession(connection))
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    columns = {
        row['name']
        for row in connection.execute('PRAGMA table_info(storage_worker_jobs)')
    }
    connection.close()

    assert int(version) == SCHEMA_VERSION == 58
    assert {
        'claim_owner', 'lease_deadline_ms', 'fencing_token', 'attempt_no',
        'cancel_sequence', 'replay_cursor',
    } <= columns
