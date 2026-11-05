"""Resident storage ownership and real native lifecycle, using disposable data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

from serverctl_pkg.native_storage_service import (
    NativeStorageService, acquire_service_lock, host_matches, write_record,
)
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def wait_for(predicate, timeout=NATIVE_SERVICE_PREPARATION_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError('lifecycle condition did not settle')


def test_unknown_locked_service_cannot_be_stopped_or_duplicated(tmp_path):
    (tmp_path / 'data').mkdir()
    service = NativeStorageService(str(tmp_path), sys.executable)
    descriptor = acquire_service_lock(service.lock_path)
    try:
        with pytest.raises(RuntimeError, match='draining'):
            service.stop()
        with pytest.raises(RuntimeError, match='draining'):
            service.ensure({'binarySha256': 'a' * 64}, {})
    finally:
        os.close(descriptor)


def test_failed_service_requires_explicit_budget_reset(tmp_path):
    (tmp_path / 'data').mkdir()
    service = NativeStorageService(str(tmp_path), sys.executable)
    plan = {'binarySha256': 'a' * 64}
    write_record(service.state_path, {
        'format': 'tofu.native-storage-service/v1', 'projectPath': str(tmp_path),
        'connectionFile': str(service.connection_path), 'state': 'failed',
        'plan': plan, 'serviceId': 'b' * 32,
    })
    assert not service.needs_replacement(plan)
    assert service.needs_replacement(plan, reset_failed=True)
    with pytest.raises(RuntimeError, match='explicit restart'):
        service.ensure(plan, {})


def test_stop_preserves_native_shutdown_failure(tmp_path, monkeypatch):
    from serverctl_pkg import native_storage_service as module
    (tmp_path / 'data').mkdir()
    service = NativeStorageService(str(tmp_path), sys.executable)
    record = {
        'format': 'tofu.native-storage-service/v1', 'projectPath': str(tmp_path),
        'connectionFile': str(service.connection_path), 'state': 'ready',
        'managerPid': os.getpid(), 'pid': 12345, 'serviceId': 'b' * 32,
    }
    write_record(service.state_path, record)
    alive = True

    def fail_shutdown(pid, signum):
        nonlocal alive
        assert (pid, signum) == (12345, signal.SIGTERM)
        write_record(service.state_path, {**record, 'state': 'failed', 'error': 'close failed'})
        alive = False

    monkeypatch.setattr(module, 'host_matches', lambda _: alive)
    monkeypatch.setattr(module.os, 'kill', fail_shutdown)
    with pytest.raises(RuntimeError, match='shutdown.*close failed'):
        service.stop()
    assert service.record()['state'] == 'failed'


def test_host_exit_cannot_reset_native_crash_budget_on_reconcile(tmp_path, monkeypatch):
    from server_manager import LifecycleManager, proc_start_ticks
    (tmp_path / 'data').mkdir()
    manager = LifecycleManager(str(tmp_path), sys.executable)
    service = manager._native_storage
    write_record(service.state_path, {
        'format': 'tofu.native-storage-service/v1', 'projectPath': str(tmp_path),
        'connectionFile': str(service.connection_path), 'state': 'ready',
        'host': socket.gethostname(), 'pid': -1, 'processStartTime': 1,
        'managerPid': os.getpid(), 'managerStartTime': proc_start_ticks(os.getpid()),
        'plan': {}, 'serviceId': 'b' * 32,
    })
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    monkeypatch.setattr(service, 'prepare', lambda *_: pytest.fail('must not reset crash budget'))
    try:
        with pytest.raises(RuntimeError, match='explicit restart'):
            manager._recover_native_storage_service(os.getpid())
        assert service.record()['state'] == 'failed'
    finally:
        manager.close()


@pytest.fixture
def native_environment(tmp_path, monkeypatch):
    binary = os.environ.get('TOFU_TEST_TOFUDB_BIN')
    if not binary:
        pytest.skip('requires an explicitly source-certified TOFU_TEST_TOFUDB_BIN')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/native_storage_connection_v2.json').symlink_to(
        ROOT / 'contracts/native_storage_connection_v2.json')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('TOFU_', 'TOFUDB_'))}
    environment.update({
        'PYTHONPATH': str(ROOT), 'TOFU_PROJECT_PATH': str(tmp_path),
        'TOFU_DATA_DIR': str(tmp_path / 'data'),
        'TOFU_DEPLOYMENT_MODE': 'personal', 'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb',
        'TOFUDB_TRANSPORT': 'daemon', 'TOFUDB_BIN': binary,
        'TOFUDB_OWNER_ID': '1', 'TOFUDB_TENANT_ID': '1',
        'TOFUDB_EXPECTED_SHA256': os.environ['TOFUDB_EXPECTED_SHA256'],
    })
    for key in ('TOFUDB_BIN', 'TOFUDB_OWNER_ID', 'TOFUDB_TENANT_ID', 'TOFUDB_EXPECTED_SHA256'):
        monkeypatch.setenv(key, environment[key])
    return environment


def read_connection(service):
    from lib.storage.connection_file import read_connection_file
    try:
        return read_connection_file(service.connection_path)
    except RuntimeError:
        return None


def test_native_stays_warm_across_clients_and_rotates_after_crash(tmp_path, native_environment):
    from lib.storage.supervisor import StorageSupervisor
    from lib.storage_authority_sql.preflight import ProjectLease
    from server_manager import pid_is_alive, read_storage_lease_status

    service = NativeStorageService(str(tmp_path), sys.executable)
    plan = service.prepare(native_environment)
    assert plan
    clients = []
    web = None
    try:
        connection_path = service.ensure(plan, native_environment)
        first = wait_for(lambda: read_connection(service))
        assert service.owns_lease(read_storage_lease_status(tmp_path / 'data'))
        with pytest.raises(Exception, match='lease'):
            ProjectLease(tmp_path / 'data', owner_kind='offline_maintenance').acquire()
        # A verified Web owner is allowed during native crash recovery; an
        # unrelated Web lock remains refused by the ordinary ProjectLease.
        web_code = (
            "import fcntl,os,socket,sys,time; "
            "f=open('data/.server.lock','w+'); "
            "fcntl.flock(f,fcntl.LOCK_EX); "
            "f.write(str(os.getpid())+'@'+socket.gethostname()); f.flush(); "
            "print('locked',flush=True); time.sleep(120)"
        )
        web = subprocess.Popen([sys.executable, '-c', web_code], cwd=tmp_path,
            env={**native_environment, 'TOFU_STORAGE_CONNECTION_FILE': connection_path},
            stdout=subprocess.PIPE, text=True)
        assert select.select([web.stdout], [], [], 5)[0]
        assert web.stdout.readline().strip() == 'locked'
        timings = []
        for index in range(3):
            start = time.monotonic()
            supervisor = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=connection_path)
            clients.append(supervisor)
            client = supervisor.start()
            timings.append(time.monotonic() - start)
            if index == 0:
                client.command('conversation.create', {
                    'conv_id': 'resident-test', 'user_id': 1, 'title': 'Resident data', 'settings': {},
                }, 'resident-create')
            else:
                result = client.query('conversation.get', {'conv_id': 'resident-test', 'user_id': 1})
                assert result['metadata']['title'] == 'Resident data'
            supervisor.stop()
            assert pid_is_alive(first['pid'])
            assert read_connection(service)['pid'] == first['pid']
            assert service.ensure(plan, native_environment) == connection_path
        os.kill(first['pid'], signal.SIGKILL)
        rotated = wait_for(lambda: (doc if (doc := read_connection(service))
                                  and doc['pid'] != first['pid'] else None))
        assert rotated['token'] != first['token']
        supervisor = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=connection_path)
        clients.append(supervisor)
        client = supervisor.start()
        assert client.query('conversation.get', {'conv_id': 'resident-test', 'user_id': 1})['metadata']['title'] == 'Resident data'
        # Read-only evidence for the task; no brittle performance threshold.
        print(json.dumps({'warmAttachSeconds': timings, 'firstNativePid': first['pid'],
                          'recoveredNativePid': rotated['pid']}))
    finally:
        for client in clients:
            client.stop()
        if web is not None:
            web.terminate()
            web.wait(timeout=5)
            web.stdout.close()
        record = service.record()
        service.stop()
        assert not host_matches(record)
    assert not read_storage_lease_status(tmp_path / 'data')['held']
    assert not service.connection_path.exists()


def test_native_runtime_reattaches_after_prepared_engine_replacement(
    tmp_path, native_environment, monkeypatch,
):
    from lib.storage.errors import StorageError
    from lib.storage.runtime import StorageRuntime
    from lib.storage.supervisor import StorageSupervisor
    from server_manager import pid_is_alive

    # Own the executable as well as the database; never touch a deployed engine.
    engine = tmp_path / 'tofu-db'
    shutil.copyfile(native_environment['TOFUDB_BIN'], engine)
    engine.chmod(0o755)
    pin = tmp_path / 'tofu-db.sha256'
    pin.write_text(native_environment['TOFUDB_EXPECTED_SHA256'])
    environment = {**native_environment, 'TOFUDB_BIN': str(engine)}
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256')
    service = NativeStorageService(str(tmp_path), sys.executable)
    plan = service.prepare(environment)
    runtime = None
    try:
        connection_path = service.ensure(plan, environment)
        first = wait_for(lambda: read_connection(service))
        supervisor = StorageSupervisor(
            project_root=tmp_path, backend='tofudb', connection_file=connection_path)
        supervisor._liveness_interval_seconds = 0.05
        # A live engine is detached only after its unresponsiveness budget
        # on a host that is not overloaded; keep both deterministic here.
        supervisor._liveness_unresponsive_seconds = 0.2
        monkeypatch.setattr(
            'lib.storage.supervisor._host_overload_detail', lambda *args: None)
        runtime = StorageRuntime(supervisor)
        client = runtime.start()
        client.command('conversation.create', {
            'conv_id': 'reattach-survivor', 'user_id': 1, 'title': 'Preserved data',
        }, 'create-reattach-survivor')

        # Appending bytes preserves this test ELF's runnable source identity,
        # but gives the staged deployment a different content digest. Rename
        # leaves the running process's mapped executable untouched.
        replacement = tmp_path / 'prepared-tofu-db'
        shutil.copyfile(engine, replacement)
        with replacement.open('ab') as handle:
            handle.write(b'prepared replacement\n')
        replacement.chmod(0o755)
        replacement.replace(engine)
        pin.write_text(hashlib.sha256(engine.read_bytes()).hexdigest())

        def failed_probe(**kwargs):
            raise StorageError('database_unavailable', 'injected transport failure', retryable=True)

        monkeypatch.setattr(client, 'health', failed_probe)
        with runtime._ready_condition:
            recovered = runtime._ready_condition.wait_for(
                lambda: runtime.ready and supervisor.client is not client, timeout=15.0)
        assert recovered, runtime.status()
        assert not runtime.status()['degraded']
        assert pid_is_alive(first['pid'])
        assert read_connection(service)['pid'] == first['pid']
        recovered_client = runtime.client(write=True)
        assert recovered_client.query('conversation.get', {
            'conv_id': 'reattach-survivor', 'user_id': 1,
        })['metadata']['title'] == 'Preserved data'
        recovered_client.command('conversation.create', {
            'conv_id': 'reattach-new', 'user_id': 1, 'title': 'Writes recovered',
        }, 'create-reattach-new')
        assert recovered_client.query('conversation.get', {
            'conv_id': 'reattach-new', 'user_id': 1,
        })['metadata']['title'] == 'Writes recovered'
    finally:
        if runtime is not None:
            runtime.stop()
        service.stop()


def test_resident_engine_crash_after_a_staged_deployment_relaunches_the_admitted_engine(
    tmp_path, native_environment, monkeypatch,
):
    """2026-10-27 topology: resident host, attached application, deployment, crash.

    A live preparation replaced and re-pinned the engine path while the
    admitted engine served. The host must relaunch the admitted bytes after the
    engine dies, and the application must re-attach to them; refusing the
    relaunch latched storage for 5 h 23 min.
    """
    from lib.storage.runtime import StorageRuntime
    from lib.storage.supervisor import StorageSupervisor

    engine = tmp_path / 'tofu-db'
    shutil.copyfile(native_environment['TOFUDB_BIN'], engine)
    engine.chmod(0o755)
    admitted = native_environment['TOFUDB_EXPECTED_SHA256']
    pin = tmp_path / 'tofu-db.sha256'
    pin.write_text(admitted)
    environment = {**native_environment, 'TOFUDB_BIN': str(engine)}
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256')
    service = NativeStorageService(str(tmp_path), sys.executable)
    plan = service.prepare(environment)
    runtime = None
    try:
        connection_path = service.ensure(plan, environment)
        first = wait_for(lambda: read_connection(service))
        supervisor = StorageSupervisor(
            project_root=tmp_path, backend='tofudb', connection_file=connection_path)
        supervisor._liveness_interval_seconds = 0.05
        runtime = StorageRuntime(supervisor)
        runtime.start().command('conversation.create', {
            'conv_id': 'deployment-survivor', 'user_id': 1, 'title': 'Survives deployment',
        }, 'create-deployment-survivor')

        replacement = tmp_path / 'prepared-tofu-db'
        shutil.copyfile(engine, replacement)
        with replacement.open('ab') as handle:
            handle.write(b'prepared replacement\n')
        replacement.chmod(0o755)
        replacement.replace(engine)
        pin.write_text(hashlib.sha256(engine.read_bytes()).hexdigest())

        os.kill(first['pid'], signal.SIGKILL)
        rotated = wait_for(lambda: (doc if (doc := read_connection(service))
                                  and doc['pid'] != first['pid'] else None))
        assert rotated['binarySha256'] == admitted
        assert hashlib.sha256(
            Path(f"/proc/{rotated['pid']}/exe").read_bytes()).hexdigest() == admitted
        with runtime._ready_condition:
            recovered = runtime._ready_condition.wait_for(
                lambda: runtime.ready and supervisor.status()['pid'] == rotated['pid'],
                timeout=60.0)
        assert recovered, runtime.status()
        assert not runtime.status()['degraded']
        assert runtime.client().query('conversation.get', {
            'conv_id': 'deployment-survivor', 'user_id': 1,
        })['metadata']['title'] == 'Survives deployment'
        assert service.record()['state'] != 'failed'
    finally:
        if runtime is not None:
            runtime.stop()
        service.stop()


@pytest.mark.parametrize('manager_end', ['exec_then_stop', 'kill'])
def test_manager_exec_retains_native_and_manager_death_drains(tmp_path, native_environment, manager_end):
    from server_manager import pid_is_alive, read_storage_lease_status
    script = tmp_path / 'manager_fixture.py'
    script.write_text('''import json,os,sys
from serverctl_pkg.native_storage_service import NativeStorageService
s=NativeStorageService(os.environ['TOFU_PROJECT_PATH'],sys.executable)
p=s.prepare(dict(os.environ)); s.ensure(p,dict(os.environ))
print(json.dumps(s.record()),flush=True)
if sys.stdin.readline().strip()=='exec':
    os.execv(sys.executable,[sys.executable,__file__])
s.stop()
''')
    process = subprocess.Popen([sys.executable, str(script)], cwd=tmp_path, env=native_environment,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    service = NativeStorageService(str(tmp_path), sys.executable)
    native = None
    try:
        assert select.select([process.stdout], [], [], NATIVE_SERVICE_PREPARATION_SECONDS)[0]
        first = json.loads(process.stdout.readline())
        native = wait_for(lambda: read_connection(service))
        if manager_end == 'exec_then_stop':
            process.stdin.write('exec\n'); process.stdin.flush()
            assert select.select([process.stdout], [], [], NATIVE_SERVICE_PREPARATION_SECONDS)[0]
            second = json.loads(process.stdout.readline())
            assert second['pid'] == first['pid']
            assert second['serviceId'] == first['serviceId']
            assert read_connection(service)['pid'] == native['pid']
            process.stdin.write('stop\n'); process.stdin.flush()
        else:
            process.kill()  # Only manager PID: do not hide orphan failures with a group kill.
        process.wait(timeout=35)
        wait_for(lambda: not pid_is_alive(native['pid']))
        wait_for(lambda: not host_matches(first))
        assert not read_storage_lease_status(tmp_path / 'data')['held']
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=35)
        service.stop()
        process.stdin.close(); process.stdout.close(); process.stderr.close()


def test_lifecycle_restart_reuses_service_and_config_upgrade_drains_it(tmp_path, native_environment, monkeypatch):
    import server_manager
    from server_manager import LifecycleManager, read_storage_lease_status
    # Minimal application exercises the real manager, lease and native client;
    # frontend preparation has its own failure/old-worker survival contracts.
    (tmp_path / 'server.py').write_text('''import fcntl,json,os,signal,socket,threading
from pathlib import Path
from lib.storage.supervisor import StorageSupervisor
stop=threading.Event()
signal.signal(signal.SIGTERM,lambda *_:stop.set())
lock=open('data/.server.lock','w+')
fcntl.flock(lock,fcntl.LOCK_EX)
lock.write(str(os.getpid())+'@'+socket.gethostname());lock.flush()
s=StorageSupervisor(project_root=Path.cwd(),backend='tofudb')
try:
    s.start()
    Path('data/worker-ready.json').write_text(json.dumps({'pid':os.getpid(),'nativePid':s.status()['pid']}))
    stop.wait()
finally:
    s.stop();lock.close()
''')
    manager = LifecycleManager(str(tmp_path), sys.executable)
    environment = {**native_environment, 'TOFU_MANAGED_BY': 'supervisor', 'TOFU_SERVER_WORKER': '1',
                   'TOFU_RUNTIME_REPLICA': 'off', 'TOFU_SERVER_PYTHON_CACHE': 'off'}
    monkeypatch.setattr(manager, '_worker_environment', lambda *_: dict(environment))
    monkeypatch.setattr(server_manager, 'run_frontend_preflight', lambda *_: (True, ''))

    def ready(pid):
        try:
            value = json.loads((tmp_path / 'data/worker-ready.json').read_text())
            return value if value['pid'] == pid else None
        except (OSError, ValueError):
            return None

    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        started = manager.start(server_env={'PORT': str(port), 'TOFU_DATA_DIR': str(tmp_path / 'data')})
        assert started['ok'], started.get('message', started)
        first = wait_for(lambda: ready(started['launcherPid']))
        service_id = manager._native_storage.record()['serviceId']
        timings = []
        for _ in range(2):
            begin = time.monotonic()
            restarted = manager.restart()
            assert restarted['ok'], restarted
            result = wait_for(lambda: ready(restarted['launcherPid']))
            timings.append(time.monotonic() - begin)
            assert result['nativePid'] == first['nativePid']
            assert manager._native_storage.record()['serviceId'] == service_id
        environment['TOFU_STORAGE_STARTUP_TIMEOUT_SECONDS'] = '120'
        upgraded = manager.restart()
        assert upgraded['ok'], upgraded
        replaced = wait_for(lambda: ready(upgraded['launcherPid']))
        assert replaced['nativePid'] != first['nativePid']
        assert manager._native_storage.record()['serviceId'] != service_id
        print(json.dumps({'minimalWorkerRestartSeconds': timings}))
    finally:
        stopped = manager.stop()
        manager.close()
        assert stopped['ok'], stopped
    assert not read_storage_lease_status(tmp_path / 'data')['held']


_DEPARTING_MANAGER = '''import json,sys
import server_manager
server_manager.run_frontend_preflight=lambda *_: (True, '')
environment=json.loads(sys.argv[2])
manager=server_manager.LifecycleManager(sys.argv[1],sys.executable)
manager._worker_environment=lambda *_: dict(environment)
started=manager.start(server_env=json.loads(sys.argv[3]))
print(json.dumps({key: started.get(key) for key in ('ok','launcherPid','message')}),flush=True)
sys.stdin.readline()
'''

_STORAGE_WORKER = '''import fcntl,hashlib,json,os,signal,socket,threading,time
from pathlib import Path
import lib.storage.supervisor as storage_supervisor
from lib.storage.runtime import StorageRuntime
stop=threading.Event()
signal.signal(signal.SIGTERM,lambda *_:stop.set())
lock=open('data/.server.lock','w+')
fcntl.flock(lock,fcntl.LOCK_EX)
lock.write(str(os.getpid())+'@'+socket.gethostname());lock.flush()
report=Path('data/worker-storage.json')
first_generation=not report.exists()
owner=storage_supervisor.StorageSupervisor(project_root=Path.cwd(),backend='tofudb')
owner._liveness_interval_seconds=0.05
runtime=StorageRuntime(owner)
try:
    client=runtime.start()
    if first_generation:
        client.command('conversation.create',{'conv_id':'preserved-worker','user_id':1,
            'title':'Survives the manager','settings':{}},'create-preserved-worker')
    while not stop.wait(0.05):
        try:
            title=runtime.client().query('conversation.get',
                {'conv_id':'preserved-worker','user_id':1})['metadata']['title']
        except Exception:
            continue
        attachment=hashlib.sha256(owner._token.encode()).hexdigest()
        Path('data/worker-storage.tmp').write_text(json.dumps({'pid':os.getpid(),'attachment':attachment,'title':title}))
        os.replace('data/worker-storage.tmp',report)
finally:
    runtime.stop();lock.close()
'''


@pytest.mark.parametrize('generation', ['unchanged', 'changed'])
def test_successor_manager_restores_storage_for_the_preserved_worker(
        tmp_path, native_environment, monkeypatch, generation):
    """Manager death drains its storage host; the successor must not strand the worker.

    2026-11-05: the watchdog restarted a signalled Supervisor, its host drained
    on manager EOF, and the successor refused to start the changed storage
    generation under the live worker without draining it: 1 h 40 min outage.
    """
    import server_manager
    from server_manager import LifecycleManager, pid_is_alive, read_storage_lease_status
    (tmp_path / 'server.py').write_text(_STORAGE_WORKER)
    environment = {**native_environment, 'TOFU_MANAGED_BY': 'supervisor', 'TOFU_SERVER_WORKER': '1',
                   'TOFU_RUNTIME_REPLICA': 'off', 'TOFU_SERVER_PYTHON_CACHE': 'off'}
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        server_env = {'PORT': str(probe.getsockname()[1]), 'TOFU_DATA_DIR': str(tmp_path / 'data')}
    service = NativeStorageService(str(tmp_path), sys.executable)

    def storage_report():
        try:
            return json.loads((tmp_path / 'data/worker-storage.json').read_text())
        except (OSError, ValueError):
            return None

    departing = subprocess.Popen(
        [sys.executable, '-c', _DEPARTING_MANAGER, str(tmp_path), json.dumps(environment),
         json.dumps(server_env)],
        cwd=tmp_path, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    successor = None
    worker_pid = None
    try:
        assert select.select([departing.stdout], [], [], NATIVE_SERVICE_PREPARATION_SECONDS)[0]
        started = json.loads(departing.stdout.readline())
        assert started['ok'], started
        worker_pid = started['launcherPid']
        first = wait_for(lambda: (report := storage_report()) and report['pid'] == worker_pid and report)
        departed_host = service.record()

        departing.kill()  # Only the manager: the worker is independently owned.
        departing.wait(timeout=10)
        wait_for(lambda: not host_matches(departed_host))
        assert pid_is_alive(worker_pid)

        successor_environment = dict(environment)
        if generation == 'changed':
            # Any storage-identity input changed after the departed host started.
            successor_environment['TOFU_STORAGE_STARTUP_TIMEOUT_SECONDS'] = '120'
        monkeypatch.setattr(server_manager, 'run_frontend_preflight', lambda *_: (True, ''))
        successor = LifecycleManager(str(tmp_path), sys.executable)
        monkeypatch.setattr(successor, '_worker_environment', lambda *_: dict(successor_environment))

        def storage_restored():
            successor.reconcile()
            report = storage_report()
            return report if report and report['attachment'] != first['attachment'] else None

        restored = wait_for(storage_restored)
        assert restored['title'] == 'Survives the manager'
        replacement_host = service.record()
        assert replacement_host['serviceId'] != departed_host['serviceId']
        assert service.owns_lease(read_storage_lease_status(tmp_path / 'data'))
        if generation == 'unchanged':
            assert restored['pid'] == worker_pid
            assert replacement_host['plan'] == departed_host['plan']
        else:
            # The live worker never adopts another generation; it is drained first.
            assert restored['pid'] != worker_pid
            assert not pid_is_alive(worker_pid)
            assert replacement_host['plan'] != departed_host['plan']
    finally:
        if departing.poll() is None:
            departing.kill()
            departing.wait(timeout=10)
        departing.stdin.close()
        departing.stdout.close()
        if successor is not None:
            stopped = successor.stop()
            successor.close()
            assert stopped['ok'], stopped
        elif worker_pid and pid_is_alive(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)
        if host_matches(service.record()):
            service.stop()
    assert not read_storage_lease_status(tmp_path / 'data')['held']


def test_resident_service_preserves_relocated_data_authority(tmp_path, native_environment):
    from lib.storage.supervisor import StorageSupervisor
    from lib.storage.errors import StorageError
    from server_manager import read_storage_lease_status

    relocated = tmp_path / 'relocated' / 'data'
    relocated.mkdir(parents=True)
    environment = {**native_environment, 'TOFU_DATA_DIR': str(relocated)}
    service = NativeStorageService(str(tmp_path), sys.executable)
    plan = service.prepare(environment)
    try:
        connection = service.ensure(plan, environment)
        document = wait_for(lambda: read_connection(service))
        assert document['dataDirectory'] == str(relocated)
        assert read_storage_lease_status(relocated)['held']
        assert not (tmp_path / 'data/tofudb-authority').exists()
        wrong = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=connection)
        with pytest.raises(StorageError, match='data directory mismatch'):
            wrong._external_client(document)
    finally:
        service.stop()
    assert not read_storage_lease_status(relocated)['held']
