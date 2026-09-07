"""Backend-neutral storage.v1 contract exercised through the real process."""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/a.py', 'lib/storage.py'}

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from lib.storage import (
    StorageClient, StorageError, StorageRuntime, StorageSupervisor,
)
from lib.storage.errors import http_status_for_storage_error
from lib.storage_sidecar.preflight import ProjectLease


pytestmark = pytest.mark.unit
_BACKENDS = ['sqlite']
if os.environ.get('TOFU_STORAGE_TEST_POSTGRES') == '1':
    _BACKENDS.append('postgres')


@pytest.fixture(params=_BACKENDS)
def storage(request, tmp_path: Path, monkeypatch):
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


def _import_conversation(
    client,
    conv_id: str,
    *,
    user_id: int = 1,
    messages: list[dict] | None = None,
    title: str = '',
    settings: dict | None = None,
    created_at: int = 1,
    updated_at: int = 1,
):
    """Create a header and append canonical settled turn fixtures."""
    nonce = time.time_ns()
    created = client.command(
        'conversation.create', {
            'conv_id': conv_id,
            'user_id': user_id,
            'title': title,
            'created_at': created_at,
            'updated_at': updated_at,
            'settings': dict(settings or {}),
        }, f'test-create-conversation:{user_id}:{conv_id}:{nonce}')
    transcript = list(messages or [])
    for index, message in enumerate(transcript):
        role = message.get('role')
        actor = {'user': 'human', 'assistant': 'assistant'}.get(role)
        if actor is None:
            raise ValueError(f'unsupported fixture role: {role!r}')
        client.command(
            'turn.append_settled', {
                'conversation_id': conv_id,
                'user_id': user_id,
                'command_id': f'fixture:{conv_id}:{index}:{nonce}',
                'actor': actor,
                'kind': 'fixture',
                'projection': {
                    key: value for key, value in message.items()
                    if key != 'role'
                },
                'created_at': int(message.get('timestamp') or created_at + index),
            }, f'test-append-turn:{user_id}:{conv_id}:{index}:{nonce}')
    if transcript:
        client.command(
            'conversation.metadata.update', {
                'conv_id': conv_id,
                'user_id': user_id,
                'updates': {'updated_at': updated_at},
            }, f'test-conversation-time:{user_id}:{conv_id}:{nonce}')
    return created


def test_turn_storage_operations_reject_missing_owner(storage):
    with pytest.raises(StorageError) as raised:
        storage.client.query(
            'turn.exists', {'conversation_id': 'ownerless-conversation'})

    assert raised.value.code == 'database_protocol_error'


def test_desktop_egress_preference_is_owner_scoped_bounded_and_receipted(storage):
    client = storage.client
    assert client.query(
        'desktop.egress_agent.get', {'owner_user_id': 1}) == {
            'present': False, 'agent_id': '', 'updated_at_ms': 0}

    initialized = client.command(
        'desktop.egress_agent.initialize',
        {'owner_user_id': 1, 'agent_id': 'agent-1'},
        'desktop-egress-owner-1-initialize',
    )
    assert initialized['present'] is True
    assert initialized['agent_id'] == 'agent-1'
    assert client.command(
        'desktop.egress_agent.initialize',
        {'owner_user_id': 1, 'agent_id': 'agent-1'},
        'desktop-egress-owner-1-initialize',
    ) == initialized
    assert client.query(
        'desktop.egress_agent.get', {'owner_user_id': 2})['present'] is False

    updated = client.command(
        'desktop.egress_agent.set',
        {'owner_user_id': 1, 'agent_id': 'agent-2'},
        'desktop-egress-owner-1-set',
    )
    assert updated['agent_id'] == 'agent-2'
    with pytest.raises(StorageError, match='Invalid owner_user_id'):
        client.query('desktop.egress_agent.get', {})
    with pytest.raises(StorageError, match='Invalid agent_id'):
        client.command(
            'desktop.egress_agent.set',
            {'owner_user_id': 1, 'agent_id': 'x' * 129},
            'desktop-egress-owner-1-too-long',
        )


def test_health_preflight_and_project_local_files(storage, tmp_path):
    health = storage.client.health()
    assert health['ready'] is True
    assert health['backend'] in _BACKENDS
    assert health['protocol'] == 'storage.v1'
    assert health['preflight']['atomic_replace'] is True
    assert health['preflight']['file_lock'] is True
    if health['backend'] == 'sqlite':
        assert (tmp_path / 'data' / 'tofu.db').exists()
    else:
        # Distributed PostgreSQL is an external authority.  A Sidecar may
        # create only its local lease/preflight files, never a local cluster.
        assert not (tmp_path / 'data' / 'pgdata').exists()
    assert not list(tmp_path.glob('.storage-preflight-*'))
    metrics = storage.client.metrics()
    rpc = metrics['rpc']
    assert rpc['capacity'] == 8
    assert 1 <= rpc['active'] <= rpc['capacity']
    assert rpc['idle_trim_attempts'] >= 0
    assert rpc['idle_trim_reclaimed_bytes'] >= 0
    assert rpc['idle_trim_duration_ns_total'] >= 0
    assert rpc['idle_trim_last_duration_ns'] >= 0
    assert metrics['process']['rss_bytes'] > 0
    assert metrics['process']['open_fds_or_handles'] > 0
    assert metrics['process']['threads'] >= 1
    assert metrics['attempt_events']['max_nonterminal_payload_bytes'] == 4 * 1024 * 1024
    assert isinstance(metrics['attempt_events']['by_type'], dict)
    if health['backend'] == 'sqlite':
        writer = metrics['writer']
        assert 4 <= writer['queue_capacity'] <= 1024
        assert sum(writer['queue_depths'].values()) <= writer['queue_capacity']
        assert writer['queue_rejections'] >= 0
        assert writer['cancelled_before_start'] >= 0


def test_project_lease_stamp_distinguishes_running_from_clean_stop(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    lease = ProjectLease(data_dir)
    lease.acquire()
    lease_path = data_dir / '.storage-sidecar-lease.json'
    running = json.loads(lease_path.read_text(encoding='utf-8'))
    assert running['status'] == 'running'
    assert running['pid'] == os.getpid()
    assert running['owner_kind'] == 'storage_sidecar'
    assert running['owner_label'] == 'Storage sidecar'

    lease.release()

    stopped = json.loads(lease_path.read_text(encoding='utf-8'))
    assert stopped['lease_id'] == running['lease_id']
    assert stopped['status'] == 'stopped'
    assert stopped['stopped_unix_ms'] >= stopped['started_unix_ms']


def test_project_lease_publishes_credential_free_storage_locator(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    authority = data_dir / 'tofu.db'
    authority.touch()
    lease = ProjectLease(data_dir)
    lease.acquire()
    lease.publish_storage_locator({
        'format': 'tofu.storage-locator/v1',
        'backend': 'sqlite',
        'authority_path': str(authority.resolve()),
        'configured_path': str(authority.resolve()),
        'fastpath_active': False,
    })
    lease_path = data_dir / '.storage-sidecar-lease.json'
    running = json.loads(lease_path.read_text(encoding='utf-8'))

    locator = running['storage_locator']
    assert locator['authority_path'] == str(authority.resolve())
    assert locator['fastpath_active'] is False
    assert locator['published_unix_ms'] >= running['started_unix_ms']
    assert not {'token', 'dsn', 'password'} & set(locator)

    lease.release()
    stopped = json.loads(lease_path.read_text(encoding='utf-8'))
    assert stopped['storage_locator'] == locator


@pytest.mark.skipif(os.name == 'nt', reason='POSIX flock contract')
def test_project_lease_rejects_live_web_owner_unless_it_is_the_parent(tmp_path):
    import fcntl

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    server_lock = data_dir / '.server.lock'
    server_lock.write_text(f'{os.getpid()}@test-host\n', encoding='utf-8')
    with server_lock.open('r+b') as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StorageError, match='Web process') as raised:
            ProjectLease(data_dir).acquire()
        assert raised.value.code == 'database_unavailable'

        child = ProjectLease(
            data_dir, expected_parent_pid=os.getpid())
        child.acquire()
        child.release()


def test_supervisor_surfaces_sidecar_startup_diagnostic(tmp_path, monkeypatch):
    """A refused child startup must never degrade into json.loads('') noise."""
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    lease = ProjectLease(data_dir)
    lease.acquire()
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    try:
        with pytest.raises(
                RuntimeError,
                match='Another storage sidecar holds the project lease'):
            supervisor.start()
    finally:
        supervisor.stop()
        lease.release()


def test_parent_watch_requests_stop_after_supervisor_disappears(monkeypatch):
    import lib.storage_sidecar.__main__ as sidecar_main

    requested = threading.Event()
    monkeypatch.setattr(sidecar_main.os, 'getppid', lambda: 456)
    watcher = sidecar_main._start_parent_watch(
        123, requested.set, interval=0.01)
    assert watcher is not None
    try:
        assert requested.wait(1.0)
    finally:
        watcher.set()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pipe descriptor contract')
def test_parent_watch_requests_stop_when_owner_image_channel_closes():
    import lib.storage_sidecar.__main__ as sidecar_main

    requested = threading.Event()
    read_descriptor, write_descriptor = os.pipe()
    ownership_stream = os.fdopen(read_descriptor, 'rb', buffering=0)
    watcher = sidecar_main._start_parent_watch(
        os.getpid(),
        requested.set,
        ownership_stream=ownership_stream,
    )
    assert watcher is not None
    try:
        os.close(write_descriptor)
        assert requested.wait(1.0)
    finally:
        watcher.set()
        ownership_stream.close()


def test_parent_watch_fails_closed_without_a_valid_owner_channel():
    import lib.storage_sidecar.__main__ as sidecar_main

    class BrokenOwnershipStream:
        def fileno(self):
            raise OSError('descriptor unavailable')

    with pytest.raises(RuntimeError, match='ownership channel is unavailable'):
        sidecar_main._start_parent_watch(
            os.getpid(),
            lambda: None,
            ownership_stream=BrokenOwnershipStream(),
        )


def test_close_on_exec_owner_channel_releases_sidecar_with_same_parent_pid(
        tmp_path, monkeypatch):
    """The lease follows one process image, not merely a reusable PID."""
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    parent_pid = os.getpid()
    lease = None
    try:
        supervisor.start()
        process = supervisor._process
        assert process is not None
        assert process.stdin is not None
        assert os.get_inheritable(process.stdin.fileno()) is False

        # execv preserves parent_pid but closes this non-inheritable descriptor.
        # Closing it directly exercises the child-side half without replacing
        # the pytest process image.
        process.stdin.close()
        process.wait(timeout=10)
        assert os.getpid() == parent_pid

        lease = ProjectLease(tmp_path / 'data')
        lease.acquire()
    finally:
        if lease is not None:
            lease.release()
        supervisor.stop()


def test_sqlite_boot_path_does_not_run_an_unbounded_full_integrity_scan():
    import inspect
    from lib.storage_sidecar.adapters.sqlite import SQLiteBackend

    startup_source = inspect.getsource(SQLiteBackend.start)
    maintenance_source = inspect.getsource(SQLiteBackend.integrity_check)
    assert "writer_connection.execute('PRAGMA integrity_check')" not in startup_source
    assert 'PRAGMA integrity_check' in maintenance_source


def test_process_service_reports_ready_and_releases_owner(tmp_path, monkeypatch):
    from lib.storage.service import (
        install_runtime_for_test,
        start_storage,
        stop_storage,
        storage_status,
    )

    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    runtime = StorageRuntime(StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60))
    install_runtime_for_test(runtime)
    try:
        assert storage_status()['state'] == 'stopped'
        start_storage()
        ready = storage_status()
        assert ready['ready'] is True
        assert ready['state'] == 'ready'
        assert ready['backend'] == 'sqlite'
        assert isinstance(ready['pid'], int)

        stop_storage(timeout=5.0)

        assert storage_status()['state'] == 'not_started'
        assert runtime.status()['state'] == 'stopped'
    finally:
        install_runtime_for_test(None)


def test_process_service_keeps_failed_stop_owner_discoverable():
    from lib.storage.service import (
        install_runtime_for_test,
        stop_storage,
        storage_runtime,
        storage_status,
    )

    class Runtime:
        fail_stop = True

        def stop(self, timeout=10.0):
            if self.fail_stop:
                raise RuntimeError(f'owner still alive after {timeout}s')

        def status(self):
            return {'ready': False, 'state': 'stopping', 'pid': 12345}

    runtime = Runtime()
    install_runtime_for_test(runtime)
    try:
        with pytest.raises(RuntimeError, match='owner still alive'):
            stop_storage(timeout=0.1)
        assert storage_runtime() is runtime
        assert storage_status()['state'] == 'stopping'

        runtime.fail_stop = False
        stop_storage(timeout=0.1)
        assert storage_status()['state'] == 'not_started'
    finally:
        runtime.fail_stop = False
        install_runtime_for_test(None)


def test_command_receipt_replay_and_conflict(storage, tmp_path):
    payload = {'namespace': 'contract', 'key': 'once', 'value': {'amount': 7}}
    first = storage.client.command('record.put', payload, 'same-command')
    replay = storage.client.command('record.put', payload, 'same-command')
    assert replay == first
    assert storage.client.query(
        'record.get', {'namespace': 'contract', 'key': 'once'})['version'] == 1

    if storage.client.health()['backend'] == 'sqlite':
        import sqlite3

        from lib.storage_sidecar.receipt_codec import command_receipt_key_v2

        connection = sqlite3.connect(
            f'file:{tmp_path / "data" / "tofu.db"}?mode=ro', uri=True)
        try:
            legacy_count = connection.execute(
                'SELECT COUNT(*) FROM storage_command_receipts '
                'WHERE command_id=?', ('same-command',)
            ).fetchone()[0]
            compact = connection.execute(
                'SELECT operation,length(command_key),length(request_digest) '
                'FROM storage_command_receipts_v2 WHERE command_key=?',
                (command_receipt_key_v2('same-command'),),
            ).fetchone()
        finally:
            connection.close()
        assert legacy_count == 0
        assert compact == ('record.put', 32, 32)

    with pytest.raises(StorageError) as raised:
        storage.client.command(
            'record.put', {**payload, 'value': {'amount': 8}}, 'same-command')
    assert raised.value.code == 'database_conflict'
    assert raised.value.retryable is False
    assert http_status_for_storage_error(raised.value) == 409


def test_incompressible_large_receipt_rolls_back_atomically(storage):
    value = ''.join(
        hashlib.sha256(str(index).encode()).hexdigest()
        for index in range(5_000)
    )
    client = storage.client
    client.command('conversation.create', {
        'conv_id': 'oversize-receipt-conv', 'user_id': 1,
        'title': 'Receipt rollback', 'created_at': 1, 'updated_at': 1,
        'settings': {},
    }, 'oversize-receipt-create')
    with pytest.raises(StorageError) as raised:
        client.command(
            'turn.append_settled', {
                'conversation_id': 'oversize-receipt-conv', 'user_id': 1,
                'command_id': 'oversize-incompressible-receipt',
                'actor': 'assistant', 'projection': {'content': value},
                'created_at': 2,
            },
            'oversize-incompressible-receipt',
        )

    assert raised.value.code == 'database_protocol_error'
    assert 'too large for a receipt' in raised.value.message
    document = client.query('conversation.get', {
        'conv_id': 'oversize-receipt-conv', 'user_id': 1,
    })
    assert document['messages'] == []
    assert client.query('turn.sync.snapshot', {
        'conversation_id': 'oversize-receipt-conv', 'user_id': 1,
    })['turns'] == []


def test_timer_domain_is_transactional_and_idempotent(storage):
    payload = {
        'timer_id': 'tmr_contract_1', 'user_id': 41,
        'conv_id': 'conv_contract_1',
        'check_instruction': 'wait for done',
        'continuation_message': 'continue', 'poll_interval': 10,
        'max_polls': 3, 'created_at': 'now', 'updated_at': 'now',
        'tools_config': {}, 'origin': 'background',
    }
    created = storage.client.command('timer.create', payload, payload['timer_id'])
    assert created['applied'] is True
    assert storage.client.command('timer.create', payload, payload['timer_id']) == created
    timer = storage.client.query('timer.get', {
        'timer_id': payload['timer_id'], 'user_id': payload['user_id']})
    assert timer['status'] == 'active'
    progress = storage.client.command('timer.update', {
        'timer_id': payload['timer_id'], 'poll_count': 1,
        'user_id': payload['user_id'],
        'last_poll_decision': 'wait', 'last_poll_reason': 'not yet',
        'updated_at': 'later',
    }, 'timer-progress-1')
    assert progress['changed'] is True
    log = storage.client.command('timer.poll.append', {
        'timer_id': payload['timer_id'], 'poll_time': 'later',
        'user_id': payload['user_id'],
        'decision': 'wait', 'reason': 'not yet', 'poll_id': 'poll-1',
    }, 'timer-poll-1')
    assert log['inserted'] is True
    assert storage.client.command('timer.poll.append', {
        'timer_id': payload['timer_id'], 'poll_time': 'later',
        'user_id': payload['user_id'],
        'decision': 'wait', 'reason': 'not yet', 'poll_id': 'poll-1',
    }, 'timer-poll-replay') == {'inserted': False, 'id': log['id']}
    assert storage.client.query('timer.poll.log', {
        'timer_id': payload['timer_id'], 'user_id': payload['user_id'],
        'limit': 10})[0]['poll_id'] == 'poll-1'
    assert storage.client.command('timer.cancel', {
        'timer_id': payload['timer_id'], 'now': 'cancelled',
        'user_id': payload['user_id'],
    }, 'timer-cancel-1')['changed'] is True
    assert storage.client.query('timer.get', {
        'timer_id': payload['timer_id'],
        'user_id': payload['user_id']})['status'] == 'cancelled'


def test_scheduler_domain_records_tasks_and_proactive_polls(storage):
    client = storage.client
    payload = {
        'task_id': 'task_contract_1', 'user_id': 42,
        'name': 'Contract task',
        'schedule': '*/5 * * * *', 'command': 'status',
        'task_type': 'agent', 'created_at': 'now', 'updated_at': 'now',
        'target_conv_id': 'conv-contract', 'tools_config': {},
        'condition_kind': 'hybrid',
    }
    created = client.command('scheduler.task.create', payload, payload['task_id'])
    assert created['applied'] is True
    assert client.command('scheduler.task.create', payload, payload['task_id']) == created
    assert client.query('scheduler.task.get', {
        'task_id': payload['task_id'],
        'user_id': payload['user_id']})['task_type'] == 'agent'
    assert client.command('scheduler.task.record_result', {
        'task_id': payload['task_id'], 'now': 'later', 'success': True,
        'user_id': payload['user_id'],
        'result': 'ok'}, 'scheduler-result-1')['changed'] is True
    client.command('scheduler.poll.append', {
        'task_id': payload['task_id'], 'poll_time': 'later',
        'user_id': payload['user_id'],
        'decision': 'skip', 'reason': 'waiting', 'status_snapshot': 'idle',
        'tier': 'hybrid', 'predicate_matched': 0, 'llm_agreed': 0,
    }, 'scheduler-poll-1')
    assert client.query('scheduler.poll.log', {
        'task_id': payload['task_id'], 'user_id': payload['user_id'],
        'limit': 10})[0]['decision'] == 'skip'


def test_scheduler_manager_uses_semantic_storage(storage, monkeypatch):
    from lib.identity import PrincipalContext
    from lib.scheduler.manager import ScheduledTaskManager

    monkeypatch.setattr('lib.scheduler.manager._scheduler_client',
                        lambda write=False: storage.client)
    manager = ScheduledTaskManager()
    task = manager.create_task(
        'Manager contract task', '*/5 * * * *', 'status',
        task_type='prompt',
        principal=PrincipalContext.user(
            subject_id='scheduler-contract-user',
            owner_user_id=1,
            scopes={'agents:scheduler'},
        ))
    assert manager.get_task(task['id'], user_id=1)['name'] == 'Manager contract task'
    assert manager.toggle_task(task['id'], user_id=1, enabled=False) is False
    assert manager.list_tasks(user_id=1) == []
    assert manager.list_tasks(user_id=1, include_disabled=True)[0]['enabled'] == 0
    assert manager.delete_task(task['id'], user_id=1) is True


def test_scheduler_manager_rejects_ownerless_or_unscoped_creation():
    from lib.identity import PrincipalContext
    from lib.scheduler.manager import ScheduledTaskManager

    manager = ScheduledTaskManager()
    args = ('Owner contract', '*/5 * * * *', 'status')
    with pytest.raises(TypeError, match='PrincipalContext'):
        manager.create_task(*args, principal=None)
    with pytest.raises(PermissionError, match='owning user'):
        manager.create_task(
            *args,
            principal=PrincipalContext.system(
                subject_id='ownerless-maintenance',
                scopes={'agents:scheduler'},
            ),
        )
    with pytest.raises(PermissionError, match='agents:scheduler'):
        manager.create_task(
            *args,
            principal=PrincipalContext.user(
                subject_id='unscoped-user', owner_user_id=1,
            ),
        )


def test_scheduler_defaults_and_due_claims_are_atomic(storage):
    payload = {
        'system_key': 'contract-default',
        'task_id': 'system-91-contract-default',
        'user_id': 91,
        'name': 'Contract default',
        'schedule': '* * * * *',
        'command': 'status',
        'task_type': 'prompt',
        'created_at': '2026-08-24T10:00:00',
        'updated_at': '2026-08-24T10:00:00',
        'tools_config': {},
    }
    first = storage.client.command(
        'scheduler.task.ensure', payload, 'scheduler-ensure-contract-1')
    second = storage.client.command(
        'scheduler.task.ensure', {
            **payload, 'name': 'Renamed contract default',
        }, 'scheduler-ensure-contract-2')
    assert first['created'] is True
    assert second['created'] is False
    assert second['updated'] is True
    assert first['task']['id'] == second['task']['id']
    assert second['task']['system_key'] == 'contract-default'
    assert second['task']['name'] == 'Renamed contract default'

    claim = {
        'task_id': payload['task_id'], 'user_id': payload['user_id'],
        'lane': 'run', 'now': '2026-08-24T10:01:00',
        'minimum_interval_seconds': 55,
    }
    claimed = storage.client.command(
        'scheduler.task.claim_due', claim, 'scheduler-claim-contract-1')
    assert claimed['claimed'] is True
    assert storage.client.command(
        'scheduler.task.claim_due', claim,
        'scheduler-claim-contract-1') == claimed
    assert storage.client.command(
        'scheduler.task.claim_due', claim,
        'scheduler-claim-contract-2')['claimed'] is False
    assert storage.client.command(
        'scheduler.task.claim_due', {
            **claim, 'now': '2026-08-24T10:02:00'},
        'scheduler-claim-contract-3')['claimed'] is True


def test_timer_and_scheduler_records_are_owner_scoped(storage):
    timer_payload = {
        'timer_id': 'tmr_owner_scope', 'user_id': 71,
        'conv_id': 'conv-owner-scope', 'check_instruction': 'ready?',
        'continuation_message': 'continue', 'poll_interval': 10,
        'max_polls': 2, 'created_at': 'now', 'updated_at': 'now',
        'tools_config': {}, 'origin': 'background',
    }
    storage.client.command(
        'timer.create', timer_payload, timer_payload['timer_id'])
    assert storage.client.query('timer.get', {
        'timer_id': timer_payload['timer_id'], 'user_id': 72}) is None
    assert storage.client.query('timer.list', {
        'user_id': 72, 'limit': 20}) == []
    assert storage.client.command('timer.cancel', {
        'timer_id': timer_payload['timer_id'], 'user_id': 72, 'now': 'later',
    }, 'wrong-owner-timer-cancel')['changed'] is False

    task_payload = {
        'task_id': 'scheduler_owner_scope', 'user_id': 71,
        'name': 'Owner task', 'schedule': '*/5 * * * *',
        'command': 'status', 'task_type': 'agent', 'created_at': 'now',
        'updated_at': 'now', 'target_conv_id': 'conv-owner-scope',
        'tools_config': {},
    }
    storage.client.command(
        'scheduler.task.create', task_payload, task_payload['task_id'])
    assert storage.client.query('scheduler.task.get', {
        'task_id': task_payload['task_id'], 'user_id': 72}) is None
    assert storage.client.query('scheduler.task.list', {
        'user_id': 72, 'limit': 20, 'enabled_only': False}) == []
    assert storage.client.command('scheduler.task.update', {
        'task_id': task_payload['task_id'], 'user_id': 72, 'enabled': 0,
    }, 'wrong-owner-scheduler-update')['changed'] is False
    assert storage.client.command('scheduler.task.delete', {
        'task_id': task_payload['task_id'], 'user_id': 72,
    }, 'wrong-owner-scheduler-delete')['deleted'] is False


def test_sidecar_turn_pair_enforces_lane_serialization(storage, monkeypatch):
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    from lib.turn_lifecycle import LifecycleConflict, create_turn_pair

    first = create_turn_pair(
        'conv-sidecar-lane-serialization', command_id='lane-first',
        input_projection={'content': 'first'}, config={}, user_id=81,
        conversation_defaults={
            'allowCreate': True, 'title': 'Lane test', 'settings': {}},
    )
    replay = create_turn_pair(
        'conv-sidecar-lane-serialization', command_id='lane-first',
        input_projection={'content': 'mutated retry'}, config={}, user_id=81,
    )
    assert replay['turn']['turnId'] == first['turn']['turnId']
    assert replay['idempotentReplay'] is True

    with pytest.raises(LifecycleConflict) as busy:
        create_turn_pair(
            'conv-sidecar-lane-serialization', command_id='lane-second',
            input_projection={'content': 'second'}, config={}, user_id=81,
        )
    assert busy.value.code == 'lane_busy'
    assert busy.value.turn['turnId'] == first['turn']['turnId']

    with pytest.raises(LifecycleConflict) as virtual_busy:
        create_turn_pair(
            'conv-sidecar-lane-serialization',
            command_id='lane-goal-continuation',
            input_projection={'content': 'continue'},
            config={},
            user_id=81,
            input_actor='virtual_user',
            require_lane_idle=True,
        )
    assert virtual_busy.value.code == 'lane_busy'

    with pytest.raises(LifecycleConflict) as advanced:
        create_turn_pair(
            'conv-sidecar-lane-serialization', command_id='lane-branch-stale',
            input_projection={'content': 'generated'}, config={}, user_id=81,
            input_actor='virtual_user',
            parent_turn_id=first['submittedTurn']['turnId'],
            require_parent_is_lane_tail=True,
        )
    assert advanced.value.code == 'lane_advanced'


def test_human_turn_atomically_supersedes_goal_continuation_intent(
    storage, monkeypatch,
):
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    from lib import message_queue as queue
    from lib.turn_lifecycle import create_turn_pair, fail_start

    conversation_id = 'conv-human-supersedes-goal'
    initial = create_turn_pair(
        conversation_id,
        command_id='goal-supersession-initial',
        input_projection={'content': 'initial'},
        config={},
        user_id=82,
        conversation_defaults={
            'allowCreate': True, 'title': 'Goal supersession', 'settings': {}},
    )
    fail_start(
        initial['attempt']['attemptId'],
        {'kind': 'fixture_settlement', 'message': 'make lane idle'},
        user_id=82,
    )
    queue.enqueue_message(
        conversation_id,
        {'text': 'continue old objective', '_goalContinuation': True},
        {'autopilot': True, '_goalObjective': 'old objective'},
        kind=queue.KIND_GOAL_CONTINUATION,
        user_id=82,
    )

    create_turn_pair(
        conversation_id,
        command_id='new-human-intent',
        input_projection={'content': 'new human objective'},
        config={},
        user_id=82,
    )

    assert queue.get_queue(conversation_id, user_id=82) == []


def test_leased_goal_continuation_rejects_newer_queued_human(
    storage, monkeypatch,
):
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    from lib import message_queue as queue
    from lib.turn_lifecycle import (
        LifecycleConflict,
        create_turn_pair,
        fail_start,
    )

    conversation_id = 'conv-goal-human-race'
    initial = create_turn_pair(
        conversation_id,
        command_id='goal-race-initial',
        input_projection={'content': 'initial'},
        config={},
        user_id=83,
        conversation_defaults={
            'allowCreate': True, 'title': 'Goal race', 'settings': {}},
    )
    fail_start(
        initial['attempt']['attemptId'],
        {'kind': 'fixture_settlement', 'message': 'make lane idle'},
        user_id=83,
    )
    queue.enqueue_message(
        conversation_id,
        {'text': 'new human objective'},
        {'model': 'test'},
        user_id=83,
    )

    with pytest.raises(LifecycleConflict) as superseded:
        create_turn_pair(
            conversation_id,
            command_id='leased-stale-goal',
            input_projection={'content': 'continue old objective'},
            config={},
            input_actor='virtual_user',
            require_lane_idle=True,
            reject_if_human_queued=True,
            user_id=83,
        )

    assert superseded.value.code == 'superseded_by_human'
    assert [row['kind'] for row in queue.get_queue(
        conversation_id, user_id=83)] == [queue.KIND_REAL]


def test_turn_lifecycle_sidecar_create_read_and_event_sequence(storage, monkeypatch):
    from lib.turn_lifecycle import (
        bind_task, claim_attempt_start, create_turn_pair, get_attempt,
        get_conversation_revision, get_turn, list_turns, mark_task_started, read_events,
        record_task_event,
    )
    command_priorities = []

    class _CapturingClient:
        def __getattr__(self, name):
            return getattr(storage.client, name)

        def command(self, operation, payload, command_id, priority='user',
                    deadline=None):
            command_priorities.append((operation, priority))
            return storage.client.command(
                operation, payload, command_id, priority=priority,
                deadline=deadline)

    client = _CapturingClient()
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: client)
    result = create_turn_pair(
        'turn-sidecar-conv', command_id='turn-command-1',
        input_projection={'content': 'hello'}, config={}, user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'Turn sidecar', 'createdAt': 1,
            'settings': {},
        })
    assert result['idempotentReplay'] is False
    replay_before_claim = create_turn_pair(
        'turn-sidecar-conv', command_id='turn-command-1',
        input_projection={'content': 'changed'}, config={}, user_id=1,
    )
    assert replay_before_claim['idempotentReplay'] is True
    assert replay_before_claim['_needsStart'] is True
    assert replay_before_claim['submittedTurn']['turnId'] == (
        result['submittedTurn']['turnId'])
    assert replay_before_claim['conversationRevision'] == 1
    assert replay_before_claim['streamCursor'] == 1
    assert get_turn('turn-sidecar-conv', result['turn']['turnId'], user_id=1)['status'] == 'pending'
    assert get_attempt(result['attempt']['attemptId'], user_id=1)['status'] == 'pending'
    assert len(list_turns('turn-sidecar-conv', user_id=1)['turns']) == 2
    assert get_conversation_revision('turn-sidecar-conv', user_id=1) == 1
    assert read_events(result['attempt']['attemptId'], user_id=1)[0]['type'] == 'status_changed'
    assert claim_attempt_start(result['attempt']['attemptId'], user_id=1) is True
    replay_after_claim = create_turn_pair(
        'turn-sidecar-conv', command_id='turn-command-1',
        input_projection={'content': 'changed'}, config={}, user_id=1,
    )
    assert replay_after_claim['idempotentReplay'] is True
    assert replay_after_claim['_needsStart'] is False
    attempt_id = result['attempt']['attemptId']
    assert bind_task(
        attempt_id, 'task-turn-1', user_id=1,
    )['status'] == 'pending'
    assert get_turn(
        'turn-sidecar-conv', result['turn']['turnId'], user_id=1,
    )['status'] == 'pending'
    assert mark_task_started(attempt_id, 'wrong-task', user_id=1) is None
    assert mark_task_started(attempt_id, 'task-turn-1', user_id=2) is None
    assert get_attempt(attempt_id, user_id=1)['status'] == 'pending'
    with pytest.raises(StorageError) as rebinding:
        bind_task(attempt_id, 'other-task', user_id=1)
    assert rebinding.value.code == 'database_conflict'

    events_before_start = len(read_events(attempt_id, user_id=1))
    started = mark_task_started(attempt_id, 'task-turn-1', user_id=1)
    assert started['status'] == 'running'
    assert isinstance(started['startedAt'], int)
    assert len(read_events(attempt_id, user_id=1)) == events_before_start + 1
    assert mark_task_started(
        attempt_id, 'task-turn-1', user_id=1,
    )['startedAt'] == started['startedAt']
    assert len(read_events(attempt_id, user_id=1)) == events_before_start + 1
    task = {
        'id': 'task-turn-1',
        '_attemptId': attempt_id,
        '_userId': 1,
        'content': 'working',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'status': 'running',
    }
    cache_before = storage.client.metrics()['turn_projection_cache']
    assert record_task_event(
        task, {'type': 'tool_start', 'toolName': 'run_command'}) is True
    cache_after_first = storage.client.metrics()['turn_projection_cache']
    assert cache_after_first['misses'] == cache_before['misses'] + 1
    assert cache_after_first['entries'] == 1

    task['content'] = 'still working'
    assert record_task_event(
        task, {'type': 'tool_end', 'toolName': 'run_command'}) is True
    cache_after_second = storage.client.metrics()['turn_projection_cache']
    assert cache_after_second['hits'] == cache_after_first['hits'] + 1
    assert cache_after_second['entries'] == 1
    projection_metrics = storage.client.metrics()['attempt_events'][
        'by_type']['projection_updated']
    assert projection_metrics['projection_blob_write_deferrals'] >= 1
    assert projection_metrics['projection_blob_write_deferred_bytes'] > 0
    assert projection_metrics['projection_checkpoint_materializations'] >= 1
    assert projection_metrics['projection_checkpoint_materialized_bytes'] > 0
    assert projection_metrics['projection_inline_released_bytes'] > 0

    task['content'] = 'done'
    task['status'] = 'done'
    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    assert storage.client.metrics()['turn_projection_cache']['entries'] == 0
    assert get_attempt(attempt_id, user_id=1)['status'] == 'completed'
    assert read_events(attempt_id, user_id=1)[-1]['type'] == 'terminal_settlement'
    assert ('turn.event.record', 'event') in command_priorities


def test_turn_event_record_preserves_empty_patch_without_projection_blob_write(
        storage, monkeypatch):
    """The shared SQLite/Postgres contract exposes exact no-op write savings."""
    from lib.turn_lifecycle import (
        bind_task,
        create_turn_pair,
        get_attempt,
        get_turn,
        read_events,
    )

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-unchanged-projection',
        command_id='turn-unchanged-projection-create',
        input_projection={'content': 'hello'},
        config={},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Unchanged projection',
            'createdAt': 1,
            'settings': {},
        },
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'turn-unchanged-projection-task', user_id=1)
    initial_turn = get_turn(
        'turn-unchanged-projection', turn_id, user_id=1)
    event_payload = {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'turn-unchanged-projection-task',
        'projection': initial_turn['projection'],
        'terminal': False,
        'status': 'running',
        'settlement': {},
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'phase'},
    }
    first = storage.client.command(
        'turn.event.record',
        event_payload,
        'turn-unchanged-projection-event-1',
        priority='event',
    )
    assert first['applied'] is True
    before_turn = get_turn(
        'turn-unchanged-projection', turn_id, user_id=1)
    before_metrics = storage.client.metrics()['attempt_events']
    before_type = before_metrics['by_type'].get('projection_updated', {})

    result = storage.client.command(
        'turn.event.record',
        event_payload,
        'turn-unchanged-projection-event-2',
        priority='event',
    )

    assert result['applied'] is True
    slim_payload = {
        **event_payload,
        'slim': True,
        'content': str(event_payload['projection'].get('content') or ''),
        'thinking': str(event_payload['projection'].get('thinking') or ''),
        'event_payload': {'updateKind': 'tool_progress'},
    }
    slim_result = storage.client.command(
        'turn.event.record',
        slim_payload,
        'turn-unchanged-projection-event-3',
        priority='event',
    )
    assert slim_result['applied'] is True
    after_turn = get_turn(
        'turn-unchanged-projection', turn_id, user_id=1)
    assert after_turn['projection'] == before_turn['projection']
    assert after_turn['projectionRevision'] == before_turn['projectionRevision'] + 2
    assert get_attempt(attempt_id, user_id=1)['status'] == 'running'
    assert read_events(
        attempt_id, user_id=1, projection_mode='patch')[-1][
            'payload']['projectionPatch']['operations'] == []

    after_type = storage.client.metrics()['attempt_events'][
        'by_type']['projection_updated']
    assert after_type['projection_blob_write_skips'] == (
        before_type.get('projection_blob_write_skips', 0) + 2)
    assert after_type['projection_blob_write_skipped_bytes'] > (
        before_type.get('projection_blob_write_skipped_bytes', 0))


def test_database_not_found_is_a_stable_404_storage_error():
    error = StorageError('database_not_found', 'Conversation not found')

    assert error.code == 'database_not_found'
    assert http_status_for_storage_error(error) == 404


def test_turn_lifecycle_sidecar_create_attempt_is_cas_and_idempotent(storage, monkeypatch):
    from lib.turn_lifecycle import (
        create_attempt, create_turn_pair, get_turn, record_task_event,
    )
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-attempt-sidecar', command_id='turn-attempt-create',
        input_projection={'content': 'hello'}, config={}, user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'Attempt sidecar', 'createdAt': 1,
            'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    record_task_event(
        {'_attemptId': attempt_id, '_userId': 1,
         'content': 'done', 'thinking': '',
         'toolRounds': [], 'segments': [], 'status': 'done'},
        {'type': 'done', 'finishReason': 'stop'})
    settled = get_turn('turn-attempt-sidecar', created['turn']['turnId'], user_id=1)
    next_attempt = create_attempt(
        'turn-attempt-sidecar', created['turn']['turnId'],
        command_id='turn-attempt-regenerate', operation='regenerate',
        expected_projection_revision=settled['projectionRevision'], user_id=1)
    assert next_attempt['idempotentReplay'] is False
    replay = create_attempt(
        'turn-attempt-sidecar', created['turn']['turnId'],
        command_id='turn-attempt-regenerate', operation='regenerate',
        expected_projection_revision=0, user_id=1)
    assert replay['idempotentReplay'] is True
    assert replay['attempt']['attemptId'] == next_attempt['attempt']['attemptId']


def test_turn_lifecycle_sidecar_projection_branch_and_delete(storage, monkeypatch):
    from lib.turn_lifecycle import (
        create_branch_lane, create_turn_pair, delete_branch_lane,
        delete_turns, get_turn, record_task_event, update_turn_projection,
    )
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-admin-sidecar', command_id='turn-admin-create',
        input_projection={'content': 'hello'}, config={}, user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'Admin sidecar', 'createdAt': 1,
            'settings': {},
        })
    record_task_event(
        {'_attemptId': created['attempt']['attemptId'], '_userId': 1,
         'content': 'done',
         'thinking': '', 'toolRounds': [], 'segments': [], 'status': 'done'},
        {'type': 'done', 'finishReason': 'stop'})
    turn = get_turn('turn-admin-sidecar', created['turn']['turnId'], user_id=1)
    edited = update_turn_projection(
        'turn-admin-sidecar', turn['turnId'], projection={
            'content': 'edited', 'role': 'assistant',
            '_turnId': turn['turnId'], '_projectionRevision': 999,
        },
        expected_projection_revision=turn['projectionRevision'], user_id=1)
    assert edited['turn']['projection'] == {
        'content': 'edited',
        'segments': [{
            'blockId': 'text:terminal',
            'deliverable': True,
            'terminal': True,
            'text': 'edited',
            'type': 'text',
        }],
    }
    lane = create_branch_lane(
        'turn-admin-sidecar', turn['turnId'], title='Branch',
        expected_projection_revision=edited['turn']['projectionRevision'], user_id=1)
    deleted_lane = delete_branch_lane(
        'turn-admin-sidecar', turn['turnId'], lane['lane']['laneId'], user_id=1)
    assert deleted_lane['deletedLaneId'] == lane['lane']['laneId']
    deleted = delete_turns(
        'turn-admin-sidecar', [turn['turnId']], user_id=1)
    assert turn['turnId'] in deleted['deletedTurnIds']


def test_manual_compaction_commits_through_real_sidecar_turn_authority(
        storage, monkeypatch):
    """DefaultConversationStore → receipt → turn.compact → derived v1 view."""
    from lib.tasks_pkg.persistence_store import DefaultConversationStore
    from lib.turn_lifecycle import create_turn_pair, record_task_event
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    parent = None
    for index in range(2):
        created = create_turn_pair(
            'manual-compact-sidecar', command_id=f'manual-pair-{index}',
            input_projection={'content': f'user {index}'}, config={},
            parent_turn_id=parent, user_id=1,
            conversation_defaults=(
                {'allowCreate': True, 'title': 'Native compact',
                 'createdAt': 1, 'settings': {}}
                if index == 0 else None))
        task = {
            '_attemptId': created['attempt']['attemptId'],
            '_userId': 1,
            'content': f'assistant {index}', 'thinking': '',
            'toolRounds': [], 'segments': [], 'status': 'done',
        }
        assert record_task_event(
            task, {'type': 'done', 'finishReason': 'stop'})
        parent = created['turn']['turnId']

    store = DefaultConversationStore()
    current, _updated_at, revision = store.load_transcript(
        'manual-compact-sidecar', user_id=1)
    assert len(current) == 4 and all(row.get('_turnId') for row in current)
    desired = [{
        'role': 'assistant',
        'content': '## native summary',
        '_isCompactionSummary': True,
        '_compactionArchiveId': 'archive-9',
    }, *current[2:]]
    command_id = 'manual-compact-real-sidecar'
    assert store.compact_turn_transcript(
        'manual-compact-sidecar', current, desired, revision,
        command_id=command_id, user_id=1) == 1
    # Lost-ACK replay of the exact command returns the committed result even
    # though the conversation revision has advanced since the original call.
    assert store.compact_turn_transcript(
        'manual-compact-sidecar', current, desired, revision,
        command_id=command_id, user_id=1) == 1

    settled, _updated_at, settled_revision = (
        store.load_transcript('manual-compact-sidecar', user_id=1))
    assert settled_revision == revision + 1
    assert [row['content'] for row in settled] == [
        '## native summary', 'user 1', 'assistant 1']
    assert settled[0]['role'] == 'assistant'
    assert settled[0]['compaction'] == {
        'archiveId': 'archive-9', 'blockId': 'compaction'}
    assert settled[0].get('_turnId')


def test_turn_lifecycle_sidecar_recovery_settles_pending_attempt(storage, monkeypatch):
    from lib.turn_lifecycle import (
        bind_task, create_turn_pair, get_attempt, get_turn, read_events,
        record_task_event, recover_running_attempts,
    )
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-recovery-sidecar', command_id='turn-recovery-create',
        input_projection={'content': 'hello'}, config={'model': 'gpt-4o'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'Recovery sidecar', 'createdAt': 1,
            'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    bind_task(attempt_id, 'task-recovery-sidecar', user_id=1)
    task = {
        '_attemptId': attempt_id, '_turnProtocolV2': True, '_userId': 1,
        'id': 'task-recovery-sidecar', 'status': 'running',
        'content': 'durable partial', 'thinking': 'work',
        'toolRounds': [{
            'roundNum': 1, 'llmRound': 0,
            'toolCallId': 'recovery-call',
            'toolName': 'read_files', 'toolArgs': '{}',
            'toolContent': 'checkpoint result', 'status': 'done',
            'assistantContent': 'checkpoint',
        }],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'delta', 'content': 'durable partial'})
    assert recover_running_attempts() == 1
    assert get_attempt(attempt_id, user_id=1)['status'] == 'interrupted'
    assert read_events(attempt_id, user_id=1)[-1]['type'] == 'terminal_settlement'
    # Parity with the legacy-DB recovery contract
    # (test_terminal_transaction_and_restart_recovery_preserve_projection in
    # tests/test_turn_lifecycle_v2.py): the recovered settlement must COMPUTE
    # its resume options from the durable projection, not hardcode
    # regenerate-only — the user can honestly continue from the tool-call
    # checkpoint (and losslessly, when the model supports assistant prefill).
    turn = get_turn('turn-recovery-sidecar', created['turn']['turnId'], user_id=1)
    assert turn['status'] == 'interrupted'
    assert turn['settlement']['cause'] == 'server_restart'
    operations = {item['operation']
                  for item in turn['settlement']['resumeOptions']}
    assert 'continue' in operations
    assert 'checkpoint_resume' in operations
    assert 'regenerate' in operations
    assert recover_running_attempts() == 0


@pytest.mark.parametrize("settlement", [
    {"resumeOptions": {}},
    {"resumeOptions": [None]},
    {"resumeOptions": [{"operation": "continue", "anchor": []}]},
    {"resumeOptions": [{"operation": "", "anchor": {}}]},
    {"resumeOptions": [
        {"operation": "continue", "anchor": {}},
        {"operation": "continue", "anchor": {"type": "other"}},
    ]},
])
def test_stored_resume_options_reject_malformed_or_ambiguous_authority(
        settlement):
    from lib.storage_sidecar.operations_pkg._turns import (
        _validated_resume_option_anchors,
    )

    with pytest.raises(StorageError) as raised:
        _validated_resume_option_anchors(settlement)

    assert raised.value.code == "database_protocol_error"


def test_stored_resume_options_keep_legacy_strings_and_exact_anchors():
    from lib.storage_sidecar.operations_pkg._turns import (
        _validated_resume_option_anchors,
    )

    options, anchors = _validated_resume_option_anchors({
        "resumeOptions": [
            "continue",
            {"operation": "checkpoint_resume", "anchor": {
                "type": "tool_checkpoint", "keptToolRounds": 2,
            }},
        ],
    })

    assert options == {"continue", "checkpoint_resume"}
    assert anchors == {
        "continue": {},
        "checkpoint_resume": {
            "type": "tool_checkpoint", "keptToolRounds": 2,
        },
    }


@pytest.mark.parametrize("stored_value", [None, [], "[]", 0, False])
def test_object_valued_turn_authority_never_truthiness_repairs_corruption(
        stored_value):
    from lib.storage_sidecar.operations_pkg._turns import _stored_object

    with pytest.raises(StorageError) as raised:
        _stored_object(stored_value, "test field")

    assert raised.value.code == "database_integrity"


def test_turn_recovery_sidecar_chunked_and_liveness_guards(storage, monkeypatch):
    """2026-08-19 "回答中/重连中" zombie-turn incident contracts:

    1. ``turn.recover`` settles a BOUNDED chunk per call and reports
       ``remaining`` (a single unbounded transaction blew the 5s writer
       watchdog on multi-MiB projections and rolled the whole recovery back).
    2. ``recover_running_attempts`` loops the chunks to completion.
    3. The liveness guards (``created_before_ms`` / ``exclude_task_ids``)
       used by the post-serving backstop never sweep a live/new attempt.
    """
    from lib.turn_lifecycle import (
        bind_task, create_turn_pair, get_attempt, recover_running_attempts,
    )
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    attempt_ids = []
    for i in range(3):
        conv_id = f'turn-recovery-chunked-{i}'
        created = create_turn_pair(
            conv_id, command_id=f'chunk-create-{i}',
            input_projection={'content': f'q{i}'},
            config={'model': 'gpt-4o'}, user_id=1,
            conversation_defaults={
                'allowCreate': True,
                'title': f'Chunked recovery {i}',
                'createdAt': 1,
                'settings': {},
            })
        attempt_id = created['attempt']['attemptId']
        bind_task(attempt_id, f'task-chunk-{i}', user_id=1)
        attempt_ids.append(attempt_id)

    # 1. One row per chunk → progress + honest remainder each call.
    first = storage.client.command(
        'turn.recover', {'max_rows': 1}, 'chunk-recover-1')
    assert first == {'recovered': 1, 'remaining': 2}
    second = storage.client.command(
        'turn.recover', {'max_rows': 1}, 'chunk-recover-2')
    assert second == {'recovered': 1, 'remaining': 1}

    # 3a. Live-registry exclusion: the surviving attempt's task is "live".
    guarded = storage.client.command(
        'turn.recover', {'exclude_task_ids': ['task-chunk-2']},
        'chunk-recover-3')
    assert guarded == {'recovered': 0, 'remaining': 0}
    assert get_attempt(attempt_ids[2], user_id=1)['status'] in {'pending', 'running'}

    # 3b. Created-before gate: a gate older than every attempt settles none.
    assert recover_running_attempts(created_before_ms=1) == 0
    assert get_attempt(attempt_ids[2], user_id=1)['status'] in {'pending', 'running'}

    # 2. Unguarded sweep loops chunks (default budget) and finishes the job.
    assert recover_running_attempts() == 1
    assert get_attempt(attempt_ids[2], user_id=1)['status'] == 'interrupted'
    assert recover_running_attempts() == 0


def test_turn_native_conversation_reports_truthful_msg_count(storage, monkeypatch):
    """Sidebar metadata is derived from turns without protocol settings."""
    from lib.turn_lifecycle import create_turn_pair
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)

    created = create_turn_pair(
        'turn-native-conv', command_id='turn-native-create',
        input_projection={'content': 'hello'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Native',
                               'createdAt': 1, 'settings': {'model': 'm1'}})
    assert created['idempotentReplay'] is False

    listed = storage.client.query(
        'conversation.list', {'user_id': 1, 'order_by': 'id_asc',
                              'include_messages': False})
    by_id = {d['metadata']['id']: d['metadata'] for d in listed}
    assert by_id['turn-native-conv']['msg_count'] == 2
    assert by_id['turn-native-conv']['settings'] == {'model': 'm1'}

    got = storage.client.query(
        'conversation.get', {'conv_id': 'turn-native-conv', 'user_id': 1})
    assert got['metadata']['msg_count'] == 2
    assert got['metadata']['settings'] == {'model': 'm1'}

    # A legacy blob conversation (non-zero archive msg_count) is untouched.
    _import_conversation(
        storage.client,
        'legacy-blob-conv',
        messages=[{'role': 'user', 'content': 'q'}],
        title='Legacy',
    )
    legacy = storage.client.query(
        'conversation.get', {'conv_id': 'legacy-blob-conv', 'user_id': 1})
    assert legacy['metadata']['msg_count'] == 1
    assert legacy['metadata']['settings'] == {}


def test_conversation_list_project_filter_precedes_limit_and_owner(storage):
    client = storage.client
    _import_conversation(
        client,
        'project-filter-owned-match',
        user_id=1,
        title='Owned match',
        settings={'projectPath': '/projects/alpha', 'large': 'x' * 10_000},
        updated_at=10,
    )
    _import_conversation(
        client,
        'project-filter-owned-other',
        user_id=1,
        title='Newer other project',
        settings={'projectPath': '/projects/beta'},
        updated_at=30,
    )
    _import_conversation(
        client,
        'project-filter-foreign-match',
        user_id=2,
        title='Newest foreign match',
        settings={'projectPath': '/projects/alpha'},
        updated_at=40,
    )

    listed = client.query('conversation.list', {
        'user_id': 1,
        'project_path': '/projects/alpha',
        'order_by': 'updated_at_desc',
        'limit': 1,
        'include_messages': False,
        'settings_keys': [],
    })

    assert [row['metadata']['id'] for row in listed] == [
        'project-filter-owned-match'
    ]
    assert listed[0]['metadata']['settings'] == {}

    for invalid in ('', 7, True, []):
        with pytest.raises(StorageError) as raised:
            client.query('conversation.list', {
                'user_id': 1,
                'project_path': invalid,
                'include_messages': False,
            })
        assert raised.value.code == 'database_protocol_error'


def test_conversation_list_title_filter_is_exact_bounded_and_owner_scoped(
    storage, monkeypatch
):
    from lib.storage_sidecar.operations_pkg import _conversations

    monkeypatch.setattr(_conversations, '_TITLE_FILTER_SCAN_PAGE_SIZE', 2)
    client = storage.client
    conversations = [
        ('title-filter-newest-miss', 1, 'Unrelated', '/projects/alpha', 50),
        ('title-filter-second-miss', 1, 'Still unrelated', '/projects/alpha', 40),
        ('title-filter-unicode', 1, 'KEY 100%_plan', '/projects/alpha', 30),
        ('title-filter-older', 1, 'key older', '/projects/alpha', 20),
        ('title-filter-other-project', 1, 'key elsewhere', '/projects/beta', 60),
        ('title-filter-foreign', 2, 'key foreign', '/projects/alpha', 70),
    ]
    for conversation_id, user_id, title, project_path, updated_at in conversations:
        _import_conversation(
            client,
            conversation_id,
            user_id=user_id,
            title=title,
            settings={'projectPath': project_path},
            updated_at=updated_at,
        )

    listed = client.query('conversation.list', {
        'user_id': 1,
        'project_path': '/projects/alpha',
        'title_contains': 'key',
        'order_by': 'updated_at_desc',
        'limit': 1,
        'include_messages': False,
        'settings_keys': [],
    })
    assert [row['metadata']['id'] for row in listed] == [
        'title-filter-unicode'
    ]

    literal = client.query('conversation.list', {
        'user_id': 1,
        'title_contains': '100%_',
        'order_by': 'id_asc',
        'limit': 10,
        'include_messages': False,
    })
    assert [row['metadata']['id'] for row in literal] == [
        'title-filter-unicode'
    ]

    for invalid in ('', 7, True, [], 'x' * 513):
        with pytest.raises(StorageError) as raised:
            client.query('conversation.list', {
                'user_id': 1,
                'title_contains': invalid,
                'include_messages': False,
            })
        assert raised.value.code == 'database_protocol_error'


def test_turn_only_header_and_settled_ingestion_are_idempotent(storage):
    client = storage.client
    client.command('conversation.create', {
        'conv_id': 'settled-ingestion', 'user_id': 1,
        'title': 'Ingested', 'created_at': 10, 'updated_at': 10,
        'settings': {'model': 'm1'},
    }, 'settled-ingestion:create')
    payloads = [
        {
            'conversation_id': 'settled-ingestion', 'user_id': 1,
            'command_id': 'settled-ingestion:user', 'actor': 'human',
            'projection': {'content': 'question', 'timestamp': 10},
            'created_at': 10,
        },
        {
            'conversation_id': 'settled-ingestion', 'user_id': 1,
            'command_id': 'settled-ingestion:assistant', 'actor': 'assistant',
            'projection': {'content': 'answer', 'timestamp': 20},
            'created_at': 20,
        },
    ]
    first = client.command(
        'turn.append_settled', payloads[0], 'settled-ingestion:user:receipt')
    replay = client.command(
        'turn.append_settled', payloads[0], 'settled-ingestion:user:receipt')
    assert replay == first
    client.command(
        'turn.append_settled', payloads[1],
        'settled-ingestion:assistant:receipt')

    document = client.query('conversation.get', {
        'conv_id': 'settled-ingestion', 'user_id': 1,
    })
    assert [message['content'] for message in document['messages']] == [
        'question', 'answer']
    assert document['metadata']['msg_count'] == 2
    snapshot = client.query('turn.sync.snapshot', {
        'conversation_id': 'settled-ingestion', 'user_id': 1,
    })
    assert [turn['ordinal'] for turn in snapshot['turns']] == [0, 1]
    assert snapshot['settings'] == {'model': 'm1'}

    with pytest.raises(StorageError, match='Unknown storage operation'):
        client.command('conversation.import_batch', {}, 'retired-import')
    with pytest.raises(StorageError, match='Unknown storage operation'):
        client.command('turn.archive.migrate', {}, 'retired-archive-migrate')


def test_turn_projection_storage_codec_is_private_and_lossless(
        storage, tmp_path):
    """Large private receipts and durable interning remain lossless."""
    payload = 'projection-result-' * 20_000
    projection = {
        'content': 'answer',
        'segments': [{
            'type': 'tool_use', 'id': 'codec-call', 'name': 'read_files',
            'input': '{"paths":["a.py"]}',
            'result': {'content': payload, 'isError': False},
        }],
        'toolRounds': [{
            'toolCallId': 'codec-call', 'toolName': 'read_files',
            'toolArgs': '{"paths":["a.py"]}', 'toolContent': payload,
        }],
    }
    client = storage.client
    client.command('conversation.create', {
        'conv_id': 'projection-codec-conv', 'user_id': 1, 'title': 'Codec',
        'created_at': 1, 'updated_at': 1, 'settings': {},
    }, 'projection-codec-create')
    command_payload = {
        'conversation_id': 'projection-codec-conv', 'user_id': 1,
        'command_id': 'projection-codec-append', 'actor': 'assistant',
        'projection': projection, 'created_at': 2,
    }
    first = client.command(
        'turn.append_settled', command_payload, 'projection-codec-append')
    replay = client.command(
        'turn.append_settled', command_payload, 'projection-codec-append')
    assert replay == first

    document = storage.client.query('conversation.get', {
        'conv_id': 'projection-codec-conv', 'user_id': 1,
    })
    assert len(document['messages']) == 1
    public_message = document['messages'][0]
    assert public_message['segments'] == projection['segments']
    assert public_message['toolRounds'] == projection['toolRounds']
    assert '_tofuStorageProjectionCodec' not in public_message

    if storage.client.health()['backend'] == 'sqlite':
        # Read-only WAL inspection proves that the private representation is
        # actually durable, not merely an in-memory codec unit-test result.
        import sqlite3

        database_path = tmp_path / 'data' / 'tofu.db'
        connection = sqlite3.connect(
            f'file:{database_path}?mode=ro', uri=True)
        try:
            projection_row = connection.execute(
                'SELECT projection_json FROM storage_conversation_turns '
                'WHERE conversation_id=? AND user_id=?',
                ('projection-codec-conv', 1),
            ).fetchone()
            from lib.storage_sidecar.receipt_codec import command_receipt_key_v2
            receipt_row = connection.execute(
                'SELECT response_json FROM storage_command_receipts_v2 '
                'WHERE command_key=?',
                (command_receipt_key_v2('projection-codec-append'),),
            ).fetchone()
        finally:
            connection.close()
        stored_projection = json.loads(projection_row[0])
        assert '_tofuStorageProjectionCodec' in stored_projection
        assert 'input' not in stored_projection['segments'][0]
        assert 'content' not in stored_projection['segments'][0]['result']
        from lib.storage_sidecar.receipt_codec import COMPRESSED_RECEIPT_MAGIC
        assert receipt_row[0].startswith(COMPRESSED_RECEIPT_MAGIC)
        assert len(receipt_row[0]) <= 64 * 1024


def test_turn_lifecycle_sidecar_visible_sync_is_replay_safe(storage, monkeypatch):
    from lib.turn_lifecycle import (
        create_turn_pair,
        get_turn,
        sync_visible_run_turns,
    )
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-visible-sidecar', command_id='turn-visible-create',
        input_projection={'content': 'hello'}, config={}, user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'Visible sidecar', 'createdAt': 1,
            'settings': {},
        })
    task = {'_attemptId': created['attempt']['attemptId'], '_userId': 1,
            '_turnId': created['turn']['turnId'], 'convId': 'turn-visible-sidecar',
            'config': {}}
    messages = [{'role': 'assistant', 'content': 'phase one'},
                {'role': 'assistant', 'content': 'phase two'}]
    sync_visible_run_turns(task, messages)
    first = list(task['_turnVisibleRunTurnIds'])
    changes = storage.client.query(
        'turn.sync.changes', {
            'conversation_id': 'turn-visible-sidecar',
            'user_id': 1,
            'after': 1,
        },
    )['events']
    assert len(changes) == 1
    visible_event = changes[0]['payload']['event']
    visible_payload = visible_event['payload']
    patch = visible_payload['projectionPatch']
    assert patch['baseRevision'] == created['turn']['projectionRevision']
    assert patch['targetRevision'] == created['turn']['projectionRevision'] + 1
    assert get_turn(
        'turn-visible-sidecar', created['turn']['turnId'], user_id=1,
    )['projectionRevision'] == patch['targetRevision']

    child_turn = visible_payload['turns'][0]
    child_events = storage.client.query(
        'turn.events.list', {
            'attempt_id': child_turn['currentAttemptId'],
            'user_id': 1,
            'projection_mode': 'patch',
        },
    )
    assert len(child_events) == 1
    assert 'projection' not in child_events[0]['payload']

    sync_visible_run_turns(task, messages)
    assert task['_turnVisibleRunTurnIds'] == first
    assert storage.client.query(
        'turn.sync.snapshot', {
            'conversation_id': 'turn-visible-sidecar', 'user_id': 1,
        },
    )['syncSequence'] == changes[0]['syncSeq']


def _record_streaming_projection_events(
        storage, attempt_id, task_id, projections, command_prefix):
    for index, projection in enumerate(projections):
        result = storage.client.command(
            'turn.event.record',
            {
                'attempt_id': attempt_id,
                'user_id': 1,
                'task_id': task_id,
                'projection': projection,
                'terminal': False,
                'status': 'running',
                'settlement': {},
                'event_type': 'projection_updated',
                'event_payload': {'updateKind': 'stream'},
            },
            f'{command_prefix}-{index}',
            priority='event',
        )
        assert result['applied'] is True


def test_visible_sync_advances_revision_across_live_projection_head(
        storage, monkeypatch):
    """A visible-sync root advance MUST keep the live patch head foldable."""
    from lib.turn_lifecycle import (
        bind_task,
        create_turn_pair,
        get_turn,
        sync_visible_run_turns,
    )
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-visible-live-head',
        command_id='turn-visible-live-head-create',
        input_projection={'content': 'hello'},
        config={},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Visible live head',
            'createdAt': 1,
            'settings': {},
        },
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'turn-visible-live-head-task', user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        '_turnId': turn_id,
        'convId': 'turn-visible-live-head',
        'config': {},
    }
    # The first sync flips the root into a flow turn (head-clearing reset).
    sync_visible_run_turns(task, [{'role': 'assistant', 'content': 'phase one'}])
    # Streaming events rebuild a live patch head on the flow root.
    _record_streaming_projection_events(
        storage,
        attempt_id,
        'turn-visible-live-head-task',
        [
            {'content': 'stream one', 'thinking': '',
             'segments': [], 'toolRounds': []},
            {'content': 'stream two', 'thinking': '',
             'segments': [], 'toolRounds': []},
        ],
        'turn-visible-live-head-event',
    )
    # Committing the next visible phase advances the root revision across
    # the live head; hydration must survive that advance.
    sync_visible_run_turns(task, [
        {'role': 'assistant', 'content': 'phase one'},
        {'role': 'assistant', 'content': 'phase two'},
    ])
    refreshed = get_turn('turn-visible-live-head', turn_id, user_id=1)
    assert refreshed['projection']['content'] == 'stream two'
    # bind +1, flow flip +1, two streams +2, sync bump +1.
    assert refreshed['projectionRevision'] == (
        created['turn']['projectionRevision'] + 5)
    assert len(task['_turnVisibleRunTurnIds']) == 2


def test_visible_sync_restores_live_round_fidelity(storage, monkeypatch):
    """Brief flow-summary rounds inherit full tool facts from the live root.

    The flow projection carries bounded preview rounds (query + result
    snippet, no toolArgs); the root turn's live checkpoint holds the same
    rounds at full fidelity under the same toolCallId.  The sync boundary
    must rebuild display-complete durable turns from it: fill-only, so the
    flow projection's own values always win.
    """
    from lib.turn_lifecycle import (
        bind_task,
        create_turn_pair,
        get_turn,
        sync_visible_run_turns,
    )
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-visible-fidelity',
        command_id='turn-visible-fidelity-create',
        input_projection={'content': 'hello'},
        config={},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Visible fidelity',
            'createdAt': 1,
            'settings': {},
        },
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'turn-visible-fidelity-task', user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        '_turnId': turn_id,
        'convId': 'turn-visible-fidelity',
        'config': {},
    }
    live_round = {
        'toolCallId': 'call-live-1',
        'toolName': 'run_command',
        'toolArgs': {'command': 'ls -la', 'description': 'list'},
        'toolContent': '{"exitCode": 0}',
        'status': 'done',
        'roundNum': 1,
        'llmRound': 1,
        'tStart': 10,
        'tEnd': 20,
        'attemptId': attempt_id,
        'taskId': 'turn-visible-fidelity-task',
    }
    _record_streaming_projection_events(
        storage,
        attempt_id,
        'turn-visible-fidelity-task',
        [{'content': 'stream', 'thinking': 'live reasoning',
          'segments': [], 'toolRounds': [live_round]}],
        'turn-visible-fidelity-event',
    )
    brief_round = {
        'toolCallId': 'call-live-1',
        'toolName': 'run_command',
        'query': 'ls -la',
        'status': 'done',
        'roundNum': 1,
        'llmRound': 1,
        'tStart': 10,
        'results': [{'snippet': 'ok'}],
    }
    sync_visible_run_turns(task, [
        {'role': 'assistant', 'content': 'phase one',
         'thinking': 'node one reasoning', 'toolRounds': [brief_round]},
    ])
    projection = get_turn(
        'turn-visible-fidelity', turn_id, user_id=1)['projection']
    (round_dict,) = [item for item in projection['toolRounds']
                     if item.get('toolCallId') == 'call-live-1']
    assert round_dict['toolArgs'] == {'command': 'ls -la', 'description': 'list'}
    assert round_dict['toolContent'] == '{"exitCode": 0}'
    assert round_dict['tEnd'] == 20
    assert round_dict['attemptId'] == attempt_id
    # Fill-only: the flow projection's own fields are never overwritten.
    assert round_dict['query'] == 'ls -la'
    assert round_dict['results'] == [{'snippet': 'ok'}]
    segment = next(item for item in projection['segments']
                   if item.get('type') == 'tool_use'
                   and item.get('id') == 'call-live-1')
    assert segment['input'] == {'command': 'ls -la', 'description': 'list'}
    assert segment['result']['content'] == '{"exitCode": 0}'
    assert projection['thinking'] == 'node one reasoning'

    # The next node commits as a child turn; it inherits from that node's
    # live rounds currently checkpointed on the root.
    live_round_two = dict(live_round, toolCallId='call-live-2',
                          toolArgs={'command': 'pytest -q'})
    _record_streaming_projection_events(
        storage,
        attempt_id,
        'turn-visible-fidelity-task',
        [{'content': 'stream two', 'thinking': '',
          'segments': [], 'toolRounds': [live_round_two]}],
        'turn-visible-fidelity-event-two',
    )
    sync_visible_run_turns(task, [
        {'role': 'assistant', 'content': 'phase one',
         'thinking': 'node one reasoning', 'toolRounds': [brief_round]},
        {'role': 'assistant', 'content': 'phase two',
         'thinking': 'node two reasoning',
         'toolRounds': [dict(brief_round, toolCallId='call-live-2',
                             query='pytest -q')]},
    ])
    child = get_turn('turn-visible-fidelity',
                     task['_turnVisibleRunTurnIds'][1], user_id=1)
    (child_round,) = [item for item in child['projection']['toolRounds']
                      if item.get('toolCallId') == 'call-live-2']
    assert child_round['toolArgs'] == {'command': 'pytest -q'}
    assert child_round['query'] == 'pytest -q'
    child_segment = next(
        item for item in child['projection']['segments']
        if item.get('type') == 'tool_use' and item.get('id') == 'call-live-2')
    assert child_segment['input'] == {'command': 'pytest -q'}
    assert child['projection']['thinking'] == 'node two reasoning'


def test_task_projection_preserves_committed_thinking_for_visible_runs():
    """Once a run owns visible turns, the per-node buffer reset must not
    wipe the first turn's committed thinking (same guard as content)."""
    from lib.turn_lifecycle import _delta_text_fields, _task_projection
    previous = {'content': 'phase one', 'thinking': 'node one reasoning',
                'toolRounds': []}
    task = {'config': {}, 'content': '', 'thinking': '',
            '_turnVisibleRunTurnIds': ['root', 'child-1']}
    projection = _task_projection(task, previous)
    assert projection['thinking'] == 'node one reasoning'
    assert projection['content'] == 'phase one'
    assert _delta_text_fields(task, previous) == (
        'phase one', 'node one reasoning')
    # Without visible-run ownership the live buffer still folds through.
    live_task = {'config': {}, 'content': 'c', 'thinking': 'live'}
    assert _task_projection(live_task, previous)['thinking'] == 'live'
    assert _delta_text_fields(live_task, previous) == ('c', 'live')
    # None thinking falls back to the previous projection either way.
    assert _task_projection(
        {'config': {}}, previous)['thinking'] == 'node one reasoning'


def test_related_announce_advances_revision_across_live_projection_head(
        storage, monkeypatch):
    """A related-turn announce MUST NOT orphan the root's live patch head."""
    from lib.turn_lifecycle import bind_task, create_turn_pair, get_turn
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-related-live-head',
        command_id='turn-related-live-head-create',
        input_projection={'content': 'hello'},
        config={},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Related live head',
            'createdAt': 1,
            'settings': {},
        },
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'turn-related-live-head-task', user_id=1)
    _record_streaming_projection_events(
        storage,
        attempt_id,
        'turn-related-live-head-task',
        [
            {'content': 'stream one', 'thinking': '',
             'segments': [], 'toolRounds': []},
            {'content': 'stream two', 'thinking': '',
             'segments': [], 'toolRounds': []},
        ],
        'turn-related-live-head-event',
    )
    input_turn_id = next(
        row['turnId']
        for row in storage.client.query('turn.list', {
            'conversation_id': 'turn-related-live-head',
            'user_id': 1,
        })
        if row['turnId'] != turn_id
    )
    storage.client.command(
        'turn.related.announce',
        {
            'attempt_id': attempt_id,
            'user_id': 1,
            'turn_ids': [input_turn_id],
        },
        'turn-related-live-head-announce',
    )
    refreshed = get_turn('turn-related-live-head', turn_id, user_id=1)
    assert refreshed['projection']['content'] == 'stream two'
    # bind +1, two streams +2, announce bridge +1.
    assert refreshed['projectionRevision'] == (
        created['turn']['projectionRevision'] + 4)


def test_related_announce_bridges_externalized_projection_checkpoint(
        storage, monkeypatch):
    """A checkpoint-fenced row MUST gain a one-patch bridge on announce."""
    from lib.turn_lifecycle import bind_task, create_turn_pair, get_turn
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-related-checkpoint',
        command_id='turn-related-checkpoint-create',
        input_projection={'content': 'hello'},
        config={},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Related checkpoint',
            'createdAt': 1,
            'settings': {},
        },
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'turn-related-checkpoint-task', user_id=1)
    large = 'x' * (700 * 1024)
    # The second event exhausts the 1 MiB head budget, externalizing the
    # projection into a revision-fenced checkpoint.
    _record_streaming_projection_events(
        storage,
        attempt_id,
        'turn-related-checkpoint-task',
        [
            {'content': large + ' one', 'thinking': '',
             'segments': [], 'toolRounds': []},
            {'content': large + ' two', 'thinking': '',
             'segments': [], 'toolRounds': []},
        ],
        'turn-related-checkpoint-event',
    )
    input_turn_id = next(
        row['turnId']
        for row in storage.client.query('turn.list', {
            'conversation_id': 'turn-related-checkpoint',
            'user_id': 1,
        })
        if row['turnId'] != turn_id
    )
    storage.client.command(
        'turn.related.announce',
        {
            'attempt_id': attempt_id,
            'user_id': 1,
            'turn_ids': [input_turn_id],
        },
        'turn-related-checkpoint-announce',
    )
    refreshed = get_turn('turn-related-checkpoint', turn_id, user_id=1)
    assert refreshed['projection']['content'] == large + ' two'
    # bind +1, two streams +2, announce bridge +1.
    assert refreshed['projectionRevision'] == (
        created['turn']['projectionRevision'] + 4)

def test_visible_turn_shape_projects_orchestration_header_facts():
    from lib.storage_sidecar.operations_pkg._turns import _visible_shape

    actor, kind, projection = _visible_shape({
        'role': 'user',
        'content': 'revise the plan',
        '_isFlowReview': True,
        '_flowIteration': 3,
        '_flowApproved': False,
        '_flowNextPhase': 'planner',
        '_isStuck': True,
        'model': 'deepseek-v4-pro',
        'orchestration': {'modelRoute': {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        }},
    }, 'flow_node')

    assert (actor, kind) == ('critic', 'flow_node')
    assert projection['orchestration'] == {
        'iteration': 3,
        'approved': False,
        'nextPhase': 'planner',
        'stuck': True,
        'modelRoute': {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        },
    }
    assert projection['model'] == 'deepseek-v4-pro'
    assert not any(key.startswith('_ep') or key == '_isStuck'
                   for key in projection)


def test_visible_turn_shape_assembles_durable_segments_and_file_block():
    """A visible-sync message arrives with toolRounds but no segments: the
    write must persist the assembled timeline (tools inline, terminal text
    last) plus the derived file-changes block. The settled surface renders
    from durable segments — a serve-time repair cannot fix a browser that
    consumed the raw sync event."""
    from lib.storage_sidecar.operations_pkg._turns import _visible_shape

    actor, kind, projection = _visible_shape({
        'role': 'assistant',
        'content': 'final answer',
        'thinking': '',
        '_flowIteration': 2,
        'toolRounds': [{
            'roundNum': 1, 'toolCallId': 'flow-tool-x',
            'toolName': 'run_command', 'query': 'make test',
            'status': 'done',
            'results': [{'toolName': 'run_command', 'command': 'make test',
                         'exitCode': 0, 'fetched': True}],
        }],
        'modifiedFileList': [{'action': 'patched', 'path': 'lib/a.py'}],
        'modifiedFiles': 1,
    }, 'flow_node')

    assert (actor, kind) == ('assistant', 'flow_node')
    kinds = [segment.get('type') for segment in projection['segments']]
    assert kinds == ['tool_use', 'text'], kinds
    assert projection['segments'][0]['name'] == 'run_command'
    assert projection['segments'][-1]['text'] == 'final answer'
    assert projection['modifiedFileList'] == [
        {'action': 'patched', 'path': 'lib/a.py'}]
    assert projection['modifiedFiles'] == 1
    block = projection['fileChanges']
    assert block['blockId'] == 'file-changes'
    assert block['count'] == 1


def test_recent_projects_are_atomic_and_owner_scoped(storage):
    client = storage.client
    first_payload = {
        'user_id': 1, 'project_path': '/workspace/a', 'last_used': 100,
    }
    assert client.command(
        'project.recent.touch', first_payload, 'recent-owner-1-a-first'
    )['count'] == 1
    # Ambiguous-ACK replay uses the same command identity and must not apply
    # the logical touch twice.
    assert client.command(
        'project.recent.touch', first_payload, 'recent-owner-1-a-first'
    )['count'] == 1
    assert client.command('project.recent.touch', {
        'user_id': 1, 'project_path': '/workspace/a', 'last_used': 200,
    }, 'recent-owner-1-a-second')['count'] == 2
    assert client.command('project.recent.touch', {
        'user_id': 2, 'project_path': '/workspace/a', 'last_used': 300,
    }, 'recent-owner-2-a')['count'] == 1

    assert client.query('project.recent.list', {'user_id': 1}) == [{
        'path': '/workspace/a', 'count': 2, 'last_used': 200,
    }]
    assert client.query('project.recent.list', {'user_id': 2}) == [{
        'path': '/workspace/a', 'count': 1, 'last_used': 300,
    }]
    assert client.command('project.recent.clear', {
        'user_id': 1,
    }, 'recent-owner-1-clear') == {'deleted': 1}
    assert client.query('project.recent.list', {'user_id': 1}) == []
    assert len(client.query('project.recent.list', {'user_id': 2})) == 1


def test_recent_project_batch_is_bounded_atomic_and_replay_safe(storage):
    client = storage.client
    payload = {
        'user_id': 7,
        'project_paths': ['/workspace/b', '/workspace/a', '/workspace/b'],
        'last_used': 400,
    }
    assert client.command(
        'project.recent.touch_many', payload, 'recent-owner-7-batch'
    ) == {'touched': 2}
    # Lost-ACK replay returns the compact receipt without incrementing again.
    assert client.command(
        'project.recent.touch_many', payload, 'recent-owner-7-batch'
    ) == {'touched': 2}
    rows = {
        row['path']: row
        for row in client.query('project.recent.list', {'user_id': 7})
    }
    assert rows == {
        '/workspace/a': {'path': '/workspace/a', 'count': 1, 'last_used': 400},
        '/workspace/b': {'path': '/workspace/b', 'count': 1, 'last_used': 400},
    }
    assert client.query('project.recent.list', {'user_id': 8}) == []

    with pytest.raises(StorageError) as raised:
        client.command('project.recent.touch_many', {
            'user_id': 7,
            'project_paths': [f'/workspace/{index}' for index in range(33)],
            'last_used': 401,
        }, 'recent-owner-7-overflow')
    assert raised.value.code == 'database_protocol_error'


def test_knowledge_documents_settings_and_assets_are_owner_scoped(storage):
    client = storage.client
    now = time.time()
    document = {
        'id': 'knowledge-owner-document',
        'sha256': hashlib.sha256(b'knowledge-owner-document').hexdigest(),
        'name': 'Private handbook',
        'stored_name': 'knowledge-owner-document.md',
        'kind': '.md',
        'size_bytes': 100,
        'method': 'contract-test',
        'warnings_json': '[]',
        'text_chars': 16,
        'chunk_count': 1,
        'pages': 0,
        'created_at': now,
        'updated_at': now,
        'chunks': [{
            'ordinal': 0,
            'section': '',
            'location': '',
            'content': 'private evidence',
            'search_text': 'private evidence',
            'assets': [{'id': 'asset-private', 'relation': 'primary'}],
        }],
        'assets': [{
            'id': 'asset-private',
            'ordinal': 0,
            'kind': 'image',
            'stored_name': 'private.png',
            'mime_type': 'image/png',
            'sha256': hashlib.sha256(b'private-image').hexdigest(),
            'size_bytes': 100,
            'width': 10,
            'height': 10,
            'page': 0,
            'pages_json': '[]',
            'bbox_json': '[]',
            'caption': '',
            'ocr_text': '',
            'description': '',
            'enrichment_status': 'not_requested',
            'enrichment_model': '',
            'enrichment_error': '',
            'created_at': now,
            'updated_at': now,
        }],
    }
    created = client.command('knowledge.document.create', {
        'user_id': 1,
        'document_id': document['id'],
        'document': document,
    }, 'knowledge-owner-1-create')
    assert created['created'] is True
    assert created['document']['id'] == document['id']
    assert 'chunks' not in created['document']
    content = client.query('knowledge.document.content', {
        'user_id': 1,
        'document_id': document['id'],
        'offset': 0,
        'limit': 1,
    })
    assert content['chunks'][0]['assets'] == [
        {'id': 'asset-private', 'relation': 'primary'}]
    assert client.query('knowledge.document.content', {
        'user_id': 2,
        'document_id': document['id'],
        'offset': 0,
        'limit': 1,
    }) is None
    assert client.query('knowledge.document.list', {'user_id': 2}) == []
    assert client.query('knowledge.asset.get', {
        'user_id': 2, 'asset_id': 'asset-private',
    }) is None

    assert client.command('knowledge.settings.patch', {
        'user_id': 1, 'enabled': True, 'visual_enrichment': True,
    }, 'knowledge-owner-1-settings')['visual_enrichment'] is True
    assert client.query('knowledge.settings.get', {
        'user_id': 2,
    }) == {'enabled': False, 'visual_enrichment': False}
    assert client.query('knowledge.enrichment.owners', {'limit': 1}) == [1]
    with pytest.raises(StorageError, match='Invalid limit'):
        client.query('knowledge.enrichment.owners', {'limit': 513})

    assert client.query('knowledge.enrichment.activity', {'user_id': 1}) == {
        'pending_assets': 1,
        'asset_issues': 0,
        'visual_enrichment': True,
    }
    assert client.command('knowledge.settings.patch', {
        'user_id': 1, 'visual_enrichment': False,
    }, 'knowledge-owner-1-visual-disable')['visual_enrichment'] is False
    assert client.query('knowledge.asset.get', {
        'user_id': 1, 'asset_id': 'asset-private',
    })['enrichment_status'] == 'not_requested'
    assert client.command('knowledge.settings.patch', {
        'user_id': 1, 'visual_enrichment': True,
    }, 'knowledge-owner-1-visual-reenable')['visual_enrichment'] is True

    claimed = client.command(
        'knowledge.asset.claim', {'user_id': 1},
        'knowledge-owner-1-claim')
    assert claimed['id'] == 'asset-private'
    assert claimed['enrichment_status'] == 'running'
    assert client.command(
        'knowledge.asset.claim', {'user_id': 1},
        'knowledge-owner-1-claim') == claimed
    updated = client.command('knowledge.asset.update', {
        'user_id': 1,
        'asset_id': 'asset-private',
        'updates': {
            'description': 'chart description',
            'enrichment_status': 'ready',
        },
        'chunk_content': 'chart evidence',
        'chunk_search_text': 'chart evidence',
    }, 'knowledge-owner-1-enrich')
    assert updated['updated'] is True
    assert updated['asset']['id'] == 'asset-private'
    assert updated['asset']['enrichment_status'] == 'ready'
    assert updated['asset']['description'] == 'chart description'
    assert updated['asset']['document_id'] == 'knowledge-owner-document'
    assert updated['asset']['document_name'] == 'Private handbook'
    assert client.query('knowledge.enrichment.activity', {'user_id': 1}) == {
        'pending_assets': 0,
        'asset_issues': 0,
        'visual_enrichment': True,
    }

    stale_document = {
        **document,
        'id': 'knowledge-stale-document',
        'sha256': hashlib.sha256(b'knowledge-stale-document').hexdigest(),
        'stored_name': 'knowledge-stale-document.md',
        'created_at': now - 1900,
        'updated_at': now - 1900,
        'chunks': [{
            **document['chunks'][0],
            'assets': [{'id': 'asset-stale', 'relation': 'primary'}],
        }],
        'assets': [{
            **document['assets'][0],
            'id': 'asset-stale',
            'stored_name': 'stale.png',
            'sha256': hashlib.sha256(b'stale-image').hexdigest(),
            'enrichment_status': 'running',
            'created_at': now - 1900,
            'updated_at': now - 1900,
        }],
    }
    client.command('knowledge.document.create', {
        'user_id': 2,
        'document_id': stale_document['id'],
        'document': stale_document,
    }, 'knowledge-owner-2-stale-create')
    reclaimed = client.command(
        'knowledge.asset.claim', {'user_id': 2},
        'knowledge-owner-2-stale-claim')
    assert reclaimed['id'] == 'asset-stale'
    assert reclaimed['enrichment_status'] == 'running'
    assert reclaimed['updated_at'] > now - 30


def test_autopilot_marker_is_sidecar_owned_and_idempotent(storage):
    client = storage.client
    conv_id = 'queue-marker-contract'
    config = {'model': 'contract-model', 'searchMode': 'off'}
    _import_conversation(client, conv_id, title='Queue marker')

    first = client.command('queue.autopilot.arm', {
        'conv_id': conv_id, 'user_id': 1,
        'queue_id': 'queue-marker-1',
        'config': config,
    }, None)
    assert first['armed'] is True
    assert first['queueId'] == 'queue-marker-1'

    second = client.command('queue.autopilot.arm', {
        'conv_id': conv_id, 'user_id': 1,
        'queue_id': 'queue-marker-2',
        'config': {'model': 'must-not-replace'},
    }, None)
    assert second['armed'] is False
    assert second['queueId'] == first['queueId']
    assert client.query('queue.autopilot.get', {
        'conv_id': conv_id, 'user_id': 1})['config'] == config
    assert any(
        row['convId'] == conv_id
        for row in client.query('queue.autopilot.list_all', {})
    )

    cleared = client.command(
        'queue.autopilot.clear', {'conv_id': conv_id, 'user_id': 1},
        'queue-marker-clear-1')
    assert cleared == {'cleared': True}
    assert client.command(
        'queue.autopilot.clear', {'conv_id': conv_id, 'user_id': 1},
        'queue-marker-clear-1') == cleared
    assert client.query('queue.autopilot.get', {
        'conv_id': conv_id, 'user_id': 1}) is None


def test_queue_enqueue_order_lease_and_finalize_are_atomic(storage):
    from lib.turn_source_queue_contract import (
        QUEUE_REAP_PROBE_CONTRACT,
        QUEUE_REAP_PROBE_CONVERSATIONS_FIELD,
        QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD,
        QUEUE_REAP_PROBE_REQUEST_FIELD,
        QUEUE_REAP_PROBE_RESPONSE_FIELD,
    )

    client = storage.client
    conv_id = 'queue-core-contract'
    _import_conversation(client, conv_id, title='Queue core')
    first_payload = {
        'conv_id': conv_id, 'user_id': 1, 'queue_id': 'queue-real-1',
        'message': {'text': 'human'}, 'config': {'model': 'm1'},
        'kind': 'real', 'priority': 10, 'created_at_ms': 1,
    }
    first = client.command('queue.enqueue', first_payload, 'queue-real-1')
    assert first['position'] == 1
    assert client.command(
        'queue.enqueue', first_payload, 'queue-real-1') == first
    workflow = client.command('queue.enqueue', {
        'conv_id': conv_id, 'user_id': 1,
        'queue_id': 'queue-workflow-1',
        'message': {'text': 'workflow', 'boardTaskId': 'epic-1'},
        'config': {}, 'kind': 'workflow_step', 'priority': 50,
        'created_at_ms': 2,
    }, 'queue-workflow-1')
    assert workflow['position'] == 2
    assert [row['queueId'] for row in client.query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})] == [
        'queue-real-1', 'queue-workflow-1',
    ]
    assert client.query('queue.conversations.list_all', {}) == [
        {'convId': conv_id, 'userId': 1}]
    assert client.query('queue.conversations.list_all', {'kind': 'real'}) == [
        {'convId': conv_id, 'userId': 1}]
    assert client.query('queue.conversations.list_all', {
        QUEUE_REAP_PROBE_REQUEST_FIELD: QUEUE_REAP_PROBE_CONTRACT,
        'now_ms': 100,
    }) == {
        QUEUE_REAP_PROBE_RESPONSE_FIELD: QUEUE_REAP_PROBE_CONTRACT,
        QUEUE_REAP_PROBE_CONVERSATIONS_FIELD: [
            {'convId': conv_id, 'userId': 1},
        ],
        QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD: False,
    }
    with pytest.raises(StorageError) as invalid_probe:
        client.query('queue.conversations.list_all', {
            QUEUE_REAP_PROBE_REQUEST_FIELD: 'tofu.queue.reap-probe/unknown',
            'now_ms': 100,
        })
    assert invalid_probe.value.code == 'database_protocol_error'

    leased = client.command('queue.dequeue', {
        'conv_id': conv_id, 'user_id': 1,
        'now_ms': 100, 'lease_ms': 1000,
    }, None)
    assert leased['queueId'] == 'queue-real-1'
    assert client.command('queue.lease.bind', {
        'queue_id': 'queue-real-1', 'user_id': 1,
        'task_id': 'task-queue-1',
        'now_ms': 100, 'lease_ms': 1000,
    }, 'queue-bind-contract') == {'bound': True}
    assert client.command('queue.dequeue', {
        'conv_id': conv_id, 'user_id': 1,
        'now_ms': 100, 'lease_ms': 1000,
    }, None)['queueId'] == 'queue-workflow-1'
    expired_probe = client.query('queue.conversations.list_all', {
        QUEUE_REAP_PROBE_REQUEST_FIELD: QUEUE_REAP_PROBE_CONTRACT,
        'now_ms': 1101,
    })
    assert expired_probe[QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD] is True
    assert expired_probe[QUEUE_REAP_PROBE_CONVERSATIONS_FIELD] == [
        {'convId': conv_id, 'userId': 1},
    ]
    assert client.command(
        'queue.lease.release', {
            'queue_id': 'queue-real-1', 'user_id': 1},
        'queue-release-contract') == {'released': True}
    assert client.command(
        'queue.finalize', {
            'conv_id': conv_id, 'user_id': 1,
            'queue_id': 'queue-workflow-1',
        }, 'queue-finalize-contract') == {'finalized': True}
    assert client.query('queue.depth', {
        'conv_id': conv_id, 'user_id': 1})['depth'] == 1
    assert client.command(
        'queue.clear', {
            'conv_id': conv_id, 'user_id': 1},
        'queue-clear-contract') == {
            'cleared': 1,
        }


def test_queue_goal_continuation_dedupes_and_clears_by_owned_kind(storage):
    client = storage.client
    conv_id = 'queue-goal-continuation-contract'
    _import_conversation(client, conv_id, title='Goal continuation queue')
    human = {
        'conv_id': conv_id, 'user_id': 1, 'queue_id': 'queue-human-keep',
        'message': {'text': 'human'}, 'config': {},
        'kind': 'real', 'priority': 10, 'created_at_ms': 1,
    }
    client.command('queue.enqueue', human, 'queue-human-keep')
    goal = {
        'conv_id': conv_id, 'user_id': 1, 'queue_id': 'queue-goal-first',
        'message': {'text': 'continue'},
        'config': {'_goalObjective': 'durable objective'},
        'kind': 'goal_continuation', 'priority': 20, 'created_at_ms': 2,
    }
    first = client.command('queue.enqueue', goal, 'queue-goal-first')
    duplicate = client.command('queue.enqueue', {
        **goal, 'queue_id': 'queue-goal-second', 'created_at_ms': 3,
    }, 'queue-goal-second')

    assert first['deduped'] is False
    assert duplicate['deduped'] is True
    assert duplicate['queueId'] == first['queueId']
    assert client.command('queue.kind.clear', {
        'conv_id': conv_id, 'user_id': 1, 'kind': 'goal_continuation',
    }, 'queue-goal-clear') == {'cleared': 1}
    remaining = client.query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert [(row['queueId'], row['position'], row['kind']) for row in remaining] == [
        ('queue-human-keep', 1, 'real'),
    ]
def test_queue_list_carries_get_queue_preview_contract(storage):
    """queue.list rows carry the documented get_queue preview shape
    (text / has* / peer attribution) ALONGSIDE payload/config.

    Regression anchor: the queue-bar poll (lib.message_queue.get_queue →
    routes/chat_queue.py) reads the preview keys WITHOUT unpacking payload.
    queue.list used to return rows minus the preview keys, so in sidecar mode
    every queued message — brain-dispatched kickoffs included — rendered as
    the generic 'attachment' fallback in the UI.
    """
    client = storage.client
    conv_id = 'queue-preview-contract'
    _import_conversation(client, conv_id, title='Queue preview')
    client.command('queue.enqueue', {
        'conv_id': conv_id, 'user_id': 1,
        'queue_id': 'queue-preview-workflow',
        'message': {'text': '[Project Brain — autonomous dispatch] pick up epic',
                    'boardTaskId': 'epic-9'},
        'config': {'model': 'm1'}, 'kind': 'workflow_step', 'priority': 50,
        'created_at_ms': 1,
    }, 'queue-preview-workflow-cmd')
    client.command('queue.enqueue', {
        'conv_id': conv_id, 'user_id': 1,
        'queue_id': 'queue-preview-peer',
        'message': {'text': '[Peer message …] framed body',
                    '_peerMessage': True, '_peerText': 'clean original',
                    '_fromConv': 'mradmzmdxyz123', '_peerHuman': True,
                    'images': ['img']},
        'config': {}, 'kind': 'peer_msg', 'priority': 100,
        'created_at_ms': 2,
    }, 'queue-preview-peer-cmd')

    rows = client.query('queue.list', {
        'conv_id': conv_id, 'user_id': 1})
    assert [row['queueId'] for row in rows] == [
        'queue-preview-workflow', 'queue-preview-peer']

    workflow = rows[0]
    assert workflow['text'].startswith('[Project Brain')
    assert workflow['hasImages'] is False
    assert workflow['payload']['boardTaskId'] == 'epic-9'
    assert workflow['config'] == {'model': 'm1'}

    peer = rows[1]
    # The clean _peerText wins over the framed model-facing body.
    assert peer['text'] == 'clean original'
    assert peer['hasImages'] is True
    assert peer['isPeerMessage'] is True
    assert peer['fromConv'] == 'mradmzmdxyz123'
    assert peer['isPeerHuman'] is True


def test_queue_and_autopilot_operations_are_owner_isolated(storage):
    """Every public queue key includes the authenticated conversation owner."""
    client = storage.client
    for user_id in (11, 22):
        conv_id = f'queue-owner-{user_id}'
        _import_conversation(
            client, conv_id, user_id=user_id, title=f'Owner {user_id}')
        client.command('queue.enqueue', {
            'conv_id': conv_id,
            'user_id': user_id,
            'queue_id': f'queue-owner-item-{user_id}',
            'message': {'text': f'owned by {user_id}'},
            'config': {},
            'kind': 'real',
            'priority': 10,
            'created_at_ms': user_id,
        }, f'queue-owner-enqueue:{user_id}')

    assert client.query('queue.list', {
        'conv_id': 'queue-owner-11', 'user_id': 22}) == []
    assert client.query('queue.depth', {
        'conv_id': 'queue-owner-11', 'user_id': 22}) == {'depth': 0}
    assert client.command('queue.remove', {
        'conv_id': 'queue-owner-11',
        'user_id': 22,
        'queue_id': 'queue-owner-item-11',
    }, 'queue-owner-wrong-remove') == {'removed': False}
    assert [row['queueId'] for row in client.query('queue.list', {
        'conv_id': 'queue-owner-11', 'user_id': 11})] == [
            'queue-owner-item-11']

    client.command('queue.autopilot.arm', {
        'conv_id': 'queue-owner-11',
        'user_id': 11,
        'queue_id': 'queue-owner-marker-11',
        'config': {},
    }, 'queue-owner-marker-arm')
    assert client.query('queue.autopilot.get', {
        'conv_id': 'queue-owner-11', 'user_id': 22}) is None
    assert client.command('queue.autopilot.clear', {
        'conv_id': 'queue-owner-11', 'user_id': 22,
    }, 'queue-owner-wrong-marker-clear') == {'cleared': False}
    assert client.query('queue.autopilot.get', {
        'conv_id': 'queue-owner-11', 'user_id': 11})['queueId'] == (
            'queue-owner-marker-11')


def test_turn_native_conversation_reports_turn_count(storage, monkeypatch):
    """Sidebar loss 2026-08-17 (restart/refresh): a turns-v2 conversation keeps
    its transcript in ``storage_conversation_turns`` and its ``messages_json``
    archive frozen at the empty placeholder, so ``msg_count`` stays 0. The
    sidebar visibility gate drops any conversation whose count is 0 and whose
    body is not cached client-side, so turn-native conversations vanished after
    a restart. Pin: the metadata list reports the real turn count for a
    turn-native conversation, and leaves a blob-backed conversation's stored
    count untouched.
    """
    from lib.turn_lifecycle import create_turn_pair
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    # A turn-native conversation: created by the turn pair (blob planted empty),
    # content lives only in storage_conversation_turns.
    create_turn_pair(
        'turn-native-conv', command_id='turn-native-1',
        input_projection={'content': 'hello'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Native'})
    # A blob-backed legacy conversation with a real stored count.
    _import_conversation(
        storage.client,
        'blob-conv',
        messages=[{'role': 'user', 'content': 'first'},
                  {'role': 'assistant', 'content': 'answer'}],
        title='Blob',
        updated_at=2,
    )

    listed = storage.client.query(
        'conversation.list',
        {'user_id': 1, 'order_by': 'id_asc', 'include_messages': False})
    counts = {item['metadata']['id']: item['metadata']['msg_count']
              for item in listed}
    # The turn-native conversation must advertise its 2 turns (1 input + 1
    # reply) so the sidebar gate keeps it visible after a restart.
    assert counts['turn-native-conv'] == 2
    # The blob-backed conversation keeps its authoritative stored count.
    assert counts['blob-conv'] == 2

    # conversation.get agrees with the list on the turn-native count.
    got = storage.client.query(
        'conversation.get', {'conv_id': 'turn-native-conv', 'user_id': 1})
    assert got['metadata']['msg_count'] == 2


def test_turn_native_search_projection_end_to_end(storage, monkeypatch):
    """The real sidecar registry/schema must maintain and query the v2 index;
    editing a settled turn retracts the old term in the same command."""
    from lib.turn_lifecycle import create_turn_pair
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    created = create_turn_pair(
        'turn-search-conv', command_id='turn-search-create',
        input_projection={'content': 'original turn search needle'},
        config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Search'})

    deadline = time.monotonic() + 5.0
    found = []
    while time.monotonic() < deadline:
        found = storage.client.query('conversation.search', {
            'query': 'original turn search', 'user_id': 1, 'limit': 20})
        if found:
            break
        time.sleep(0.025)
    assert [item['id'] for item in found] == ['turn-search-conv']

    submitted = created['submittedTurn']
    storage.client.command('turn.projection.update', {
        'conversation_id': 'turn-search-conv', 'user_id': 1,
        'turn_id': submitted['turnId'],
        'expected_projection_revision': submitted['projectionRevision'],
        'projection': {'content': 'replacement turn search needle'},
    }, 'turn-search-edit')

    deadline = time.monotonic() + 5.0
    old = []
    new = []
    while time.monotonic() < deadline:
        old = storage.client.query('conversation.search', {
            'query': 'original turn search', 'user_id': 1, 'limit': 20})
        new = storage.client.query('conversation.search', {
            'query': 'replacement turn search', 'user_id': 1, 'limit': 20})
        if not old and new:
            break
        time.sleep(0.025)
    assert old == []
    assert [item['id'] for item in new] == ['turn-search-conv']

    backfill = storage.client.command('turn.search.backfill', {
        'cursor': '', 'max_rows': 8, 'max_bytes': 2_000_000,
    }, 'turn-search-backfill-test', priority='maintenance')
    assert backfill['failed'] == 0
    assert backfill['scheduled'] is True


def test_conversation_list_keeps_metadata_light_and_reads_legacy_archive():
    """Full reads retain pre-turn history without taxing metadata pages."""
    from lib.storage_sidecar import operations

    captured = []

    class _FakeSession:
        backend = 'sqlite'

        def fetch_all(self, sql, params=()):
            captured.append(sql)
            if 'count(*) AS n' in sql:
                return [{'cid': 'conv-1', 'user_id': 1, 'n': 3}]
            if 'SELECT * FROM storage_conversation_turns' in sql:
                return []
            row = {
                'id': 'conv-1', 'user_id': 1, 'title': 'T',
                'created_at_ms': 1, 'updated_at_ms': 2,
                'settings_json': '{"folderId": "f"}', 'msg_count': 3,
                'rev': 7,
            }
            if 'messages_json' in sql:
                row['messages_json'] = json.dumps([
                    {'role': 'user', 'content': 'archived question'},
                    {'role': 'assistant', 'content': 'archived answer'},
                ])
            if 'search_text' in sql:
                row['search_text'] = 'hello'
            return [row]

    documents = operations._conversation_list(
        _FakeSession(), {'user_id': 1, 'include_messages': False})
    assert captured and 'messages_json' not in captured[0]
    assert 'search_text' not in captured[0]
    assert 'LIMIT ?' in captured[0]
    assert documents[0]['messages'] == []
    assert documents[0]['metadata']['msg_count'] == 3
    assert documents[0]['metadata']['settings'] == {'folderId': 'f'}
    assert documents[0]['metadata']['search_text'] == ''

    captured.clear()
    documents = operations._conversation_list(
        _FakeSession(), {'user_id': 1, 'include_messages': True})
    assert captured and 'messages_json' in captured[0]
    assert 'search_text' not in captured[0]
    assert [message['content'] for message in documents[0]['messages']] == [
        'archived question', 'archived answer']
    assert documents[0]['metadata']['search_text'] == ''


def test_conversation_get_falls_back_to_frozen_archive_and_preserves_count():
    """A pre-turn conversation is durable history, not an empty placeholder."""
    from lib.storage_sidecar import operations

    captured = []
    header = {
        'id': 'legacy-conv', 'user_id': 7, 'title': 'Legacy',
        'created_at_ms': 1, 'updated_at_ms': 2,
        'settings_json': '{}', 'msg_count': 2, 'search_text': 'legacy',
        'rev': 4,
    }
    archived = [
        {'role': 'user', 'content': 'old question'},
        {'role': 'assistant', 'content': 'old answer'},
    ]

    class _FakeSession:
        backend = 'sqlite'

        def fetch_one(self, sql, params=()):
            captured.append(('one', sql, params))
            if 'COUNT(*) AS c' in sql:
                return {'c': 0}
            if 'messages_json' in sql:
                return {'messages_json': json.dumps(archived)}
            return dict(header)

        def fetch_all(self, sql, params=()):
            captured.append(('all', sql, params))
            return []

    document = operations._conversation_get(_FakeSession(), {
        'conv_id': 'legacy-conv', 'user_id': 7, 'derive_messages': True,
    })

    assert document['messages'] == archived
    assert document['metadata']['msg_count'] == 2
    assert 'messages_json' not in captured[0][1]
    assert 'search_text' not in captured[0][1]
    assert 'conversation_id=? AND user_id=?' in captured[1][1]
    assert 'messages_json' in captured[2][1]

    captured.clear()
    page = operations._conversation_get(_FakeSession(), {
        'conv_id': 'legacy-conv', 'user_id': 7, 'derive_messages': True,
        'message_window': 1, 'before_sequence': 1,
    })
    assert page['messages'] == archived[:1]
    assert page['metadata']['msg_count'] == 2
    assert page['message_page'] == {
        'total_count': 2, 'start': 0, 'end': 1,
    }
    assert page['metadata']['search_text'] == ''
    assert 'messages_json' not in captured[0][1]
    assert 'search_text' not in captured[0][1]
    assert 'COUNT(*) AS c' in captured[1][1]
    assert 'messages_json' in captured[2][1]

    captured.clear()
    metadata = operations._conversation_get(_FakeSession(), {
        'conv_id': 'legacy-conv', 'user_id': 7, 'derive_messages': False,
    })
    assert metadata['messages'] == []
    assert metadata['metadata']['search_text'] == ''
    assert 'search_text' not in captured[0][1]

    row = dict(header)
    operations._backfill_turn_message_counts(_FakeSession(), [row])
    assert row['msg_count'] == 2


def test_conversation_get_turn_window_is_bounded_and_cursor_addressable(storage):
    """Turn-native detail reads page in SQL and expose absolute cursors."""
    messages = [
        {'role': 'user' if index % 2 == 0 else 'assistant',
         'content': f'message-{index}'}
        for index in range(4)
    ]
    _import_conversation(
        storage.client, 'window-conv', messages=messages, title='Window')

    tail = storage.client.query('conversation.get', {
        'conv_id': 'window-conv', 'user_id': 1, 'message_window': 2,
    })
    assert [message['content'] for message in tail['messages']] == [
        'message-2', 'message-3']
    assert tail['metadata']['msg_count'] == 4
    assert tail['message_page'] == {
        'total_count': 4, 'start': 2, 'end': 4,
    }

    previous = storage.client.query('conversation.get', {
        'conv_id': 'window-conv', 'user_id': 1, 'message_window': 2,
        'before_sequence': 2,
    })
    assert [message['content'] for message in previous['messages']] == [
        'message-0', 'message-1']
    assert previous['message_page'] == {
        'total_count': 4, 'start': 0, 'end': 2,
    }


def test_conversation_list_settings_keys_projection_bounds_sidebar_payload():
    """The 37.6 MB ?meta=1 regression: the metadata-only ``conversation.list``
    projected the WHOLE ``settings_json`` blob per row, and per-conversation
    settings can carry ``autopilotSummaries`` / ``autopilotObjective`` (tens to
    hundreds of KiB each). The sidebar only needs a small whitelist of shell
    facts. Pin: ``settings_keys`` projects the settings dict in the storage
    operation (so the heavy blobs never cross the RPC frame) and leaves
    unspecified rows at their full settings for other callers.
    """
    from lib.storage_sidecar import operations
    from lib.storage.errors import StorageError

    class _FakeSession:
        backend = 'sqlite'

        def __init__(self):
            self.statements = []

        def fetch_all(self, sql, params=()):
            self.statements.append(sql)
            if 'count(*) AS n' in sql:
                return [{'cid': 'conv-1', 'user_id': 1, 'n': 3}]
            return [{
                'id': 'conv-1', 'user_id': 1, 'title': 'T',
                'created_at_ms': 1, 'updated_at_ms': 2,
                'settings_json': json.dumps({
                    'folderId': 'f',
                    'lastMsgRole': 'assistant',
                    'autopilotSummaries': {'run-1': {'content': 'x' * 100000}},
                    'model': 'm',
                }),
                'msg_count': 3, 'rev': 7,
            }]

    projected_session = _FakeSession()
    projected = operations._conversation_list(
        projected_session,
        {'user_id': 1, 'include_messages': False,
         'settings_keys': ['folderId', 'lastMsgRole']})
    assert projected[0]['metadata']['settings'] == {
        'folderId': 'f', 'lastMsgRole': 'assistant'}
    assert 'autopilotSummaries' not in projected[0]['metadata']['settings']
    assert 'model' not in projected[0]['metadata']['settings']

    empty_session = _FakeSession()
    empty = operations._conversation_list(
        empty_session,
        {'user_id': 1, 'include_messages': False, 'settings_keys': []},
    )
    assert empty[0]['metadata']['settings'] == {}
    assert any(
        "'{}' AS settings_json" in statement
        for statement in empty_session.statements
    ), 'an empty whitelist must not read or decode the stored settings blob'

    # Without a projection the settings blob passes through unchanged for
    # non-sidebar callers (project summaries, project dispatch, …).
    full = operations._conversation_list(
        _FakeSession(), {'user_id': 1, 'include_messages': False})
    assert 'autopilotSummaries' in full[0]['metadata']['settings']

    # Malformed projections are rejected as a protocol error.
    for bad in ('folderId', [1, 2], ['']):
        with pytest.raises(StorageError) as raised:
            operations._conversation_list(
                _FakeSession(),
                {'user_id': 1, 'include_messages': False, 'settings_keys': bad})
        assert raised.value.code == 'database_protocol_error'


@pytest.mark.parametrize(
    ('backend', 'folder_expression'),
    [
        ('sqlite', "json_extract(settings_json, '$.folderId') = ?"),
        ('postgres', "settings_json ->> 'folderId' = ?"),
    ],
)
def test_conversation_catalog_page_pushes_owner_filter_cursor_and_limit_to_sql(
    backend, folder_expression
):
    """The sidebar page must not materialize an owner's complete archive."""
    from lib.storage_sidecar import operations

    captured = []

    class _FakeSession:
        def __init__(self):
            self.backend = backend

        def fetch_one(self, sql, params=()):
            captured.append(('one', sql, params))
            return {'c': 9}

        def fetch_all(self, sql, params=()):
            captured.append(('all', sql, params))
            if 'count(*) AS n' in sql:
                return [
                    {'cid': 'c3', 'user_id': 7, 'n': 4},
                    {'cid': 'c2', 'user_id': 7, 'n': 2},
                ]
            return [
                {
                    'id': cid,
                    'user_id': 7,
                    'title': cid.upper(),
                    'created_at_ms': updated_at - 1,
                    'updated_at_ms': updated_at,
                    'settings_json': json.dumps({
                        'folderId': 'folder-a',
                        'lastMsgRole': 'assistant',
                        'large': 'x' * 1000,
                    }),
                    'msg_count': 0,
                    'rev': 1,
                }
                for cid, updated_at in [('c3', 499), ('c2', 498), ('c1', 497)]
            ]

    result = operations._conversation_list(_FakeSession(), {
        'catalog_page': True,
        'user_id': 7,
        'include_messages': False,
        'order_by': 'updated_at_desc',
        'folder_id': 'folder-a',
        'before_updated_at': 500,
        'before_id': 'c4',
        'limit': 2,
        'settings_keys': ['folderId', 'lastMsgRole'],
    })

    assert result['total_count'] == 9
    assert result['has_more'] is True
    assert [item['metadata']['id'] for item in result['items']] == ['c3', 'c2']
    assert [item['metadata']['msg_count'] for item in result['items']] == [4, 2]
    assert result['items'][0]['metadata']['settings'] == {
        'folderId': 'folder-a', 'lastMsgRole': 'assistant'}

    _, count_sql, count_params = captured[0]
    _, page_sql, page_params = captured[1]
    assert 'user_id = ?' in count_sql and folder_expression in count_sql
    assert count_params == (7, 'folder-a')
    assert 'user_id = ?' in page_sql and folder_expression in page_sql
    assert page_sql.index('user_id = ?') < page_sql.index('ORDER BY')
    assert page_sql.index('ORDER BY') < page_sql.index('LIMIT ?')
    assert page_params == (7, 'folder-a', 500, 500, 'c4', 3)
    assert all('messages_json' not in sql for _, sql, _ in captured)


@pytest.mark.parametrize(
    ('backend', 'project_expression'),
    [
        ('sqlite', "json_extract(settings_json, '$.projectPath') = ?"),
        ('postgres', "settings_json ->> 'projectPath' = ?"),
    ],
)
def test_conversation_list_project_filter_has_backend_parity(
    backend, project_expression
):
    from lib.storage_sidecar import operations

    captured = []

    class _FakeSession:
        def __init__(self):
            self.backend = backend

        def fetch_all(self, sql, params=()):
            captured.append((sql, params))
            return []

    assert operations._conversation_list(
        _FakeSession(),
        {
            'user_id': 7,
            'project_path': '/projects/alpha',
            'include_messages': False,
            'settings_keys': [],
            'limit': 24,
        },
    ) == []
    sql, params = captured[0]
    assert 'user_id = ?' in sql
    assert project_expression in sql
    assert sql.index('user_id = ?') < sql.index(project_expression)
    assert sql.index(project_expression) < sql.index('ORDER BY')
    assert sql.index('ORDER BY') < sql.index('LIMIT ?')
    assert params == (7, '/projects/alpha', 24)


def test_conversation_count_returns_authoritative_total_without_blobs():
    """The sidebar's X-Total-Count must be a real COUNT(*), not the bounded
    window length, so the browser can tell a recent page from a complete list
    before pruning locally-cached conversations.
    """
    from lib.storage_sidecar import operations

    captured = []

    class _FakeSession:
        backend = 'sqlite'

        def fetch_one(self, sql, params=()):
            captured.append(sql)
            assert 'messages_json' not in sql and 'settings_json' not in sql
            return {'c': 1234}

    result = operations._conversation_count(_FakeSession(), {'user_id': 1})
    assert result == {'count': 1234}
    assert captured and 'COUNT(*)' in captured[0]


@pytest.mark.parametrize(
    ('backend', 'timestamp_expression'),
    [
        (
            'sqlite',
            "json_extract(COALESCE(cp.projection_json,t.projection_json), "
            "'$.timestamp')",
        ),
        (
            'postgres',
            "COALESCE(cp.projection_json,t.projection_json) -> 'timestamp'",
        ),
    ],
)
def test_conversation_activity_dates_projects_only_timestamp_scalars(
        backend, timestamp_expression):
    """Turn and legacy content stay behind backend JSON scalar projections."""
    from lib.storage_sidecar import operations

    calls = []

    class _FakeSession:
        def __init__(self):
            self.backend = backend

        def fetch_all(self, sql, params=()):
            calls.append(('all', sql, params))
            if 'FROM storage_conversation_turns' in sql:
                return [
                    {
                        'cid': 'turn-backed',
                        'turn_created_at': 150,
                        'projection_timestamp': None,
                    },
                    {
                        'cid': 'turn-backed',
                        'turn_created_at': 160,
                        'projection_timestamp': 'malformed',
                    },
                ]
            if 'SELECT id,messages_json' in sql:
                return [
                    {
                        'id': 'legacy',
                        'messages_json': json.dumps([
                            {
                                'role': 'user',
                                'timestamp': 150,
                                'content': 'private',
                            },
                            {
                                'role': 'assistant',
                                'content': 'fallback private',
                            },
                        ]),
                    },
                ]
            assert 'messages_json' not in sql
            return [
                {
                    'id': 'turn-backed',
                    'created_at_ms': 100,
                    'updated_at_ms': 250,
                },
                {
                    'id': 'legacy',
                    'created_at_ms': 100,
                    'updated_at_ms': 250,
                },
            ]

    result = operations._conversation_activity_dates(_FakeSession(), {
        'user_id': 7,
        'updated_at_gte': 100,
        'created_at_lt': 300,
        'day_boundaries_ms': [100, 200, 300],
        'limit': 50,
    })

    assert result == {'candidate_count': 2, 'counts': [2, 2]}
    _, candidate_sql, candidate_params = calls[0]
    assert 'messages_json' not in candidate_sql
    assert candidate_params == (7, 100, 300, 50)
    _, turn_sql, turn_params = calls[1]
    assert timestamp_expression in turn_sql
    assert 'LEFT JOIN storage_turn_projection_checkpoints AS cp' in turn_sql
    assert 'content' not in turn_sql and 'projection_json AS' not in turn_sql
    assert turn_params == (7, 'turn-backed', 'legacy')
    _, legacy_sql, legacy_params = calls[2]
    assert 'SELECT id,messages_json' in legacy_sql
    assert legacy_params == (7, 'legacy')
    assert [kind for kind, _, _ in calls] == ['all', 'all', 'all']


def test_conversation_activity_dates_roundtrip_is_distinct_and_owner_scoped(
        storage):
    client = storage.client
    _import_conversation(
        client,
        'activity-owned',
        user_id=1,
        created_at=100,
        updated_at=250,
        messages=[
            {'role': 'user', 'timestamp': 150, 'content': 'a'},
            {'role': 'assistant', 'timestamp': 160, 'content': 'b'},
            {'role': 'user', 'timestamp': 250, 'content': 'c'},
        ],
    )
    _import_conversation(
        client,
        'activity-foreign',
        user_id=2,
        created_at=100,
        updated_at=250,
        messages=[
            {'role': 'user', 'timestamp': 150, 'content': 'foreign'},
        ],
    )

    result = client.query('conversation.activity_dates', {
        'user_id': 1,
        'updated_at_gte': 100,
        'created_at_lt': 300,
        'day_boundaries_ms': [100, 200, 300],
        'limit': 10_000,
    })

    assert result == {'candidate_count': 1, 'counts': [1, 1]}


def test_conversation_count_op_roundtrip(storage):
    """The registered ``conversation.count`` op answers through the real
    sidecar process (the route calls it for the authoritative sidebar total)."""
    client = storage.client
    _import_conversation(
        client,
        'count-conv',
        messages=[{'role': 'user', 'content': 'hi'}],
        title='Count',
        updated_at=2,
    )
    assert client.query('conversation.count', {'user_id': 1}) == {'count': 1}


def test_conversation_catalog_page_roundtrip_is_owner_scoped_and_cursor_bounded(
    storage,
):
    client = storage.client
    for conv_id, user_id, updated_at in (
        ('catalog-c1', 1, 1),
        ('catalog-c2', 1, 2),
        ('catalog-c3', 1, 3),
        ('catalog-other-owner', 2, 4),
    ):
        _import_conversation(
            client,
            conv_id,
            user_id=user_id,
            updated_at=updated_at,
            settings={'folderId': 'folder-a', 'large': 'x' * 20_000},
        )

    first = client.query('conversation.list', {
        'catalog_page': True,
        'user_id': 1,
        'include_messages': False,
        'folder_id': 'folder-a',
        'limit': 2,
        'settings_keys': ['folderId'],
    })
    assert first['total_count'] == 3
    assert first['has_more'] is True
    assert [item['metadata']['id'] for item in first['items']] == [
        'catalog-c3', 'catalog-c2']
    assert all(item['metadata']['settings'] == {'folderId': 'folder-a'}
               for item in first['items'])

    second = client.query('conversation.list', {
        'catalog_page': True,
        'user_id': 1,
        'include_messages': False,
        'folder_id': 'folder-a',
        'before_updated_at': 2,
        'before_id': 'catalog-c2',
        'limit': 2,
        'settings_keys': ['folderId'],
    })
    assert second['total_count'] == 3
    assert second['has_more'] is False
    assert [item['metadata']['id'] for item in second['items']] == ['catalog-c1']


def test_conversation_settings_replace_is_snapshot_cas_and_supports_delete(storage):
    """A settings replacement compares its complete old snapshot under lock."""
    client = storage.client
    _import_conversation(
        client,
        'conv-settings-cas',
        settings={'removeMe': True, 'counter': 0},
    )
    before = {'removeMe': True, 'counter': 0}
    first = client.command(
        'conversation.settings.update',
        {
            'conv_id': 'conv-settings-cas',
            'user_id': 1,
            'updates': {'counter': 1},
            'replace': True,
            'expected_settings': before,
        },
        'settings-cas:first',
    )
    assert first['applied'] is True

    stale = client.command(
        'conversation.settings.update',
        {
            'conv_id': 'conv-settings-cas',
            'user_id': 1,
            'updates': {'removeMe': True, 'counter': 99},
            'replace': True,
            'expected_settings': before,
        },
        'settings-cas:stale',
    )
    assert stale['applied'] is False
    assert stale['conflict'] is True

    document = client.query(
        'conversation.get',
        {
            'conv_id': 'conv-settings-cas',
            'user_id': 1,
            'derive_messages': False,
        },
    )
    assert document['metadata']['settings'] == {'counter': 1}


def test_conversation_list_derives_turn_counts_and_preserves_archive_counts():
    """The list projection respects both durable transcript generations.

    Canonical owner-scoped main-lane turns override the stored aggregate. A
    conversation with no turns may still have its only durable transcript in
    the frozen pre-turn archive, so its stored count remains authoritative.
    Branch turns stay excluded because the frontend nests them below a parent
    rather than projecting them into ``conv.messages``.
    """
    from lib.storage_sidecar import operations

    count_sql = []
    count_params = []

    class _FakeSession:
        backend = 'sqlite'

        def fetch_all(self, sql, params=()):
            if 'FROM storage_conversation_turns' in sql:
                count_sql.append(sql)
                count_params.append(params)
                # Only one conversation has canonical main-lane turns.
                return [{'cid': 'turn-native-conv', 'user_id': 1, 'n': 2}]
            # The conversation listing projection.
            return [
                {'id': 'turn-native-conv', 'user_id': 1, 'title': 'T',
                 'created_at_ms': 1, 'updated_at_ms': 2,
                 'settings_json': '{}', 'msg_count': 0, 'rev': 5},
                {'id': 'legacy-conv', 'user_id': 1, 'title': 'L',
                 'created_at_ms': 1, 'updated_at_ms': 2,
                 'settings_json': '{}', 'msg_count': 7, 'rev': 3},
            ]

    documents = operations._conversation_list(
        _FakeSession(), {'user_id': 1, 'include_messages': False})
    by_id = {d['metadata']['id']: d['metadata'] for d in documents}
    assert by_id['turn-native-conv']['msg_count'] == 2
    assert by_id['legacy-conv']['msg_count'] == 7
    # The derivation counts only the main lane for every owner/id pair.
    assert count_sql and "lane_id = 'main'" in count_sql[0]
    assert count_params[0] == (
        'turn-native-conv', 1, 'legacy-conv', 1)

    # A conversation with zero turns AND zero archive count stays 0 — a
    # genuinely empty row must not be invented into visibility.
    class _EmptySession:
        backend = 'sqlite'

        def fetch_all(self, sql, params=()):
            if 'FROM storage_conversation_turns' in sql:
                return []
            return [
                {'id': 'ghost-conv', 'user_id': 1, 'title': 'G',
                 'created_at_ms': 1, 'updated_at_ms': 2,
                 'settings_json': '{}', 'msg_count': 0, 'rev': 1},
            ]

    empty = operations._conversation_list(
        _EmptySession(), {'user_id': 1, 'include_messages': False})
    assert empty[0]['metadata']['msg_count'] == 0


def test_billing_settlement_fault_rolls_back_and_replays_once(
        tmp_path, monkeypatch):
    """A failure after wallet mutation cannot publish a partial settlement."""
    monkeypatch.setenv('TOFU_STORAGE_ENABLE_FAULT_INJECTION', '1')
    monkeypatch.setenv(
        'TOFU_STORAGE_FAULT_ONCE', 'billing.payment.settle.before_status')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        client = supervisor.client
        payment = {
            'id': 'pay_fault_once', 'user_id': 'user-fault',
            'provider': 'contract', 'provider_id': 'provider-fault',
            'amount_minor': 1000, 'currency': 'USD',
            'credit_micro': 1_000_000, 'status': 'pending', 'raw': {},
        }
        client.command('billing.payment.record', payment, 'record-fault-payment')
        settle = {
            'payment_id': payment['id'], 'raw': None,
            'ledger_id': 'ledger-fault-payment',
        }
        with pytest.raises(StorageError) as raised:
            client.command('billing.payment.settle', settle, 'settle-fault-payment')
        assert raised.value.code == 'database_internal'
        assert client.query('billing.wallet.get', {
            'user_id': payment['user_id'],
        })['balance_micro'] == 0
        assert client.query('billing.payment.find', {
            'provider': payment['provider'],
            'provider_id': payment['provider_id'],
        })['status'] == 'pending'

        result = client.command(
            'billing.payment.settle', settle, 'settle-fault-payment')
        assert result['settled'] is True
        replay = client.command(
            'billing.payment.settle', settle, 'settle-fault-payment')
        assert replay == result
        assert client.query('billing.wallet.get', {
            'user_id': payment['user_id'],
        })['balance_micro'] == payment['credit_micro']
    finally:
        supervisor.stop()


def test_billing_wallet_payment_and_settlement_are_atomic_on_both_backends(
        storage):
    client = storage.client
    user_id = 'billing-contract-user'
    topup = {
        'user_id': user_id, 'amount_micro': 1000, 'kind': 'topup',
        'ref_type': 'contract', 'ref_id': 'initial', 'note': '',
        'ledger_id': 'ledger-initial', 'allow_negative': False,
    }
    first = client.command('billing.wallet.apply', topup, 'billing-topup')
    assert client.command(
        'billing.wallet.apply', topup, 'billing-topup') == first
    assert first['wallet']['balance_micro'] == 1000

    reserve = {
        'user_id': user_id, 'amount_micro': -700, 'kind': 'reserve',
        'ref_type': 'reserve', 'ref_id': 'task-contract', 'note': '',
        'ledger_id': 'ledger-reserve', 'allow_negative': False,
    }
    assert client.command(
        'billing.wallet.apply', reserve, 'billing-reserve')['applied']
    insufficient = client.command('billing.wallet.apply', {
        **reserve, 'amount_micro': -500, 'ref_id': 'too-large',
        'ledger_id': 'ledger-too-large',
    }, 'billing-insufficient')
    assert insufficient['insufficient'] is True
    assert insufficient['wallet']['balance_micro'] == 300

    settled = client.command('billing.wallet.settle', {
        'user_id': user_id, 'ref_id': 'task-contract',
        'reserved_micro': 700, 'actual_micro': 300, 'note': '',
        'release_id': 'ledger-release', 'debit_id': 'ledger-debit',
    }, 'billing-settle-task')
    assert settled['wallet']['balance_micro'] == 700

    payment = {
        'id': 'payment-contract', 'user_id': user_id,
        'provider': 'contract', 'provider_id': 'payment-provider-contract',
        'amount_minor': 5, 'currency': 'USD', 'credit_micro': 500,
        'status': 'pending', 'raw': {},
    }
    client.command('billing.payment.record', payment, 'billing-payment-record')
    payment_settle = {
        'payment_id': payment['id'], 'raw': None,
        'ledger_id': 'ledger-payment',
    }
    receipt = client.command(
        'billing.payment.settle', payment_settle, 'billing-payment-settle')
    assert receipt['settled'] is True
    assert client.command(
        'billing.payment.settle', payment_settle,
        'billing-payment-settle') == receipt
    wallet = client.query('billing.wallet.get', {'user_id': user_id})
    recomputed = client.query(
        'billing.ledger.recompute', {'user_id': user_id})
    assert wallet['balance_micro'] == recomputed['balance_micro'] == 1200


def test_integration_claim_cas_and_receipts_are_atomic_on_both_backends(
        storage):
    client = storage.client

    def ready(task_id: str, now: float) -> None:
        register = {
            'user_id': 1,
            'project_root': '/contract-project', 'task_id': task_id,
            'title': task_id, 'workspace_path': f'/workspace/{task_id}',
            'managed': False, 'base_sha': 'a' * 40, 'now': now,
        }
        assert client.command(
            'integration.workspace.register', register,
            f'integration-register-{task_id}') == {'ok': True}
        assert client.command(
            'integration.workspace.register', register,
            f'integration-register-{task_id}') == {'ok': True}
        assert client.command('integration.workspace.save_checkpoint', {
            'user_id': 1,
            'project_root': '/contract-project', 'task_id': task_id,
            'checkpoint_sha': task_id[0] * 40, 'now': now + 0.1,
        }, f'integration-checkpoint-{task_id}') == {'ok': True}
        submit = {
            'user_id': 1,
            'project_root': '/contract-project', 'task_id': task_id,
            'now': now + 0.2,
        }
        assert client.command(
            'integration.workspace.submit', submit,
            f'integration-submit-{task_id}') == {'ok': True}
        assert client.command(
            'integration.workspace.submit', submit,
            f'integration-submit-{task_id}') == {'ok': True}

    ready('alpha', 1.0)
    ready('beta', 2.0)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda index: client.command(
            'integration.workspace.claim_next', {'now': 10.0 + index / 100},
            f'integration-claim-{index}'), range(8)))
    winners = [row for row in claimed if row is not None]
    assert len(winners) == 1

    status = client.query(
        'integration.status', {
            'user_id': 1, 'project_root': '/contract-project',
        })
    assert sum(row['state'] == 'integrating' for row in status['rows']) == 1
    assert sum(row['state'] == 'ready' for row in status['rows']) == 1
    assert [event['kind'] for event in status['events']].count('registered') == 2

    winner = winners[0]
    merged_payload = {
        'row_id': winner['id'], 'project_root': '/contract-project',
        'task_id': winner['task_id'], 'candidate_sha': 'c' * 40,
        'error': '', 'now': 20.0,
    }
    first = client.command(
        'integration.workspace.mark_merged', merged_payload,
        'integration-mark-merged')
    assert first == {'changed': True}
    assert client.command(
        'integration.workspace.mark_merged', merged_payload,
        'integration-mark-merged') == first
    assert client.command(
        'integration.workspace.mark_merged', {
            **merged_payload, 'now': 21.0,
        }, 'integration-mark-merged-again') == {'changed': False}


def test_natural_event_key_deduplicates_without_receipt(storage):
    payload = {'task_id': 'task-1', 'sequence': 3, 'event': {'kind': 'delta'}}
    assert storage.client.command('event.append', payload, None, priority='event')['inserted']
    assert not storage.client.command(
        'event.append', payload, None, priority='event')['inserted']
    assert storage.client.query('event.list', {'task_id': 'task-1'}) == [{
        'sequence': 3,
        'event': {'kind': 'delta'},
        'created_at_ms': storage.client.query(
            'event.list', {'task_id': 'task-1'})[0]['created_at_ms'],
    }]
    with pytest.raises(StorageError) as raised:
        storage.client.command('event.append', {
            **payload, 'event': {'kind': 'conflicting'},
        }, None, priority='event')
    assert raised.value.code == 'database_conflict'


def test_event_bounds_count_sparse_sequences_without_loading_payloads(storage):
    client = storage.client
    for sequence in (3, 9, 12):
        client.command(
            'event.append', {
                'task_id': 'event-bounds-sparse',
                'sequence': sequence,
                'event': {'type': 'progress', 'payload': 'x' * 10_000},
            },
            None,
            priority='event',
        )

    assert client.query('event.bounds', {
        'task_id': 'event-bounds-sparse',
    }) == {
        'retained_count': 3,
        'base_cursor': 3,
        'next_cursor': 13,
    }
    assert client.query('event.bounds', {
        'task_id': 'event-bounds-empty',
    }) == {
        'retained_count': 0,
        'base_cursor': 0,
        'next_cursor': 0,
    }


def test_task_result_replay_get_defers_heavy_payload_and_filters_owner(storage):
    client = storage.client
    content = 'answer-' * 100_000
    value = {
        'task_id': 'compact-replay-task',
        'conv_id': 'compact-replay-conv',
        'user_id': 17,
        'content': content,
        'thinking': 'reasoning',
        'error': None,
        'status': 'done',
        'tool_rounds': '[{"large":"diagnostic"}]',
        'metadata': json.dumps({
            'finishReason': 'stop',
            'model': 'compact-model',
            'requestId': 'compact-request',
            'flowTrace': [{'large': 'dedicated'}],
        }),
        'segments': None,
        'created_at': 10,
        'completed_at': 20,
    }
    client.command(
        'task_results.checkpoint', {
            'key': value['task_id'], 'value': value, 'expected_version': 0,
        },
        None,
    )

    compact = client.query('task_results.replay_get', {
        'key': value['task_id'], 'user_id': 17,
    })
    assert compact['status'] == 'done'
    assert compact['metadata'] == {
        'finishReason': 'stop',
        'model': 'compact-model',
        'requestId': 'compact-request',
    }
    assert 'content' not in compact
    assert 'thinking' not in compact
    assert 'tool_rounds' not in compact
    assert client.query('task_results.replay_get', {
        'key': value['task_id'], 'user_id': 18,
    }) is None

    full = client.query('task_results.replay_get', {
        'key': value['task_id'], 'user_id': 17,
        'include_terminal_payload': True,
        'include_metadata': True,
    })
    assert full['content'] == content
    assert full['thinking'] == 'reasoning'
    assert full['metadata']['flowTrace'] == [{'large': 'dedicated'}]
    assert len(json.dumps(compact)) < 2_048
    assert len(content) > 500_000


def test_task_result_field_codec_stays_private_and_round_trips(
        storage, tmp_path):
    from lib.storage_sidecar.task_result_field_codec import (
        TASK_RESULT_FIELD_CODEC_KEY,
    )

    value = {
        'task_id': 'field-codec-task',
        'conv_id': 'field-codec-conversation',
        'user_id': 31,
        'status': 'done',
        'segments': 'segment payload ' * 20_000,
        'metadata': json.dumps({
            'finishReason': 'stop',
            'toolSummary': 'summary payload ' * 10_000,
        }),
        'tool_rounds': None,
        'content': 'public answer',
        'thinking': '',
        'error': None,
        'created_at': 10,
        'completed_at': 20,
    }
    result = storage.client.command(
        'task_results.checkpoint', {
            'key': value['task_id'], 'value': value, 'expected_version': 0,
        }, None)

    assert result['version'] == 1
    assert storage.client.query('record.get', {
        'namespace': 'task_results', 'key': value['task_id'],
    })['value'] == value
    listed = storage.client.query('record.list', {
        'namespace': 'task_results', 'prefix': value['task_id'], 'limit': 1,
    })
    assert listed[0]['value'] == value
    replay = storage.client.query('task_results.replay_get', {
        'key': value['task_id'], 'user_id': value['user_id'],
        'include_metadata': True,
    })
    assert replay['metadata']['toolSummary'].startswith('summary payload ')
    assert 'segments' not in replay

    injected = dict(value)
    injected['task_id'] = 'field-codec-injection'
    injected['segments'] = {
        TASK_RESULT_FIELD_CODEC_KEY: {'payload': 'private'},
    }
    with pytest.raises(StorageError) as raised:
        storage.client.command('task_results.checkpoint', {
            'key': injected['task_id'], 'value': injected,
            'expected_version': 0,
        }, None)
    assert raised.value.code == 'database_protocol_error'

    if storage.client.health()['backend'] == 'sqlite':
        connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
        raw = connection.execute(
            "SELECT value_json FROM storage_records WHERE namespace=? "
            "AND record_key=?",
            ('task_results', value['task_id']),
        ).fetchone()[0]
        connection.close()
        stored = json.loads(raw)
        assert TASK_RESULT_FIELD_CODEC_KEY in stored['segments']
        assert TASK_RESULT_FIELD_CODEC_KEY in stored['metadata']
        assert stored['status'] == 'done'
        assert len(raw) < len(json.dumps(value).encode()) * 0.1


def test_task_result_compact_replay_skips_unrequested_corrupt_field(
        storage, tmp_path):
    if storage.client.health()['backend'] != 'sqlite':
        pytest.skip('fault injection uses the SQLite authority')
    from lib.storage_sidecar.task_result_field_codec import (
        TASK_RESULT_FIELD_CODEC_KEY,
    )

    value = {
        'task_id': 'selective-codec-task',
        'conv_id': 'selective-codec-conversation',
        'user_id': 32,
        'status': 'done',
        'segments': 'segment payload ' * 20_000,
        'metadata': json.dumps({'finishReason': 'stop'}),
        'content': 'answer',
        'thinking': '',
        'error': None,
        'created_at': 10,
        'completed_at': 20,
    }
    storage.client.command('task_results.checkpoint', {
        'key': value['task_id'], 'value': value, 'expected_version': 0,
    }, None)
    connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
    raw = connection.execute(
        "SELECT value_json FROM storage_records WHERE namespace=? "
        "AND record_key=?",
        ('task_results', value['task_id']),
    ).fetchone()[0]
    stored = json.loads(raw)
    stored['segments'][TASK_RESULT_FIELD_CODEC_KEY]['payload'] = 'bad'
    connection.execute(
        "UPDATE storage_records SET value_json=? WHERE namespace=? "
        "AND record_key=?",
        (json.dumps(stored), 'task_results', value['task_id']),
    )
    connection.commit()
    connection.close()

    compact = storage.client.query('task_results.replay_get', {
        'key': value['task_id'], 'user_id': value['user_id'],
    })
    assert compact['status'] == 'done'
    with pytest.raises(StorageError) as raised:
        storage.client.query('record.get', {
            'namespace': 'task_results', 'key': value['task_id'],
        })
    assert raised.value.code == 'database_integrity'


def test_task_result_plain_ambiguous_replay_survives_codec_rollout(
        storage, tmp_path):
    if storage.client.health()['backend'] != 'sqlite':
        pytest.skip('mixed-version seed uses the SQLite authority')
    from lib.storage_sidecar.task_result_field_codec import (
        TASK_RESULT_FIELD_CODEC_KEY,
    )

    value = {
        'task_id': 'plain-replay-task',
        'conv_id': '',
        'user_id': 33,
        'status': 'done',
        'segments': 'historical plain segment ' * 10_000,
        'metadata': '{}',
        'content': 'answer',
        'thinking': '',
        'error': None,
        'created_at': 10,
        'completed_at': 20,
    }
    plain = json.dumps(value, sort_keys=True, separators=(',', ':'))
    connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
    connection.execute(
        "INSERT INTO storage_records(namespace,record_key,value_json,version,"
        "updated_at_ms) VALUES (?,?,?,?,?)",
        ('task_results', value['task_id'], plain, 1, 20),
    )
    connection.commit()
    connection.close()

    replay = storage.client.command('task_results.checkpoint', {
        'key': value['task_id'], 'value': value, 'expected_version': 0,
    }, None)
    assert replay['version'] == 1
    rewritten = storage.client.command('task_results.checkpoint', {
        'key': value['task_id'], 'value': value, 'expected_version': 1,
    }, None)
    assert rewritten['version'] == 2

    connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
    raw, version = connection.execute(
        "SELECT value_json,version FROM storage_records WHERE namespace=? "
        "AND record_key=?",
        ('task_results', value['task_id']),
    ).fetchone()
    connection.close()
    assert version == 2
    assert TASK_RESULT_FIELD_CODEC_KEY in json.loads(raw)['segments']


def test_task_result_replay_get_rejects_ambiguous_owner_and_invalid_clocks(
        storage):
    client = storage.client
    ambiguous_owner = {
        'task_id': 'compact-replay-bool-owner',
        'user_id': True,
        'status': 'done',
        'created_at': 10,
        'completed_at': 20,
    }
    client.command(
        'task_results.checkpoint', {
            'key': ambiguous_owner['task_id'],
            'value': ambiguous_owner,
            'expected_version': 0,
        },
        None,
    )
    assert client.query('task_results.replay_get', {
        'key': ambiguous_owner['task_id'], 'user_id': 1,
    }) is None

    invalid_clock = {
        'task_id': 'compact-replay-invalid-clock',
        'user_id': 17,
        'status': 'done',
        'created_at': 10,
        'completed_at': 'not-a-clock',
    }
    client.command(
        'task_results.checkpoint', {
            'key': invalid_clock['task_id'],
            'value': invalid_clock,
            'expected_version': 0,
        },
        None,
    )
    with pytest.raises(StorageError) as raised:
        client.query('task_results.replay_get', {
            'key': invalid_clock['task_id'], 'user_id': 17,
        })
    assert raised.value.code == 'database_integrity'


def test_large_task_event_payload_is_privately_compressed_and_replays(
        storage, tmp_path):
    import orjson

    from lib.storage_sidecar.task_event_codec import COMPRESSED_TASK_EVENT_MAGIC

    event = {
        'type': 'messages_snapshot',
        'messages': [{'role': 'user', 'content': 'repeatable ' * 100_000}],
    }
    payload = {'task_id': 'compressed-event', 'sequence': 1, 'event': event}

    first = storage.client.command(
        'event.append', payload, None, priority='event')
    second = storage.client.command(
        'event.append', payload, None, priority='event')

    assert first['inserted'] is True
    assert second['inserted'] is False
    assert storage.client.query(
        'event.list', {'task_id': 'compressed-event'})[0]['event'] == event
    if storage.client.health()['backend'] == 'sqlite':
        import sqlite3
        connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
        try:
            stored = connection.execute(
                'SELECT event_json FROM storage_events WHERE task_id=?',
                ('compressed-event',),
            ).fetchone()[0]
        finally:
            connection.close()
        assert bytes(stored).startswith(COMPRESSED_TASK_EVENT_MAGIC)
        assert len(stored) < len(orjson.dumps(event)) // 10


def test_task_event_retention_never_prunes_project_brain_streams(storage):
    client = storage.client
    client.command(
        'event.append', {
            'task_id': 'retention-task', 'sequence': 0,
            'event': {'type': 'delta', 'content': 'streaming'},
        }, None, priority='event')
    client.command(
        'event.append', {
            'task_id': 'retention-task', 'sequence': 1,
            'event': {'type': 'messages_snapshot', 'messages': []},
        }, None, priority='event')
    project = '/retention-project'
    client.command(
        'project_brain.narrative.add', {
            'owner_user_id': 7, 'project_key': project,
            'kind': 'decision', 'text': 'Retained project fact',
            'work_id': '', 'conversation_id': 'conv-retention',
            'timestamp': int(time.time() * 1000),
        }, 'retention-project-narrative')

    future_cutoff = int(time.time() * 1000) + 10_000
    streaming = client.command(
        'event.prune', {
            'created_before_ms': future_cutoff,
            'retention_class': 'streaming', 'limit': 100,
        }, None, priority='maintenance')
    assert streaming['deleted'] == 1
    assert [row['sequence'] for row in client.query(
        'event.list', {'task_id': 'retention-task'})] == [1]

    structural = client.command(
        'event.prune', {
            'created_before_ms': future_cutoff,
            'retention_class': 'structural', 'limit': 100,
        }, None, priority='maintenance')
    assert structural['deleted'] == 1
    assert client.query(
        'event.list', {'task_id': 'retention-task'}) == []
    projection = client.query('project_brain.get', {
        'owner_user_id': 7, 'project_key': project,
    })
    assert projection['narratives'][0]['text'] == 'Retained project fact'
    rebuilt = client.maintenance('project_brain.rebuild', {
        'owner_user_id': 7, 'project_key': project,
    })
    assert rebuilt['projection']['narratives'][0]['text'] == (
        'Retained project fact')


def test_event_inspector_summary_counts_roots_and_swarm_children(storage):
    client = storage.client
    events = [
        {'task_id': 'inspector-root', 'sequence': 0,
         'event': {'type': 'messages_snapshot', 'kind': 'request'}},
        {'task_id': 'inspector-root', 'sequence': 1,
         'event': {'type': 'messages_snapshot', 'kind': 'state'}},
        {'task_id': 'inspector-root', 'sequence': 2,
         'event': {'type': 'delta'}},
        {'task_id': 'inspector-root', 'sequence': 3,
         'event': {'type': 'flow_iteration'}},
        {'task_id': 'inspector-root', 'sequence': 4,
         'event': {'type': 'endpoint_iteration'}},
        {'task_id': 'inspector-root#agent:research', 'sequence': 0,
         'event': {'type': 'messages_snapshot', 'kind': 'request'}},
    ]
    client.command(
        'event.append_batch', {'events': events}, None, priority='event')

    result = client.query(
        'event.inspector_summary', {'task_ids': ['inspector-root']})
    by_id = {record['task_id']: record for record in result['records']}
    assert set(by_id) == {
        'inspector-root', 'inspector-root#agent:research'}
    assert by_id['inspector-root'] == {
        'task_id': 'inspector-root',
        'request_count': 1,
        'state_count': 1,
        'legacy_count': 0,
        'event_count': 4,
        'first_event_at_ms': by_id['inspector-root']['first_event_at_ms'],
    }
    assert by_id['inspector-root#agent:research']['request_count'] == 1
    assert by_id['inspector-root#agent:research']['event_count'] == 1

def test_compaction_archives_are_owner_scoped_and_cas_safe(storage, monkeypatch):
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda write=False: storage.client)
    from lib.tasks_pkg.persistence_store import DefaultConversationStore
    from lib.turn_lifecycle import create_turn_pair

    store = DefaultConversationStore()
    create_turn_pair(
        '/workspace/archive', command_id='archive-conversation-create',
        input_projection={'content': 'seed'}, config={}, user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'Archive', 'createdAt': 1,
            'settings': {},
        })
    first = store.archive_transcript(
        '/workspace/archive', [{'role': 'user', 'content': 'a'}], user_id=1)
    second = store.archive_transcript(
        '/workspace/archive', [{'role': 'user', 'content': 'b'}], user_id=1)
    assert first and second and first != second
    with pytest.raises(StorageError, match='32 KiB'):
        storage.client.command(
            'compaction_archive.create',
            {
                'archive_id': 'oversized-receipt',
                'conversation_id': '/workspace/archive',
                'user_id': 1,
                'messages': [],
                'receipt': {'oversized': 'x' * (33 * 1024)},
            },
            'oversized-compaction-receipt',
        )
    receipt = {
        'schemaVersion': 'tofu.compaction-receipt/v1',
        'status': 'completed',
        'strategy': 'selective_summary',
    }
    store.update_archive_summary(
        first, 'summary', 3, 2, user_id=1, receipt=receipt)
    rows = store.list_compaction_archives('/workspace/archive', user_id=1)
    assert [row['id'] for row in rows] == [first, second]
    assert rows[0]['resultStatus'] == 'completed'
    assert rows[0]['resultStrategy'] == 'selective_summary'
    assert 'receipt' not in rows[0], 'history listing must stay metadata-only'
    loaded = store.get_compaction_archive(
        '/workspace/archive', first, user_id=1)
    assert loaded['archive']['summary'] == 'summary'
    assert loaded['messages'][0]['content'] == 'a'
    assert loaded['archive']['schemaVersion'] == 'tofu.compaction-archive/v3'
    assert loaded['archive']['receipt'] == receipt
    assert loaded['archive']['tokenCountKind'] == 'estimated'
    assert loaded['archive']['payloadSizeUnit'] == 'bytes'
    summary_only = store.get_compaction_archive(
        '/workspace/archive', first, user_id=1, include_messages=False)
    assert summary_only['archive']['summary'] == 'summary'
    assert summary_only['archive']['messagesCount'] == 1
    assert summary_only['archive']['receipt'] == receipt
    assert 'messages' not in summary_only
    with pytest.raises(StorageError) as raised:
        store.list_compaction_archives('/workspace/archive', user_id=2)
    assert raised.value.code == 'database_not_found'
    assert store.prune_archives('/workspace/archive', 1, user_id=1) == 1
    assert store.delete_archives('/workspace/archive', user_id=1) == {
        'deleted': 1}
    assert store.list_compaction_archives('/workspace/archive', user_id=1) == []


def test_sidecar_task_result_write_fence_and_abort_tombstone(storage, monkeypatch):
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda write=False: storage.client)
    from lib.tasks_pkg.manager._persist import _upsert_task_row
    from lib.tasks_pkg.manager._registry import (
        _db_abort_tombstoned, _write_abort_tombstone_row,
    )

    task = {'id': 'task-sidecar-result', '_userId': 1, 'created_at': 1.0}
    assert _upsert_task_row(
        task, '', content='partial', thinking='', status='running',
        error_json='{}', tr_json='[]', meta_json='{}', segments_json='[]')
    assert _write_abort_tombstone_row(
        task['id'], 'test', user_id=1) is True
    assert _db_abort_tombstoned(task['id'], user_id=1) is True
    # A checkpoint preserves the tombstone. Recovery then fences a stale
    # terminal writer from resurrecting the row.
    assert _upsert_task_row(
        task, '', content='partial-2', thinking='', status='running',
        error_json='{}', tr_json='[]', meta_json='{}', segments_json='[]')
    assert _upsert_task_row(
        task, '', content='recovered', thinking='', status='interrupted',
        error_json='{}', tr_json='[]', meta_json='{}', segments_json='[]')
    assert not _upsert_task_row(
        task, '', content='late', thinking='', status='done',
        error_json='{}', tr_json='[]', meta_json='{}', segments_json='[]')
    row = storage.client.query(
        'record.get', {'namespace': 'task_results', 'key': task['id']})
    assert row['value']['status'] == 'interrupted'
    assert row['value']['abort_requested_at']


def test_task_result_abort_is_atomic_idempotent_and_owner_scoped(
        storage, tmp_path):
    task_id = 'task-owner-scoped-abort'
    value = {
        'task_id': task_id,
        'conv_id': '',
        'user_id': 7,
        'status': 'running',
        'segments': 'preserved abort segment ' * 20_000,
        'content': '',
        'thinking': '',
        'created_at': 1,
        'completed_at': 2,
    }
    storage.client.command(
        'task_results.checkpoint', {
            'key': task_id, 'value': value, 'expected_version': 0,
        }, None)

    foreign = storage.client.command(
        'task_results.abort', {
            'task_id': task_id, 'user_id': 8, 'source': 'foreign',
        }, None)
    assert foreign == {'signaled': False, 'changed': False}
    assert storage.client.query(
        'task_results.abort_requested', {
            'task_id': task_id, 'user_id': 8}) == {'requested': False}

    first = storage.client.command(
        'task_results.abort', {
            'task_id': task_id, 'user_id': 7, 'source': 'owner',
        }, None)
    assert first['signaled'] is True
    assert first['changed'] is True
    replay = storage.client.command(
        'task_results.abort', {
            'task_id': task_id, 'user_id': 7, 'source': 'owner-retry',
        }, None)
    assert replay == {'signaled': True, 'changed': False}
    assert storage.client.query(
        'task_results.abort_requested', {
            'task_id': task_id, 'user_id': 7}) == {'requested': True}
    assert storage.client.query('record.get', {
        'namespace': 'task_results', 'key': task_id,
    })['value']['segments'] == value['segments']
    if storage.client.health()['backend'] == 'sqlite':
        from lib.storage_sidecar.task_result_field_codec import (
            TASK_RESULT_FIELD_CODEC_KEY,
        )
        connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
        raw = connection.execute(
            "SELECT value_json FROM storage_records WHERE namespace=? "
            "AND record_key=?",
            ('task_results', task_id),
        ).fetchone()[0]
        connection.close()
        assert TASK_RESULT_FIELD_CODEC_KEY in json.loads(raw)['segments']


def test_guarded_task_checkpoint_hides_foreign_keys_and_fences_regressions(
        storage):
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )

    task_id = 'task-guarded-owner-collision'
    value = {
        'task_id': task_id,
        'conv_id': '',
        'user_id': 7,
        'status': 'pending',
        'content': '',
        'thinking': '',
        'created_at': 1,
        'completed_at': 2,
    }
    first = storage.client.command(
        'task_results.checkpoint', {
            'key': task_id, 'value': value, 'expected_version': 0,
        }, None)

    foreign = storage.client.command(
        'task_results.checkpoint', {
            'key': task_id,
            'value': {**value, 'user_id': 8, 'status': 'running'},
            'expected_version': first['version'],
            'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
            'require_parent': False,
        }, None)
    assert foreign == {
        'key': task_id,
        'version': 0,
        'updated_at_ms': 0,
        'owned': False,
        'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    }

    done = storage.client.command(
        'task_results.checkpoint', {
            'key': task_id,
            'value': {**value, 'status': 'done'},
            'expected_version': first['version'],
            'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
            'require_parent': False,
        }, None)
    late_birth = storage.client.command(
        'task_results.checkpoint', {
            'key': task_id,
            'value': value,
            'expected_version': done['version'],
            'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
            'require_parent': False,
        }, None)
    assert late_birth['owned'] is False

    with pytest.raises(StorageError) as mismatched:
        storage.client.command(
            'task_results.checkpoint', {
                'key': 'guarded-wire-key',
                'value': {**value, 'task_id': 'different-value-key'},
                'expected_version': 0,
                'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                'require_parent': False,
            }, None)
    assert mismatched.value.code == 'database_protocol_error'

    authority = storage.client.query(
        'record.get', {'namespace': 'task_results', 'key': task_id})
    assert authority['version'] == done['version']
    assert authority['value']['user_id'] == 7
    assert authority['value']['status'] == 'done'


def test_task_checkpoint_cache_facts_are_atomic_and_replay_safe(storage):
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD,
        TASK_RESULT_CACHE_SETTINGS_CONTRACT,
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
    )

    conv_id = 'task-cache-fact-replay'
    _import_conversation(
        storage.client,
        conv_id,
        user_id=7,
        settings={'unrelated': 'preserved'},
    )

    def checkpoint_payload(task_id, hwm, last_read):
        return {
            'key': task_id,
            'value': {
                'task_id': task_id,
                'conv_id': conv_id,
                'user_id': 7,
                'status': 'done',
                'content': '',
                'thinking': '',
                'created_at': 1,
                'completed_at': 2,
                TASK_RESULT_CACHE_PREFIX_HWM_FIELD: hwm,
                TASK_RESULT_LAST_TURN_CACHE_READ_FIELD: last_read,
            },
            'expected_version': 0,
            'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
            'require_parent': True,
            'cache_settings_contract': TASK_RESULT_CACHE_SETTINGS_CONTRACT,
        }

    older_payload = checkpoint_payload('task-cache-older', 10, 3_000)
    older = storage.client.command(
        'task_results.checkpoint', older_payload, None)
    newer = storage.client.command(
        'task_results.checkpoint',
        checkpoint_payload('task-cache-newer', 20, 4_000),
        None,
    )
    assert older['cache_settings_committed'] is True
    assert newer['cache_settings_committed'] is True

    replay = storage.client.command(
        'task_results.checkpoint', older_payload, None)
    assert replay['version'] == older['version']
    assert replay[TASK_RESULT_CACHE_PREFIX_HWM_FIELD] == 20
    assert replay[TASK_RESULT_LAST_TURN_CACHE_READ_FIELD] == 4_000
    document = storage.client.query(
        'conversation.get', {
            'conv_id': conv_id,
            'user_id': 7,
            'derive_messages': False,
        })
    assert document['metadata']['settings'] == {
        'cachePrefixHWM': 20,
        'lastTurnCacheRead': 4_000,
        'unrelated': 'preserved',
    }


def test_task_checkpoint_cache_facts_require_the_combined_contract(storage):
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD,
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )

    conv_id = 'task-cache-fact-invalid'
    _import_conversation(storage.client, conv_id, user_id=7)
    with pytest.raises(StorageError) as raised:
        storage.client.command(
            'task_results.checkpoint', {
                'key': 'task-cache-fact-invalid',
                'value': {
                    'task_id': 'task-cache-fact-invalid',
                    'conv_id': conv_id,
                    'user_id': 7,
                    'status': 'done',
                    TASK_RESULT_CACHE_PREFIX_HWM_FIELD: 10,
                },
                'expected_version': 0,
                'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                'require_parent': True,
            },
            None,
        )
    assert raised.value.code == 'database_protocol_error'
    assert storage.client.query(
        'record.get', {
            'namespace': 'task_results',
            'key': 'task-cache-fact-invalid',
        }) is None


def test_sidecar_task_result_transient_write_failure_is_not_a_fence(storage, monkeypatch):
    """A transient store failure (writer-acquisition timeout) is NOT a
    recovery/terminal fence. Terminal diagnostics retry with backoff and
    succeed when pressure clears. A reconstructible running checkpoint instead
    makes one short maintenance-lane attempt and RAISES when it cannot enter;
    callers still cannot mistake pressure for the False stale-owner verdict
    that suppresses downstream terminal work (2026-08-20 incident)."""
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda write=False: storage.client)
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    real_command = storage.client.command
    attempts = {'n': 0}

    def flaky(command, payload, command_id, **kwargs):
        if command == 'task_results.checkpoint' and attempts['n'] < 2:
            attempts['n'] += 1
            raise StorageError('database_unavailable',
                               'Storage writer acquisition timed out')
        return real_command(command, payload, command_id, **kwargs)

    monkeypatch.setattr(storage.client, 'command', flaky)
    task = {'id': 'task-transient-retry', '_userId': 1, 'created_at': 1.0}
    assert _upsert_task_row(
        task, '', content='terminal', thinking='', status='done',
        error_json=None, tr_json=None, meta_json=None) is True
    assert attempts['n'] == 2

    running_attempts = []

    def always_fail(command, payload, command_id, **kwargs):
        if command == 'task_results.checkpoint':
            running_attempts.append(kwargs)
            raise StorageError('database_unavailable',
                               'Storage writer acquisition timed out')
        return real_command(command, payload, command_id, **kwargs)

    monkeypatch.setattr(storage.client, 'command', always_fail)
    task2 = {'id': 'task-transient-raise', '_userId': 1, 'created_at': 1.0}
    with pytest.raises(StorageError):
        _upsert_task_row(
            task2, '', content='partial', thinking='', status='running',
            error_json=None, tr_json=None, meta_json=None)
    assert running_attempts == [{
        'priority': 'maintenance',
        'deadline': 0.5,
    }]


def test_sidecar_startup_recovery_keeps_task_and_turn_authorities_separate(
        storage, monkeypatch):
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda write=False: storage.client)
    _import_conversation(
        storage.client,
        'conv-recovery',
        messages=[
            {'role': 'user', 'content': 'hello', 'timestamp': 1},
            {'role': 'assistant', 'content': 'old', 'timestamp': 2,
             '_taskId': 'task-recovery'},
        ],
        title='Recovery',
    )
    storage.client.command('record.put', {
        'namespace': 'task_results', 'key': 'task-recovery',
        'value': {
            'task_id': 'task-recovery', 'conv_id': 'conv-recovery',
            'status': 'running', 'content': 'old plus recovered',
            'thinking': 'partial thought', 'tool_rounds': [],
            'metadata': {},
        },
    }, 'task-recovery-create')
    storage.client.command('record.put', {
        'namespace': 'task_results', 'key': 'task-recovery-pending',
        'value': {
            'task_id': 'task-recovery-pending', 'conv_id': 'conv-recovery',
            'status': 'pending', 'content': '', 'thinking': '',
            'tool_rounds': [], 'metadata': {},
        },
    }, 'task-recovery-pending-create')

    from lib.tasks_pkg.manager._recovery import recover_stale_tasks_on_startup
    result = recover_stale_tasks_on_startup(
        prev_shutdown={'verdict': 'unclean'})
    assert result['recoveredTaskCount'] == 2
    assert result['conversationIds'] == ['conv-recovery']
    assert result['interruptedReason'] == 'process_killed'
    row = storage.client.query(
        'record.get', {'namespace': 'task_results', 'key': 'task-recovery'})
    assert row['value']['status'] == 'interrupted'
    assert row['value']['interruptedReason'] == 'process_killed'
    assert row['value']['completed_at'] > 0
    pending_row = storage.client.query(
        'record.get', {
            'namespace': 'task_results', 'key': 'task-recovery-pending'})
    assert pending_row['value']['status'] == 'interrupted'
    assert pending_row['value']['interruptedReason'] == 'process_killed'
    document = storage.client.query(
        'conversation.get', {'conv_id': 'conv-recovery', 'user_id': 1})
    # A task snapshot is never reverse-projected into the transcript.
    assert document['messages'][-1]['content'] == 'old'
    assert document['messages'][-1].get('finishReason') is None


def test_event_batch_is_atomic_and_naturally_deduplicated(storage):
    events = [{
        'task_id': f'batch-task-{index % 2}', 'sequence': index // 2,
        'event': {'kind': 'delta', 'index': index},
    } for index in range(100)]
    first = storage.client.command(
        'event.append_batch', {'events': events}, None, priority='event')
    replay = storage.client.command(
        'event.append_batch', {'events': events}, None, priority='event')
    assert first['inserted'] == 100 and first['deduplicated'] == 0
    assert replay['inserted'] == 0 and replay['deduplicated'] == 100
    assert len(replay['results']) == 100


def test_rate_limit_bucket_admission_is_atomic(storage):
    workers = 16
    limit = 5
    barrier = threading.Barrier(workers)

    def hit(index: int):
        barrier.wait(timeout=10)
        command_id = f'rate-hit-{index}'
        return storage.client.command(
            'rate_limit.record_and_check',
            {
                'endpoint': '/contract',
                'client_key': '198.51.100.8',
                'event_id': command_id,
                'limit': limit,
                'per_seconds': 60,
            },
            command_id,
        )

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(hit, range(workers)))
    assert sum(1 for item in results if item['allowed']) == limit
    assert all(item['count'] <= limit for item in results)


def test_rate_limit_prunes_expired_rows_across_unrelated_buckets(storage):
    first = storage.client.command(
        'rate_limit.record_and_check', {
            'endpoint': '/old', 'client_key': '198.51.100.1',
            'event_id': 'rate-expiry-old', 'limit': 5, 'per_seconds': 1,
        }, 'rate-expiry-old')
    assert first == {'allowed': True, 'count': 1, 'pruned': 0}

    time.sleep(1.2)
    trigger = storage.client.command(
        'rate_limit.record_and_check', {
            'endpoint': '/unrelated', 'client_key': '198.51.100.2',
            'event_id': 'rate-expiry-trigger', 'limit': 5,
            'per_seconds': 60,
        }, 'rate-expiry-trigger')
    assert trigger['pruned'] >= 1

    fresh = storage.client.command(
        'rate_limit.record_and_check', {
            'endpoint': '/old', 'client_key': '198.51.100.1',
            'event_id': 'rate-expiry-fresh', 'limit': 5, 'per_seconds': 1,
        }, 'rate-expiry-fresh')
    assert fresh['allowed'] is True
    assert fresh['count'] == 1


def test_orchestration_aggregate_semantics(storage):
    assert storage.client.command(
        'orchestration.run.create',
        {
            'run_id': 'run-contract', 'definition': {'nodes': []},
            'input': 'go', 'orch_id': 'flow-1', 'name': 'Contract',
            'created_by': 'tester', 'user_id': 1,
        },
        'orch-create-contract',
    ) == {'created': True}
    created = storage.client.query(
        'orchestration.run.get', {'run_id': 'run-contract', 'user_id': 1})
    assert created['status'] == 'pending'
    assert created['definition'] == {'nodes': []}

    event = {'type': 'flow_start', 'node_id': 'root'}
    projected = storage.client.command(
        'orchestration.event.project',
        {'run_id': 'run-contract', 'user_id': 1, 'sequence': 0,
         'event': event, 'status': 'running'},
        None,
    )
    assert projected == {'projected': True, 'inserted': True}
    assert storage.client.command(
        'orchestration.event.project',
        {'run_id': 'run-contract', 'user_id': 1, 'sequence': 0,
         'event': event, 'status': 'running'},
        None,
    ) == {'projected': True, 'inserted': False}
    with pytest.raises(StorageError) as conflict:
        storage.client.command(
            'orchestration.event.append',
            {'run_id': 'run-contract', 'user_id': 1, 'sequence': 0,
             'event': {'type': 'different'}},
            None,
        )
    assert conflict.value.code == 'database_conflict'

    page = storage.client.query(
        'orchestration.event.page',
        {'run_id': 'run-contract', 'user_id': 1, 'cursor': 0})
    assert page['events'] == [{**event, 'seq': 0}]
    assert page['caught_up'] is True
    assert storage.client.command(
        'orchestration.run.update_status',
        {'run_id': 'run-contract', 'user_id': 1,
         'status': 'done', 'final': 'ok'},
        'orch-finish-contract',
    )['changed']
    with pytest.raises(StorageError) as terminal:
        storage.client.command(
            'orchestration.event.project',
            {'run_id': 'run-contract', 'user_id': 1, 'sequence': 1,
             'event': {'type': 'late'}, 'status': 'running'},
            None,
        )
    assert terminal.value.code == 'database_conflict'
    assert storage.client.query(
        'orchestration.event.page',
        {'run_id': 'run-contract', 'user_id': 1, 'cursor': 0})['events'] == [
            {**event, 'seq': 0}]
    assert storage.client.command(
        'orchestration.run.delete', {'run_id': 'run-contract', 'user_id': 1},
        'orch-delete-contract')['deleted']
    assert storage.client.query(
        'orchestration.run.get', {
            'run_id': 'run-contract', 'user_id': 1}) is None


def test_swarm_checkpoint_aggregate_semantics(storage):
    client = storage.client
    assert client.command(
        'swarm.session.save', {
            'swarm_key': 'swarm-contract', 'conv_id': 'conv-1',
            'task_id': 'task-1', 'status': 'running',
            'specs': [{'id': 'a1'}], 'config': {'model': 'test'},
            'now_ms': 100,
        }, 'swarm-session-create') == {'saved': True}
    assert client.command(
        'swarm.session.save', {
            'swarm_key': 'swarm-contract', 'conv_id': 'conv-2',
            'task_id': 'task-2', 'status': 'running',
            'specs': [{'id': 'a1'}], 'config': {'model': 'test-2'},
            'now_ms': 200,
        }, 'swarm-session-update') == {'saved': True}
    client.command(
        'swarm.agent.save', {
            'swarm_key': 'swarm-contract', 'agent_id': 'a1',
            'role': 'coder', 'objective': 'resume safely',
            'status': 'completed', 'messages': [{'role': 'assistant'}],
            'result': {'final_answer': 'done'}, 'rounds_used': 1,
            'delivered': True, 'now_ms': 300,
        }, 'swarm-agent-create')
    client.command(
        'swarm.agent.save', {
            'swarm_key': 'swarm-contract', 'agent_id': 'a1',
            'role': 'coder', 'objective': 'resume safely',
            'status': 'completed', 'messages': [{'role': 'assistant', 'content': 'done'}],
            'result': {'final_answer': 'done'}, 'rounds_used': 2,
            'delivered': None, 'now_ms': 400,
        }, 'swarm-agent-update')
    detail = client.query(
        'swarm.session.get', {'swarm_key': 'swarm-contract'})
    assert detail['conv_id'] == 'conv-2'
    assert detail['created_at'] == 100
    assert detail['updated_at'] == 200
    assert detail['agents'][0]['delivered'] is True
    assert detail['agents'][0]['rounds_used'] == 2
    assert client.query('swarm.resumable.list', {}) == []

    client.command(
        'swarm.agent.save', {
            'swarm_key': 'swarm-contract', 'agent_id': 'a1',
            'role': 'coder', 'objective': 'resume safely',
            'status': 'running', 'messages': [], 'result': {},
            'rounds_used': 2, 'delivered': None, 'now_ms': 500,
        }, 'swarm-agent-running')
    resumable = client.query('swarm.resumable.list', {})
    assert [item['swarm_key'] for item in resumable] == ['swarm-contract']
    assert resumable[0]['agents'][0]['status'] == 'running'

    assert client.command(
        'swarm.session.quarantine_ownerless', {
            'swarm_key': 'swarm-contract', 'now_ms': 600,
        }, 'swarm-session-quarantine') == {'changed': True}
    quarantined = client.query(
        'swarm.session.get', {'swarm_key': 'swarm-contract'})
    assert quarantined['status'] == 'quarantined:ownerless'
    assert quarantined['agents'][0]['status'] == 'running'
    assert client.query('swarm.resumable.list', {}) == []
    assert client.command(
        'swarm.session.quarantine_ownerless', {
            'swarm_key': 'swarm-contract', 'now_ms': 700,
        }, 'swarm-session-quarantine-repeat') == {'changed': False}
    assert client.command(
        'swarm.session.terminate', {
            'swarm_key': 'swarm-contract', 'now_ms': 750,
        }, 'swarm-session-terminate-quarantine') == {'changed': False}
    assert client.query(
        'swarm.session.get', {
            'swarm_key': 'swarm-contract'})['status'] == 'quarantined:ownerless'

    with pytest.raises(StorageError) as invalid_status:
        client.command(
            'swarm.session.save', {
                'swarm_key': 'swarm-contract', 'conv_id': 'conv-2',
                'task_id': 'task-2', 'status': 'quarantined:ownerless',
                'specs': [{'id': 'a1'}], 'config': {'user_id': 1},
                'now_ms': 775,
            }, 'swarm-session-invalid-status')
    assert invalid_status.value.code == 'database_protocol_error'

    # A concurrent/operator repair wins: quarantine re-checks the owner in
    # its own transaction and cannot hide newly owned work.
    assert client.command(
        'swarm.session.save', {
            'swarm_key': 'swarm-contract', 'conv_id': 'conv-2',
            'task_id': 'task-2', 'status': 'running',
            'specs': [{'id': 'a1'}],
            'config': {'model': 'test-2', 'user_id': 1},
            'now_ms': 800,
        }, 'swarm-session-owner-repair') == {'saved': True}
    assert client.command(
        'swarm.session.quarantine_ownerless', {
            'swarm_key': 'swarm-contract', 'now_ms': 900,
        }, 'swarm-session-quarantine-after-repair') == {'changed': False}
    assert [item['swarm_key'] for item in client.query(
        'swarm.resumable.list', {})] == ['swarm-contract']

    assert client.command(
        'swarm.session.delete', {'swarm_key': 'swarm-contract'},
        'swarm-session-delete') == {'deleted': True}
    assert client.query(
        'swarm.session.get', {'swarm_key': 'swarm-contract'}) is None


def test_research_artifact_aggregate_semantics(storage):
    client = storage.client
    client.command('paper.report.upsert', {
        'user_id': 1, 'paper_hash': 'paper-contract', 'lang': 'en',
        'report': 'ordinary paper', 'model': 'm',
        'meta': {'direction': 'must not leak', 'kind': 'insight'},
        'created_at': 999,
    }, 'paper-report-contract')
    base = {
        'paper_hash': 'research-contract', 'model': 'm', 'created_at': 1000,
    }
    assert client.command('research.artifact.upsert', {
        **base, 'user_id': 1, 'lang_key': 'survey:en', 'report': '# survey',
        'meta': {'kind': 'survey', 'direction': 'storage architecture',
                 'open_gaps': {'open_gaps': []}},
    }, 'research-survey-contract') == {'saved': True}
    client.command('research.artifact.upsert', {
        **base, 'user_id': 1, 'lang_key': 'ideate:en', 'report': '# ideas',
        'meta': {'kind': 'ideate', 'direction': 'storage architecture',
                 'accepted': [{'id': 'a'}], 'rejected': [{'id': 'r'}],
                 'gate_reached': 'accepted'},
    }, 'research-ideate-contract')

    artifacts = client.query('research.artifacts.get', {
        'user_id': 1, 'paper_hash': 'research-contract', 'lang': 'en',
    })
    assert [item['lang_key'] for item in artifacts] == [
        'ideate:en', 'survey:en']
    listed = client.query(
        'research.directions.list', {'user_id': 1, 'limit': 50})
    assert [item['direction'] for item in listed] == ['storage architecture']
    assert listed[0]['accepted'] == 1
    assert listed[0]['has_survey'] is True
    assert client.query('paper.report.get', {
        'user_id': 1, 'paper_hash': 'paper-contract', 'lang': 'en',
    })['report'] == 'ordinary paper'
    excerpt = client.query('paper.report.get', {
        'user_id': 1, 'paper_hash': 'paper-contract', 'lang': 'en',
        'max_report_chars': 8,
    })
    assert excerpt['report'] == 'ordinary'
    assert excerpt['model'] == '' and excerpt['meta'] == {}
    excerpts = client.query('paper.report.excerpts', {
        'user_id': 1, 'lang': 'en',
        'paper_hashes': ['missing', 'paper-contract'],
        'max_report_chars': 8,
    })
    assert excerpts == [{
        'user_id': 1, 'paper_hash': 'paper-contract', 'lang': 'en',
        'report': 'ordinary', 'created_at': 999,
    }]
    fallback = client.query('paper.report.resolve', {
        'user_id': 1, 'paper_hash': 'paper-contract',
        'preferred_lang': 'zh', 'fallback_lang': 'en',
    })
    assert fallback['lang'] == 'en' and fallback['report'] == 'ordinary paper'
    client.command('paper.report.upsert', {
        'user_id': 1, 'paper_hash': 'paper-contract', 'lang': 'zh',
        'report': '中文论文', 'model': 'm-zh', 'meta': {'language': 'zh'},
        'created_at': 1000,
    }, 'paper-report-resolve-zh')
    preferred = client.query('paper.report.resolve', {
        'user_id': 1, 'paper_hash': 'paper-contract',
        'preferred_lang': 'zh', 'fallback_lang': 'en',
    })
    assert preferred['lang'] == 'zh' and preferred['report'] == '中文论文'
    assert client.query('paper.report.resolve', {
        'user_id': 2, 'paper_hash': 'paper-contract',
        'preferred_lang': 'zh', 'fallback_lang': 'en',
    }) is None
    for index, sibling_lang in enumerate(
        ('insight:zh', 'termfill:zh', 'checkpoints:zh'), start=1
    ):
        client.command('paper.report.upsert', {
            'user_id': 1, 'paper_hash': 'paper-contract',
            'lang': sibling_lang, 'report': f'sibling-{index}', 'model': '',
            'meta': {'index': index}, 'created_at': 1000 + index,
        }, f'paper-report-reopen-{index}')
    reopened = client.query('paper.report.reopen', {
        'user_id': 1, 'paper_hash': 'paper-contract',
        'preferred_lang': 'zh', 'fallback_lang': 'en',
        'sibling_langs_by_base': {
            'zh': ['insight:zh', 'termfill:zh', 'checkpoints:zh'],
            'en': ['insight:en'],
        },
    })
    assert reopened['report']['lang'] == 'zh'
    assert [row['lang'] for row in reopened['siblings']] == [
        'insight:zh', 'termfill:zh', 'checkpoints:zh']
    assert reopened['siblings'][0]['meta'] == {'index': 1}
    owner_miss = client.query('paper.report.reopen', {
        'user_id': 2, 'paper_hash': 'paper-contract',
        'preferred_lang': 'zh', 'fallback_lang': 'en',
        'sibling_langs_by_base': {'zh': ['insight:zh']},
    })
    assert owner_miss == {'report': None, 'siblings': []}


def test_paper_second_pass_merge_is_atomic_and_preserves_siblings(storage):
    client = storage.client
    client.command('paper.report.upsert', {
        'user_id': 1, 'paper_hash': 'second-pass-contract', 'lang': 'en',
        'report': 'body', 'model': 'm', 'created_at': 1000,
        'meta': {
            'promptTokens': 100, 'completionTokens': 20,
            'costCny': 0.01, 'costUsd': 0.001,
        },
    }, 'second-pass-report')
    entries = {
        'insight': {
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
            'costCny': 0.002, 'costUsd': 0.0002,
        },
        'checkpoints': {
            'usage': {'prompt_tokens': 5, 'completion_tokens': 1},
            'costCny': 0.001, 'costUsd': 0.0001,
        },
    }

    def merge(item):
        name, entry = item
        return client.command('paper.report.second_pass.merge', {
            'user_id': 1, 'paper_hash': 'second-pass-contract', 'lang': 'en',
            'name': name, 'entry': entry,
        }, f'second-pass-{name}')

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(merge, entries.items()))
    assert all(result['found'] for result in results)

    stored = client.query('paper.report.get', {
        'user_id': 1, 'paper_hash': 'second-pass-contract', 'lang': 'en',
    })['meta']
    assert set(stored['secondPasses']) == {'insight', 'checkpoints'}
    assert stored['totalUsage']['prompt_tokens'] == 115
    assert stored['totalUsage']['completion_tokens'] == 23
    assert stored['totalCostCny'] == pytest.approx(0.013)
    assert stored['totalCostUsd'] == pytest.approx(0.0013)

    def accumulate(index):
        return client.command('paper.report.second_pass.accumulate', {
            'user_id': 1, 'paper_hash': 'second-pass-contract', 'lang': 'en',
            'name': 'deepen',
            'usage': {'prompt_tokens': 5, 'completion_tokens': 2},
            'costCny': 0.0005, 'costUsd': 0.00005,
        }, f'second-pass-accumulate-{index}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        accumulated = list(pool.map(accumulate, range(2)))
    assert all(result['found'] for result in accumulated)
    stored = client.query('paper.report.get', {
        'user_id': 1, 'paper_hash': 'second-pass-contract', 'lang': 'en',
    })['meta']
    assert stored['secondPasses']['deepen']['calls'] == 2
    assert stored['secondPasses']['deepen']['usage']['prompt_tokens'] == 10
    assert stored['totalUsage']['prompt_tokens'] == 125
    assert stored['totalUsage']['completion_tokens'] == 27
    assert stored['totalCostCny'] == pytest.approx(0.014)
    assert stored['totalCostUsd'] == pytest.approx(0.0014)

    missing = client.command('paper.report.second_pass.merge', {
        'user_id': 1, 'paper_hash': 'missing-report', 'lang': 'en',
        'name': 'insight', 'entry': {},
    }, 'second-pass-missing')
    assert missing == {'found': False, 'meta': None}


def test_paper_translation_semantics(storage):
    client = storage.client
    assert client.query('paper.translation.get', {
        'user_id': 1, 'paper_hash': 'translation-contract',
        'lang': 'review:neurips:zh',
    }) is None
    assert client.command('paper.translation.upsert', {
        'user_id': 1, 'paper_hash': 'translation-contract',
        'lang': 'review:neurips:zh',
        'text': '第一版', 'model': 'm1', 'created_at': 1000,
    }, 'translation-create') == {'saved': True}
    assert client.command('paper.translation.upsert', {
        'user_id': 1, 'paper_hash': 'translation-contract',
        'lang': 'review:neurips:zh',
        'text': '第二版', 'model': 'm2', 'created_at': 2000,
    }, 'translation-update') == {'saved': True}
    assert client.query('paper.translation.get', {
        'user_id': 1, 'paper_hash': 'translation-contract',
        'lang': 'review:neurips:zh',
    }) == {
        'user_id': 1, 'paper_hash': 'translation-contract',
        'lang': 'review:neurips:zh',
        'text': '第二版', 'model': 'm2', 'created_at': 2000,
    }


def test_paper_artifacts_are_isolated_by_owner(storage):
    client = storage.client
    for owner, body in ((1, 'owner-one'), (2, 'owner-two')):
        client.command('paper.report.upsert', {
            'user_id': owner, 'paper_hash': 'shared-private-hash', 'lang': 'en',
            'report': body, 'model': 'm', 'meta': {'owner': owner},
            'created_at': 1000 + owner,
        }, f'owner-report-{owner}')
        client.command('paper.translation.upsert', {
            'user_id': owner, 'paper_hash': 'shared-private-hash', 'lang': 'zh',
            'text': f'translation-{owner}', 'model': 'm',
            'created_at': 1000 + owner,
        }, f'owner-translation-{owner}')

    assert client.query('paper.report.get', {
        'user_id': 1, 'paper_hash': 'shared-private-hash', 'lang': 'en',
    })['report'] == 'owner-one'
    assert client.query('paper.report.get', {
        'user_id': 2, 'paper_hash': 'shared-private-hash', 'lang': 'en',
    })['report'] == 'owner-two'
    assert client.query('paper.report.latest', {
        'user_id': 2, 'paper_hash': 'shared-private-hash',
    })['meta'] == {'owner': 2}
    assert client.query('paper.translation.get', {
        'user_id': 1, 'paper_hash': 'shared-private-hash', 'lang': 'zh',
    })['text'] == 'translation-1'


def test_paper_notes_are_owner_scoped(storage):
    client = storage.client
    base = {
        'id': 'same-note-id', 'paper_hash': 'shared-private-hash', 'lang': 'en',
        'anchor': {'heading_idx': 1, 'quote': 'evidence'},
        'created_at': 1000, 'updated_at': 1000,
    }
    for owner in (1, 2):
        assert client.command('paper.note.create', {
            **base, 'user_id': owner, 'note': f'note-{owner}',
        }, f'note-create-{owner}') == {'saved': True}
    assert client.query('paper.note.list', {
        'user_id': 1, 'paper_hash': 'shared-private-hash', 'lang': 'en',
    })[0]['note'] == 'note-1'
    assert client.command('paper.note.update', {
        'user_id': 1, 'id': 'same-note-id', 'note': 'updated-one',
        'updated_at': 2000,
    }, 'note-update-one') == {'updated': True}
    assert client.query('paper.note.list', {
        'user_id': 2, 'paper_hash': 'shared-private-hash', 'lang': 'en',
    })[0]['note'] == 'note-2'
    assert client.command('paper.note.delete', {
        'user_id': 1, 'id': 'same-note-id',
    }, 'note-delete-one') == {'deleted': True}
    assert client.query('paper.note.list', {
        'user_id': 2, 'paper_hash': 'shared-private-hash', 'lang': 'en',
    })[0]['note'] == 'note-2'


def test_paper_library_context_and_identity_semantics(storage):
    client = storage.client
    for index, title in enumerate(('Current paper', 'Prior one', 'Prior two')):
        assert client.command('paper.library.put', {
            'id': f'paper-{index}', 'user_id': 1, 'title': title,
            'arxiv_id': f'2608.0000{index}',
            'paper_hash': f'hash-{index}',
            'parsed_text': f'parsed-{index}',
            'created_at': 1000 + index, 'updated_at': 1000 + index,
        }, f'paper-library-{index}') == {'saved': True}

    client.command('paper.report.upsert', {
        'user_id': 2, 'paper_hash': 'hash-0', 'lang': 'en',
        'report': 'foreign', 'model': '', 'meta': {}, 'created_at': 1,
    }, 'paper-library-foreign-report')
    client.command('paper.report.upsert', {
        'user_id': 1, 'paper_hash': 'hash-1', 'lang': 'review:en',
        'report': 'owned', 'model': '', 'meta': {}, 'created_at': 2,
    }, 'paper-library-owned-report')
    listed = client.query('paper.library.list', {'user_id': 1})
    has_report = {row['paperHash']: row['hasReport'] for row in listed}
    assert has_report == {
        'hash-0': False, 'hash-1': True, 'hash-2': False,
    }
    summaries = client.query('paper.library.summaries', {'user_id': 1})
    assert all(
        not {'parsedText', 'qaHistory', 'images', 'babelCache'} & row.keys()
        for row in summaries
    )
    assert [row['id'] for row in summaries] == [
        row['id'] for row in listed]
    detail = client.query('paper.library.get', {
        'user_id': 1, 'id': 'paper-1',
    })
    assert detail['parsedText'] == 'parsed-1'
    assert detail['paperHash'] == 'hash-1'
    assert detail['hasReport'] is True
    assert client.query('paper.library.get', {
        'user_id': 2, 'id': 'paper-1',
    }) is None
    reader = client.query('paper.library.reader', {
        'user_id': 1, 'id': 'paper-1',
    })
    assert reader['parsedText'] == 'parsed-1'
    assert 'babelCache' not in reader
    assert client.query('paper.library.reader', {
        'user_id': 2, 'id': 'paper-1',
    }) is None

    assert client.query('paper.library.identity', {
        'user_id': 1, 'paper_hash': 'hash-0',
    }) == {
        'title': 'Current paper', 'arxiv_id': '2608.00000',
        'parsed_text': 'parsed-0', 'parsed_text_length': 8,
    }
    assert client.query('paper.library.identity', {
        'user_id': 1, 'paper_hash': 'hash-0', 'max_text_chars': 4,
    }) == {
        'title': 'Current paper', 'arxiv_id': '2608.00000',
        'parsed_text': 'pars', 'parsed_text_length': 8,
    }
    assert client.query('paper.library.identity', {
        'user_id': 1, 'paper_hash': 'hash-0', 'max_text_chars': 0,
    }) == {
        'title': 'Current paper', 'arxiv_id': '2608.00000',
        'parsed_text': '', 'parsed_text_length': 8,
    }
    assert client.query('paper.library.identity', {
        'user_id': 2, 'paper_hash': 'hash-0', 'max_text_chars': 4,
    }) is None
    assert client.query('paper.library.recent', {
        'user_id': 1, 'exclude_paper_hash': 'hash-0', 'limit': 40,
    }) == [
        {'title': 'Prior two', 'arxiv_id': '2608.00002'},
        {'title': 'Prior one', 'arxiv_id': '2608.00001'},
    ]
    targeted = client.query('paper.library.inputs', {
        'user_id': 1,
        'arxiv_ids': ['2608.00000', '2608.00002'],
        'max_text_chars': 4,
    })
    assert [row['arxivId'] for row in targeted] == [
        '2608.00002', '2608.00000']
    assert [row['parsedText'] for row in targeted] == ['pars', 'pars']
    assert [row['parsedTextLength'] for row in targeted] == [8, 8]
    assert client.query('paper.library.inputs', {
        'user_id': 2, 'arxiv_ids': ['2608.00000'],
        'max_text_chars': 4,
    }) == []


def test_paper_library_title_backfill_preserves_real_titles(storage):
    client = storage.client
    for index, title in enumerate(('', 'arXiv:2608.10001')):
        client.command('paper.library.put', {
            'id': f'paper-title-{index}', 'user_id': index + 1,
            'title': title, 'paper_hash': 'title-backfill-contract',
            'created_at': 1000 + index, 'updated_at': 1000 + index,
        }, f'paper-title-seed-{index}')
    assert client.command('paper.library.title.backfill', {
        'user_id': 1, 'paper_hash': 'title-backfill-contract',
        'title': 'Recovered title',
    }, 'paper-title-backfill') == {'title': 'Recovered title', 'updated': 1}
    assert client.query('paper.library.identity', {
        'user_id': 1, 'paper_hash': 'title-backfill-contract',
    })['title'] == 'Recovered title'
    assert client.query('paper.library.identity', {
        'user_id': 2, 'paper_hash': 'title-backfill-contract',
    })['title'] == 'arXiv:2608.10001'

    client.command('paper.library.put', {
        'id': 'paper-title-custom', 'user_id': 1,
        'title': 'My reading notes',
        'paper_hash': 'title-backfill-contract',
        'created_at': 4_000_000_000, 'updated_at': 4_000_000_000,
    }, 'paper-title-custom')
    assert client.command('paper.library.title.backfill', {
        'user_id': 1, 'paper_hash': 'title-backfill-contract',
        'title': 'Wrong replacement',
    }, 'paper-title-backfill-again') == {
        'title': 'My reading notes', 'updated': 0,
    }


def test_daily_cost_cache_semantics(storage):
    client = storage.client
    for day, cost in (('2026-08-12', 1.25), ('2026-08-13', 2.5)):
        assert client.command('daily_cost.upsert', {
            'user_id': 1, 'date': day, 'cost': cost,
            'conversations': {'conv-1': {'cost': cost, 'tokens': 10}},
            'computed_at': 1000,
        }, f'daily-cost-{day}') == {'saved': True}
    month = client.query('daily_cost.month', {
        'user_id': 1, 'year': 2026, 'month': 8,
    })
    assert [row['date'] for row in month] == ['2026-08-12', '2026-08-13']
    assert month[-1]['conversations']['conv-1']['cost'] == 2.5
    assert client.query('daily_cost.latest', {'user_id': 1})['date'] == '2026-08-13'
    assert client.query('daily_cost.persisted_dates', {
        'user_id': 1, 'dates': ['2026-08-11', '2026-08-13'],
    }) == {'dates': ['2026-08-13']}
    assert client.command('daily_cost.delete', {
        'user_id': 1, 'date': '2026-08-12',
    }, 'daily-cost-delete-one') == {'deleted': 1}
    assert client.command('daily_cost.delete', {
        'user_id': 1,
    }, 'daily-cost-delete-all') == {'deleted': 1}
    assert client.query('daily_cost.latest', {'user_id': 1}) is None


def test_paper_podcast_semantics(storage):
    client = storage.client
    key = {
        'user_id': 1, 'paper_hash': 'podcast-contract', 'mode': 'short',
        'lang': 'zh', 'voice': 'alloy',
    }
    assert client.query('paper.podcast.get', key) is None
    assert client.command('paper.podcast.upsert', {
        **key, 'status': 'generating', 'script': {},
        'meta': {'task_id': 'pod-1'}, 'duration_sec': 0,
        'created_at': 1000, 'updated_at': 1000,
    }, 'podcast-generating') == {'saved': True}
    assert client.command('paper.podcast.mark_interrupted', {
        'updated_at': 2000,
    }, 'podcast-interrupt') == {'changed': 1}
    assert client.query('paper.podcast.get', key)['status'] == 'interrupted'
    assert client.command('paper.podcast.upsert', {
        **key, 'status': 'done',
        'script': {'segments': [{'text': 'hello'}]},
        'meta': {'source_kind': 'report_zh'}, 'file_path': 'paper.wav',
        'duration_sec': 3.5, 'model': 'writer', 'tts_model': 'voice',
        'created_at': 1000, 'updated_at': 3000,
    }, 'podcast-done') == {'saved': True}
    row = client.query('paper.podcast.get', key)
    assert row['status'] == 'done'
    assert row['script_json']['segments'][0]['text'] == 'hello'
    assert row['meta'] == {'source_kind': 'report_zh'}
    assert row['duration_sec'] == 3.5


def test_artifact_semantics(storage):
    client = storage.client
    base = {
        'conv_id': 'artifact-contract', 'task_id': 'task-a',
        'msg_id': 'message-a', 'source': 'write_file',
        'format': 'markdown', 'title': 'report.md',
        'source_ref': {'path': 'report.md'}, 'meta': {'words': 2},
    }
    first = client.command('artifact.create', {
        **base, 'artifact_id': 'artifact-v1', 'content': '# first\n',
        'created_at': 100,
    }, 'artifact-create-v1')
    assert first['created'] is True
    assert first['artifact']['version'] == 1
    assert first['artifact']['parent_id'] == ''
    assert first['artifact']['meta'] == {'words': 2}

    duplicate = client.command('artifact.create', {
        **base, 'artifact_id': 'artifact-duplicate', 'content': '# first\n',
        'created_at': 101,
    }, 'artifact-create-duplicate')
    assert duplicate == {'created': False, 'artifact': first['artifact']}

    second = client.command('artifact.create', {
        **base, 'artifact_id': 'artifact-v2', 'content': '# second\n',
        'created_at': 102,
    }, 'artifact-create-v2')
    assert second['artifact']['version'] == 2
    assert second['artifact']['parent_id'] == 'artifact-v1'
    full = client.query('artifact.get', {
        'artifact_id': 'artifact-v2', 'include_content': True,
    })
    assert full['content'] == '# second\n'
    assert [row['id'] for row in client.query(
        'artifact.versions', {'artifact_id': 'artifact-v2'})
    ] == ['artifact-v1', 'artifact-v2']

    assert client.command('artifact.pin', {
        'artifact_id': 'artifact-v1', 'pinned': True,
    }, 'artifact-pin-v1') == {'changed': True}
    library = client.query('artifact.library', {'limit': 20})
    assert [row['id'] for row in library[:2]] == [
        'artifact-v1', 'artifact-v2']
    assert client.command('artifact.delete', {
        'artifact_id': 'artifact-v2', 'deleted_at': 200,
    }, 'artifact-delete-v2') == {'deleted': True}
    assert client.query('artifact.get', {
        'artifact_id': 'artifact-v2', 'include_content': False,
    }) is None
    listed = client.query('artifact.list', {
        'conv_id': 'artifact-contract', 'include_deleted': True,
    })
    assert {row['id'] for row in listed} == {'artifact-v1', 'artifact-v2'}


def test_artifact_concurrent_dedupe_is_atomic(storage):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(8)

    def create(index):
        barrier.wait()
        return storage.client.command('artifact.create', {
            'artifact_id': f'artifact-race-{index}',
            'conv_id': 'artifact-race', 'source': 'inline_doc',
            'source_ref': {}, 'format': 'html', 'title': 'race.html',
            'content': '<h1>same</h1>', 'meta': {},
            'created_at': 100 + index,
        }, f'artifact-race-command-{index}')

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(8)))
    assert sum(result['created'] for result in results) == 1
    assert len({result['artifact']['id'] for result in results}) == 1
    assert len(storage.client.query('artifact.list', {
        'conv_id': 'artifact-race', 'include_deleted': False,
    })) == 1


def test_tenant_user_semantics(storage):
    client = storage.client
    payload = {
        'user_id': 'tenant-user-1', 'email': 'owner@example.com',
        'password_hash': 'pbkdf2$salt$digest', 'display_name': 'Owner',
        'role': 'user', 'metadata': {'oidc_sub': 'subject-1'},
        'created_at': 100,
    }
    created = client.command(
        'tenant.user.create', payload, 'tenant-user-create-1')
    assert created == client.command(
        'tenant.user.create', payload, 'tenant-user-create-1')
    assert created['email'] == 'owner@example.com'
    assert created['owner_user_id'] == 2
    assert created['metadata'] == {'oidc_sub': 'subject-1'}
    assert 'password_hash' not in created
    assert client.query('tenant.user.get', {
        'email': 'OWNER@example.com',
    })['id'] == 'tenant-user-1'

    with pytest.raises(StorageError) as duplicate:
        client.command('tenant.user.create', {
            **payload, 'user_id': 'tenant-user-2',
        }, 'tenant-user-create-duplicate')
    assert duplicate.value.code == 'database_conflict'

    auth = client.query(
        'tenant.user.authentication', {'email': 'owner@example.com'})
    assert auth['password_hash'] == 'pbkdf2$salt$digest'
    assert auth['user']['id'] == 'tenant-user-1'
    updated = client.command('tenant.user.set_role', {
        'user_id': 'tenant-user-1', 'role': 'admin',
    }, 'tenant-user-role-1')
    assert updated['role'] == 'admin'
    updated = client.command('tenant.user.set_status', {
        'user_id': 'tenant-user-1', 'status': 'suspended',
    }, 'tenant-user-status-1')
    assert updated['status'] == 'suspended'
    assert client.command('tenant.user.record_login', {
        'user_id': 'tenant-user-1', 'last_login_at': 200,
    }, 'tenant-user-login-1') == {'updated': True}
    listed = client.query('tenant.user.list', {
        'limit': 10, 'offset': 0, 'status': 'suspended',
    })
    assert [row['id'] for row in listed] == ['tenant-user-1']
    assert listed[0]['last_login_at'] == 200


def test_credential_authority_is_owner_scoped_and_account_aware(storage):
    client = storage.client
    first_user = client.command('tenant.user.create', {
        'user_id': 'account-a', 'email': 'a@example.com',
        'password_hash': '', 'display_name': 'A', 'role': 'user',
        'metadata': {}, 'created_at': 100,
    }, 'credential-account-a')
    second_user = client.command('tenant.user.create', {
        'user_id': 'account-b', 'email': 'b@example.com',
        'password_hash': '', 'display_name': 'B', 'role': 'user',
        'metadata': {}, 'created_at': 101,
    }, 'credential-account-b')
    assert (first_user['owner_user_id'], second_user['owner_user_id']) == (2, 3)

    credential = {
        'credential_id': 'key-a',
        'owner_user_id': first_user['owner_user_id'],
        'account_user_id': first_user['id'],
        'tenant_id': '',
        'name': 'A session',
        'prefix': 'tofu_live_abc123',
        'secret_hash': 'a' * 64,
        'scopes': ['chat'],
        'rate_limit_rpm': 60,
        'rate_limit_tpd': 0,
        'created_at': 100.0,
        'expires_at': None,
        'metadata': {'origin': 'test'},
    }
    created = client.command(
        'credential.create', credential, 'credential-create-a')
    assert created['owner_user_id'] == 2
    assert created['account_user_id'] == 'account-a'
    assert 'secret_hash' not in created
    assert client.query('credential.get', {
        'credential_id': 'key-a', 'owner_user_id': 3, 'tenant_id': '',
    }) is None

    authenticated = client.command('credential.authenticate', {
        'secret_hash': 'a' * 64, 'now': 200.0,
    }, None)
    assert authenticated['owner_user_id'] == 2
    assert authenticated['last_used_at'] == 200.0

    client.command('tenant.user.set_status', {
        'user_id': first_user['id'], 'status': 'suspended',
    }, 'credential-account-suspend-a')
    assert client.command('credential.authenticate', {
        'secret_hash': 'a' * 64, 'now': 201.0,
    }, None) is None
    identified = client.query(
        'credential.identify', {'secret_hash': 'a' * 64})
    assert identified['owner_user_id'] == 2
    assert identified['revoked_at'] is None
    assert client.command('credential.revoke', {
        'credential_id': 'key-a', 'owner_user_id': 2,
        'tenant_id': '', 'revoked_at': 202.0,
    }, 'credential-revoke-a')['revoked'] is True
    assert client.query('credential.get', {
        'credential_id': 'key-a', 'owner_user_id': 2, 'tenant_id': '',
    }) is None
    assert client.query(
        'credential.identify', {'secret_hash': 'a' * 64}
    )['revoked_at'] == 202.0

    with pytest.raises(StorageError) as mismatch:
        client.command('credential.create', {
            **credential,
            'credential_id': 'key-cross-owner',
            'secret_hash': 'b' * 64,
            'owner_user_id': second_user['owner_user_id'],
        }, 'credential-cross-owner')
    assert mismatch.value.code == 'database_conflict'


def test_optimizer_aggregate_semantics(storage):
    client = storage.client
    assert client.command('optimizer.proposal.create', {
        'user_id': 7,
        'proposal_id': 'opt-contract', 'created_at': '2026-08-14T10:00:00',
        'title': 'Bound writer queue', 'rationale': 'protect memory',
        'action_type': 'set_limit', 'action_args': '{"limit":200}',
        'severity': 'high', 'confidence': 0.9, 'evidence': '["metric"]',
        'status': 'pending_review', 'status_reason': '',
    }, 'optimizer-proposal-contract') == {'proposal_id': 'opt-contract'}
    proposal = client.query(
        'optimizer.proposal.get', {
            'user_id': 7, 'proposal_id': 'opt-contract'})
    assert proposal['title'] == 'Bound writer queue'
    assert proposal['confidence'] == 0.9
    assert len(client.query('optimizer.proposal.list', {
        'user_id': 7, 'status': 'pending_review', 'limit': 10,
    })) == 1
    client.command('optimizer.proposal.update', {
        'user_id': 7,
        'proposal_id': 'opt-contract', 'status': 'applied', 'reason': 'test',
    }, 'optimizer-proposal-applied')
    client.command('optimizer.action.record', {
        'user_id': 7,
        'log_id': 'act-contract', 'proposal_id': 'opt-contract',
        'applied_at': '2026-08-14T10:01:00',
        'expires_at': '2026-08-15T10:01:00', 'pre_metric': '{}',
    }, 'optimizer-action-contract')
    client.command('optimizer.action.outcome', {
        'user_id': 7,
        'log_id': 'act-contract', 'outcome_metric': '{"ok":true}',
        'recorded_at': '2026-08-14T11:00:00',
    }, 'optimizer-outcome-contract')
    expired = client.query('optimizer.action.expired', {
        'user_id': 7, 'now_iso': '2026-08-16T00:00:00',
    })
    assert [row['id'] for row in expired] == ['act-contract']
    assert expired[0]['p_status'] == 'applied'
    assert client.query('optimizer.action.for_proposal', {
        'user_id': 7, 'proposal_id': 'opt-contract',
    })['outcome_metric'] == '{"ok":true}'
    client.command('optimizer.action.revert', {
        'user_id': 7,
        'log_id': 'act-contract', 'reverted_at': '2026-08-16T00:00:01',
        'reason': 'expired',
    }, 'optimizer-revert-contract')
    assert client.query('optimizer.action.list', {
        'user_id': 7, 'include_reverted': False, 'limit': 10,
    }) == []
    assert len(client.query('optimizer.action.list', {
        'user_id': 7, 'include_reverted': True, 'limit': 10,
    })) == 1


def test_optimizer_operations_fail_closed_across_owners(storage):
    client = storage.client
    proposal = {
        'proposal_id': 'same-opaque-id',
        'created_at': '2026-08-14T10:00:00',
        'title': 'Owner seven',
        'rationale': 'isolation',
        'action_type': 'set_limit',
        'action_args': '{}',
        'severity': 'low',
        'confidence': 0.5,
        'evidence': '[]',
        'status': 'pending_review',
        'status_reason': '',
    }
    client.command(
        'optimizer.proposal.create', {'user_id': 7, **proposal},
        'optimizer-owner-7-create')

    assert client.query('optimizer.proposal.get', {
        'user_id': 8, 'proposal_id': 'same-opaque-id'}) is None
    assert client.query('optimizer.proposal.list', {
        'user_id': 8, 'status': '', 'limit': 10}) == []
    assert client.command('optimizer.proposal.update', {
        'user_id': 8, 'proposal_id': 'same-opaque-id',
        'status': 'rejected', 'reason': 'foreign',
    }, 'optimizer-owner-8-update')['changed'] is False
    with pytest.raises(StorageError) as missing_proposal:
        client.command('optimizer.action.record', {
            'user_id': 8, 'log_id': 'foreign-action',
            'proposal_id': 'same-opaque-id',
            'applied_at': '2026-08-14T10:01:00',
            'expires_at': '2026-08-15T10:01:00',
            'pre_metric': '{}',
        }, 'optimizer-owner-8-foreign-action')
    assert missing_proposal.value.code == 'database_integrity'

    # Composite durable identities allow the same opaque id for another owner
    # without creating a lookup or mutation channel between them.
    client.command(
        'optimizer.proposal.create', {
            'user_id': 8,
            **{**proposal, 'title': 'Owner eight'},
        },
        'optimizer-owner-8-create',
    )
    assert client.query('optimizer.proposal.get', {
        'user_id': 7, 'proposal_id': 'same-opaque-id'})['title'] == 'Owner seven'
    assert client.query('optimizer.proposal.get', {
        'user_id': 8, 'proposal_id': 'same-opaque-id'})['title'] == 'Owner eight'

    client.command('optimizer.proposal.update', {
        'user_id': 7, 'proposal_id': 'same-opaque-id',
        'status': 'applied', 'reason': 'owner seven',
    }, 'optimizer-owner-7-apply')
    client.command('optimizer.action.record', {
        'user_id': 7, 'log_id': 'owner-seven-action',
        'proposal_id': 'same-opaque-id',
        'applied_at': '2026-08-14T10:01:00',
        'expires_at': '2026-08-15T10:01:00',
        'pre_metric': '{}',
    }, 'optimizer-owner-7-action')
    assert client.command('optimizer.action.outcome', {
        'user_id': 8, 'log_id': 'owner-seven-action',
        'outcome_metric': '{"foreign":true}',
        'recorded_at': '2026-08-14T11:00:00',
    }, 'optimizer-owner-8-outcome')['changed'] is False
    assert client.command('optimizer.action.revert', {
        'user_id': 8, 'log_id': 'owner-seven-action',
        'reverted_at': '2026-08-16T00:00:01', 'reason': 'foreign',
    }, 'optimizer-owner-8-revert')['changed'] is False
    assert client.query('optimizer.action.for_proposal', {
        'user_id': 8, 'proposal_id': 'same-opaque-id'}) is None
    assert client.query('optimizer.action.list', {
        'user_id': 8, 'include_reverted': True, 'limit': 10}) == []
    assert client.query('optimizer.action.expired', {
        'user_id': 8, 'now_iso': '2026-08-16T00:00:00'}) == []
    owner_seven_action = client.query('optimizer.action.for_proposal', {
        'user_id': 7, 'proposal_id': 'same-opaque-id'})
    assert owner_seven_action['outcome_metric'] == ''
    assert owner_seven_action['reverted_at'] == ''

    for bad_user_id in (None, 0):
        payload = {'proposal_id': 'same-opaque-id'}
        if bad_user_id is not None:
            payload['user_id'] = bad_user_id
        with pytest.raises(StorageError) as invalid_owner:
            client.query('optimizer.proposal.get', payload)
        assert invalid_owner.value.code == 'database_protocol_error'


def test_log_aggregate_batch_query_and_sweep(storage):
    client = storage.client
    rows = [
        {'fingerprint': 'fp-active', 'level': 'ERROR', 'logger': 'test',
         'template': 'capacity reached 95%', 'sample': 'sample', 'count': 3,
         'first_seen': 100, 'last_seen': 300},
        {'fingerprint': 'fp-stale', 'level': 'WARNING', 'logger': 'test',
         'template': 'stale event', 'sample': 'old', 'count': 1,
         'first_seen': 100, 'last_seen': 100},
    ]
    assert client.command(
        'log_aggregate.flush', {'rows': rows}, None,
        priority='event') == {'flushed': 2, 'swept': 0}
    client.command('log_aggregate.flush', {'rows': [{
        **rows[0], 'count': 2, 'last_seen': 400,
    }]}, None, priority='event')
    queried = client.query('log_aggregate.query', {
        'level': 'ERROR', 'sort': 'count', 'limit': 10, 'q': '95%',
    })
    assert queried['total_rows'] == 1
    assert queried['total_events'] == 5
    assert queried['items'][0]['count'] == 5
    swept = client.command(
        'log_aggregate.flush', {'rows': [], 'cutoff_ms': 200}, None,
        priority='maintenance')
    assert swept == {'flushed': 0, 'swept': 1}
    assert client.query('log_aggregate.query', {
        'level': '', 'sort': 'last_seen', 'limit': 10, 'q': '',
    })['total_rows'] == 1


def test_plugin_manifest_and_named_operations(storage):
    manifest = {
        'namespace': 'example.notes',
        'version': 1,
        'tables': [{
            'name': 'notes',
            'columns': [
                {'name': 'id', 'type': 'string', 'required': True},
                {'name': 'title', 'type': 'string', 'required': True},
                {'name': 'done', 'type': 'boolean'},
            ],
            'primary_key': ['id'],
            'indexes': [{'name': 'by_done', 'columns': ['done']}],
        }],
        'operations': [
            {'name': 'get_note', 'kind': 'query', 'action': 'get', 'table': 'notes'},
            {'name': 'list_notes', 'kind': 'query', 'action': 'list', 'table': 'notes'},
            {'name': 'put_note', 'kind': 'command', 'action': 'put', 'table': 'notes'},
            {'name': 'delete_note', 'kind': 'command', 'action': 'delete', 'table': 'notes'},
        ],
    }
    assert storage.client.command(
        'plugin.register', {'manifest': manifest}, 'register-notes') == {
            'namespace': 'example.notes', 'version': 1,
        }
    put = storage.client.command(
        'plugin.example.notes.put_note',
        {'document': {'id': 'n1', 'title': 'First', 'done': False}},
        'put-note-1',
    )
    assert put['version'] == 1
    assert storage.client.query(
        'plugin.example.notes.get_note', {'id': 'n1'})['document']['title'] == 'First'
    listed = storage.client.query(
        'plugin.example.notes.list_notes', {'filters': {'done': False}})
    assert [row['document']['id'] for row in listed] == ['n1']


def test_plugin_incompatible_manifest_fails_closed(storage):
    with pytest.raises(StorageError) as raised:
        storage.client.command(
            'plugin.register',
            {'manifest': {
                'namespace': 'example.bad', 'version': 1,
                'tables': [{'name': 'x', 'columns': [], 'primary_key': ['id']}],
                'operations': [],
            }},
            'register-bad',
        )
    assert raised.value.code == 'plugin_storage_incompatible'


def test_wrong_token_and_unknown_operation_are_protocol_errors(storage):
    bad = StorageClient(
        storage.client.endpoint[0], storage.client.endpoint[1], 'x' * 48)
    with pytest.raises(StorageError) as auth_error:
        bad.health()
    assert auth_error.value.code == 'database_protocol_error'
    with pytest.raises(StorageError) as operation_error:
        storage.client.query('arbitrary.sql', {'sql': 'DROP TABLE users'})
    assert operation_error.value.code == 'database_protocol_error'


def test_malformed_semantic_inputs_are_classified_consistently(storage):
    with pytest.raises(StorageError) as bad_limit:
        storage.client.query(
            'record.list', {'namespace': 'contract', 'limit': 'many'})
    assert bad_limit.value.code == 'database_protocol_error'

    with pytest.raises(StorageError) as bad_version:
        storage.client.command(
            'record.put',
            {'namespace': 'contract', 'key': 'bad-version', 'value': 1,
             'expected_version': 'zero'},
            'bad-version',
        )
    assert bad_version.value.code == 'database_protocol_error'

    with pytest.raises(StorageError) as bad_priority:
        storage.client.command(
            'event.append',
            {'task_id': 'task-priority', 'sequence': 1, 'event': {}},
            None,
            priority='urgent',
        )
    assert bad_priority.value.code == 'database_protocol_error'

    with pytest.raises(StorageError) as bad_command_id:
        storage.client.command(
            'record.put',
            {'namespace': 'contract', 'key': 'bad-id', 'value': 1},
            ['not', 'text'],  # type: ignore[arg-type]
        )
    assert bad_command_id.value.code == 'database_protocol_error'


def test_backup_is_verified_and_kept_inside_project(storage, tmp_path):
    storage.client.command(
        'record.put', {'namespace': 'backup', 'key': 'kept', 'value': 1},
        'backup-seed')
    result = storage.client.maintenance('system.backup', deadline=30)
    target = (tmp_path / result['backup']).resolve()
    assert result['ok'] is True
    assert target.exists()
    target.relative_to((tmp_path / 'data' / 'backups').resolve())
    if target.is_file():
        assert target.stat().st_size == result['bytes']
    else:
        assert result['bytes'] > 0


def test_sidecar_crash_revokes_readiness_without_backend_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    crashed = threading.Event()
    codes = []
    supervisor = StorageSupervisor(
        project_root=tmp_path,
        backend='sqlite',
        startup_timeout=60,
        on_crash=lambda code: (codes.append(code), crashed.set()),
    )
    supervisor.start()
    ready_status = supervisor.status()
    assert ready_status['ready'] is True
    assert ready_status['state'] == 'ready'
    assert ready_status['backend'] == 'sqlite'
    assert isinstance(ready_status['pid'], int)
    process = supervisor._process
    assert process is not None
    process.kill()
    assert crashed.wait(5)
    assert supervisor.ready is False
    crashed_status = supervisor.status()
    assert crashed_status['state'] == 'exited'
    assert crashed_status['last_exit_code'] is not None
    assert codes
    # No fallback client/backend is synthesized after the crash.
    with pytest.raises(RuntimeError, match='not ready'):
        _ = supervisor.client


def test_runtime_fences_then_restarts_and_rehandshakes_same_backend(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    fenced = threading.Event()
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    runtime = StorageRuntime(supervisor, on_write_fence=fenced.set)
    try:
        runtime.start().command(
            'record.put',
            {'namespace': 'restart', 'key': 'durable', 'value': True},
            'restart-seed',
        )
        process = supervisor._process
        assert process is not None
        process.kill()
        assert fenced.wait(5)
        deadline = time.monotonic() + 15
        while not runtime.ready and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runtime.ready is True
        status = runtime.status()
        assert status['state'] == 'ready'
        assert status['restart_attempts'] >= 1
        assert status['last_exit_code'] is not None
        assert runtime.client().health()['backend'] == 'sqlite'
        assert runtime.client().query(
            'record.get', {'namespace': 'restart', 'key': 'durable'})['value'] is True
    finally:
        runtime.stop()


@pytest.mark.skipif(
    os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1',
    reason='real PostgreSQL contract is opt-in',
)
def test_postgres_kill_reconnects_external_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '1')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '1')
    fenced = threading.Event()
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='postgres', startup_timeout=60)
    runtime = StorageRuntime(supervisor, on_write_fence=fenced.set)
    try:
        runtime.start().command(
            'record.put',
            {'namespace': 'pg-restart', 'key': 'durable', 'value': True},
            'pg-restart-seed',
        )
        process = supervisor._process
        assert process is not None
        process.kill()
        assert fenced.wait(10)
        deadline = time.monotonic() + 60
        while not runtime.ready and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runtime.ready
        assert runtime.client().query(
            'record.get',
            {'namespace': 'pg-restart', 'key': 'durable'},
        )['value'] is True
    finally:
        runtime.stop()
    assert not (tmp_path / 'data' / 'pgdata').exists()


def test_private_test_backend_selector_is_strict_and_personal_defaults_sqlite(
        tmp_path, monkeypatch):
    from lib.storage_sidecar import config as sidecar_config

    SidecarConfig = sidecar_config.SidecarConfig

    monkeypatch.setenv('TOFU_STORAGE_TOKEN', 't' * 48)
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.delenv('TOFU_STORAGE_TEST_BACKEND', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_SQLITE_READ_POOL', raising=False)
    monkeypatch.delenv(
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB', raising=False)
    monkeypatch.delenv(
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB', raising=False)
    monkeypatch.delenv(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_IDLE_TRIM_RSS_MIB', raising=False)
    monkeypatch.delenv(
        'TOFU_STORAGE_IDLE_TRIM_COOLDOWN_SECONDS', raising=False)
    monkeypatch.delenv('TOFU_TURN_SEARCH_PROJECTION_MAX_MIB', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_RPC_CAPACITY', raising=False)
    monkeypatch.delenv(
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', raising=False)
    expected = {
        'TOFU_STORAGE_SQLITE_READ_POOL': 8,
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': 16,
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': 64,
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': 32,
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': 1024,
        'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB': 512,
        'TOFU_STORAGE_RPC_CAPACITY': 8,
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': 128,
        'TOFU_LOG_TOTAL_BUDGET_MB': 128,
    }
    monkeypatch.setattr(
        sidecar_config, 'deployment_resource_default',
        lambda name, _environment: expected[name])
    config = SidecarConfig.from_environment()
    assert config.backend == 'sqlite'
    assert config.read_pool_size == 8
    assert config.sqlite_writer_queue_capacity == 16
    assert config.sqlite_writer_cache_mib == 64
    assert config.turn_projection_cache_mib == 32
    assert config.fastpath_wal_rebase_max_mib == 1024
    assert config.idle_trim_rss_mib == 384
    assert config.idle_trim_cooldown_s == 60.0
    assert config.turn_search_projection_max_mib == 512
    assert config.turn_search_projection_dir == (
        tmp_path / 'data' / 'projections').resolve()
    assert config.rpc_capacity == 8
    assert config.rpc_inflight_max_mib == 128
    monkeypatch.setenv(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', '999999')
    with pytest.raises(RuntimeError, match='must be between 64 and 16384'):
        SidecarConfig.from_environment()
    monkeypatch.setenv(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', '16384')
    assert SidecarConfig.from_environment().fastpath_wal_rebase_max_mib \
        == 16_384
    monkeypatch.delenv(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', raising=False)
    monkeypatch.setenv('TOFU_STORAGE_IDLE_TRIM_RSS_MIB', '768')
    monkeypatch.setenv('TOFU_STORAGE_IDLE_TRIM_COOLDOWN_SECONDS', '90')
    overridden_trim = SidecarConfig.from_environment()
    assert overridden_trim.idle_trim_rss_mib == 768
    assert overridden_trim.idle_trim_cooldown_s == 90.0
    monkeypatch.setenv('TOFU_STORAGE_RPC_CAPACITY', '12')
    assert SidecarConfig.from_environment().rpc_capacity == 12
    monkeypatch.setenv('TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', '256')
    assert SidecarConfig.from_environment().rpc_inflight_max_mib == 256
    monkeypatch.setenv('TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', '64')
    with pytest.raises(RuntimeError, match='must be between 128 and 8192'):
        SidecarConfig.from_environment()
    monkeypatch.setenv('TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', '256')
    monkeypatch.setenv('TOFU_STORAGE_WRITER_STALL_GRACE_S', '60')
    monkeypatch.setenv('TOFU_STORAGE_WRITER_HARD_KILL_S', '60')
    with pytest.raises(RuntimeError, match='must be greater'):
        SidecarConfig.from_environment()
    monkeypatch.delenv('TOFU_STORAGE_WRITER_STALL_GRACE_S')
    monkeypatch.delenv('TOFU_STORAGE_WRITER_HARD_KILL_S')
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY', '3')
    with pytest.raises(RuntimeError, match='must be between 4 and 1024'):
        SidecarConfig.from_environment()
    monkeypatch.delenv('TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY')
    monkeypatch.setenv('TOFU_STORAGE_TEST_BACKEND', 'pg')
    with pytest.raises(RuntimeError, match='must be sqlite or postgres'):
        SidecarConfig.from_environment()


def test_idle_heap_trim_cooldown_preserves_deployment_profile_boundary():
    from lib.storage_sidecar.config import (
        _default_idle_trim_cooldown_seconds,
    )

    assert _default_idle_trim_cooldown_seconds('personal') == 60.0
    assert _default_idle_trim_cooldown_seconds('distributed') == 300.0


def test_task_results_cost_experiment_scan_projects_only_outcomes():
    """A/B report 500 (2026-08-20): the settings report scanned
    ``conversation.list(include_messages=True)`` — MiB-sized transcripts in
    one frame, event-loop-stalled, blind to turn-native conversations. The
    replacement scans the compact per-task outcome that the terminal persist
    already writes into ``task_results``. Pin: the op returns ONLY the tiny
    outcome projection (heavy content/thinking never cross the wire),
    applies the exact completed_at window, and fences rows to conversations
    the requesting user still owns.
    """
    from lib.storage_sidecar import operations

    now_ms = 1_800_000_000_000
    outcome = {
        'experiment_id': 'context_cost-v1', 'arm': 'optimized',
        'status': 'assigned', 'completedAt': now_ms,
        'metrics': {'costUsd': 0.1},
    }

    def _record(task_id, conv_id, completed_at, metadata, **extra):
        value = {
            'task_id': task_id, 'conv_id': conv_id,
            'content': 'x' * 100_000, 'thinking': 'y' * 100_000,
            'tool_rounds': 'z' * 100_000,
            'status': 'done', 'created_at': completed_at - 10,
            'completed_at': completed_at,
            'metadata': metadata,
        }
        value.update(extra)
        return {
            'record_key': task_id,
            'value_json': json.dumps(value),
            'updated_at_ms': completed_at,
        }

    rows = [
        _record('task-keep', 'conv-owned', now_ms,
                json.dumps({'costExperiment': outcome})),
        # Outside the exact window even though updated recently.
        _record('task-old', 'conv-owned', now_ms - 90 * 86_400_000,
                json.dumps({'costExperiment': outcome})),
        # No experiment payload.
        _record('task-plain', 'conv-owned', now_ms,
                json.dumps({'model': 'm1'})),
        # Malformed metadata is counted, never fatal.
        _record('task-broken', 'conv-owned', now_ms,
                '{broken context_cost-v1'),
        # Another user's conversation.
        _record('task-foreign', 'conv-foreign', now_ms,
                json.dumps({'costExperiment': outcome})),
        # Orphan recovery row (conversation deleted).
        _record('task-orphan', 'conv-gone', now_ms,
                json.dumps({'costExperiment': outcome})),
    ]
    queries = []
    query_params = []

    class _FakeSession:
        backend = 'sqlite'

        def fetch_all(self, sql, params=()):
            queries.append(sql)
            query_params.append(params)
            if 'FROM storage_records' in sql:
                return rows
            if 'FROM storage_conversations' in sql:
                return [{'id': 'conv-owned'}]
            raise AssertionError(f'unexpected query: {sql}')

    result = operations._task_results_cost_experiment_scan(
        _FakeSession(), {'user_id': 1,
                         'completed_at_gte': now_ms - 14 * 86_400_000,
                         'experiment_id': 'context_cost-v1',
                         'limit': 5000})
    assert result['scanned'] == len(rows)
    assert result['invalid'] == 1
    assert result['capped'] is False
    assert [item['task_id'] for item in result['records']] == ['task-keep']
    kept = result['records'][0]
    assert kept['conv_id'] == 'conv-owned'
    assert kept['completed_at'] == now_ms
    assert kept['outcome'] == outcome
    # The heavy payload never leaves the store.
    assert set(kept) == {'task_id', 'conv_id', 'completed_at', 'outcome'}
    # The first stage is a bounded primary-key walk with no BLOB JSON/LIKE
    # expression; owner and exact experiment identity are applied before the
    # returned record cap by the compact second-stage projection.
    assert 'LIMIT ?' in queries[0]
    assert 'record_key > ?' in queries[0]
    assert 'JOIN ' not in queries[0]
    assert 'json_extract' not in queries[0]
    assert ' LIKE ' not in queries[0]
    assert any('FROM storage_conversations' in query for query in queries[1:])
    assert query_params[0] == ('task_results', '', 8)

    rows.append(_record(
        'task-keep-2', 'conv-owned', now_ms,
        json.dumps({'costExperiment': outcome}),
    ))
    capped = operations._task_results_cost_experiment_scan(
        _FakeSession(), {'user_id': 1,
                         'completed_at_gte': now_ms - 14 * 86_400_000,
                         'experiment_id': 'context_cost-v1',
                         'limit': 1})
    assert capped['capped'] is True
    assert len(capped['records']) == 1


def test_cost_experiment_scan_filters_owner_id_window_before_real_cap(
        storage, tmp_path):
    """Exercise the semantic query against the real SQLite/optional PG adapter."""
    client = storage.client
    now_ms = int(time.time() * 1000)
    _import_conversation(client, 'exp-owned-a', user_id=1, updated_at=now_ms)
    _import_conversation(client, 'exp-owned-b', user_id=1, updated_at=now_ms)
    _import_conversation(client, 'exp-foreign', user_id=2, updated_at=now_ms)

    def checkpoint(task_id, conv_id, user_id, experiment_id, completed_at):
        outcome = {
            'experimentId': experiment_id,
            'experiment_id': experiment_id,
            'status': 'assigned',
            'arm': 'control',
            'completedAt': completed_at,
        }
        client.command('task_results.checkpoint', {
            'key': task_id,
            'expected_version': 0,
            'value': {
                'task_id': task_id,
                'conv_id': conv_id,
                'user_id': user_id,
                'status': 'done',
                'content': 'heavy' * 10_000,
                'thinking': 'heavy' * 10_000,
                'created_at': completed_at - 1,
                'completed_at': completed_at,
                'metadata': json.dumps({
                    'costExperiment': outcome,
                    'toolSummary': 'bounded scan summary ' * 10_000,
                }),
            },
        }, None)

    checkpoint('relevant-a', 'exp-owned-a', 1, 'target-v1', now_ms)
    checkpoint('relevant-b', 'exp-owned-b', 1, 'target-v1', now_ms - 1)
    # These are written later/coarsely recent but must not consume the cap.
    checkpoint('other-experiment', 'exp-owned-a', 1, 'other-v1', now_ms)
    checkpoint('foreign-owner', 'exp-foreign', 2, 'target-v1', now_ms)
    checkpoint('outside-window', 'exp-owned-a', 1, 'target-v1', now_ms - 10_000)

    projected = client.query(
        'record.get', {'namespace': 'task_results', 'key': 'relevant-a'})
    assert projected['value']['cost_experiment_id'] == 'target-v1'

    payload = {
        'user_id': 1,
        'experiment_id': 'target-v1',
        'completed_at_gte': now_ms - 1_000,
        'limit': 1,
    }
    capped = client.query('task_results.cost_experiment_scan', payload)
    assert capped['capped'] is True
    assert len(capped['records']) == 1
    assert capped['records'][0]['task_id'] in {'relevant-a', 'relevant-b'}

    complete = client.query(
        'task_results.cost_experiment_scan', {**payload, 'limit': 10})
    assert complete['capped'] is False
    assert {row['task_id'] for row in complete['records']} == {
        'relevant-a', 'relevant-b'}
    if storage.client.health()['backend'] == 'sqlite':
        from lib.storage_sidecar.task_result_field_codec import (
            TASK_RESULT_FIELD_CODEC_KEY,
        )
        connection = sqlite3.connect(tmp_path / 'data' / 'tofu.db')
        raw = connection.execute(
            "SELECT value_json FROM storage_records WHERE namespace=? "
            "AND record_key=?",
            ('task_results', 'relevant-a'),
        ).fetchone()[0]
        connection.close()
        assert TASK_RESULT_FIELD_CODEC_KEY in json.loads(raw)['metadata']


def test_cost_experiment_scan_returns_a_bounded_monotonic_cursor():
    """Legacy BLOB scans expose progress so a timeout never restarts at zero."""
    from lib.storage_sidecar import operations

    now_ms = 1_800_000_000_000
    outcome = {
        'experimentId': 'cursor-v1',
        'experiment_id': 'cursor-v1',
        'arm': 'control',
    }

    def row(key):
        return {
            'record_key': key,
            'updated_at_ms': now_ms,
            'value_json': json.dumps({
                'conv_id': 'owned-conv',
                'completed_at': now_ms,
                'metadata': json.dumps({'costExperiment': outcome}),
            }),
        }

    source = [row('task-a'), row('task-b'), row('task-c')]

    class _FakeSession:
        backend = 'sqlite'

        def fetch_all(self, sql, params=()):
            if 'FROM storage_records' in sql:
                after_key = params[1]
                limit = (1 if 'SELECT record_key FROM' in sql
                         else params[-1])
                return [item for item in source
                        if item['record_key'] > after_key][:limit]
            if 'FROM storage_conversations' in sql:
                return [{'id': 'owned-conv'}]
            raise AssertionError(f'unexpected query: {sql}')

    first = operations._task_results_cost_experiment_scan(
        _FakeSession(), {
            'user_id': 1,
            'completed_at_gte': now_ms - 1,
            'experiment_id': 'cursor-v1',
            'limit': 10,
            'scan_limit': 2,
            'after_key': '',
        })
    assert first['scanned'] == 2
    assert first['exhausted'] is False
    assert first['next_cursor'] == 'task-b'
    assert [item['task_id'] for item in first['records']] == [
        'task-b', 'task-a']

    second = operations._task_results_cost_experiment_scan(
        _FakeSession(), {
            'user_id': 1,
            'completed_at_gte': now_ms - 1,
            'experiment_id': 'cursor-v1',
            'limit': 10,
            'scan_limit': 2,
            'after_key': first['next_cursor'],
        })
    assert second['scanned'] == 1
    assert second['exhausted'] is True
    assert second['next_cursor'] == ''
    assert [item['task_id'] for item in second['records']] == ['task-c']


def test_turn_image_query_is_owner_revision_and_index_scoped(storage):
    import base64

    raw = b'\x89PNG\r\n\x1a\n' + (b'legacy-image' * 100)
    encoded = base64.b64encode(raw).decode('ascii')
    _import_conversation(
        storage.client,
        'turn-image-owner',
        user_id=41,
        messages=[{
            'role': 'assistant',
            'content': 'historical image',
            'images': [{
                'base64': encoded,
                'preview': f'data:image/png;base64,{encoded}',
                'mediaType': 'image/png',
            }],
        }],
    )
    turn = storage.client.query('turn.list', {
        'conversation_id': 'turn-image-owner',
        'user_id': 41,
    })[0]
    payload = {
        'conversation_id': 'turn-image-owner',
        'user_id': 41,
        'turn_id': turn['turnId'],
        'projection_revision': turn['projectionRevision'],
        'image_index': 0,
    }

    loaded = storage.client.query('turn.image.get', payload)

    assert loaded == {
        'stale': False,
        'projectionRevision': turn['projectionRevision'],
        'mediaType': 'image/png',
        'base64': encoded,
    }
    assert storage.client.query('turn.image.get', {
        **payload, 'user_id': 42,
    }) is None
    assert storage.client.query('turn.image.get', {
        **payload, 'image_index': 1,
    }) is None
    assert storage.client.query('turn.image.get', {
        **payload,
        'projection_revision': turn['projectionRevision'] + 1,
    }) == {
        'stale': True,
        'projectionRevision': turn['projectionRevision'],
    }
