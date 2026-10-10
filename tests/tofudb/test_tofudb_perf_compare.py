"""Offline lifecycle contracts for the three-authority comparison harness."""
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from scripts.tofudb_perf_compare import LegacyEngine

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('transport', ['application', 'embedded'])
def test_read_driver_omits_unused_command_identity_and_retries_commands_with_one_id(monkeypatch, transport):
    import scripts.tofudb_perf_compare as bench
    from lib.storage.errors import StorageError

    allocated = []
    delivered = []
    monkeypatch.setattr(bench, '_cid', lambda: allocated.append('command-id') or 'command-id')
    monkeypatch.setattr(bench.time, 'sleep', lambda _: None)

    def query(*args, **kwargs):
        return {'query': True}

    def command(operation, payload, command_id, **kwargs):
        delivered.append(command_id)
        if len(delivered) == 1:
            raise StorageError('database_busy', 'retryable pre-dispatch rejection', retryable=True)
        return {'command': True}

    def embedded(operation, payload, *, command_id, **kwargs):
        if command_id is None:
            return {'status': 0, 'payload': query()}
        try:
            result = command(operation, payload, command_id)
            return {'status': 0, 'payload': result}
        except StorageError:
            return {'status': 1, 'retryable': True}

    engine = SimpleNamespace(_retry_backpressure=True, client_retries=0,
                             _embedded=SimpleNamespace(execute_json=embedded))
    client = SimpleNamespace(query=query, command=command)
    execute = (lambda op, body, write: bench._application_execute(client, op, body, write, engine)) if transport == 'application' else (
        lambda op, body, write: bench.TofuDbEngine._embedded_execute(engine, op, body, write))
    assert execute('record.get', {'namespace': 'test', 'key': 'row'}, False) == {'query': True}
    assert allocated == []
    assert execute('record.put', {'namespace': 'test', 'key': 'row', 'value': 17}, True) == {'command': True}
    assert allocated == ['command-id'] and delivered == ['command-id', 'command-id']
    assert engine.client_retries == 1


def test_sqlite_comparison_uses_requested_volume_without_fastpath(fake_storage, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'required')
    target = tmp_path / 'volume'
    target.mkdir()
    engine = LegacyEngine('sqlite', authority_dir_parent=target)
    try:
        engine.start()
        assert engine._project_root.is_relative_to(target)
        assert fake_storage[-1].env['TOFU_STORAGE_FASTPATH'] == 'off'
        assert os.environ['TOFU_STORAGE_FASTPATH'] == 'required'
    finally:
        engine.stop()


def test_raw_sqlite_uses_requested_volume(tmp_path):
    from scripts.tofudb_perf_compare import RawSqliteEngine
    engine = RawSqliteEngine(authority_dir_parent=tmp_path)
    try:
        engine.start()
        assert engine._db_path.is_relative_to(tmp_path)
    finally:
        engine.stop()


@pytest.mark.parametrize('failure', ['none', 'startup', 'workload'])
def test_report_exit_status_preserves_incomplete_comparison(
        monkeypatch, tmp_path, failure):
    import json
    import scripts.tofudb_perf_compare as bench

    stopped = []

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            if failure == 'startup':
                raise bench.BenchError('startup failed')

        def stop(self):
            stopped.append(True)

    def measure(*args):
        if failure == 'workload':
            raise bench.BenchError('request failed')
        return {'mean_ms': 1.0}

    output = tmp_path / 'report.json'
    monkeypatch.setattr(bench, 'LegacyEngine', Engine)
    monkeypatch.setattr(bench, '_workloads', lambda *args: [
        ('example', None, None, 1)])
    monkeypatch.setattr(bench, 'measure_workload', measure)
    monkeypatch.setattr(bench, '_print_report', lambda report: None)
    monkeypatch.setattr(bench.sys, 'argv', [
        'benchmark', '--engines', 'sqlite', '--output', str(output)])
    assert bench.main() == (0 if failure == 'none' else 1)
    report = json.loads(output.read_text())
    assert bool(report['skipped']) == (failure == 'startup')
    assert bool(stopped) == (failure != 'startup')
    if failure == 'workload':
        assert 'request failed' in report['workloads']['example']['sqlite']['error']


@pytest.fixture
def fake_storage(monkeypatch, tmp_path):
    import lib.storage
    import lib.storage.service
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))
    states = []

    def supervisor(**kwargs):
        state = SimpleNamespace(
            backend=kwargs['backend'], env=dict(os.environ), stopped=False,
            fail_start=False, fail_stop=False)
        states.append(state)
        return state

    class Runtime:
        def __init__(self, supervisor, **kwargs):
            self.state = supervisor

        def start(self):
            if self.state.fail_start:
                raise RuntimeError('startup failed')

        def client(self, **kwargs):
            return self.state

        def stop(self):
            self.state.stopped = True
            if self.state.fail_stop:
                raise RuntimeError('stop failed')

    def reject_global_runtime(*args, **kwargs):
        pytest.fail('benchmark must not replace the process storage runtime')

    monkeypatch.setattr(lib.storage, 'StorageSupervisor', supervisor)
    monkeypatch.setattr(lib.storage, 'StorageRuntime', Runtime)
    monkeypatch.setattr(lib.storage.service, 'install_runtime_for_test',
                        reject_global_runtime)
    return states


@pytest.mark.parametrize('order', [('postgres', 'sqlite'), ('sqlite', 'postgres')])
@pytest.mark.parametrize('inherited', [False, True])
def test_engines_isolate_environment_and_keep_own_clients(
        monkeypatch, fake_storage, order, inherited):
    keys = ('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE',
            'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', 'TOFU_STORAGE_TOKEN',
            'TOFU_STORAGE_CONNECTION_FILE')
    for key in keys:
        if inherited:
            monkeypatch.setenv(key, 'caller-owned-value')
        else:
            monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)
    engines = [LegacyEngine(name, 'postgresql://unused.invalid/disposable')
               for name in order]
    try:
        assert dict(os.environ) == before
        for engine in engines:
            engine.start()
            assert dict(os.environ) == before
        for engine, state in zip(engines, fake_storage):
            assert engine._client is state
            assert not state.stopped
            assert 'TOFU_STORAGE_CONNECTION_FILE' not in state.env
            if state.backend == 'postgres':
                assert Path(state.env[keys[0]]).read_text() == (
                    'postgresql://unused.invalid/disposable')
            else:
                assert keys[0] not in state.env
    finally:
        for engine in reversed(engines):
            engine.stop()
    assert dict(os.environ) == before
    assert all(state.stopped for state in fake_storage)
    assert all(not engine._project_root.parent.exists() for engine in engines)
    assert all(not engine._dsn_file.exists() for engine in engines
               if engine._dsn_file is not None)


@pytest.mark.parametrize('failure', ['startup', 'stop', 'unlink'])
def test_environment_restored_even_when_startup_or_cleanup_fails(
        monkeypatch, fake_storage, failure):
    import lib.storage

    original = lib.storage.StorageSupervisor

    def supervisor(**kwargs):
        state = original(**kwargs)
        state.fail_start = failure == 'startup'
        state.fail_stop = True
        return state

    monkeypatch.setattr(lib.storage, 'StorageSupervisor', supervisor)
    monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', 'caller-dsn')
    before = dict(os.environ)
    engine = LegacyEngine('postgres', 'postgresql://unused.invalid/disposable')
    if failure == 'startup':
        with pytest.raises(RuntimeError, match='startup failed'):
            engine.start()
    else:
        engine.start()
        if failure == 'unlink':
            original_unlink = Path.unlink

            def fail_unlink(path, **kwargs):
                if path == engine._dsn_file:
                    raise OSError('unlink failed')
                return original_unlink(path, **kwargs)

            with monkeypatch.context() as patcher:
                patcher.setattr(Path, 'unlink', fail_unlink)
                with pytest.raises(OSError, match='unlink failed'):
                    engine.stop()
            engine.stop()
        else:
            engine.stop()
    assert dict(os.environ) == before
    assert fake_storage[0].stopped
    assert not engine._project_root.parent.exists()
    assert not engine._dsn_file.exists()


@pytest.mark.parametrize('selection', ['', ' , ', 'sqlite,sqlite'])
def test_empty_or_duplicate_engine_selection_is_rejected(monkeypatch, selection):
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', '--engines', selection])
    with pytest.raises(SystemExit) as error:
        bench.main()
    assert error.value.code == 2


def test_explicit_missing_binary_never_falls_back(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench

    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.setenv('TOFUDB_PERF_BIN', str(tmp_path / 'missing'))
    with pytest.raises(bench.BenchError, match='executable file'):
        bench._resolve_tofudb_binary()


def test_latency_windows_preserve_growth_and_all_samples(monkeypatch):
    import scripts.tofudb_perf_compare as bench

    durations = list(range(1, 24))
    ticks = iter(t for duration in durations for t in (0, duration * 1_000_000))
    monkeypatch.setattr(bench.time, 'perf_counter_ns', lambda: next(ticks))
    calls = []
    engine = SimpleNamespace(execute=lambda *args: calls.append(args))
    result = bench.measure_workload(
        engine, None, lambda i: ('probe', {'i': i}, False), 23, warmup=2)
    assert len(calls) == 25
    assert result['mean_ms'] == 12.0
    assert result['p99_ms'] == 23.0
    windows = result['windows']
    assert len(windows) <= 10
    assert sum(window['iterations'] for window in windows) == 23
    assert windows[0]['start_iteration'] == 2
    assert windows[0]['mean_ms'] == 2.0
    assert windows[-1]['start_iteration'] == 23
    assert windows[-1]['mean_ms'] == 22.5


@pytest.mark.parametrize('iterations,warmup', [(0, 5), (-1, 5), (1, -1)])
def test_invalid_measurement_bounds_fail_before_engine_io(iterations, warmup):
    import scripts.tofudb_perf_compare as bench

    with pytest.raises(bench.BenchError, match='iterations'):
        bench.measure_workload(None, None, None, iterations, warmup)


@pytest.mark.parametrize('payload,closed,expected', [
    (b'{"port":123}\n', False, None),
    (b'{"port":', True, 'complete readiness'),
    (b'{"port":', False, 'timed out'),
    (b'x' * 4096, False, 'exceeds 4096'),
])
def test_readiness_is_bounded_even_for_partial_line(payload, closed, expected):
    import scripts.tofudb_perf_compare as bench

    reader, writer = os.pipe()
    try:
        os.write(writer, payload)
        if closed:
            os.close(writer)
            writer = None
        if expected:
            with pytest.raises(bench.BenchError, match=expected):
                bench._read_readiness(reader, timeout=0.02)
        else:
            assert bench._read_readiness(reader, timeout=0.02) == '{"port":123}'
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


def test_tofudb_init_failure_cleans_disposable_directories(monkeypatch, tmp_path):
    from serverctl_pkg import native_engine
    monkeypatch.setattr(native_engine, 'build_identity', lambda binary: {
        'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64})
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))

    def fail_init(*args, **kwargs):
        raise RuntimeError('init failed')

    monkeypatch.setattr(bench.subprocess, 'run', fail_init)
    binary = tmp_path / 'test-binary'
    binary.write_bytes(b'test executable')
    engine = bench.TofuDbEngine(binary)
    with pytest.raises(RuntimeError, match='init failed'):
        engine.start()
    assert engine._dirs
    assert all(not path.exists() for path in engine._dirs)


def test_measurement_metadata_identifies_exact_binary(tmp_path, monkeypatch):
    import hashlib
    import scripts.tofudb_perf_compare as bench

    from tofu_db import wire as wire_client
    client_source = tmp_path / 'wire-client.py'
    client_source.write_bytes(b'wire-client-build')
    monkeypatch.setattr(wire_client, '__file__', str(client_source))
    binary = tmp_path / 'tofu-db' 
    binary.write_bytes(b'benchmark-build')
    metadata = bench._measurement_metadata({
        'tofudb': SimpleNamespace(_binary=binary), 'sqlite': object()})
    assert metadata['engine_order'] == ['tofudb', 'sqlite']
    assert metadata['scope'] == 'storage-operations-not-active-conversations'
    assert metadata['performance_verdict'] == 'not_assessed'
    assert '1000-running-attempts' in metadata['uncovered']
    assert metadata['tofudb_binary'] == {
        'path': str(binary.resolve()), 'bytes': 15,
        'sha256': hashlib.sha256(b'benchmark-build').hexdigest()}

    assert metadata['tofudb_wire_client']['source_sha256'] == hashlib.sha256(b'wire-client-build').hexdigest()
    assert metadata['tofudb_wire_client']['crc32c'] in {'compiled', 'python'}
    assert metadata['tofudb_wire_client']['large_text_json'] in {'orjson-compatible-subset', 'stdlib'}


def test_search_measurement_includes_empty_continuations_and_rejects_stalled_cursor():
    import scripts.tofudb_perf_compare as bench

    replies = iter([
        {'items': [], 'next_cursor': 'scan-page', 'incomplete': True},
        {'items': [{'id': 'a'}], 'next_cursor': 'next-hit', 'incomplete': False},
        {'items': [{'id': 'b'}], 'next_cursor': None, 'incomplete': False},
    ])
    calls = []
    def execute(operation, payload, command):
        calls.append((operation, payload, command))
        return next(replies)
    source = {'query': 'needle', 'user_id': 11, 'limit': 2}
    result = bench._execute_workload_operation(SimpleNamespace(execute=execute),
                                             'conversation.search', source, False)
    assert result == [{'id': 'a'}, {'id': 'b'}]
    assert [call[1]['cursor'] for call in calls] == [None, 'scan-page', 'next-hit']
    assert [call[1]['limit'] for call in calls] == [2, 2, 1]
    assert 'page' not in source and 'cursor' not in source
    stalled = SimpleNamespace(execute=lambda *_: {'items': [], 'next_cursor': 'same', 'incomplete': True})
    with pytest.raises(bench.BenchError, match='did not advance'):
        bench._execute_workload_operation(stalled, 'conversation.search', source, False)


def test_each_workload_refreshes_idle_session_before_setup_and_timing(monkeypatch):
    import scripts.tofudb_perf_compare as bench

    calls = []
    old_session = SimpleNamespace(close=lambda: calls.append('close'))
    new_session = SimpleNamespace(close=lambda: calls.append('close-new'))
    monkeypatch.setattr(bench, 'StorageV2Session', lambda *args, **kwargs: (
        calls.append('hello') or new_session))
    engine = bench.TofuDbEngine(Path('/unused'), client_mode='wire')
    engine._port = 1234
    engine._session = old_session
    engine.execute = lambda *_: calls.append('request')
    bench.measure_workload(engine, lambda _: calls.append('setup'),
                           lambda _: ('probe', {}, False), 1, warmup=1)
    assert calls == ['close', 'hello', 'setup', 'request', 'request']
    assert engine._session is new_session


@pytest.mark.parametrize('final_coverage', ['ready', 'indexing', 'partial'])
def test_search_setup_waits_for_complete_corpus_not_only_matching_seeds(monkeypatch, final_coverage):
    import scripts.tofudb_perf_compare as bench

    checks = []
    clock = iter(range(0, 10**9, 10))
    monkeypatch.setattr(bench.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(bench.time, 'sleep', lambda _: None)
    def execute(operation, payload, command):
        if command:
            return {}
        if payload['query']:
            return {'items': [{'id': 'seed'}], 'next_cursor': None, 'incomplete': False}
        checks.append(payload)
        return {'coverage': 'indexing' if len(checks) == 1 else final_coverage,
                'coverage_reasons': []}
    engine = SimpleNamespace(name='fixture', execute=execute)
    scale = {**bench._SCALES['tiny'], 'search_docs': 1}
    setup = next(row[1] for row in bench._workloads(scale, 11) if row[0] == 'conversation_search')
    if final_coverage == 'ready':
        setup(engine)
        assert len(checks) == 2
        assert engine._search_stabilization_s is not None
    else:
        with pytest.raises(bench.BenchError, match='did not stabilize'):
            setup(engine)
        assert checks


def _phase_identities(workloads):
    by_name = {row[0]: row for row in workloads}
    conv_payload = by_name['conversation_create_burst'][2](0)[1]
    turn_payload = by_name['turn_append_burst'][2](0)[1]
    return {conv_payload['conv_id'], turn_payload['turn_id'],
            turn_payload['command_id']}


def test_run_scoped_identities_are_unique_per_run():
    import scripts.tofudb_perf_compare as bench

    first = _phase_identities(bench._workloads(bench._SCALES['tiny'], 11))
    second = _phase_identities(bench._workloads(bench._SCALES['tiny'], 11))
    assert first.isdisjoint(second)
    assert all(identity for identity in first | second)


@pytest.mark.parametrize('dbname', ['postgres', 'template0', 'template1',
                                    'production', ''])
def test_postgres_reset_refuses_non_disposable_database(dbname):
    import scripts.tofudb_perf_compare as bench

    dsn = f'host=localhost dbname={dbname}' if dbname else 'host=localhost'
    with pytest.raises(bench.BenchError, match='disposable'):
        bench._reset_postgres_database(dsn)


def test_postgres_reset_drops_recreates_then_migrates(monkeypatch):
    import psycopg
    import lib.storage_sidecar.migrate as migrate
    import scripts.tofudb_perf_compare as bench

    executed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            executed.append(str(statement))

    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: Connection())
    migrated = []
    monkeypatch.setattr(migrate, 'migrate_external_postgres',
                        lambda dsn: migrated.append(dsn) or 62)
    dsn = 'host=localhost port=55432 dbname=tofu_perf_x user=postgres'
    bench._reset_postgres_database(dsn)
    assert any('DROP DATABASE IF EXISTS' in statement
               and 'tofu_perf_x' in statement for statement in executed)
    assert any('CREATE DATABASE' in statement
               and 'tofu_perf_x' in statement for statement in executed)
    assert migrated == [dsn]


def _concurrent_row(bench, scale):
    return next(row for row in bench._workloads(scale, 11)
                if row[0] == 'concurrent_read')


def test_tofudb_durability_reaches_serve_args_and_readiness_is_verified(
        monkeypatch, tmp_path):
    from serverctl_pkg import native_engine
    monkeypatch.setattr(native_engine, 'build_identity', lambda binary: {
        'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64})
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))
    reader, writer = os.pipe()
    os.write(writer, b'{"port":34567,"walDurability":"normal"}\n')
    os.close(writer)
    captured = {}

    class Process:
        stdin = None
        stdout = os.fdopen(reader)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def popen(argv, **kwargs):
        captured['argv'] = list(argv)
        return Process()

    monkeypatch.setattr(bench.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(bench.subprocess, 'Popen', popen)
    monkeypatch.setattr(bench, 'StorageV2Session',
                        lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    binary = tmp_path / 'test-binary'
    binary.write_bytes(b'test executable')
    engine = bench.TofuDbEngine(
        binary, durability='normal', wal_flush_ms=25)
    engine.start()
    try:
        argv = captured['argv']
        assert argv[argv.index('--durability') + 1] == 'normal'
        assert argv[argv.index('--wal-flush-interval-ms') + 1] == '25'
    finally:
        engine.stop()


def test_tofudb_durability_mismatch_with_daemon_readiness_fails_closed(
        monkeypatch, tmp_path):
    from serverctl_pkg import native_engine
    monkeypatch.setattr(native_engine, 'build_identity', lambda binary: {
        'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64})
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))
    reader, writer = os.pipe()
    os.write(writer, b'{"port":34567,"walDurability":"strict"}\n')
    os.close(writer)

    class Process:
        stdin = None
        stdout = os.fdopen(reader)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(bench.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(bench.subprocess, 'Popen', lambda *a, **k: Process())
    binary = tmp_path / 'test-binary'
    binary.write_bytes(b'test executable')
    engine = bench.TofuDbEngine(
        binary, durability='normal', wal_flush_ms=10)
    with pytest.raises(bench.BenchError, match='walDurability'):
        engine.start()
    engine.stop()


def test_tofudb_schema_mismatch_is_rejected_before_opening_a_wire_session(
        monkeypatch, tmp_path):
    import json
    from serverctl_pkg import native_engine
    from tofu_db.generated_contract import STORAGE_SCHEMA_VERSION
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(native_engine, 'build_identity', lambda binary: {
        'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64})
    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))
    reader, writer = os.pipe()
    os.write(writer, json.dumps({'port': 34567, 'walDurability': 'strict',
        'schemaId': STORAGE_SCHEMA_VERSION + 1}).encode() + b'\n')
    os.close(writer)

    class Process:
        stdin = None
        stdout = os.fdopen(reader)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def unexpected_session(*args, **kwargs):
        pytest.fail('schema mismatch must be rejected before connecting')

    monkeypatch.setattr(bench.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(bench.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr(bench, 'StorageV2Session', unexpected_session)
    binary = tmp_path / 'test-binary'
    binary.write_bytes(b'test executable')
    engine = bench.TofuDbEngine(binary)
    with pytest.raises(bench.BenchError, match='schemaId.*SDK'):
        engine.start()
    engine.stop()


def test_tofudb_durability_selection_is_validated():
    import scripts.tofudb_perf_compare as bench

    with pytest.raises(bench.BenchError, match='durability'):
        bench.TofuDbEngine(Path('/unused'), durability='eventual')
    with pytest.raises(bench.BenchError, match='flush interval'):
        bench.TofuDbEngine(Path('/unused'), durability='normal',
                           wal_flush_ms=0)


def test_concurrent_read_phase_measures_writer_and_readers():
    import threading
    import scripts.tofudb_perf_compare as bench

    calls = []
    lock = threading.Lock()

    def execute(operation, payload, command):
        with lock:
            calls.append((operation, command))
        return {}

    engine = SimpleNamespace(execute=execute)
    scale = bench._SCALES['tiny']
    row = _concurrent_row(bench, scale)
    result = row[4](engine, row[1], row[2], row[3])
    assert 'error' not in result
    assert result['concurrent_readers'] == 4
    assert result['writer_iterations'] == scale['concurrent_writes']
    assert result['reads_completed'] == 4 * scale['concurrent_reads']
    assert result['writer_ops_per_sec'] > 0
    assert result['p50_ms'] >= 0
    puts = sum(1 for operation, _ in calls if operation == 'record.put')
    assert puts == scale['seed_records'] + scale['concurrent_writes']


def test_concurrent_read_phase_reports_thread_failure_loudly():
    import scripts.tofudb_perf_compare as bench

    def execute(operation, payload, command):
        if str(payload.get('key', '')).startswith('cw/'):
            raise RuntimeError('wedged engine')
        return {}

    engine = SimpleNamespace(execute=execute)
    row = _concurrent_row(bench, bench._SCALES['tiny'])
    result = row[4](engine, row[1], row[2], row[3])
    assert 'wedged engine' in result['error']


def _swarm_row(bench, scale):
    return next(row for row in bench._workloads(scale, 11)
                if row[0] == 'swarm_write')


def test_swarm_phase_measures_writers_readers_and_aggregate():
    import threading
    import scripts.tofudb_perf_compare as bench

    calls = []
    lock = threading.Lock()

    def execute(operation, payload, command):
        with lock:
            calls.append((operation, command))
        return {}

    engine = SimpleNamespace(execute=execute)
    scale = bench._SCALES['tiny']
    row = _swarm_row(bench, scale)
    result = row[4](engine, row[1], row[2], row[3])
    assert 'error' not in result
    assert result['swarm_writers'] == scale['swarm_writers']
    assert result['writer_iterations'] == (
        scale['swarm_writers'] * scale['swarm_writes_each'])
    assert result['aggregate_writer_ops_per_sec'] > 0
    assert result['reads_completed'] == (
        scale['swarm_readers'] * scale['swarm_reads_each'])
    assert result['reader_p50_ms'] >= 0
    appends = sum(1 for operation, _ in calls
                  if operation == 'turn.append_settled')
    assert appends == scale['swarm_writers'] * scale['swarm_writes_each']


def test_swarm_phase_reports_thread_failure_loudly():
    import scripts.tofudb_perf_compare as bench

    def execute(operation, payload, command):
        if operation == 'turn.append_settled':
            raise RuntimeError('wedged engine')
        return {}

    engine = SimpleNamespace(execute=execute)
    row = _swarm_row(bench, bench._SCALES['tiny'])
    result = row[4](engine, row[1], row[2], row[3])
    assert 'wedged engine' in result['error']


def test_large_turn_payload_is_tool_result_class_and_run_unique():
    import scripts.tofudb_perf_compare as bench

    first = {row[0]: row for row in bench._workloads(bench._SCALES['tiny'], 11)}
    second = {row[0]: row for row in bench._workloads(bench._SCALES['tiny'], 11)}
    payload_a = first['turn_append_large'][2](0)[1]
    payload_b = second['turn_append_large'][2](0)[1]
    assert len(payload_a['projection']['content']) == 262_144
    assert payload_a['turn_id'] != payload_b['turn_id']
    assert payload_a['command_id'] != payload_b['command_id']


def test_benchmark_does_not_add_retries_to_production_failure():
    from lib.storage.errors import StorageError
    import scripts.tofudb_perf_compare as bench

    calls = []
    def reject(*args, **kwargs):
        calls.append(args)
        raise StorageError('database_timeout', 'rejected', retryable=True)
    with pytest.raises(bench.BenchError, match='database_timeout'):
        bench._application_execute(SimpleNamespace(query=reject), 'record.get', {}, False)
    assert len(calls) == 1


def test_native_benchmark_defaults_to_production_adapter():
    import scripts.tofudb_perf_compare as bench
    calls = []
    engine = bench.TofuDbEngine(Path('/unused'))
    engine._client = SimpleNamespace(query=lambda *args, **kwargs: calls.append((args, kwargs)) or {'value': 7})
    assert engine.execute('record.get', {'key': 'k'}, False) == {'value': 7}
    assert engine.open_session().execute('record.get', {'key': 'k'}, False) == {'value': 7}
    assert len(calls) == 2


def test_small_samples_do_not_qualify_tail_latency_claims():
    import scripts.tofudb_perf_compare as bench
    assert bench._latency_summary([1.0] * 20)['p99_sample_count_sufficient'] is False
    assert bench._latency_summary([1.0] * 1000)['p99_sample_count_sufficient'] is True


def test_binary_selection_cannot_silently_ignore_application_override(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench
    from serverctl_pkg import native_engine
    monkeypatch.setattr(native_engine, 'verify_current', lambda binary, root: None)
    selected = tmp_path / 'selected'
    selected.write_bytes(b'build one')
    selected.chmod(0o700)
    monkeypatch.delenv('TOFUDB_PERF_BIN', raising=False)
    monkeypatch.setenv('TOFUDB_BIN', str(selected))
    assert bench._resolve_tofudb_binary() == selected
    monkeypatch.setenv('TOFUDB_PERF_BIN', str(tmp_path / 'other'))
    with pytest.raises(bench.BenchError, match='disagree'):
        bench._resolve_tofudb_binary()
    assert bench._resolve_tofudb_binary(selected) == selected


def test_benchmark_pins_build_across_source_replacement_and_rejects_wrong_digest(tmp_path):
    import hashlib
    import scripts.tofudb_perf_compare as bench
    source = tmp_path / 'engine'
    source.write_bytes(b'certified build')
    source.chmod(0o700)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    engine = bench.TofuDbEngine(source, expected_sha256=expected)
    try:
        engine._pin_binary()
        source.write_bytes(b'unrelated rebuild')
        assert engine._binary.read_bytes() == b'certified build'
        assert bench._measurement_metadata({'tofudb': engine})['tofudb_binary']['sha256'] == expected
        wrong = bench.TofuDbEngine(source, expected_sha256=expected)
        with pytest.raises(bench.BenchError, match='SHA256'):
            wrong._pin_binary()
        assert not wrong._dirs
    finally:
        import shutil
        for path in engine._dirs:
            shutil.rmtree(path)


def test_large_turn_profiles_cover_unique_blocks_and_exact_utf8_budgets():
    import scripts.tofudb_perf_compare as bench
    for size in [1, 17, 65536]:
        for pattern in ['repetitive', 'unique', 'escaped']:
            content = bench._large_turn_content(3, size, pattern)
            assert len(content.encode('utf-8')) == size
            assert content == bench._large_turn_content(3, size, pattern)
    first = bench._large_turn_content(-3, 65536, 'unique')
    second = bench._large_turn_content(3, 65536, 'unique')
    assert all(first[i:i + 4096] != second[i:i + 4096] for i in range(0, 65536, 4096))


def test_large_turn_history_is_seeded_before_measured_commands():
    import scripts.tofudb_perf_compare as bench
    calls = []
    engine = SimpleNamespace(execute=lambda op, payload, command: calls.append((op, payload)))
    scale = dict(bench._SCALES['tiny'], large_turn_history=3,
                 large_turn_bytes=1024, large_turn_pattern='unique')
    _, setup, operation, *_ = next(row for row in bench._workloads(scale, 11)
                                   if row[0] == 'turn_append_large')
    setup(engine)
    assert [op for op, _ in calls] == ['conversation.create'] + ['turn.append_settled'] * 3
    measured = operation(0)[1]
    assert all(payload['created_at'] < measured['created_at'] for _, payload in calls[1:])
    assert len({payload['turn_id'] for _, payload in calls[1:]} | {measured['turn_id']}) == 4
    assert all(payload['projection']['content'] != measured['projection']['content']
               for _, payload in calls[1:])


def test_rawsqlite_engine_operation_contract_and_reopen_control(tmp_path):
    """The raw-SQLite baseline speaks the harness's logical operation mix.

    Real on-disk WAL + synchronous=FULL: point writes/reads, conversation
    catalog, turn append with exact msg_count, the paged-search contract
    (bounded items + advancing cursor), cold-family floors, and graceful
    reopen with zero acknowledged loss.
    """
    import scripts.tofudb_perf_compare as bench

    engine = bench.RawSqliteEngine()
    engine.start()
    try:
        engine.execute('record.put', {
            'namespace': 'ns', 'key': 'k/1', 'value': {'n': 1}}, True)
        assert engine.execute('record.get', {
            'namespace': 'ns', 'key': 'k/1'}, False) == {'n': 1}
        assert engine.execute('record.list', {
            'namespace': 'ns', 'prefix': 'k/', 'limit': 5},
            False) == [{'key': 'k/1', 'value': {'n': 1}}]

        engine.execute('conversation.create', {
            'conv_id': 'c1', 'user_id': 11, 'title': 't', 'settings': {},
            'created_at': 1, 'updated_at': 1}, True)
        for i in range(3):
            engine.execute('turn.append_settled', {
                'conversation_id': 'c1', 'user_id': 11, 'actor': 'human',
                'status': 'completed',
                'projection': {'content': f'probe word round {i}'},
                'lane_id': 'main', 'command_id': f'c1_{i}',
                'turn_id': f'c1_turn_{i}', 'created_at': 10 + i,
                'now': 10 + i,
                'conversation_defaults': {'allowCreate': False}}, True)
        document = engine.execute('conversation.get', {
            'conv_id': 'c1', 'user_id': 11, 'message_window': 2}, False)
        assert document['metadata']['msg_count'] == 3
        assert len(document['messages']) == 2

        page1 = engine.execute('conversation.search', {
            'user_id': 11, 'query': 'probe', 'page': True, 'limit': 2},
            False)
        assert page1['incomplete'] is False and len(page1['items']) == 2
        page2 = engine.execute('conversation.search', {
            'user_id': 11, 'query': 'probe', 'page': True, 'limit': 2,
            'cursor': page1['next_cursor']}, False)
        assert page2['next_cursor'] is None and len(page2['items']) == 1
        assert {hit['turn_id'] for hit in page1['items'] + page2['items']} == \
            {f'c1_turn_{i}' for i in range(3)}
        coverage = engine.execute('conversation.search', {
            'user_id': 11, 'query': '', 'page': True, 'limit': 1}, False)
        assert coverage['coverage'] == 'ready'

        listed = engine.execute('conversation.list', {
            'user_id': 11, 'limit': 50, 'include_messages': False}, False)
        assert [row['conv_id'] for row in listed] == ['c1']
        snapshot = engine.execute('turn.sync.snapshot', {
            'conversation_id': 'c1', 'user_id': 11, 'turn_limit': 2}, False)
        assert [t['turn_id'] for t in snapshot['turns']] == [
            'c1_turn_1', 'c1_turn_2']

        engine.execute('turn.append_settled', {
            'conversation_id': 'c1', 'user_id': 11, 'actor': 'human',
            'status': 'completed', 'projection': {'content': 'durable'},
            'lane_id': 'main', 'command_id': 'c1_durable',
            'turn_id': 'c1_turn_durable', 'created_at': 99, 'now': 99,
            'conversation_defaults': {'allowCreate': False}}, True)
        engine.crash_cycle()
        recovered = engine.execute('conversation.get', {
            'conv_id': 'c1', 'user_id': 11, 'message_window': 1}, False)
        assert recovered['metadata']['msg_count'] == 4
        engine.restart_cycle()
        assert engine.execute('conversation.count', {}, False) == {'count': 1}
        assert engine.execute('task_results.recovery_pending', {
            'limit': 1}, False) == {'pending': 0}
        assert engine.execute('timer.active.count', {}, False) == {'count': 0}
        assert engine.execute('credential.list', {
            'owner_user_id': 11, 'tenant_id': ''}, False) == []

        with pytest.raises(bench.BenchError, match='unsupported operation'):
            engine.execute('queue.claim', {}, True)
        with pytest.raises(bench.BenchError, match='database_conflict'):
            engine.execute('conversation.create', {
                'conv_id': 'c1', 'user_id': 11, 'title': 'dup',
                'settings': {}, 'created_at': 2, 'updated_at': 2}, True)

        session = engine.open_session()
        try:
            assert session.execute('record.get', {
                'namespace': 'ns', 'key': 'k/1'}, False) == {'n': 1}
        finally:
            session.close()

        report = bench._engine_resource_report(engine)
        assert report['data_size_bytes'] > 0
        assert 'engine_write_bytes' in report
    finally:
        engine.stop()

    assert engine._dir is None or not Path(engine._dir).exists()

def test_rawsqlite_reopen_control_does_not_claim_process_crash_certification(tmp_path, capsys):
    import scripts.tofudb_perf_compare as bench

    engine = bench.RawSqliteEngine(authority_dir_parent=tmp_path)
    try:
        engine.start()
        scale = {**bench._SCALES['tiny'], 'crash_appends': 2}
        _, setup, operation, _, runner = next(
            row for row in bench._workloads(scale, 11) if row[0] == 'crash_recovery')
        result = runner(engine, setup, operation, 1)
        assert result['scope'] == 'graceful_reopen_control'
        assert result['recovery_method'] == 'connection_close_reopen'
        assert result['certifies_process_crash'] is False
        assert result['zero_acknowledged_loss'] is True
        bench._print_report({'engines': ['tofudb', 'rawsqlite'], 'workloads': {
            'crash_recovery': {'rawsqlite': result, 'tofudb': {
                **result, 'scope': 'crash_recovery', 'certifies_process_crash': True}}}})
        output = capsys.readouterr().out
        assert 'process crash recovery is unmeasured' in output
        assert 'incomparable measurement scopes' in output
    finally:
        engine.stop()
    assert engine._dir is None or not Path(engine._dir).exists()


def test_default_binary_refuses_stale_source_but_explicit_history_stays_labelled(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench
    from serverctl_pkg import native_engine
    binary = tmp_path / 'engine'
    binary.write_bytes(b'old executable')
    binary.chmod(0o700)
    monkeypatch.delenv('TOFUDB_PERF_BIN', raising=False)
    monkeypatch.setenv('TOFUDB_BIN', str(binary))
    def stale(*args):
        raise native_engine.EnginePreparationError('different source digest')
    monkeypatch.setattr(native_engine, 'verify_current', stale)
    with pytest.raises(bench.BenchError, match='stale or unverified'):
        bench._resolve_tofudb_binary()
    assert bench._resolve_tofudb_binary(binary) == binary
    engine = bench.TofuDbEngine(binary)
    engine._build_identity = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64}
    engine._checkout_source_sha256 = 'b' * 64
    metadata = bench._measurement_metadata({'tofudb': engine})
    assert metadata['source_matches_checkout'] is False
    assert metadata['tofudb_build_identity']['source_sha256'] == 'a' * 64


def test_native_graceful_restart_closes_application_pool_before_parent_eof(tmp_path):
    import scripts.tofudb_perf_compare as bench
    calls = []
    engine = bench.TofuDbEngine(tmp_path / 'engine')
    engine._data_dir = tmp_path
    engine._log = None
    engine._client = SimpleNamespace(close=lambda: calls.append('pool-close'))
    engine._session = SimpleNamespace(close=lambda: calls.append('wire-close'))
    engine._process = SimpleNamespace(
        stdin=SimpleNamespace(close=lambda: calls.append('parent-eof')),
        wait=lambda **kwargs: calls.append('joined'))
    engine._launch_serve = lambda: calls.append('reopen')
    engine.restart_cycle()
    assert calls == ['pool-close', 'wire-close', 'parent-eof', 'joined', 'reopen']
    assert engine._client is None and engine._session is None


def test_native_stop_releases_idle_application_pool_once(tmp_path):
    import scripts.tofudb_perf_compare as bench
    calls = []
    engine = bench.TofuDbEngine(tmp_path / 'engine')
    engine._log = None
    engine._client = SimpleNamespace(close=lambda: calls.append('close'))
    engine.stop()
    engine.stop()
    assert calls == ['close']


def test_search_preparation_does_not_restart_slow_rebuild(monkeypatch):
    import scripts.tofudb_perf_compare as bench

    elapsed = [0.0]
    rebuilds = []
    observations = []
    monkeypatch.setattr(bench.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(bench.time, 'sleep', lambda _: elapsed.__setitem__(0, elapsed[0] + 30))

    def execute(operation, payload, command):
        if operation == 'turn.search.backfill':
            rebuilds.append(elapsed[0])
            return {'scheduled': True}
        if command:
            return {}
        if payload['query']:
            return {'items': [{'id': 'seed'}] if elapsed[0] >= 120 else [],
                    'next_cursor': None, 'incomplete': False}
        observations.append(elapsed[0])
        return {'coverage': 'ready' if elapsed[0] >= 120 else 'indexing',
                'coverage_reasons': []}

    engine = SimpleNamespace(name='slow-fixture', execute=execute)
    scale = {**bench._SCALES['tiny'], 'search_docs': 1}
    setup = next(row[1] for row in bench._workloads(scale, 11)
                 if row[0] == 'conversation_search')
    setup(engine)
    assert rebuilds == [0.0]
    assert observations == [0.0, 30.0, 60.0, 90.0, 120.0]
    assert engine._search_stabilization_s == 120.0


@pytest.fixture
def native_embedded_io(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench
    from tofu_db import embedded
    io = {'bytes': 100, 'pids': []}

    def read(pid):
        io['pids'].append(pid)
        return io['bytes']

    class Runtime:
        def __init__(self, *args, **kwargs):
            io['bytes'] += 7  # Runtime open/replay belongs to this window.
        def build_identity(self):
            return {'source_sha256': 'fixture'}
        def close(self):
            io['bytes'] += 20
        def abandon(self):
            io['bytes'] += 10

    monkeypatch.setattr(bench, '_pid_write_bytes', read)
    monkeypatch.setattr(embedded, 'EmbeddedRuntime', Runtime)
    engine = bench.TofuDbEngine(tmp_path / 'unused-binary', client_mode='embedded')
    engine._log = None
    return bench, engine, io


def test_embedded_write_report_preserves_all_reopen_and_abandon_windows(native_embedded_io):
    bench, engine, io = native_embedded_io
    engine._launch_embedded()
    io['bytes'] += 100
    engine.restart_cycle()
    io['bytes'] += 200
    engine.crash_cycle()
    io['bytes'] += 300
    report = bench._engine_resource_report(engine)
    assert report['engine_write_bytes'] == io['bytes'] - 100
    assert report['engine_write_bytes_complete'] is True
    assert 'upper bound' in report['engine_write_bytes_scope']
    assert set(io['pids']) == {os.getpid()}
    engine.stop()
    after_stop = bench._engine_resource_report(engine)['engine_write_bytes']
    assert after_stop == io['bytes'] - 100
    io['bytes'] += 999  # Unrelated harness writes after close are excluded.
    engine.stop()
    assert bench._engine_resource_report(engine)['engine_write_bytes'] == after_stop


def test_zero_write_window_is_reported_as_zero_not_unavailable(native_embedded_io):
    bench, engine, _ = native_embedded_io
    engine._begin_write_bytes_window()
    engine._accumulate_write_bytes()
    report = bench._engine_resource_report(engine)
    assert report['engine_write_bytes'] == 0
    assert report['engine_write_bytes_complete'] is True


def test_missing_write_counter_marks_partial_report_without_inventing_bytes(native_embedded_io, monkeypatch):
    bench, engine, io = native_embedded_io
    engine._launch_embedded()
    io['bytes'] += 100
    engine.restart_cycle()
    confirmed = engine._write_bytes_accumulated
    monkeypatch.setattr(bench, '_pid_write_bytes', lambda pid: None)
    engine.stop()
    report = bench._engine_resource_report(engine)
    assert report['engine_write_bytes'] == confirmed
    assert report['engine_write_bytes_complete'] is False


def test_failed_embedded_close_keeps_original_error_and_runtime(native_embedded_io):
    bench, engine, io = native_embedded_io
    engine._launch_embedded()
    runtime = engine._embedded
    def close():
        io['bytes'] += 23
        raise RuntimeError('close boundary failed')
    runtime.close = close
    with pytest.raises(RuntimeError, match='close boundary failed'):
        engine.restart_cycle()
    assert engine._embedded is runtime
    report = bench._engine_resource_report(engine)
    assert report['engine_write_bytes'] == 30
    assert report['engine_write_bytes_complete'] is False


def test_daemon_counter_uses_child_pid_and_folds_each_window_once(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench
    current = {41: 100, 42: 500}
    monkeypatch.setattr(bench, '_pid_write_bytes', lambda pid: current.get(pid))
    engine = bench.TofuDbEngine(tmp_path / 'unused-binary', client_mode='application')
    engine._process = SimpleNamespace(pid=41)
    engine._begin_write_bytes_window()
    current[41] += 40
    engine._accumulate_write_bytes()
    engine._accumulate_write_bytes()
    engine._process = SimpleNamespace(pid=42)
    engine._begin_write_bytes_window()
    current[42] += 60
    report = bench._engine_resource_report(engine)
    assert report['engine_write_bytes'] == 100
    assert report['engine_write_bytes_complete'] is True
    assert 'shutdown tail excluded' in report['engine_write_bytes_scope']
