"""Real SQL capture retains transaction authority and typed replay parameters."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import time

import pytest
from cryptography.fernet import Fernet

from lib.secret_envelope import reset_secret_envelope_for_test
from lib.storage_authority_sql.json_document import JsonBytes
from lib.storage_authority_sql.logical_outbox import LogicalOutboxPipeline, policy_from_config
from lib.storage_authority_sql.logical_replay import BackendReplayTarget, replay_records
from lib.storage_authority_sql.logical_shadow import LogicalCommitShadow
from tests.storage_authority_sql.test_native_checkpoint_storage import backend

pytestmark = pytest.mark.unit


@pytest.fixture
def pipeline(backend, tmp_path, monkeypatch):
    reset_secret_envelope_for_test()
    monkeypatch.setenv('TOFU_SECRET_ENCRYPTION_KEY', Fernet.generate_key().decode())
    config = replace(backend.config, logical_shadow_mode='required',
                     logical_shadow_dir=tmp_path/'logical', process_role='api')
    value = LogicalOutboxPipeline(backend, policy_from_config(config, backend))
    value.start()
    try:
        yield value
    finally:
        value.close()
        reset_secret_envelope_for_test()


def capture(pipeline, callback, *, operation='test.logical_capture'):
    result = pipeline.backend.command(operation, '', None, 'user', lambda session:
        pipeline.execute_and_capture(session, callback, operation=operation,
            request_id=hashlib.sha256(operation.encode()).hexdigest(),
            request_digest=hashlib.sha256(operation.encode()).hexdigest(), command_id=None,
            payload={'tenant_id': '3', 'owner_user_id': 7}), time.monotonic()+20,
        receipt_required=False, join_on_timeout=True)
    pipeline.notify(result[1])
    return result[0]


def published(pipeline):
    """Exercise the actual encrypted publisher and contiguous durable sink."""
    with LogicalCommitShadow(pipeline.policy.sink_root, stream_id=pipeline._stream_id) as sink:
        pipeline._sink = sink
        try:
            while batch := pipeline._pending_batch():
                for record in batch:
                    pipeline._publish(record)
            return sink.read_records()
        finally:
            pipeline._sink = None


@pytest.fixture
def target_backend(backend, tmp_path, request):
    from tests.support.postgres_isolation import isolated_postgres_dsn
    from tests.storage_authority_sql.test_sqlite_read_pool import _config
    directory = tmp_path/'target'
    directory.mkdir()
    config = _config(directory)
    target_kind = backend.name
    if getattr(request, 'param', 'same') == 'other':
        if os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1':
            pytest.skip('owned PostgreSQL cluster required for cross-backend replay')
        target_kind = 'postgres' if backend.name == 'sqlite' else 'sqlite'
    with ExitStack() as resources:
        if target_kind == 'postgres':
            source = directory/'source-dsn'
            source.touch(mode=0o600)
            source.write_text(Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE']).read_text())
            dsn = resources.enter_context(isolated_postgres_dsn(source, directory))
            config = replace(config, backend='postgres', postgres_dsn=dsn.read_text())
        from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
        from lib.storage_authority_sql.adapters.postgres import PostgresBackend
        value = (PostgresBackend if target_kind == 'postgres' else SQLiteBackend)(config)
        value.start()
        resources.callback(value.close)
        yield value


def test_recorded_command_keeps_locked_run_transition(pipeline, backend):
    from lib.storage_authority_sql.operations_pkg._runs import _orchestration_run_update
    def transition(session):
        session.execute('INSERT INTO orchestration_runs(id, user_id, tenant_id, orch_id, status, '
            'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            ('captured-run', 7, '3', 'test', 'running', 1, 1))
        return _orchestration_run_update(session, dict(run_id='captured-run', user_id=7,
            tenant_id='3', status='done', final='complete'))
    assert capture(pipeline, transition)['changed']
    row = backend.query('test.read', lambda session: session.fetch_one(
        'SELECT status, final FROM orchestration_runs WHERE id=?', ('captured-run',)), time.monotonic()+10)
    assert row['status'] == 'done' and row['final'] == 'complete'
    assert len(published(pipeline)) == 1


def test_jsondoc_and_binary_survive_actual_backend_replay(pipeline, target_backend):
    value = {'中文': ['quoted " value', {'count': 42}], 'empty': None}
    def insert(session):
        session.execute('INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) '
            'VALUES (?, ?, ?, ?, ?)', ('replay', 'typed', JsonBytes(json.dumps(value).encode()), 1, 1))
        session.execute('INSERT INTO storage_command_receipts_v2(command_key, operation, request_digest, response_json, committed_at_ms) '
            'VALUES (?, ?, ?, ?, ?)', (b'\x00'*32, 'test.binary', b'\xff'*32, b'\xff\x00binary', 1))
        return {'ok': True}
    capture(pipeline, insert)
    records = published(pipeline)
    target = BackendReplayTarget(target_backend, target_name='typed', stream_id=records[0]['stream_id'])
    assert replay_records(records, target).applied_records == 1
    stored = target_backend.query('test.read', lambda session: session.fetch_one(
        'SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?', ('replay', 'typed')),
        time.monotonic()+10)['value_json']
    assert (json.loads(stored) if isinstance(stored, (str, bytes)) else stored) == value
    binary = target_backend.query('test.binary', lambda session: session.fetch_one(
        'SELECT response_json, request_digest FROM storage_command_receipts_v2 WHERE command_key=?', (b'\x00'*32,)),
        time.monotonic()+10)
    assert bytes(binary['response_json']) == b'\xff\x00binary' and bytes(binary['request_digest']) == b'\xff'*32


def test_recorded_worker_claim_retains_backend_lease_authority(pipeline, backend):
    from lib.storage_authority_sql.operations_pkg._worker_jobs import _worker_job_enqueue, _worker_job_claim_next, _worker_job_claim_state
    now = int(time.time()*1000)
    capture(pipeline, lambda session: _worker_job_enqueue(session, dict(task_id='recorded-job', user_id=7,
        tenant_id='3', task_kind='conversation-attempt', idempotency_key='recorded-job', payload={'conversationId': 'test'},
        now_ms=now)), operation='test.enqueue')
    claim = capture(pipeline, lambda session: _worker_job_claim_next(session, dict(worker_id='holder', now_ms=now,
        lease_ms=10000, task_kinds=['conversation-attempt'])), operation='test.claim')
    assert claim['taskId'] == 'recorded-job'
    proof = dict(task_id=claim['taskId'], worker_id='holder', fencing_token=claim['fencingToken'], now_ms=now)
    assert backend.query('test.state', lambda session: _worker_job_claim_state(session, proof), time.monotonic()+10)['ok']
