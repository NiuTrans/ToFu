"""Transactional outbox, publisher crash, and bounded-resource contracts."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from cryptography.fernet import Fernet

from lib.storage import StorageSupervisor
from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.secret_envelope import reset_secret_envelope_for_test
from lib.storage_sidecar.operation_domains import REGISTRY_VERSION
from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema
from lib.storage_sidecar.adapters.sqlite import SQLiteBackend, SQLiteSession
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.logical_outbox import (
    LogicalMutationRecordingSession,
    LogicalOutboxPipeline,
    LogicalOutboxPolicy,
    _fetch_pending,
    decode_logical_payload,
    policy_from_config,
)
from lib.storage_sidecar.logical_replay import (
    BackendReplayTarget,
    ReplayCheckpoint,
    replay_records,
)
from lib.storage_sidecar.logical_shadow import LogicalCommitShadow
from lib.storage_sidecar import logical_outbox
from lib.storage_sidecar.server import create_server


pytestmark = pytest.mark.unit
_TOKEN = 'logical-outbox-test-token-' * 2


@pytest.fixture(autouse=True)
def _isolated_logical_encryption_key(monkeypatch):
    reset_secret_envelope_for_test()
    monkeypatch.setenv(
        'TOFU_SECRET_ENCRYPTION_KEY', Fernet.generate_key().decode('ascii'))
    try:
        yield
    finally:
        reset_secret_envelope_for_test()


def _config(tmp_path: Path, *, role: str = 'all') -> SidecarConfig:
    data_dir = tmp_path / 'data'
    logs_dir = tmp_path / 'logs'
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return SidecarConfig(
        project_root=tmp_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        backend='sqlite',
        deployment_mode='personal',
        process_role=role,
        replica_id=None,
        token=_TOKEN,
        sqlite_path=data_dir / 'tofu.db',
        postgres_dsn='',
        redis_url='',
        allow_schema_migration=True,
        read_pool_size=1,
        write_pool_size=1,
        logical_shadow_mode='required',
        logical_shadow_dir=tmp_path / 'logical-commits',
    )


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('condition did not become true before timeout')


class _PolicyBackend:
    def __init__(self, *, fastpath_active: bool):
        self.fastpath_active = fastpath_active

    def metrics(self):
        return {'fastpath': {'active': self.fastpath_active}}


def test_policy_is_topology_aware_and_distributed_publish_has_one_role(tmp_path):
    sqlite_config = replace(
        _config(tmp_path),
        logical_shadow_mode='auto',
        logical_shadow_dir=None,
    )
    disabled = policy_from_config(
        sqlite_config, _PolicyBackend(fastpath_active=False))
    assert disabled.capture_enabled is False

    enabled = policy_from_config(
        sqlite_config, _PolicyBackend(fastpath_active=True))
    assert enabled.capture_enabled is True
    assert enabled.publisher_enabled is True
    assert enabled.sink_root == sqlite_config.data_dir / 'logical-commits'

    postgres_config = replace(
        sqlite_config,
        backend='postgres',
        deployment_mode='distributed',
        process_role='api',
        replica_id='api-0',
        logical_shadow_mode='required',
    )
    with pytest.raises(RuntimeError, match='explicit shared'):
        policy_from_config(
            postgres_config, _PolicyBackend(fastpath_active=False))
    capture_only = policy_from_config(
        replace(
            postgres_config,
            logical_shadow_dir=tmp_path / 'shared-logical-sink',
        ),
        _PolicyBackend(fastpath_active=False),
    )
    assert capture_only.capture_enabled is True
    assert capture_only.publisher_enabled is False


def test_logical_configuration_is_explicit_bounded_and_absolute(
    tmp_path, monkeypatch,
):
    root = tmp_path / 'config-root'
    sink = tmp_path / 'shared-logical-sink'
    monkeypatch.setenv('TOFU_STORAGE_TOKEN', _TOKEN)
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(root))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW', 'required')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW_DIR', str(sink))
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_ACCESS', 'group')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_OUTBOX_MAX_MIB', '32')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW_MAX_MIB', '128')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SEGMENT_MIB', '32')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB', '8')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_PUBLISH_BATCH', '7')

    config = SidecarConfig.from_environment()
    assert config.logical_shadow_mode == 'required'
    assert config.logical_shadow_dir == sink.resolve()
    assert config.logical_shadow_access == 'group'
    assert config.logical_outbox_max_bytes == 32 * 1024 * 1024
    assert config.logical_shadow_max_bytes == 128 * 1024 * 1024
    assert config.logical_shadow_segment_bytes == 32 * 1024 * 1024
    assert config.logical_record_max_bytes == 8 * 1024 * 1024
    assert config.logical_publish_batch_size == 7

    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB', '17')
    with pytest.raises(RuntimeError, match='at most half'):
        SidecarConfig.from_environment()
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB', '8')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW_DIR', 'relative/sink')
    with pytest.raises(RuntimeError, match='must be absolute'):
        SidecarConfig.from_environment()


def test_schema_38_expands_to_transactional_outbox_without_row_rewrite(tmp_path):
    connection = sqlite3.connect(tmp_path / 'schema.sqlite3')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta('
        'meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)')
    connection.execute(
        'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)',
        ('schema_version', '38'),
    )
    initialize_schema(SQLiteSession(connection))
    columns = {
        row['name']
        for row in connection.execute(
            'PRAGMA table_info(storage_logical_outbox)').fetchall()
    }
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()['meta_value']
    connection.close()
    assert {
        'sequence', 'event_id', 'schema_version', 'registry_version',
        'encryption_key_id', 'payload_ciphertext', 'record_bytes',
    } <= columns
    assert int(version) == SCHEMA_VERSION


def test_logical_capture_preserves_each_witness_in_an_exact_backend_batch():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE records(id TEXT PRIMARY KEY, value TEXT)")
    connection.executemany(
        "INSERT INTO records(id,value) VALUES(?,?)",
        (("one", "old"), ("two", "old")),
    )
    recorder = LogicalMutationRecordingSession(SQLiteSession(connection))
    try:
        affected = recorder.execute_many_exact(
            "UPDATE records SET value=? WHERE id=?",
            (("new-one", "one"), ("new-two", "two")),
        )
        stored = connection.execute(
            "SELECT id,value FROM records ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    assert affected == 2
    assert [tuple(row) for row in stored] == [
        ("one", "new-one"),
        ("two", "new-two"),
    ]
    assert [mutation["rowcount"] for mutation in recorder.mutations] == [1, 1]
    assert [mutation["params"] for mutation in recorder.mutations] == [
        ["new-one", "one"],
        ["new-two", "two"],
    ]


def test_rpc_command_and_receipt_share_one_logical_event(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    backend = SQLiteBackend(config)
    backend.start()
    pipeline = LogicalOutboxPipeline.from_config(config, backend)
    pipeline.start()
    server = create_server(backend, _TOKEN, logical_outbox=pipeline)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = StorageClient('127.0.0.1', server.server_address[1], _TOKEN)
    try:
        payload = {
            'namespace': 'logical-test',
            'key': 'same-command',
            'value': {'secret_marker': 'must-not-appear-in-log'},
        }
        first = client.command('record.put', payload, 'logical-command-1')
        replay = client.command('record.put', payload, 'logical-command-1')
        assert replay == first
        _wait_until(
            lambda: pipeline.status()['published_sequence'] == 1
            and pipeline.status()['pending_records'] == 0)
        health = client.health()
        assert health['ready'] is True
        assert health['logical_outbox']['state'] == 'ready'
        stream_id = pipeline.status()['stream_id']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        pipeline.close()
        backend.close()

    with LogicalCommitShadow(
        config.logical_shadow_dir,
        stream_id=stream_id,
    ) as reopened:
        records = reopened.read_records()
    assert len(records) == 1
    assert records[0]['operation'] == 'record.put'
    assert records[0]['owner_user_id'] == 0
    assert records[0]['tenant_id'] == 'system'
    contract = records[0]['payload']['contract']
    assert contract['operation_registry_version'] == REGISTRY_VERSION
    assert contract['schema_version'] == SCHEMA_VERSION
    assert contract['payload_codec'] == 'tofu.bound-fernet-json.v1'
    clear = decode_logical_payload(records[0])
    assert len(clear['mutations']) == 1
    assert clear['mutations'][0]['sql'].startswith(
        'INSERT INTO storage_records')
    assert clear['request']['value'] == {
        'secret_marker': 'must-not-appear-in-log',
    }
    assert b'must-not-appear-in-log' not in b''.join(
        path.read_bytes()
        for path in config.logical_shadow_dir.glob('segment-*')
    )
    with pytest.raises(ValueError, match='binding'):
        decode_logical_payload({**records[0], 'event_id': 'transplanted-event'})


def test_encrypted_mutation_stream_replays_into_fresh_backend_atomically(
    tmp_path, monkeypatch,
):
    """Exercise source RPC -> segment -> fresh DB -> durable checkpoint."""
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    source_config = _config(tmp_path / 'source')
    source_backend = SQLiteBackend(source_config)
    source_backend.start()
    source_pipeline = LogicalOutboxPipeline.from_config(
        source_config, source_backend)
    source_pipeline.start()
    source_server = create_server(
        source_backend, _TOKEN, logical_outbox=source_pipeline)
    source_thread = threading.Thread(
        target=source_server.serve_forever, daemon=True)
    source_thread.start()
    source_client = StorageClient(
        '127.0.0.1', source_server.server_address[1], _TOKEN)
    try:
        expected = {'private': 'portable-but-encrypted', 'count': 3}
        source_client.command('record.put', {
            'namespace': 'replay-drill',
            'key': 'fresh-target',
            'value': expected,
        }, 'replay-drill-command')
        _wait_until(
            lambda: source_pipeline.status()['published_sequence'] == 1)
        stream_id = source_pipeline.status()['stream_id']
    finally:
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=5)
        source_pipeline.close()
        source_backend.close()

    with LogicalCommitShadow(
        source_config.logical_shadow_dir,
        stream_id=stream_id,
    ) as source_sink:
        records = source_sink.read_records()
    assert len(records) == 1
    assert len(decode_logical_payload(records[0])['mutations']) == 1

    target_config = _config(tmp_path / 'target', role='api')
    target_backend = SQLiteBackend(target_config)
    target_backend.start()
    target = BackendReplayTarget(
        target_backend,
        target_name='fresh-target-drill',
        stream_id=stream_id,
    )
    try:
        replayed = replay_records(records, target)
        stored = target_backend.query(
            'logical_replay.test.verify',
            lambda session: session.fetch_one(
                'SELECT value_json, version FROM storage_records '
                'WHERE namespace = ? AND record_key = ?',
                ('replay-drill', 'fresh-target'),
            ),
            time.monotonic() + 5,
        )
        assert replayed.applied_records == 1
        assert replayed.last_sequence == 1
        assert target.checkpoint() == ReplayCheckpoint(
            stream_id=stream_id,
            last_sequence=1,
            chain_digest=replayed.chain_digest,
        )
        assert stored is not None
        assert stored['version'] == 1
        assert json.loads(stored['value_json']) == expected
    finally:
        target_backend.close()


def test_sidecar_process_parses_and_owns_required_pipeline(tmp_path, monkeypatch):
    sink_root = tmp_path / 'process-logical-commits'
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW', 'required')
    monkeypatch.setenv('TOFU_STORAGE_LOGICAL_SHADOW_DIR', str(sink_root))
    supervisor = StorageSupervisor(
        project_root=tmp_path,
        backend='sqlite',
        startup_timeout=30,
    )
    supervisor.start()
    try:
        supervisor.client.command('record.put', {
            'namespace': 'logical-process-test',
            'key': 'boot-wiring',
            'value': True,
        }, 'logical-process-command')
        _wait_until(
            lambda: supervisor.client.metrics()['logical_outbox'][
                'published_sequence'] == 1,
            timeout=10,
        )
        status = supervisor.client.metrics()['logical_outbox']
        assert status['state'] == 'ready'
        assert status['pending_records'] == 0
        stream_id = status['stream_id']
    finally:
        supervisor.stop()

    with LogicalCommitShadow(sink_root, stream_id=stream_id) as reopened:
        records = reopened.read_records()
    assert [record['operation'] for record in records] == ['record.put']


def test_oversize_capture_rolls_back_the_domain_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path, role='api'))
    backend.start()
    policy = LogicalOutboxPolicy(
        mode='required',
        capture_enabled=True,
        publisher_enabled=False,
        sink_root=None,
        reason='test capture only',
        max_pending_bytes=4096,
        max_record_bytes=512,
        max_segment_bytes=8192,
        max_shadow_bytes=16384,
        publish_batch_size=1,
    )
    pipeline = LogicalOutboxPipeline(backend, policy)
    pipeline.start()
    try:
        def command(session):
            session.execute(
                'INSERT INTO storage_records('
                'namespace, record_key, value_json, version, updated_at_ms) '
                'VALUES (?, ?, ?, ?, ?)',
                ('logical-test', 'rolled-back', b'{}', 1, 1),
            )
            pipeline.capture(
                session,
                operation='record.put',
                request_id='oversize-request',
                request_digest='a' * 64,
                command_id='oversize-command',
                payload={'value': 'small'},
                response={'value': 'x' * 2048},
            )

        with pytest.raises(StorageError) as raised:
            backend.command(
                'test.oversize',
                'b' * 64,
                None,
                'user',
                command,
                time.monotonic() + 5,
                receipt_required=False,
            )
        assert raised.value.code == 'storage_payload_too_large'
        assert backend.query(
            'test.rollback.verify',
            lambda session: session.fetch_one(
                'SELECT 1 AS present FROM storage_records '
                'WHERE namespace = ? AND record_key = ?',
                ('logical-test', 'rolled-back'),
            ),
            time.monotonic() + 5,
        ) is None
    finally:
        pipeline.close()
        backend.close()


def test_natural_idempotent_noop_does_not_allocate_an_outbox_sequence(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path, role='api')
    backend = SQLiteBackend(config)
    backend.start()
    pipeline = LogicalOutboxPipeline.from_config(config, backend)
    pipeline.start()
    try:
        def run(session, *, insert: bool):
            return pipeline.execute_and_capture(
                session,
                lambda recorded: (
                    recorded.execute(
                        'INSERT INTO storage_records('
                        'namespace, record_key, value_json, version, '
                        'updated_at_ms) VALUES (?, ?, ?, ?, ?)',
                        ('logical-noop', 'one', b'{}', 1, 1),
                    ) if insert else recorded.execute(
                        'UPDATE storage_records SET version = version '
                        'WHERE namespace = ? AND record_key = ?',
                        ('missing', 'missing'),
                    )
                ),
                operation='record.put',
                request_id='logical-noop-request',
                request_digest='d' * 64,
                command_id='logical-noop-command',
                payload={'namespace': 'logical-noop'},
            )

        first = backend.command(
            'test.logical-noop.first', 'd' * 64, None, 'user',
            lambda session: run(session, insert=True),
            time.monotonic() + 5,
            receipt_required=False,
        )
        second = backend.command(
            'test.logical-noop.second', 'e' * 64, None, 'user',
            lambda session: run(session, insert=False),
            time.monotonic() + 5,
            receipt_required=False,
        )
        state = backend.query(
            'test.logical-noop.verify',
            lambda session: {
                'count': session.fetch_one(
                    'SELECT COUNT(*) AS count FROM storage_logical_outbox'
                )['count'],
                'sequence': session.fetch_one(
                    'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                    ('logical_outbox_last_sequence',),
                )['meta_value'],
            },
            time.monotonic() + 5,
        )
        assert first[1] is not None
        assert second[1] is None
        assert int(state['count']) == 1
        assert int(state['sequence']) == 1
    finally:
        pipeline.close()
        backend.close()


def test_encryption_key_change_fails_before_accepting_more_commands(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path, role='api')
    backend = SQLiteBackend(config)
    backend.start()
    first = LogicalOutboxPipeline.from_config(config, backend)
    first.start()
    first.close()

    reset_secret_envelope_for_test()
    monkeypatch.setenv(
        'TOFU_SECRET_ENCRYPTION_KEY', Fernet.generate_key().decode('ascii'))
    changed = LogicalOutboxPipeline.from_config(config, backend)
    try:
        with pytest.raises(StorageError) as raised:
            changed.start()
        assert raised.value.code == 'database_integrity'
    finally:
        changed.close()
        backend.close()


def test_pending_byte_ceiling_backpressures_and_rolls_back_only_new_work(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path, role='api'))
    backend.start()
    policy = LogicalOutboxPolicy(
        mode='required',
        capture_enabled=True,
        publisher_enabled=False,
        sink_root=None,
        reason='test bounded backlog',
        max_pending_bytes=4096,
        max_record_bytes=2048,
        max_segment_bytes=8192,
        max_shadow_bytes=16384,
        publish_batch_size=1,
    )
    pipeline = LogicalOutboxPipeline(backend, policy)
    pipeline.start()
    committed = 0
    try:
        for index in range(10):
            def command(session, *, item=index):
                session.execute(
                    'INSERT INTO storage_records('
                    'namespace, record_key, value_json, version, updated_at_ms) '
                    'VALUES (?, ?, ?, ?, ?)',
                    ('logical-budget', str(item), b'{}', 1, 1),
                )
                return pipeline.capture(
                    session,
                    operation='record.put',
                    request_id=f'budget-request-{item}',
                    request_digest=f'{item + 1:064x}',
                    command_id=f'budget-command-{item}',
                    payload={'value': 'x' * 256},
                    response={'ok': True},
                )

            try:
                backend.command(
                    'test.bounded-backlog',
                    f'{index + 1:064x}',
                    None,
                    'user',
                    command,
                    time.monotonic() + 5,
                    receipt_required=False,
                )
                committed += 1
            except StorageError as exc:
                assert exc.code == 'database_busy'
                break
        else:
            raise AssertionError('bounded outbox never applied backpressure')

        counts = backend.query(
            'test.bounded-backlog.verify',
            lambda session: {
                'domain': int(session.fetch_one(
                    'SELECT COUNT(*) AS count FROM storage_records '
                    'WHERE namespace = ?', ('logical-budget',))['count']),
                'outbox': int(session.fetch_one(
                    'SELECT COUNT(*) AS count FROM storage_logical_outbox'
                )['count']),
                'pending_bytes': int(session.fetch_one(
                    'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                    ('logical_outbox_pending_bytes',))['meta_value']),
            },
            time.monotonic() + 5,
        )
        assert committed >= 1
        assert counts['domain'] == counts['outbox'] == committed
        assert counts['pending_bytes'] <= policy.max_pending_bytes
    finally:
        pipeline.close()
        backend.close()


def test_required_sink_preflight_has_a_bounded_startup_wait(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    monkeypatch.setattr(logical_outbox, '_SINK_STARTUP_TIMEOUT_S', 0.05)
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    entered = threading.Event()
    release = threading.Event()

    def blocked_sink(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        raise RuntimeError('injected remote sink stall')

    pipeline = LogicalOutboxPipeline.from_config(
        _config(tmp_path), backend, sink_factory=blocked_sink)
    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='preflight timed out'):
            pipeline.start()
        assert entered.is_set()
        assert time.monotonic() - started_at < 0.5
    finally:
        release.set()
        _wait_until(
            lambda: pipeline.status()['state'] == 'degraded', timeout=2)
        pipeline.close()
        backend.close()


def test_append_before_ack_is_idempotent_after_publisher_restart(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path, role='api')
    backend = SQLiteBackend(config)
    backend.start()
    capture_policy = LogicalOutboxPolicy(
        mode='required',
        capture_enabled=True,
        publisher_enabled=False,
        sink_root=config.logical_shadow_dir,
        reason='test capture only',
        max_pending_bytes=config.logical_outbox_max_bytes,
        max_record_bytes=config.logical_record_max_bytes,
        max_segment_bytes=config.logical_shadow_segment_bytes,
        max_shadow_bytes=config.logical_shadow_max_bytes,
        publish_batch_size=4,
    )
    capture = LogicalOutboxPipeline(backend, capture_policy)
    capture.start()
    try:
        captured_bytes = backend.command(
            'test.capture',
            'c' * 64,
            None,
            'user',
            lambda session: capture.capture(
                session,
                operation='record.put',
                request_id='crash-window-request',
                request_digest='c' * 64,
                command_id='crash-window-command',
                payload={'namespace': 'test'},
                response={'ok': True, 'raw': b'\x00\xff'},
            ),
            time.monotonic() + 5,
            receipt_required=False,
        )
        assert isinstance(captured_bytes, int)
        record = backend.query(
            'test.outbox.pending',
            lambda session: _fetch_pending(session, 1)[0],
            time.monotonic() + 5,
        )
        stream_id = capture.status()['stream_id']
    finally:
        capture.close()

    # Simulate a process dying after sink fsync but before the tiny DB ack.
    with LogicalCommitShadow(
        config.logical_shadow_dir,
        stream_id=stream_id,
    ) as sink:
        sink.append(
            operation=record.operation,
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_user_id,
            payload={
                'contract': {
                    'encryption_key_id': record.encryption_key_id,
                    'operation_registry_version': record.registry_version,
                    'payload_codec': 'tofu.bound-fernet-json.v1',
                    'schema_version': record.schema_version,
                },
                'ciphertext': record.payload_ciphertext,
            },
            command_id=record.command_id,
            request_digest=record.request_digest,
            committed_at_ms=record.committed_at_ms,
            event_id=record.event_id,
            expected_sequence=record.sequence,
        )

    publish_policy = LogicalOutboxPolicy(
        mode='required',
        capture_enabled=True,
        publisher_enabled=True,
        sink_root=config.logical_shadow_dir,
        reason='test publisher',
        max_pending_bytes=config.logical_outbox_max_bytes,
        max_record_bytes=config.logical_record_max_bytes,
        max_segment_bytes=config.logical_shadow_segment_bytes,
        max_shadow_bytes=config.logical_shadow_max_bytes,
        publish_batch_size=4,
    )
    publisher = LogicalOutboxPipeline(backend, publish_policy)
    try:
        publisher.start()
        _wait_until(lambda: publisher.status()['pending_records'] == 0)
        assert publisher.status()['published_sequence'] == 1
        assert publisher.status()['duplicate_retries'] == 1
        assert decode_logical_payload({
            'event_id': record.event_id,
            'operation': record.operation,
            'owner_user_id': record.owner_user_id,
            'payload': {
                'contract': {
                    'encryption_key_id': record.encryption_key_id,
                    'operation_registry_version': record.registry_version,
                    'payload_codec': 'tofu.bound-fernet-json.v1',
                    'schema_version': record.schema_version,
                },
                'ciphertext': record.payload_ciphertext,
            },
            'request_digest': record.request_digest,
            'sequence': record.sequence,
            'stream_id': stream_id,
            'tenant_id': record.tenant_id,
        })['response']['raw'] == {'$bytes': 'AP8='}
    finally:
        publisher.close()
        backend.close()
