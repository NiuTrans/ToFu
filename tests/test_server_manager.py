"""Contracts for the single-owner Tofu server lifecycle manager."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import socket
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import server_manager as sm


pytestmark = pytest.mark.unit


def test_worker_budget_snapshot_exposes_only_scheduling_provenance(monkeypatch):
    payload = (
        b'TOFU_RESOURCE_BUDGET_POLICY_VERSION=old\0'
        b'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS=TOFU_AGENT_WORKERS,'
        b'TOFU_PROCESS_RSS_RECYCLE_MB,SECRET\0'
        b'TOFU_AGENT_WORKERS=4\0'
        b'TOFU_PROCESS_RSS_RECYCLE_MB=6144\0'
        b'LLM_API_KEY=must-not-leak\0'
    )

    class _ProcPath:
        def read_bytes(self):
            return payload

    monkeypatch.setattr(sm, 'Path', lambda _path: _ProcPath())
    snapshot = sm.worker_resource_budget_snapshot(42)

    assert snapshot['policyVersion'] == 'old'
    assert snapshot['policyCurrent'] is False
    assert snapshot['automatic'] == [
        'TOFU_AGENT_WORKERS', 'TOFU_PROCESS_RSS_RECYCLE_MB']
    assert snapshot['values'] == {
        'TOFU_AGENT_WORKERS': '4',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '6144',
    }
    assert 'SECRET' not in repr(snapshot)
    assert 'must-not-leak' not in repr(snapshot)
REAL_RUN_FRONTEND_PREFLIGHT = sm.run_frontend_preflight


@pytest.fixture(autouse=True)
def _scrub_ambient_server_env(monkeypatch):
    """Tests launched from a tofu-spawned shell inherit the LIVE server's
    port and budget knobs (_TOFU_RUNTIME_PORT/PORT make the manager adopt
    the live server's port and then correctly refuse to kill it;
    TOFU_PROCESS_RSS_* leak into profile-budget assertions). Scrub them so
    every test starts hermetic; tests re-set what they need."""
    for name in ('_TOFU_RUNTIME_PORT', 'PORT', 'TOFU_HEARTBEAT_DIR',
                 'TOFU_PROCESS_RSS_RECYCLE_MB', 'TOFU_PROCESS_RSS_RELIEF_MB'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        sm, 'run_frontend_preflight',
        lambda *_args, **_kwargs: (True, ''),
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _project(tmp_path: Path, worker_source: str = '') -> Path:
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    (project / 'server.py').write_text(worker_source or 'raise SystemExit(0)\n')
    (project / 'stop.sh').write_text('#!/bin/sh\nexit 0\n')
    return project


def _status(project: Path, *, running=False, pid=None):
    return {
        'projectPath': str(project.resolve()),
        'running': running,
        'pid': pid,
        'host': sm._hostname() if running else None,
        'sameHost': True if running else None,
        'lockPresent': running,
        'stale': False,
        'processStartTime': 42 if running else None,
        'processCwd': str(project.resolve()) if running else None,
        'projectMatches': True if running else None,
        'cmdline': 'python server.py' if running else None,
    }


def test_monitor_failures_are_logged_with_bounded_repeat_checkpoints(
        tmp_path, monkeypatch, caplog):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project), monitor_interval=0.2)

    class FourTicks:
        calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 4

    manager._stop_event = FourTicks()
    monkeypatch.setattr(
        manager, 'reconcile',
        lambda: (_ for _ in ()).throw(RuntimeError('monitor-boom')),
    )
    monkeypatch.setattr(manager, '_save', lambda: None)

    with caplog.at_level(logging.ERROR, logger='server_manager'):
        manager._monitor_loop()

    records = [
        record for record in caplog.records
        if 'lifecycle monitor reconcile failed' in record.getMessage()
    ]
    assert [record.args[0] for record in records] == [1, 2, 4], (
        'persistent monitor failures need first/power-of-two evidence, not a '
        'silent loop or one log record every monitor tick')
    assert manager._state['observed'] == 'degraded'


def test_manager_log_maintenance_failure_is_observable_but_nonfatal(
        tmp_path, monkeypatch, caplog):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project), monitor_interval=0.2)
    import lib.log_retention as retention

    def fail_file(_path, *, create):
        raise OSError(f'log-file-unavailable create={create}')

    monkeypatch.setattr(retention, 'ensure_private_log_file', fail_file)
    with caplog.at_level(logging.WARNING, logger='server_manager'):
        manager._maintain_process_logs(force=True)

    messages = [record.getMessage() for record in caplog.records]
    assert any('stream=server_console' in message for message in messages)
    assert any('stream=server_manager' in message for message in messages)


def test_frontend_preflight_runs_project_command_with_bounded_contract(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / 'serverctl.py').write_text('# preflight command\n')
    observed = {}

    def fake_run(command, **kwargs):
        observed['command'] = command
        observed['kwargs'] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(sm.subprocess, 'run', fake_run)
    environment = {'PATH': os.environ.get('PATH', '')}

    ok, error = REAL_RUN_FRONTEND_PREFLIGHT(
        str(project), sys.executable, environment, 'automatic recovery')

    assert ok is True
    assert error == ''
    assert observed['command'][1:3] == [
        str(project / 'serverctl.py'), 'prepare-frontend']
    assert observed['command'][-1] == 'automatic recovery'
    assert observed['kwargs']['timeout'] \
        == sm.DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT
    assert observed['kwargs']['env']['TOFU_PROJECT_PATH'] \
        == str(project.resolve())


def test_worker_spawn_refuses_failed_frontend_preflight_before_popen(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(
        sm, 'run_frontend_preflight',
        lambda *_args, **_kwargs: (False, 'authoring digest mismatch'),
    )
    monkeypatch.setattr(
        sm.subprocess, 'Popen',
        lambda *_args, **_kwargs: pytest.fail(
            'worker must not spawn after frontend preflight failure'),
    )

    result = manager._spawn('automatic-recovery')

    assert result['ok'] is False
    assert 'authoring digest mismatch' in result['message']
    assert manager._state['observed'] == 'crashloop'
    assert manager._state['workerFailureCount'] == 0


@pytest.mark.skipif(os.name == 'nt', reason='POSIX flock contract')
def test_storage_lease_status_uses_lock_authority_and_safe_owner_metadata(
        tmp_path):
    import fcntl

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    lock_path = data_dir / '.storage-sidecar.lock'
    lock_path.write_bytes(b'\0')
    (data_dir / '.storage-sidecar-lease.json').write_text(json.dumps({
        'host': sm._hostname(),
        'pid': os.getpid(),
        'started_unix_ms': int(time.time() * 1000),
        'status': 'running',
        'owner_kind': 'offline_maintenance',
        'owner_label': 'SQLite deep clean',
    }), encoding='utf-8')

    with lock_path.open('r+b') as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = sm.read_storage_lease_status(data_dir)
        assert held['held'] is True
        assert held['kind'] == 'offline_maintenance'
        assert held['label'] == 'SQLite deep clean'
        assert held['pid'] == os.getpid()
        assert held['holderVerified'] is True

    # A stale running stamp is diagnostic history, never lock authority.
    assert sm.read_storage_lease_status(data_dir)['held'] is False


def test_start_queues_offline_maintenance_without_spawning_or_crashloop(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(
        sm, 'read_lock_status', lambda _path: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _port: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_args, **_kwargs: False)
    manager = sm.LifecycleManager(str(project))
    lease = {
        'held': True,
        'kind': 'offline_maintenance',
        'label': 'SQLite deep clean',
        'pid': 4321,
        'host': sm._hostname(),
        'startedAt': time.time() - 60,
        'ageSeconds': 60.0,
        'holderVerified': True,
    }
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: dict(lease))
    monkeypatch.setattr(
        manager, '_spawn',
        lambda _source: pytest.fail('maintenance must prevent worker spawn'))

    result = manager.start(source='test')

    assert result['ok'] is True
    assert result['waitingForMaintenance'] is True
    assert result['observed'] == 'maintenance'
    assert result['desired'] == 'running'
    assert result['recentFailureCount'] == 0
    assert result['storageLease']['label'] == 'SQLite deep clean'
    assert 'resume automatically' in result['message']

    lease['held'] = False
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source: spawned.append(source) or {'ok': True})
    manager.reconcile()

    assert spawned == ['storage-maintenance-complete']
    assert manager._state['storageBlocker'] == {}
    assert manager._state['consecutiveFailures'] == 0


def test_restart_waits_for_just_stopped_sidecar_lease(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    held = {
        'held': True,
        'kind': 'storage_sidecar',
        'label': 'Storage sidecar',
        'pid': 4321,
    }
    leases = iter([dict(held), dict(held), {'held': False}])
    sleeps = []
    starts = []

    monkeypatch.setattr(sm, 'read_lock_status', lambda _path: _status(project))
    monkeypatch.setattr(
        manager, 'stop',
        lambda **_kwargs: {'ok': True, 'message': 'stopped cleanly'},
    )
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: next(leases))
    monkeypatch.setattr(sm.time, 'sleep', lambda delay: sleeps.append(delay))
    monkeypatch.setattr(
        manager, 'start',
        lambda **kwargs: starts.append(kwargs) or {'ok': True},
    )

    result = manager.restart(source='test')

    assert result['ok'] is True
    assert len(sleeps) == 2
    assert starts == [{
        'server_args': [],
        'server_env': {},
        'source': 'test',
        'explicit': True,
    }]


def test_application_probe_combines_identity_and_readiness(monkeypatch):
    calls = []

    def fake_read(url, **_kwargs):
        calls.append(url)
        if url.endswith('/api/health'):
            return 200, {'ok': True, 'pid': 42}
        return 503, {
            'ok': False,
            'pid': 42,
            'ready': False,
            'state': 'starting',
            'storage': {'ready': False, 'state': 'restarting'},
            'error': {
                'code': 'database_unavailable',
                'message': 'Application storage is not ready',
            },
        }

    monkeypatch.setattr(sm, '_read_probe_json', fake_read)

    probe = sm.probe_application_readiness(
        15000, 42, preferred_scheme='http')

    assert [url.rsplit('/', 1)[-1] for url in calls] == ['ready']
    assert probe['health'] is True
    assert probe['liveness'] is True
    assert probe['ready'] is False
    assert probe['readinessState'] == 'starting'
    assert probe['storageState'] == 'restarting'
    assert probe['url'] is None
    assert 'database_unavailable' in probe['readinessError']
    assert 'lifecycle=starting, storage=restarting' in probe['readinessError']


def test_application_probe_falls_back_for_legacy_readiness_payload(monkeypatch):
    calls = []

    def fake_read(url, **_kwargs):
        calls.append(url)
        if url.endswith('/api/health'):
            return 200, {'ok': True, 'pid': 42}
        return 200, {
            'ok': True,
            'ready': True,
            'state': 'ready',
            'storage': {'ready': True, 'state': 'ready'},
        }

    monkeypatch.setattr(sm, '_read_probe_json', fake_read)

    probe = sm.probe_application_readiness(
        15000, 42, preferred_scheme='http')

    assert [url.rsplit('/', 1)[-1] for url in calls] == ['ready', 'health']
    assert probe['liveness'] is True
    assert probe['ready'] is True
    assert probe['pidMatches'] is True


def test_probe_json_preserves_structured_http_error_body(monkeypatch):
    body = json.dumps({
        'ok': False,
        'ready': False,
        'state': 'starting',
        'storage': {'ready': False, 'state': 'restarting'},
    }).encode()
    error = sm.urllib.error.HTTPError(
        'http://127.0.0.1:15000/api/ready',
        503,
        'Service Unavailable',
        {},
        io.BytesIO(body),
    )
    monkeypatch.setattr(
        sm.urllib.request, 'urlopen',
        lambda *_a, **_k: (_ for _ in ()).throw(error))

    status, payload = sm._read_probe_json(
        'http://127.0.0.1:15000/api/ready', timeout=1.0)

    assert status == 503
    assert payload['ready'] is False
    assert payload['storage']['state'] == 'restarting'


def test_live_but_unready_worker_degrades_without_wedge_restart(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    live = _status(project, running=True, pid=4321)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: live)
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [4321])
    manager = sm.LifecycleManager(str(project))
    probe = {
        'health': True,
        'liveness': True,
        'ready': False,
        'scheme': 'http',
        'livenessError': '',
        'readinessError': (
            'application readiness failed: lifecycle=starting, '
            'storage=restarting, HTTP 503'),
        'readinessState': 'starting',
        'storageState': 'restarting',
    }
    monkeypatch.setattr(manager, '_http_probe', lambda _pid: dict(probe))
    monkeypatch.setattr(
        manager, '_heartbeat_age',
        lambda _pid: sm.DEFAULT_WEDGE_STALE + 1000)
    monkeypatch.setattr(
        manager, '_terminate',
        lambda *_a, **_k: pytest.fail(
            'dependency readiness must not trigger wedge termination'))
    manager._state['wedgeSince'] = 1.0

    status = manager.status(probe_health=True)
    manager.reconcile()

    assert status['observed'] == 'degraded'
    assert status['health'] is True
    assert status['liveness'] is True
    assert status['ready'] is False
    assert status['readinessState'] == 'starting'
    assert status['storageState'] == 'restarting'
    assert 'storage=restarting' in status['lastError']
    assert manager._state['observed'] == 'degraded'
    assert manager._state['wedgeSince'] == 0.0
    assert 'storage=restarting' in manager._state['lastError']


def test_cgroup_oom_counter_reader_supports_events_file(tmp_path, monkeypatch):
    events = tmp_path / 'memory.events'
    events.write_text('low 0\noom 4\noom_kill 3\n')
    monkeypatch.setattr(sm, 'CGROUP_OOM_EVENT_PATHS', (str(events),))

    assert sm.cgroup_oom_kill_count() == 3


def test_worker_rss_recycle_limit_adapts_to_cgroup(tmp_path, monkeypatch):
    memory_max = tmp_path / 'memory.max'
    memory_max.write_text(str(1024 * 1024 * 1024))
    monkeypatch.setattr(sm, 'CGROUP_MEMORY_LIMIT_PATHS', (str(memory_max),))

    assert sm.worker_rss_recycle_limit_bytes() == int(
        1024 * 1024 * 1024 * sm.DEFAULT_WORKER_RSS_CGROUP_FRACTION)
    assert sm.worker_rss_recycle_limit_bytes('256') == 256 * sm.MIB
    assert sm.worker_rss_recycle_limit_bytes('0') == 0


def test_worker_rss_recycle_limit_uses_deployment_profile_without_cgroup(
        monkeypatch):
    monkeypatch.setattr(sm, 'cgroup_memory_limit_bytes', lambda: None)
    monkeypatch.setattr(
        sm, 'deployment_resource_default',
        lambda _name, environment: (
            8192 if environment.get('TOFU_DEPLOYMENT_MODE') == 'distributed'
            else 3072))
    assert sm.worker_rss_recycle_limit_bytes(
        environment={}) == 3072 * sm.MIB
    assert sm.worker_rss_recycle_limit_bytes(
        environment={'TOFU_DEPLOYMENT_MODE': 'distributed'},
    ) == 8192 * sm.MIB


def test_manager_recycles_owned_worker_above_rss_ceiling(tmp_path, monkeypatch):
    project = _project(tmp_path)
    alive = {'value': True}

    def status(_project_path):
        return _status(project, running=alive['value'],
                       pid=4321 if alive['value'] else None)

    monkeypatch.setattr(sm, 'read_lock_status', status)
    monkeypatch.setattr(sm, 'listener_pids',
                        lambda _port: [4321] if alive['value'] else [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: alive['value'])
    monkeypatch.setattr(sm, 'proc_rss_bytes', lambda _pid: 101 * sm.MIB)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 7)
    manager = sm.LifecycleManager(str(project))
    manager.worker_rss_recycle_bytes = 100 * sm.MIB
    manager._state['lastCgroupOomKillCount'] = 7
    terminated = []

    def terminate(worker_status, **_kwargs):
        terminated.append(worker_status['pid'])
        alive['value'] = False
        return True, False, 'stopped cleanly'

    monkeypatch.setattr(manager, '_terminate', terminate)
    manager.reconcile()

    assert terminated == [4321]
    assert manager._state['lastExitCause'] == 'manager_rss_recycle'
    assert manager._state['lastMemoryRecycleRssBytes'] == 101 * sm.MIB
    assert manager._state['workerRssBytes'] == 101 * sm.MIB
    assert manager._state['restartCount'] == 1
    assert manager._state['failureHistory']
    assert 'graceful SIGTERM' in manager._state['lastFailureReason']


def test_adopts_existing_worker_without_restart(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project, running=True, pid=1234))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [1234])
    manager = sm.LifecycleManager(str(project))
    assert manager.status()['desired'] == 'running'
    assert manager.status()['observed'] == 'running'
    assert manager.status()['launchSource'] == 'adopted'
    assert manager.status()['worker']['pid'] == 1234
    assert (project / 'data' / '.tofu_guard_disabled').exists()


def test_adoption_reconciles_stale_port_from_live_worker_argv(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    initial = sm.LifecycleManager(str(project))._default_state()
    initial.update({
        'desired': 'running',
        'port': 15599,
        'serverEnv': {'PORT': '15599'},
    })
    sm._atomic_json(project / 'data' / 'server-manager-state.json', initial)
    live = _status(project, running=True, pid=1234)
    live['cmdline'] = 'python server.py --no-tls --port 15000'
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: live)
    monkeypatch.setattr(sm, 'port_accepts', lambda port, **_k: port == 15000)
    monkeypatch.setattr(sm, 'listener_pids',
                        lambda port: [1234] if port == 15000 else [])

    manager = sm.LifecycleManager(str(project))

    status = manager.status(probe_health=False)
    persisted = json.loads(manager.state_path.read_text())
    assert status['port'] == 15000
    assert status['observed'] == 'running'
    assert status['serverEnv']['PORT'] == '15000'
    assert persisted['lastPortReconciledFrom'] == 15599
    assert persisted['lastPortReconciledSource'] == 'argv'


def test_start_is_idempotent_for_adopted_worker(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project, running=True, pid=1234))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [1234])
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(sm.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('duplicate spawn'))
    result = manager.start(server_args=['--port', '19999'], source='test')
    assert result['ok'] is True
    assert result['alreadyRunning'] is True
    assert result['pid'] == 1234
    assert result['foreignListenerPids'] == []
    assert manager.port == 15000
    assert 'options were ignored' in result['message']


def test_unknown_port_owner_fails_closed(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    manager = sm.LifecycleManager(str(project))
    result = manager.start(source='test')
    assert result['ok'] is False
    assert result['observed'] == 'conflict'
    assert 'unknown process' in result['message']


def test_starting_worker_listener_is_not_misclassified_as_foreign(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [4321])
    monkeypatch.setattr(sm, 'proc_start_ticks', lambda _pid: 99)
    monkeypatch.setattr(sm, 'proc_cwd', lambda _pid: str(project.resolve()))
    monkeypatch.setattr(sm, 'proc_cmdline', lambda _pid: 'python server.py')
    monkeypatch.setattr(sm, 'proc_env_value',
                        lambda _pid, name: 'supervisor' if name == 'TOFU_MANAGED_BY' else None)
    manager = sm.LifecycleManager(str(project))
    manager._state['worker'] = {'pid': 4321, 'processStartTime': 99}

    assert manager._port_conflict(_status(project)) == (False, [])


def test_starting_listener_with_identity_mismatch_still_fails_closed(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [4321])
    monkeypatch.setattr(sm, 'proc_start_ticks', lambda _pid: 100)
    monkeypatch.setattr(sm, 'proc_cwd', lambda _pid: str(project.resolve()))
    monkeypatch.setattr(sm, 'proc_cmdline', lambda _pid: 'python server.py')
    monkeypatch.setattr(sm, 'proc_env_value', lambda _pid, _name: 'supervisor')
    manager = sm.LifecycleManager(str(project))
    manager._state['worker'] = {'pid': 4321, 'processStartTime': 99}

    assert manager._port_conflict(_status(project)) == (True, [4321])


def test_start_rejects_malformed_http_configuration(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    assert manager.start(server_args='--port 7')['ok'] is False
    assert manager.start(server_env=['PORT=7'])['ok'] is False


def test_production_environment_rejects_pytest_storage_but_test_project_allows_it():
    pytest_data = (
        '/tmp/tofu-pytest-runs-1000/'
        'tofu-test-data-gw3-pid-979421-fixture')

    error = sm.production_server_environment_error(
        '/srv/tofu', {'TOFU_DATA_DIR': pytest_data})

    assert 'TOFU_DATA_DIR' in error
    assert 'pytest-owned temporary storage' in error
    assert sm.production_server_environment_error(
        '/tmp/pytest-of-user/pytest-7/project',
        {'TOFU_DATA_DIR': pytest_data},
    ) == ''
    assert sm.production_server_environment_error(
        '/srv/tofu', {'TOFU_DATA_DIR': '/var/lib/tofu'}) == ''


def test_manager_quarantines_persisted_pytest_environment_before_adoption(
        monkeypatch):
    contaminated = {
        'version': sm.STATE_VERSION,
        'desired': 'running',
        'observed': 'running',
        'serverArgs': ['--port', '15599'],
        'serverEnv': {
            'PORT': '15599',
            'TOFU_DATA_DIR': (
                '/tmp/tofu-pytest-runs-1000/'
                'tofu-test-data-gw3-pid-979421-fixture'),
        },
        'worker': {},
    }
    persisted = []
    monkeypatch.setattr(sm, '_read_json', lambda _path: dict(contaminated))
    monkeypatch.setattr(
        sm, '_atomic_json',
        lambda _path, payload: persisted.append(
            json.loads(json.dumps(payload))))
    monkeypatch.setattr(sm, 'project_server_env', lambda _project: {'PORT': '15000'})
    monkeypatch.setattr(
        sm, 'read_lock_status',
        lambda project: _status(Path(project)))

    manager = sm.LifecycleManager('/srv/tofu')

    assert manager._state['serverArgs'] == []
    assert manager._state['serverEnv'] == {'PORT': '15000'}
    assert manager._state['port'] == 15000
    quarantine = manager._state['environmentQuarantine']
    assert quarantine['discardedKeys'] == ['PORT', 'TOFU_DATA_DIR']
    assert 'pytest-owned temporary storage' in quarantine['reason']
    assert persisted
    assert persisted[0]['serverEnv'] == {'PORT': '15000'}


def test_restart_rejects_pytest_environment_before_stopping_worker(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    manager.project = '/srv/tofu'
    monkeypatch.setattr(
        manager, 'stop',
        lambda **_kwargs: pytest.fail('unsafe restart must not stop a worker'))

    result = manager.restart(server_env={
        'TOFU_DATA_DIR': (
            '/tmp/tofu-pytest-runs-1000/'
            'tofu-test-data-gw3-pid-979421-fixture'),
    })

    assert result['ok'] is False
    assert 'pytest-owned temporary storage' in result['message']


def test_spawn_refuses_contaminated_environment_before_preflight_or_popen(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    manager.project = '/srv/tofu'
    manager._state['serverEnv'] = {
        'TOFU_DATA_DIR': (
            '/tmp/tofu-pytest-runs-1000/'
            'tofu-test-data-gw3-pid-979421-fixture'),
    }
    monkeypatch.setattr(
        sm, 'run_frontend_preflight',
        lambda *_args, **_kwargs: pytest.fail('unsafe spawn reached preflight'))
    monkeypatch.setattr(
        sm.subprocess, 'Popen',
        lambda *_args, **_kwargs: pytest.fail('unsafe spawn reached Popen'))

    result = manager._spawn('test-contamination')

    assert result['ok'] is False
    assert manager._state['desired'] == 'stopped'
    assert 'pytest-owned temporary storage' in result['message']


def test_existing_worker_with_pytest_storage_is_never_adopted(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    manager.project = '/srv/tofu'
    live = _status(Path('/srv/tofu'), running=True, pid=4321)

    def process_environment(_pid, name):
        if name == 'TOFU_DATA_DIR':
            return (
                '/tmp/tofu-pytest-runs-1000/'
                'tofu-test-data-gw3-pid-979421-fixture')
        return None

    monkeypatch.setattr(sm, 'proc_env_value', process_environment)

    error = manager._identity_error(live)

    assert error is not None
    assert 'unsafe environment' in error
    assert 'pytest-owned temporary storage' in error


def test_stop_persists_desired_before_signal(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    live = _status(project, running=True, pid=2222)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: live)

    def fake_terminate(_status_value, **_kwargs):
        persisted = json.loads(manager.state_path.read_text())
        assert persisted['desired'] == 'stopped'
        assert persisted['observed'] == 'stopping'
        assert persisted['pendingWorkerExitIntent']['pid'] == 2222
        assert persisted['pendingWorkerExitIntent']['reason'] == 'manual'
        return True, False, 'stopped cleanly'

    monkeypatch.setattr(manager, '_terminate', fake_terminate)
    result = manager.stop(source='test')
    assert result['ok'] is True
    assert json.loads(manager.state_path.read_text())['desired'] == 'stopped'


def test_abnormal_exit_schedules_then_retries(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._record_failure()
    assert manager._state['consecutiveFailures'] == 1
    assert manager._state['nextRetryAt'] > time.time()
    manager._state['nextRetryAt'] = time.time() - 1
    spawned = []
    monkeypatch.setattr(manager, '_spawn', lambda source: spawned.append(source) or {'ok': True})
    manager._record_failure()
    assert spawned == ['automatic-recovery']


def test_failure_window_catches_repeated_post_health_crashes(
        tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'

    for index in range(manager.max_failures):
        # Model a worker that recovered health between crashes: the old
        # consecutive counter resets, but the rolling incident window must not.
        manager._state['consecutiveFailures'] = 0
        manager._state['nextRetryAt'] = 0.0
        manager._state['activeFailureAt'] = 0.0
        manager._record_failure()
        clock[0] += 10.0
        if index < manager.max_failures - 1:
            assert manager._state['observed'] == 'starting'

    status = manager.status()
    assert status['observed'] == 'crashloop'
    assert status['recentFailureCount'] == manager.max_failures
    assert status['nextRetryAt'] == 0.0


def test_crashloop_reconcile_does_not_inflate_failure_diagnostics(
        tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'

    for _index in range(manager.max_failures):
        manager._state['nextRetryAt'] = 0.0
        manager._record_failure()
        clock[0] += 10.0

    diagnostics = {
        key: manager._state[key]
        for key in (
            'restartCount', 'consecutiveFailures', 'failureHistory',
            'lastFailureAt', 'lastFailureReason',
        )
    }
    clock[0] += manager.failure_window * 2

    manager._record_failure()

    assert manager._state['observed'] == 'crashloop'
    assert {
        key: manager._state[key]
        for key in diagnostics
    } == diagnostics


def test_failure_window_expires_old_incidents(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._state['failureHistory'] = [100.0, 200.0, 300.0, 400.0]

    manager._record_failure()

    assert manager.status()['recentFailureCount'] == 1
    assert manager._state['observed'] == 'starting'


def test_failure_is_correlated_with_cgroup_oom_counter_delta(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 8)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._state['lastCgroupOomKillCount'] = 7

    manager._record_failure()

    status = manager.status()
    assert status['lastExitCause'] == 'cgroup_oom_event'
    assert status['lastCgroupOomDelta'] == 1
    assert 'advanced by 1' in status['lastFailureReason']


def test_failure_without_oom_delta_remains_unexpected_exit(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 8)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._state['lastCgroupOomKillCount'] = 8

    manager._record_failure()

    assert manager.status()['lastExitCause'] == 'unexpected_exit'


def _install_clean_worker_marker(
        manager, project, *, pid=987654, reason='signal', clean_ts=None):
    stamp = time.time() if clean_ts is None else clean_ts
    marker = project / 'data' / '.server_shutdown.json'
    marker.write_text(json.dumps({
        'state': 'clean',
        'pid': pid,
        'host': sm._hostname(),
        'clean_ts': stamp,
        'reason': reason,
    }))
    manager._state['worker'] = {
        'pid': pid,
        'host': sm._hostname(),
        'spawnedAt': stamp - 1,
        'processStartTime': 42,
        'processCwd': str(project.resolve()),
    }
    manager._state['desired'] = 'running'
    return marker


def test_clean_exit_recovers_immediately_without_spending_failure_budget(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    _install_clean_worker_marker(manager, project, reason='restart')
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source: spawned.append(source) or {'ok': True},
    )

    manager.reconcile()

    assert spawned == ['clean-restart-recovery']
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['restartCount'] == 0
    assert manager._state['failureHistory'] == []
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['lastWorkerExitKind'] == 'planned'
    assert manager._state['lastWorkerExitReason'] == 'restart'


def test_generic_clean_signal_without_manager_intent_spends_failure_budget(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 0)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    _install_clean_worker_marker(manager, project, reason='signal')

    manager.reconcile()

    assert manager._state['plannedExitCount'] == 0
    assert manager._state['workerFailureCount'] == 1
    assert manager._state['lastExitCause'] == 'unintended_signal'
    assert manager._state['failureHistory']


def test_manager_restart_intent_proves_generic_signal_was_planned(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    stamp = time.time()
    _install_clean_worker_marker(
        manager, project, reason='signal', clean_ts=stamp)
    manager._state['pendingWorkerExitIntent'] = {
        'pid': 987654,
        'host': sm._hostname(),
        'reason': 'restart',
        'requestedAt': stamp,
        'recoverySource': 'manager-restart',
    }
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source: spawned.append(source) or {'ok': True},
    )

    manager.reconcile()

    assert spawned == ['manager-restart']
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['lastWorkerExitReason'] == 'restart'


def test_clean_exit_is_counted_once_while_orphan_sidecar_lease_drains(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_launcher_is_alive', lambda: False)
    _install_clean_worker_marker(manager, project, reason='restart')
    lease_states = iter([
        {'held': True, 'kind': 'storage_sidecar', 'pid': 222},
        {'held': True, 'kind': 'storage_sidecar', 'pid': 222},
        {'held': False},
    ])
    monkeypatch.setattr(
        manager, '_active_storage_lease', lambda: next(lease_states))
    spawned = []

    def _spawn(source):
        manager._state['pendingRecoverySource'] = ''
        spawned.append(source)
        return {'ok': True}

    monkeypatch.setattr(manager, '_spawn', _spawn)

    manager.reconcile()
    manager.reconcile()
    manager.reconcile()

    assert spawned == ['clean-restart-recovery']
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['failureHistory'] == []


def test_manual_clean_exit_changes_durable_desired_state_to_stopped(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    _install_clean_worker_marker(manager, project, reason='manual')
    monkeypatch.setattr(
        manager, '_spawn',
        lambda _source: pytest.fail('manual shutdown must not respawn'),
    )

    manager.reconcile()

    assert manager._state['desired'] == 'stopped'
    assert manager._state['observed'] == 'stopped'
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['workerFailureCount'] == 0
    persisted = json.loads(manager.state_path.read_text())
    assert persisted['desired'] == 'stopped'


def test_stale_or_wrong_pid_clean_marker_cannot_hide_worker_failure(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 0)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    marker = _install_clean_worker_marker(
        manager, project, pid=987654, reason='signal')
    payload = json.loads(marker.read_text())
    payload['pid'] = 987655
    marker.write_text(json.dumps(payload))

    manager.reconcile()

    assert manager._state['plannedExitCount'] == 0
    assert manager._state['workerFailureCount'] == 1
    assert manager._state['lastExitCause'] == 'unexpected_exit'


def test_real_failure_is_counted_before_orphan_sidecar_lease_wait(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 0)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._state['worker'] = {
        'pid': 987654,
        'host': sm._hostname(),
        'spawnedAt': time.time() - 10,
    }
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {
        'held': True,
        'kind': 'storage_sidecar',
        'label': 'Storage sidecar',
        'pid': 222,
    })

    manager.reconcile()

    assert manager._state['workerFailureCount'] == 1
    assert manager._state['failureHistory']
    assert manager._state['storageBlocker']['resumeSource'] \
        == 'automatic-recovery'
    assert manager._state['nextRetryAt'] > time.time()


def test_explicit_failure_budget_reset_preserves_lifetime_diagnostics(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    manager._state.update({
        'failureHistory': [1.0, 2.0],
        'consecutiveFailures': 2,
        'activeFailureAt': 2.0,
        'nextRetryAt': 3.0,
        'restartCount': 9,
        'lastFailureAt': 2.0,
    })

    manager._clear_failure_budget()

    assert manager._state['failureHistory'] == []
    assert manager._state['consecutiveFailures'] == 0
    assert manager._state['activeFailureAt'] == 0.0
    assert manager._state['nextRetryAt'] == 0.0
    assert manager._state['restartCount'] == 9
    assert manager._state['lastFailureAt'] == 2.0


def test_persisted_manual_stop_survives_manager_restart(tmp_path, monkeypatch):
    project = _project(tmp_path)
    state = {
        **sm.LifecycleManager(str(project))._default_state(),
        'desired': 'stopped',
        'observed': 'stopping',
    }
    sm._atomic_json(project / 'data' / 'server-manager-state.json', state)
    monkeypatch.setattr(sm, 'read_lock_status',
                        lambda _p: _status(project, running=True, pid=4321))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [4321])
    manager = sm.LifecycleManager(str(project))
    assert manager.status()['desired'] == 'stopped'
    assert manager.status()['observed'] == 'stopping'
    # Desired state remains the durable instruction; reconcile completes the
    # stop that was interrupted by the manager restart.
    terminated = []
    monkeypatch.setattr(manager, '_terminate',
                        lambda st, **kw: terminated.append(st['pid']) or
                        (True, False, 'stopped cleanly'))
    manager.reconcile()
    assert terminated == [4321]
    assert manager.status()['desired'] == 'stopped'


def test_external_owner_is_conflict_never_adopted(tmp_path, monkeypatch):
    project = _project(tmp_path)
    external = _status(project, running=True, pid=7654)
    external['externalOwner'] = 'desktop'
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: external)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [7654])
    manager = sm.LifecycleManager(str(project))
    status = manager.status()
    assert status['observed'] == 'conflict'
    assert 'desktop' in status['lastError']
    result = manager.start(source='test')
    assert result['ok'] is False
    assert 'desktop' in result['message']


def test_project_env_controls_default_managed_port(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / '.env').write_text(
        'PORT=16789\nBIND_HOST=127.0.0.1\n'
        'TOFU_DEPLOYMENT_MODE=personal\n'
        'TOFU_MALLOC_ARENA_MAX=3\nTOFU_NUMERIC_THREADS=2\n'
        f'TOFU_DATA_DIR={project / "state-volume"}\n'
        'TOFU_STORAGE_RPC_CAPACITY=5\nUNSAFE=x\n')
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    assert manager.port == 16789
    assert manager._state['serverEnv'] == {
        'PORT': '16789',
        'BIND_HOST': '127.0.0.1',
        'TOFU_DEPLOYMENT_MODE': 'personal',
        'TOFU_MALLOC_ARENA_MAX': '3',
        'TOFU_NUMERIC_THREADS': '2',
        'TOFU_DATA_DIR': str(project / 'state-volume'),
        'TOFU_STORAGE_RPC_CAPACITY': '5',
    }


def test_project_env_deployment_profile_reaches_manager_memory_budget(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / '.env').write_text('TOFU_DEPLOYMENT_MODE=distributed\n')
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_memory_limit_bytes', lambda: None)

    manager = sm.LifecycleManager(str(project))

    assert manager._state['serverEnv']['TOFU_DEPLOYMENT_MODE'] == 'distributed'
    assert manager.worker_rss_recycle_bytes == 8192 * sm.MIB


def test_spawn_translates_project_allocator_override_after_env_merge(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / '.env').write_text('TOFU_MALLOC_ARENA_MAX=3\n')
    monkeypatch.setenv('MALLOC_ARENA_MAX', '19')
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'proc_start_ticks', lambda _pid: 99)
    child_environment = {}

    class _FakeProcess:
        pid = 4321

    def fake_popen(*_args, **kwargs):
        child_environment.update(kwargs['env'])
        return _FakeProcess()

    monkeypatch.setattr(sm.subprocess, 'Popen', fake_popen)
    manager = sm.LifecycleManager(str(project))

    result = manager._spawn('test')

    assert result['ok'] is True
    assert child_environment['TOFU_PROJECT_PATH'] == str(project.resolve())
    assert child_environment['TOFU_MALLOC_ARENA_MAX'] == '3'
    assert child_environment['MALLOC_ARENA_MAX'] == '3'


_WORKER = r'''import fcntl, json, os, signal, socket, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
resident_blob = bytearray(int(os.environ.get('WORKER_ALLOC_MB', '0')) * 1024 * 1024)
for resident_offset in range(0, len(resident_blob), 4096):
    resident_blob[resident_offset] = 1
data = os.path.join(os.getcwd(), 'data')
os.makedirs(data, exist_ok=True)
lock_path = os.path.join(data, '.server.lock')
lock = open(lock_path, 'w+')
fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
lock.write('%d@%s\n' % (os.getpid(), socket.gethostname()))
lock.flush()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a): pass
    def do_GET(self):
        payload = (
            {'ok': True, 'ready': True, 'state': 'ready',
             'storage': {'ready': True, 'state': 'ready'}}
            if self.path == '/api/ready' else
            {'ok': True, 'pid': os.getpid(), 'bootId': str(os.getpid())}
        )
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
def handle_sigterm(*_args):
    if os.environ.get('WORKER_CLEAN_SIGNAL') == '1':
        marker_path = os.path.join(data, '.server_shutdown.json')
        temporary_path = marker_path + '.tmp'
        with open(temporary_path, 'w', encoding='utf-8') as stream:
            json.dump({
                'state': 'clean',
                'pid': os.getpid(),
                'host': socket.gethostname(),
                'clean_ts': time.time(),
                'reason': 'signal',
            }, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, marker_path)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, handle_sigterm)
HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
'''


def test_real_worker_start_restart_stop_and_manual_stop_sticks(tmp_path, monkeypatch):
    port = _free_port()
    project = _project(tmp_path, _WORKER)
    monkeypatch.setenv('PORT', str(port))
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    manager = sm.LifecycleManager(str(project), sys.executable, monitor_interval=0.2)
    first_pid = None
    try:
        started = manager.start(source='integration')
        assert started['ok'] is True
        deadline = time.time() + 10
        while time.time() < deadline:
            current = manager.status(probe_health=True)
            if current.get('running') and current.get('health'):
                break
            time.sleep(0.1)
        assert current['observed'] == 'running'
        first_pid = current['pid']

        restarted = manager.restart(source='integration')
        assert restarted['ok'] is True
        deadline = time.time() + 10
        while time.time() < deadline:
            current = manager.status(probe_health=True)
            if current.get('running') and current.get('health') and current.get('pid') != first_pid:
                break
            time.sleep(0.1)
        assert current['pid'] != first_pid

        stopped = manager.stop(source='integration')
        assert stopped['ok'] is True
        assert manager.status()['desired'] == 'stopped'
        manager.reconcile()
        assert manager.status()['observed'] == 'stopped'
        assert not sm.port_accepts(port)
    finally:
        status = sm.read_lock_status(str(project))
        if status.get('running'):
            try:
                os.kill(int(status['pid']), 9)
            except OSError:
                pass
        manager.close()


@pytest.mark.serial
def test_real_sigkill_worker_recovers_with_new_pid_and_measured_rto(
        tmp_path, monkeypatch):
    """A real untrappable death must recover, not merely update mock state."""
    port = _free_port()
    project = _project(tmp_path, _WORKER)
    monkeypatch.setenv('PORT', str(port))
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    manager = sm.LifecycleManager(str(project), sys.executable,
                                  monitor_interval=0.2)
    first_pid = None
    try:
        assert manager.start(source='sigkill-integration')['ok'] is True
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current = manager.status(probe_health=True)
            if current.get('running') and current.get('health'):
                break
            time.sleep(0.1)
        assert current.get('health') is True
        first_pid = int(current['pid'])

        os.kill(first_pid, signal.SIGKILL)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            manager.reconcile()
            current = manager.status(probe_health=True)
            if (current.get('running') and current.get('health')
                    and current.get('pid') != first_pid):
                # One healthy reconcile settles the recovery timing fields.
                manager.reconcile()
                current = manager.status(probe_health=True)
                break
            time.sleep(0.1)

        assert current.get('health') is True
        assert current.get('pid') != first_pid
        assert current['launchSource'] == 'automatic-recovery'
        assert current['restartCount'] == 1
        assert current['recentFailureCount'] == 1
        assert 0 < current['lastRecoverySeconds'] <= 20.0
    finally:
        status = sm.read_lock_status(str(project))
        if status.get('running'):
            try:
                os.kill(int(status['pid']), signal.SIGKILL)
            except OSError:
                pass
        manager.close()


@pytest.mark.serial
def test_real_unintended_clean_signal_recovers_with_failure_budget(
        tmp_path, monkeypatch):
    """An external graceful stop lacks intent and cannot bypass crash-looping."""
    port = _free_port()
    project = _project(tmp_path, _WORKER)
    monkeypatch.setenv('PORT', str(port))
    # The miniature worker writes its shutdown certificate to the project's
    # in-tree data directory.  Pin the manager to that same declared authority
    # instead of inheriting pytest's suite-wide isolated TOFU_DATA_DIR.
    monkeypatch.setenv('TOFU_DATA_DIR', str(project))
    monkeypatch.setenv('WORKER_CLEAN_SIGNAL', '1')
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    manager = sm.LifecycleManager(str(project), sys.executable,
                                  monitor_interval=0.2)
    first_pid = None
    try:
        assert manager.start(source='clean-signal-integration')['ok'] is True
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current = manager.status(probe_health=True)
            if current.get('running') and current.get('health'):
                break
            time.sleep(0.1)
        assert current.get('health') is True
        first_pid = int(current['pid'])

        os.kill(first_pid, signal.SIGTERM)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            manager.reconcile()
            current = manager.status(probe_health=True)
            if (current.get('running') and current.get('health')
                    and current.get('pid') != first_pid):
                break
            time.sleep(0.1)

        assert current.get('health') is True
        assert current.get('pid') != first_pid
        assert current['launchSource'] == 'automatic-recovery'
        assert current['workerFailureCount'] == 1
        assert current['plannedExitCount'] == 0
        assert current['restartCount'] == 1
        assert current['recentFailureCount'] == 1
        assert current['lastWorkerExitKind'] == 'failure'
        assert current['lastWorkerExitReason'] == 'unintended_signal'
    finally:
        status = sm.read_lock_status(str(project))
        if status.get('running'):
            try:
                os.kill(int(status['pid']), signal.SIGKILL)
            except OSError:
                pass
        manager.close()


@pytest.mark.serial
def test_real_worker_over_rss_ceiling_is_gracefully_recycled(
        tmp_path, monkeypatch):
    """The stdlib manager must stop a real bloated worker before kernel OOM."""
    port = _free_port()
    project = _project(tmp_path, _WORKER)
    monkeypatch.setenv('PORT', str(port))
    monkeypatch.setenv('WORKER_ALLOC_MB', '48')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '32')
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    manager = sm.LifecycleManager(str(project), sys.executable,
                                  monitor_interval=0.2)
    worker_pid = None
    try:
        assert manager.start(source='rss-integration')['ok'] is True
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current = manager.status(probe_health=True)
            if current.get('running') and current.get('health'):
                break
            time.sleep(0.1)
        assert current.get('health') is True
        worker_pid = int(current['pid'])
        assert sm.proc_rss_bytes(worker_pid) > manager.worker_rss_recycle_bytes

        manager.reconcile()

        assert not sm.pid_is_alive(worker_pid)
        assert manager._state['lastExitCause'] == 'manager_rss_recycle'
        assert manager._state['restartCount'] == 1
        assert manager.status()['recentFailureCount'] == 1
        assert 'graceful SIGTERM' in manager._state['lastFailureReason']
    finally:
        status = sm.read_lock_status(str(project))
        if status.get('running'):
            try:
                os.kill(int(status['pid']), signal.SIGKILL)
            except OSError:
                pass
        manager.close()


def test_server_entry_delegates_before_application_imports():
    source = (Path(__file__).parents[1] / 'server.py').read_text()
    delegate = source.index('\n_delegate_executable_to_manager()\n')
    lock = source.index("_lock_path = os.path.join(_lock_dir, '.server.lock')")
    app_factory = source.index('from lib.app_factory import create_base_app')
    assert delegate < lock < app_factory
    assert "os.environ.get('TOFU_SERVER_WORKER') == '1'" in source
    assert "os.environ.get('_TOFU_REEXEC_PORT')" in source


@pytest.mark.serial
def test_serverctl_process_roundtrip(tmp_path):
    """Exercise the real CLI → detached manager → worker control chain."""
    root = Path(__file__).parents[1]
    project = tmp_path / 'cli-project'
    project.mkdir()
    (project / 'data').mkdir()
    (project / 'logs').mkdir()
    for name in (
            'serverctl.py', 'server_manager.py', 'supervisor.py',
            'supervisor.sh', 'supervisor_protocol.py', 'tofu_dotenv.py',
            'runtime_guards.py'):
        shutil.copy(root / name, project / name)
    (project / 'server.py').write_text(_WORKER)
    (project / 'stop.sh').write_text('#!/bin/sh\nexit 0\n')
    os.chmod(project / 'supervisor.sh', 0o755)
    manager_port = _free_port()
    worker_port = _free_port()
    owner_lease = subprocess.Popen(
        [sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = {
        **os.environ,
        'TOFU_SUPERVISOR_PORT': str(manager_port),
        'TOFU_SUPERVISOR_HOST': '127.0.0.1',
        'TOFU_SUPERVISOR_PYTHON': sys.executable,
        # The helper exits on pipe EOF. Normal test teardown closes it, while
        # an abruptly killed pytest worker closes the same non-inherited FD.
        # Either way the detached watchdog has a per-test ownership boundary.
        'TOFU_SUPERVISOR_OWNER_PID': str(owner_lease.pid),
        'TOFU_PROJECT_PATH': str(project),
        'PORT': str(worker_port),
        'TOFU_HEARTBEAT_DIR': str(tmp_path / 'heartbeat'),
        # This subprocess test has no TTY. Production non-interactive stops
        # consume a one-time UI approval; the internal sentinel models that
        # already-admitted boundary so cleanup can exercise the manager roundtrip.
        'TOFU_LIFECYCLE_GATE_PASSED': 'shutdown',
    }
    watchdog_pid = None
    retired_project = tmp_path / 'retired-cli-project'
    try:
        start = subprocess.run(
            [sys.executable, 'serverctl.py', 'start', '--wait', '10'],
            cwd=project, env=env, capture_output=True, text=True, timeout=30)
        assert start.returncode == 0, start.stdout + start.stderr
        status = subprocess.run(
            [sys.executable, 'serverctl.py', 'status', '--json'],
            cwd=project, env=env, capture_output=True, text=True, timeout=10)
        payload = json.loads(status.stdout)
        assert payload['managerOnline'] is True
        assert payload['projectPath'] == str(project.resolve())
        assert payload['desired'] == 'running'
        assert payload['running'] is True
        assert payload['port'] == worker_port
        watchdog_status = subprocess.run(
            ['bash', 'supervisor.sh', 'status'], cwd=project, env=env,
            capture_output=True, text=True, timeout=15)
        watchdog_match = re.search(
            r'setsid watchdog: RUNNING \(pid=(\d+)', watchdog_status.stdout)
        assert watchdog_status.returncode == 0 and watchdog_match is not None
        watchdog_pid = int(watchdog_match.group(1))

        stop = subprocess.run(
            [sys.executable, 'serverctl.py', 'stop'], cwd=project, env=env,
            capture_output=True, text=True, timeout=30)
        assert stop.returncode == 0, stop.stdout + stop.stderr
        after = subprocess.run(
            [sys.executable, 'serverctl.py', 'status', '--json'], cwd=project,
            env=env, capture_output=True, text=True, timeout=10)
        stopped = json.loads(after.stdout)
        assert stopped['desired'] == 'stopped'
        assert stopped['observed'] == 'stopped'

        # Model the xdist/temporary-checkout owner disappearing without a
        # cooperative ``supervisor.sh stop``. The manager and its self-healing
        # watchdog must release their port/process budget on their own.
        project.rename(retired_project)
        deadline = time.monotonic() + 5.0
        while sm.pid_is_alive(watchdog_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not sm.pid_is_alive(watchdog_pid)
        watchdog_pid = None
    finally:
        if owner_lease.stdin is not None:
            try:
                owner_lease.stdin.close()
            except OSError:
                pass
        try:
            owner_lease.wait(timeout=5)
        except subprocess.TimeoutExpired:
            owner_lease.terminate()
            owner_lease.wait(timeout=5)
        active_project = project if project.is_dir() else retired_project
        if (active_project / 'supervisor.sh').is_file():
            subprocess.run(
                ['bash', 'supervisor.sh', 'stop'], cwd=active_project, env=env,
                capture_output=True, text=True, timeout=30)
        if watchdog_pid is not None and sm.pid_is_alive(watchdog_pid):
            try:
                os.killpg(watchdog_pid, signal.SIGTERM)
            except OSError:
                pass
        status = sm.read_lock_status(str(active_project))
        if status.get('running'):
            try:
                os.kill(int(status['pid']), signal.SIGKILL)
            except OSError:
                pass
