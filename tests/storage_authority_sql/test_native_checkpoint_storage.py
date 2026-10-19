"""Real Rust journal -> private selected SQLite/PostgreSQL durable boundaries."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.native_checkpoint import NativeSqlCheckpoint, NativeSqlCheckpointError
from lib.storage_authority_sql.native_checkpoint_rows import HEADERS, SNAPSHOTS, RESERVED_NAMESPACES
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytestmark = pytest.mark.unit
BACKENDS = ['sqlite'] + (['postgres'] if os.environ.get('TOFU_STORAGE_TEST_POSTGRES') == '1' else [])
FORMAT = 'tofu.native-checkpoint-control/v1'


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path, monkeypatch):
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
    from tests.support.postgres_isolation import isolated_postgres_dsn
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    with ExitStack() as resources:
        if request.param == 'postgres':
            from lib.storage_authority_sql.adapters.postgres import PostgresBackend
            source = Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'])
            dsn = resources.enter_context(isolated_postgres_dsn(source, tmp_path))
            config = replace(config, backend='postgres', postgres_dsn=dsn.read_text().strip())
            value = PostgresBackend(config)
        else:
            value = SQLiteBackend(config)
        value.start()
        resources.callback(value.close)
        yield value


@pytest.fixture
def opened(backend):
    path, digest = (os.environ.get(name) for name in
                    ('TOFU_CHECKPOINT_SQL_TEST_LIBRARY', 'TOFU_CHECKPOINT_SQL_TEST_SHA256'))
    if not path or not digest:
        pytest.skip('explicit pinned checkpoint storage ABI library required')
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    with ExitStack() as resources:
        def create(**scope):
            value = NativeSqlCheckpoint(backend=backend, tenant_id=scope.get('tenant', 3),
                owner_user_id=scope.get('owner', 7), document_bytes=1 << 20, identity_bytes=1024,
                library=Path(path), sha256=digest, binary_bytes=Path(path).stat().st_size)
            resources.callback(value.close)
            return value
        yield create


def binding(task='task', owner=7, token='first'):
    return dict(owner_user_id=owner, task_id=task, epoch='epoch', binding='binding', token=token,
                header_bytes=4096, snapshot_bytes=262144, identity_bytes=1024)


def call(port, operation, capability=None, **payload):
    if capability is not None:
        payload['capability_id'] = capability
    return port.control(operation, {'format': FORMAT, **payload}, deadline_at=time.monotonic()+20,
                        response_bytes=524288)


def acquire(port, **kwargs):
    response = call(port, 'create', parameters=binding(**kwargs))
    assert response['accepted'], response
    return response['capability_id']


def snapshot(task='task', owner=7):
    return dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=owner,
        task_id=task, epoch='epoch', binding='binding', next_round=1, tool_calls=1,
        messages=[dict(role='user', content='中文 question'), dict(role='tool', content='result' * 1000)],
        usage=[{}], round_limit=3, tool_limit=3, no_progress_limit=0, failure_limit=0,
        deadline_unix_ms=(1 << 64)-1,
        progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='',
            last_evidence=None, last_failure=None, no_progress_streak=0, failure_streak=0))


def rows(backend):
    return backend.query('test.checkpoint_rows', lambda session: session.fetch_all(
        'SELECT namespace, record_key, version, value_json FROM storage_records '
        'WHERE namespace IN (?, ?) ORDER BY namespace, record_key', (HEADERS, SNAPSHOTS)), time.monotonic()+10)


def test_generation_is_current():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root/'scripts/gen_native_checkpoint_storage_abi.py'), '--check'],
                            cwd=root, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()


def test_real_journal_suspend_reopen_and_keep_never_reads_snapshot(opened, backend, monkeypatch):
    port = opened()
    capability = acquire(port)
    assert call(port, 'load', capability)['state'] is None
    assert call(port, 'begin', capability, round=0)['accepted']
    state = snapshot()
    assert call(port, 'complete', capability, state=state)['accepted']
    before = [row for row in rows(backend) if row['namespace'] == SNAPSHOTS]
    seen = []
    original = port._rows.dispatch
    def recording(request, deadline):
        seen.append(request['operation'])
        return original(request, deadline)
    monkeypatch.setattr(port._rows, 'dispatch', recording)
    assert call(port, 'finish', capability, status='aborted', suspended=True)['accepted']
    assert call(port, 'release', capability)['accepted']
    assert call(port, 'forget', capability)['accepted']
    assert 'snapshot' not in seen
    assert [row for row in rows(backend) if row['namespace'] == SNAPSHOTS] == before
    port.close()
    resumed = opened()
    new = acquire(resumed, token='next')
    assert call(resumed, 'load', new)['state'] == state
    assert call(resumed, 'begin', new, round=1)['accepted']
    assert call(resumed, 'seal', new)['accepted']


def test_owner_tenant_and_instance_capabilities_are_isolated(opened, backend):
    first = opened()
    cap = acquire(first)
    assert not call(first, 'create', parameters=binding(owner=8))['accepted']
    second = opened(owner=8)
    assert not call(second, 'load', cap)['accepted']
    acquire(second, owner=8)
    acquire(opened(tenant=4))
    assert len(rows(backend)) == 3
    assert len({row['record_key'] for row in rows(backend)}) == 3


def test_output_and_deadline_admission_precede_sql_effects(opened, backend):
    port = opened()
    payload = {'format': FORMAT, 'parameters': binding()}
    with pytest.raises(NativeSqlCheckpointError, match='buffer_small'):
        port.control('create', payload, deadline_at=time.monotonic()+10, response_bytes=1)
    with pytest.raises(NativeSqlCheckpointError, match='bounds invalid'):
        port.control('create', payload, deadline_at=time.monotonic()-1, response_bytes=524288)
    assert rows(backend) == []
    acquire(port)


def test_two_native_owners_compete_for_one_atomic_sql_lease(opened, backend):
    ports = [opened(), opened()]
    barrier = threading.Barrier(2)
    def attempt(index):
        barrier.wait(timeout=5)
        return call(ports[index], 'create', parameters=binding(token=f'writer-{index}'))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(result['accepted'] for result in results) == 1
    assert len(rows(backend)) == 1 and rows(backend)[0]['version'] == 1


def test_committed_but_unacknowledged_snapshot_poison_fences_next_round(opened, backend, monkeypatch):
    port = opened()
    cap = acquire(port)
    call(port, 'load', cap)
    assert call(port, 'begin', cap, round=0)['accepted']
    original = backend.command
    commits = []
    def lose_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == 'native_checkpoint.cas':
            commits.append(result)
            raise StorageError('database_unavailable', 'injected durable acknowledgement loss')
        return result
    monkeypatch.setattr(backend, 'command', lose_ack)
    with pytest.raises(StorageError, match='acknowledgement loss'):
        call(port, 'complete', cap, state=snapshot())
    assert len(commits) == 1
    monkeypatch.setattr(backend, 'command', original)
    response = call(port, 'begin', cap, round=1)
    assert not response['accepted'] and response['error'] == 'poisoned'
    assert len(rows(backend)) == 2
    assert all(row['version'] == 3 for row in rows(backend))


def test_close_withdraws_and_joins_real_callback_without_releasing_lease(opened, backend, monkeypatch):
    port = opened()
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    original = port._rows.dispatch
    def blocked(request, deadline):
        entered.set()
        assert release.wait(10)
        return original(request, deadline)
    monkeypatch.setattr(port._rows, 'dispatch', blocked)
    def close():
        closing.set()
        port.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(acquire, port)
        assert entered.wait(5)
        retired = executor.submit(close)
        assert closing.wait(5)
        try:
            with port._condition:
                assert port._condition.wait_for(lambda: port._state == 'closing', timeout=5)
            assert not retired.done()
            with pytest.raises(NativeSqlCheckpointError, match='closed'):
                call(port, 'create', parameters=binding(task='other'))
        finally:
            release.set()
        assert active.result(timeout=10) > 0
        retired.result(timeout=10)
    assert len(rows(backend)) == 1
    assert not call(opened(), 'create', parameters=binding(token='replacement'))['accepted']


def test_recursive_control_and_close_refuse_without_deadlock(opened, monkeypatch):
    port = opened()
    original = port._rows.dispatch
    observed = []
    def recursive(request, deadline):
        with pytest.raises(NativeSqlCheckpointError, match='reentrant'):
            call(port, 'create', parameters=binding(task='nested'))
        with pytest.raises(NativeSqlCheckpointError, match='callback'):
            port.close()
        observed.append(request['operation'])
        return original(request, deadline)
    monkeypatch.setattr(port._rows, 'dispatch', recursive)
    acquire(port)
    assert observed == ['header', 'cas']


def test_partial_sql_snapshot_write_rolls_back_header_and_poison_fences_lease(opened, backend, monkeypatch):
    port = opened()
    cap = acquire(port)
    call(port, 'load', cap)
    call(port, 'begin', cap, round=0)
    before = rows(backend)
    original = backend.command
    def fail_before_commit(*args, **kwargs):
        operation = args[4]
        def faulty(session):
            operation(session)
            raise StorageError('database_internal', 'injected transaction rollback')
        return original(*args[:4], faulty, *args[5:], **kwargs)
    monkeypatch.setattr(backend, 'command', fail_before_commit)
    with pytest.raises(StorageError, match='rollback'):
        call(port, 'complete', cap, state=snapshot())
    assert rows(backend) == before
    assert not call(port, 'begin', cap, round=1)['accepted']


def test_callback_deadline_joins_entered_sqlite_job_before_return(opened, backend, monkeypatch):
    if backend.name != 'sqlite':
        pytest.skip('PostgreSQL transactions execute synchronously on the caller')
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
    port = opened()
    entered, release = threading.Event(), threading.Event()
    original = SQLiteSession.execute
    def blocked(session, sql, params=()):
        result = original(session, sql, params)
        if sql.startswith('INSERT INTO storage_records') and params[0] == HEADERS:
            entered.set()
            assert release.wait(10)
        return result
    monkeypatch.setattr(SQLiteSession, 'execute', blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(port.control, 'create', {'format': FORMAT, 'parameters': binding()},
            deadline_at=time.monotonic()+0.5, response_bytes=524288)
        assert entered.wait(5)
        try:
            with pytest.raises(TimeoutError):
                active.result(timeout=0.6)
            closing = executor.submit(port.close)
            with pytest.raises(TimeoutError):
                closing.result(timeout=0.03)
        finally:
            release.set()
        with pytest.raises(StorageError, match='deadline expired'):
            active.result(timeout=10)
        closing.result(timeout=10)
    assert rows(backend) == []


@pytest.mark.parametrize('option', ['preview', 'outbox'])
def test_unsupported_authority_refuses_before_loading_code(backend, monkeypatch, option):
    from lib.native_binary import NativeConfigurationError
    from lib.storage_authority_sql import native_checkpoint
    backend.config = replace(backend.config, **({'distributed_preview_read_only': True}
                              if option == 'preview' else {'logical_shadow_mode': 'capture'}))
    monkeypatch.setattr(native_checkpoint, '_seal', lambda *args, **kwargs: pytest.fail('unsupported authority loaded code'))
    with pytest.raises(NativeConfigurationError, match='unsupported'):
        NativeSqlCheckpoint(backend=backend, tenant_id=3, owner_user_id=7,
            document_bytes=4096, identity_bytes=128, library=Path('/unopened.so'), sha256='0'*64, binary_bytes=4096)


def test_hash_key_collision_rechecks_exact_identity(opened, backend, monkeypatch):
    first = opened()
    acquire(first)
    second = opened(owner=8)
    monkeypatch.setattr(second._rows, 'key', lambda task: first._rows.key(task))
    with pytest.raises(StorageError, match='refused'):
        acquire(second, owner=8)
    assert len(rows(backend)) == 1 and rows(backend)[0]['version'] == 1


def test_snapshot_read_fences_exact_header_version_and_keep_retains_body(opened):
    port = opened()
    cap = acquire(port)
    call(port, 'load', cap)
    call(port, 'begin', cap, round=0)
    state = snapshot()
    call(port, 'complete', cap, state=state)
    request = dict(format=SPEC['callback_format'], operation='snapshot', tenant_id=3,
                   owner_user_id=7, task_id='task', header_bytes=4096, snapshot_bytes=262144,
                   expected_version=3)
    assert port._rows.dispatch(request, time.monotonic()+5)['value'] == state
    assert call(port, 'begin', cap, round=1)['accepted']
    assert port._rows.dispatch(request, time.monotonic()+5) == {'ok': False, 'error': 'conflict'}
    assert port._rows.dispatch({**request, 'expected_version': 4}, time.monotonic()+5)['value'] == state


def test_missing_source_handoff_refuses_without_effects(opened, backend):
    port = opened()
    response = call(port, 'handoff', parameters=binding(task='target'), resume=dict(
        conversation_id='conversation', turn_id='turn', source_attempt_id='source-attempt',
        target_attempt_id='target-attempt', source_task_id='source', reserved_micro=0))
    assert not response['accepted'] and response['error'] == 'ineligible'
    assert rows(backend) == []


def test_oversized_snapshot_refuses_without_replacing_durable_frontier(opened, backend):
    port = opened()
    cap = acquire(port)
    call(port, 'load', cap)
    call(port, 'begin', cap, round=0)
    before = rows(backend)
    state = snapshot()
    state['messages'][1]['content'] = 'x' * 262144
    result = call(port, 'complete', cap, state=state)
    assert not result['accepted'] and result['error'] == 'oversize'
    assert rows(backend) == before


def test_callback_allocation_failure_closes_the_verified_image(opened, monkeypatch):
    from lib.storage_authority_sql import native_checkpoint
    original = native_checkpoint.ffi
    before = set(os.listdir('/proc/self/fd'))
    def refuse(*args, **kwargs):
        raise RuntimeError('injected callback allocation failure')
    class CallbackFailure:
        callback = staticmethod(refuse)
        def __getattr__(self, name):
            return getattr(original, name)
    monkeypatch.setattr(native_checkpoint, 'ffi', CallbackFailure())
    try:
        with pytest.raises(RuntimeError, match='callback allocation'):
            opened()
    finally:
        monkeypatch.setattr(native_checkpoint, 'ffi', original)
    assert set(os.listdir('/proc/self/fd')) == before


def _process_checkpoint(config, image, digest, ready, pending_state=None):
    """Own real SQL/native resources in a fresh process, then await its owner."""
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
    from lib.storage_authority_sql.adapters.postgres import PostgresBackend
    backend = (SQLiteBackend if config.backend == 'sqlite' else PostgresBackend)(config)
    backend.start()
    port = NativeSqlCheckpoint(backend=backend, tenant_id=3, owner_user_id=7,
        document_bytes=1 << 20, identity_bytes=1024, library=Path(image), sha256=digest,
        binary_bytes=Path(image).stat().st_size)
    try:
        cap = acquire(port)
        call(port, 'load', cap)
        assert call(port, 'begin', cap, round=0)['accepted']
        operation = 'pending' if pending_state is not None else 'complete'
        assert call(port, operation, cap, state=pending_state if pending_state is not None else snapshot())['accepted']
        ready.send('durably-' + operation)
        ready.recv()
    finally:
        port.close()
        backend.close()
        ready.close()


def test_real_writer_sigkill_recovery_requires_death_and_loads_same_snapshot(opened, backend):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_process_checkpoint, args=(backend.config,
        os.environ['TOFU_CHECKPOINT_SQL_TEST_LIBRARY'], os.environ['TOFU_CHECKPOINT_SQL_TEST_SHA256'], child))
    process.start()
    child.close()
    port = opened()
    try:
        assert parent.poll(30), 'checkpoint writer did not publish durable frontier'
        assert parent.recv() == 'durably-complete'
        assert not call(port, 'recover', parameters=binding(token='resume'))['accepted']
        process.kill()
        process.join(10)
        assert process.exitcode == -9
        result = call(port, 'recover', parameters=binding(token='resume'))
        assert result['accepted'], result
        assert call(port, 'load', result['capability_id'])['state'] == snapshot()
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        assert not process.is_alive()
        process.close()
        parent.close()


@pytest.mark.parametrize('namespace', sorted(RESERVED_NAMESPACES))
@pytest.mark.parametrize('operation', ['get', 'list', 'put', 'delete'])
def test_generic_records_cannot_access_native_sql_journal(backend, namespace, operation):
    from lib.storage_authority_sql.operations_pkg import _records
    handler = getattr(_records, '_record_' + operation)
    def apply(session):
        return handler(session, {'namespace': namespace, 'key': 'task', 'value': {}})
    with pytest.raises(StorageError, match='Reserved'):
        backend.command('test.reserved', '', None, 'user', apply, time.monotonic()+10, receipt_required=False)
    assert rows(backend) == []
