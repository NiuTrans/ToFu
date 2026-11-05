"""Contracts for the single-owner Tofu server lifecycle manager."""

from __future__ import annotations

import io
import errno
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
from tofu_db._loopback import bind_ephemeral_loopback
from tofu_dotenv import MAX_DOTENV_BYTES, load_dotenv_file


pytestmark = pytest.mark.unit


def _resource_denied_probe_transport(
        monkeypatch, *, code=errno.EADDRNOTAVAIL, wrapped=True,
        status=200, payload=None, body=None, read_error=None):
    """Replace only the urllib/socket HTTP boundary; use real probe policy."""
    problem = OSError(code, 'local address allocation denied')
    failure = sm.urllib.error.URLError(problem) if wrapped else problem
    calls = []
    connections = []
    wire = body if body is not None else json.dumps(payload or {
        'ok': True, 'pid': 42, 'ready': True, 'state': 'ready',
        'storage': {'ready': True, 'state': 'ready'},
    }).encode()

    def urlopen(url, **kwargs):
        calls.append((url, kwargs))
        raise failure

    class Response:
        closed = False
        headers = {}
        reason = 'synthetic response'

        def close(self):
            self.closed = True

        def __init__(self):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def read(self, maximum):
            assert maximum == sm.HTTP_PROBE_RESPONSE_LIMIT + 1
            if read_error:
                raise read_error
            return wire[:maximum]

    class Connection:
        def __init__(self, host, port, **options):
            self.host, self.port, self.options = host, port, options
            self.response = Response()
            self.closed = False
            self.requests = []
            connections.append(self)

        def request(self, method, path, headers):
            self.requests.append((method, path, headers))

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr(sm.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(sm.http.client, 'HTTPConnection', Connection)
    monkeypatch.setattr(sm.http.client, 'HTTPSConnection', Connection)
    return calls, connections, failure


@pytest.mark.parametrize('code', [errno.EADDRNOTAVAIL, errno.EADDRINUSE])
@pytest.mark.parametrize('wrapped', [True, False])
@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('ready', [True, False])
def test_loopback_resource_denied_probe_retries_same_identity_endpoint(
        monkeypatch, code, wrapped, scheme, ready):
    calls, connections, _ = _resource_denied_probe_transport(
        monkeypatch, code=code, wrapped=wrapped, status=200 if ready else 503,
        payload={'ok': ready, 'pid': 42, 'ready': ready,
                 'state': 'ready' if ready else 'starting',
                 'storage': {'ready': ready, 'state': 'ready' if ready else 'restarting'}})
    result = sm.probe_application_readiness(
        15000, 42, preferred_scheme=scheme, timeout=0.75)
    assert result['pidMatches'] is True
    assert result['liveness'] is True
    assert result['ready'] is ready
    assert result['readinessState'] == ('ready' if ready else 'starting')
    assert len(calls) == len(connections) == 1
    connection = connections[0]
    assert connection.host == '127.0.0.1' and connection.port == 15000
    assert connection.options['source_address'] == ('127.0.0.2', 0)
    assert 0 < connection.options['timeout'] <= 0.75
    if scheme == 'https':
        assert connection.options['context'] is calls[0][1]['context']
    else:
        assert 'context' not in connection.options
    assert connection.requests == [('GET', '/api/ready', {'Connection': 'close'})]
    assert connection.closed and connection.response.closed
    if not ready:
        assert 'HTTP 503' in result['readinessError']


def test_loopback_source_retry_cannot_approve_a_different_worker(monkeypatch):
    _, connections, _ = _resource_denied_probe_transport(
        monkeypatch, payload={'ok': True, 'pid': 99, 'ready': True})
    result = sm.probe_application_readiness(15000, 42, timeout=0.1)
    assert result['liveness'] is False and result['ready'] is False
    assert result['pidMatches'] is False
    assert 'PID does not match' in result['livenessError']
    assert all(connection.closed and connection.response.closed for connection in connections)


@pytest.mark.parametrize('code', [errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EMFILE])
def test_loopback_probe_does_not_retry_unrelated_transport_failures(monkeypatch, code):
    _, connections, failure = _resource_denied_probe_transport(monkeypatch, code=code)
    with pytest.raises(sm.urllib.error.URLError) as caught:
        sm._read_probe_json('http://127.0.0.1:15000/api/ready', timeout=0.1)
    assert caught.value is failure
    assert not connections


@pytest.mark.parametrize('url', [
    'http://192.0.2.1:15000/api/ready', 'http://localhost:15000/api/ready',
    'http://127.0.0.2:15000/api/ready', 'http://[::1]:15000/api/ready',
    'http://user:sentinel@127.0.0.1:15000/api/ready',
])
def test_loopback_probe_never_redirects_resource_retry_to_another_target(monkeypatch, url):
    _, connections, failure = _resource_denied_probe_transport(monkeypatch)
    with pytest.raises(sm.urllib.error.URLError) as caught:
        sm._read_probe_json(url, timeout=0.1)
    assert caught.value is failure
    assert not connections


@pytest.mark.parametrize('body', [b'[]', b'not-json', b' ' * (sm.HTTP_PROBE_RESPONSE_LIMIT + 1)])
def test_loopback_source_retry_keeps_bounded_json_contract(monkeypatch, body):
    _, connections, _ = _resource_denied_probe_transport(monkeypatch, body=body)
    with pytest.raises(ValueError):
        sm._read_probe_json('http://127.0.0.1:15000/api/ready', timeout=0.1)
    assert len(connections) == 1
    assert connections[0].closed and connections[0].response.closed


def test_loopback_source_retry_closes_response_after_read_timeout(monkeypatch):
    _, connections, _ = _resource_denied_probe_transport(
        monkeypatch, read_error=TimeoutError('read timed out'))
    with pytest.raises(TimeoutError):
        sm._read_probe_json('http://127.0.0.1:15000/api/ready?probe=1', timeout=0.1)
    assert len(connections) == 1
    assert connections[0].requests[0][1] == '/api/ready?probe=1'
    assert connections[0].closed and connections[0].response.closed


def test_worker_budget_snapshot_exposes_only_scheduling_provenance(monkeypatch):
    payload = (
        b'TOFU_RESOURCE_BUDGET_POLICY_VERSION=old\0'
        b'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS=TOFU_AGENT_WORKERS,'
        b'TOFU_MAX_INFLIGHT_TASKS,SECRET\0'
        b'TOFU_AGENT_WORKERS=4\0'
        b'TOFU_MAX_INFLIGHT_TASKS=6144\0'
        # A pre-upgrade worker may still carry a retired RSS knob.
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
        'TOFU_AGENT_WORKERS', 'TOFU_MAX_INFLIGHT_TASKS']
    assert snapshot['values'] == {
        'TOFU_AGENT_WORKERS': '4',
        'TOFU_MAX_INFLIGHT_TASKS': '6144',
    }
    assert 'SECRET' not in repr(snapshot)
    assert 'must-not-leak' not in repr(snapshot)
REAL_RUN_FRONTEND_PREFLIGHT = sm.run_frontend_preflight


@pytest.fixture(autouse=True)
def _scrub_ambient_server_env(monkeypatch, tmp_path):
    """Tests launched from a tofu-spawned shell inherit the LIVE server's
    port and budget knobs (_TOFU_RUNTIME_PORT/PORT make the manager adopt
    the live server's port and then correctly refuse to kill it; retired
    TOFU_PROCESS_RSS_* knobs leak into serverEnv assertions). Scrub them so
    every test starts hermetic; tests re-set what they need.

    The manager resolves its storage authority from TOFU_DATA_DIR. The
    conftest root is shared by every test in the pytest process, so a lease
    another test held there made status()/start() report a foreign
    'conflict'; each manager under test owns the project _project() creates."""
    for name in ('_TOFU_RUNTIME_PORT', 'PORT', 'TOFU_HEARTBEAT_DIR',
                 'TOFU_PROCESS_RSS_RECYCLE_MB', 'TOFU_PROCESS_RSS_RELIEF_MB'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_DATA_DIR', str(tmp_path / 'project'))
    monkeypatch.setattr(sm, '_dotenv_warned_failures', {})
    monkeypatch.setattr(
        sm, 'run_frontend_preflight',
        lambda *_args, **_kwargs: (True, ''),
    )


def _free_port() -> int:
    with socket.socket() as sock:
        bind_ephemeral_loopback(sock)
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


def test_manager_under_test_owns_its_storage_authority(tmp_path):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))

    assert manager._storage_data_dir() == project / 'data'


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
    import lib.logging_pkg.log_retention as retention

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
    from serverctl_pkg import native_engine
    project = _project(tmp_path)
    (project / 'serverctl.py').write_text('# preflight command\n')
    observed = {}

    def fake_run(command, **kwargs):
        observed['command'] = command
        observed['kwargs'] = kwargs
        return ''

    monkeypatch.setattr(native_engine, '_run', fake_run)
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
    assert observed['kwargs']['environment']['TOFU_PROJECT_PATH'] \
        == str(project.resolve())


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_restart_preparation_failure_keeps_live_worker(tmp_path, monkeypatch, backend):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    (project / '.env').write_text(f'TOFU_PERSONAL_STORAGE_BACKEND={backend}\n')
    monkeypatch.setattr(sm, 'run_frontend_preflight', lambda *_: (False, 'engine unavailable'))
    monkeypatch.setattr(manager, 'stop', lambda **_: pytest.fail('old worker must remain live'))
    assert manager.restart()['message'] == 'engine unavailable'


def test_preflight_refreshes_managed_pin_in_child_and_manager(tmp_path, monkeypatch):
    from serverctl_pkg import native_engine
    (tmp_path / 'serverctl.py').write_text('# isolated preflight\n')
    dotenv = tmp_path / '.env'
    dotenv.write_text('TOFUDB_EXPECTED_SHA256=old-managed-pin\n')
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', 'old-managed-pin')
    environment = {'TOFUDB_EXPECTED_SHA256': 'old-managed-pin'}

    def prepare(*args, **kwargs):
        dotenv.write_text('TOFUDB_EXPECTED_SHA256=new-managed-pin\n')
        return ''

    monkeypatch.setattr(native_engine, '_run', prepare)
    assert REAL_RUN_FRONTEND_PREFLIGHT(str(tmp_path), sys.executable, environment, 'test') == (True, '')
    assert environment['TOFUDB_EXPECTED_SHA256'] == 'new-managed-pin'
    assert os.environ['TOFUDB_EXPECTED_SHA256'] == 'new-managed-pin'


@pytest.mark.parametrize('inherited', [None, 'explicit-pin'])
def test_preflight_freezes_project_pin_without_overwriting_override(tmp_path, monkeypatch, inherited):
    from serverctl_pkg import native_engine
    (tmp_path / 'serverctl.py').write_text('# isolated preflight\n')
    (tmp_path / '.env').write_text('TOFUDB_EXPECTED_SHA256=project-pin\nTOFUDB_BIN=/admitted/engine\n')
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    environment = {} if inherited is None else {'TOFUDB_EXPECTED_SHA256': inherited}
    monkeypatch.setattr(native_engine, '_run', lambda *a, **k: '')
    assert REAL_RUN_FRONTEND_PREFLIGHT(str(tmp_path), sys.executable, environment, 'test') == (True, '')
    assert environment['TOFUDB_EXPECTED_SHA256'] == (inherited or 'project-pin')
    assert environment['TOFUDB_BIN'] == '/admitted/engine'
    assert 'TOFUDB_EXPECTED_SHA256' not in os.environ


def test_restart_preparation_cannot_undo_concurrent_stop(tmp_path, monkeypatch):
    manager = sm.LifecycleManager(str(_project(tmp_path)))
    manager._state['desired'] = 'running'
    def prepare(*_):
        manager._state['desired'] = 'stopped'
        return True, ''
    monkeypatch.setattr(sm, 'run_frontend_preflight', prepare)
    monkeypatch.setattr(manager, 'stop', lambda **_: pytest.fail('stale restart must not stop a worker'))
    result = manager.restart()
    assert result['ok'] is False
    assert 'lifecycle changed' in result['message']
    assert manager._state['desired'] == 'stopped'


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


_STALE_CODEX_HOME = '/stale-supervisor-launch-shell/.codex-local'
_FRESH_CODEX_HOME = '/current-project-dotenv/.codex'


@pytest.mark.parametrize('entry', ['restart', 'automatic-recovery'])
def test_worker_generation_takes_current_dotenv_over_stale_supervisor_env(
        tmp_path, monkeypatch, entry):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setenv('CODEX_HOME', _STALE_CODEX_HOME)
    (project / '.env').write_text(f'CODEX_HOME={_FRESH_CODEX_HOME}\n')
    prepared = []

    def preflight(_project, _python, environment, _operation):
        prepared.append(dict(environment))
        return False, 'stop before replacing the worker'

    monkeypatch.setattr(sm, 'run_frontend_preflight', preflight)
    monkeypatch.setattr(manager, 'stop', lambda **_: pytest.fail('worker must stay live'))
    monkeypatch.setattr(
        sm.subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('no spawn'))

    if entry == 'restart':
        manager.restart(source='application-restart')
    else:
        manager._spawn(entry)

    assert [environment['CODEX_HOME'] for environment in prepared] == [_FRESH_CODEX_HOME]
    assert os.environ['CODEX_HOME'] == _STALE_CODEX_HOME


def test_worker_environment_layers_dotenv_between_ambient_and_manager_values(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    for name in ('TOFU_AGENT_WORKERS', sm.RESOURCE_BUDGET_AUTOMATIC_ENV,
                 sm.RESOURCE_BUDGET_POLICY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_AMBIENT_ONLY_SENTINEL', 'ambient')
    monkeypatch.setenv('TOFU_MANAGED_BY', 'shell')
    (project / '.env').write_text(
        'PORT=16001\n'
        'TOFU_MANAGED_BY=dotenv\n'
        'TOFU_PROJECT_PATH=/another/project\n'
        'TOFU_EXTERNAL_CONSOLE_LOG=/another/console.log\n'
        'TOFU_AGENT_WORKERS=3\n')

    environment = manager._worker_environment({}, 15123)

    assert environment['TOFU_AMBIENT_ONLY_SENTINEL'] == 'ambient'
    assert environment['PORT'] == '15123'
    assert environment['TOFU_MANAGED_BY'] == 'supervisor'
    assert environment['TOFU_SERVER_WORKER'] == '1'
    assert environment['TOFU_PROJECT_PATH'] == manager.project
    assert environment['TOFU_EXTERNAL_CONSOLE_LOG'] == str(manager.worker_log)
    # Probed defaults fill only knobs that the current .env left empty.
    assert environment['TOFU_AGENT_WORKERS'] == '3'
    assert 'TOFU_AGENT_WORKERS' not in (
        environment[sm.RESOURCE_BUDGET_AUTOMATIC_ENV].split(','))


def test_validated_server_env_override_wins_over_project_dotenv(tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setenv('TOFU_AGENT_QUEUE_CAPACITY', '2')
    (project / '.env').write_text(
        'TOFU_AGENT_QUEUE_CAPACITY=8\nTOFU_HTTP_WRITE_STALL_SECS=30\n')

    _, forwarded, error = manager._validated_server_environment(
        {'TOFU_AGENT_QUEUE_CAPACITY': '32'})
    environment = manager._worker_environment(forwarded, 15123)

    assert error == ''
    assert environment['TOFU_AGENT_QUEUE_CAPACITY'] == '32'
    assert environment['TOFU_HTTP_WRITE_STALL_SECS'] == '30'


def test_deleted_dotenv_key_falls_back_to_ambient_until_explicitly_emptied(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(sm, 'install_process_resource_defaults', lambda _env: {})
    monkeypatch.setenv('CODEX_HOME', _STALE_CODEX_HOME)
    dotenv = project / '.env'
    observed = []
    for declared in (f'CODEX_HOME={_FRESH_CODEX_HOME}\n', '', 'CODEX_HOME=\n'):
        dotenv.write_text(declared)
        observed.append(manager._worker_environment({}, 15123)['CODEX_HOME'])

    assert observed == [_FRESH_CODEX_HOME, _STALE_CODEX_HOME, '']


_INTERPRETER_BOOTSTRAP_DOTENV = {
    'PYTHONPATH': '/dotenv/site',
    'PYTHONHOME': '/dotenv/home',
    'PYTHONSTARTUP': '/dotenv/startup.py',
    'LD_PRELOAD': '/dotenv/preload.so',
    'LD_LIBRARY_PATH': '/dotenv/lib',
    'DYLD_LIBRARY_PATH': '/dotenv/dyld',
    'DYLD_INSERT_LIBRARIES': '/dotenv/insert.dylib',
}


def test_interpreter_bootstrap_dotenv_keys_stay_post_start_only(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(sm, 'install_process_resource_defaults', lambda _env: {})
    monkeypatch.setenv('CODEX_HOME', _STALE_CODEX_HOME)
    inherited = {'LD_LIBRARY_PATH': '/ambient/lib',
                 'DYLD_LIBRARY_PATH': '/ambient/dyld'}
    for name in _INTERPRETER_BOOTSTRAP_DOTENV:
        if name in inherited:
            monkeypatch.setenv(name, inherited[name])
        else:
            monkeypatch.delenv(name, raising=False)
    (project / '.env').write_text(
        f'CODEX_HOME={_FRESH_CODEX_HOME}\n' + ''.join(
            f'{name}={value}\n'
            for name, value in _INTERPRETER_BOOTSTRAP_DOTENV.items()))

    environment = manager._worker_environment({}, 15123)

    assert environment['CODEX_HOME'] == _FRESH_CODEX_HOME
    assert {name: environment.get(name) for name in _INTERPRETER_BOOTSTRAP_DOTENV} == {
        name: inherited.get(name) for name in _INTERPRETER_BOOTSTRAP_DOTENV}
    # The worker's boot loader still fills the names its exec environment
    # lacks, after the interpreter and loader have started.
    load_dotenv_file(project / '.env', environment)
    assert {name: environment[name] for name in _INTERPRETER_BOOTSTRAP_DOTENV} == {
        **_INTERPRETER_BOOTSTRAP_DOTENV, **inherited}


_UNUSABLE_DOTENV_DAMAGE = ('directory', 'oversized', 'not-utf8')


def _write_unusable_dotenv(dotenv: Path, damage: str, declared: str) -> None:
    if damage == 'directory':
        dotenv.mkdir()
    elif damage == 'oversized':
        dotenv.write_text(declared + '#' * MAX_DOTENV_BYTES)
    else:
        dotenv.write_bytes(declared.encode() + b'\xff\n')


@pytest.mark.parametrize('damage', _UNUSABLE_DOTENV_DAMAGE)
def test_unusable_project_dotenv_keeps_ambient_worker_environment(
        tmp_path, monkeypatch, caplog, damage):
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setenv('CODEX_HOME', _STALE_CODEX_HOME)
    # Only the composition is under test; the resource probe may drift.
    monkeypatch.setattr(sm, 'install_process_resource_defaults', lambda _env: {})
    server_env = {'TOFU_AGENT_QUEUE_CAPACITY': '8'}
    without_dotenv = manager._worker_environment(server_env, 15123)
    declared = f'CODEX_HOME={_FRESH_CODEX_HOME}\nTOFU_DOTENV_ONLY_SENTINEL=1\n'
    _write_unusable_dotenv(project / '.env', damage, declared)

    with caplog.at_level(logging.WARNING, logger='server_manager'):
        environment = manager._worker_environment(server_env, 15123)

    assert environment == without_dotenv
    assert environment['CODEX_HOME'] == _STALE_CODEX_HOME
    warnings = [record.getMessage() for record in caplog.records
                if record.name == 'server_manager'
                and record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'inherited Supervisor values remain in effect' in warnings[0]


@pytest.mark.parametrize('damage', _UNUSABLE_DOTENV_DAMAGE)
def test_unusable_project_dotenv_keeps_manager_construction_alive(
        tmp_path, caplog, damage):
    project = _project(tmp_path)
    _write_unusable_dotenv(
        project / '.env', damage, 'PORT=16001\nTOFU_AGENT_QUEUE_CAPACITY=8\n')

    with caplog.at_level(logging.WARNING, logger='server_manager'):
        manager = sm.LifecycleManager(str(project))

    assert manager._project_env == {}
    assert sm.project_server_env(str(project)) == {}
    assert manager.port == sm.DEFAULT_SERVER_PORT
    assert 'inherited Supervisor values remain in effect' in caplog.text


def _unreadable_dotenv_warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == 'server_manager'
            and record.levelno == logging.WARNING
            and 'project .env not applied' in record.getMessage()]


def test_unreadable_project_dotenv_warns_once_per_distinct_failure(
        tmp_path, monkeypatch, caplog):
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'install_process_resource_defaults', lambda _env: {})
    dotenv = project / '.env'
    dotenv.write_bytes(b'PORT=16001\n\xff\n')

    with caplog.at_level(logging.WARNING, logger='server_manager'):
        manager = sm.LifecycleManager(str(project))
        for _ in range(3):
            manager._worker_environment({}, 15123)
            sm.project_server_env(str(project))
        assert len(_unreadable_dotenv_warnings(caplog)) == 1
        dotenv.write_text('PORT=16001\n' + '#' * MAX_DOTENV_BYTES)
        manager._worker_environment({}, 15123)
        manager._worker_environment({}, 15123)
        assert len(_unreadable_dotenv_warnings(caplog)) == 2
        # A recovery ends the episode: the same failure later is news again.
        dotenv.write_text('PORT=16001\n')
        assert sm.project_server_env(str(project)) == {'PORT': '16001'}
        dotenv.write_bytes(b'PORT=16001\n\xff\n')
        manager._worker_environment({}, 15123)
        manager._worker_environment({}, 15123)

    warnings = _unreadable_dotenv_warnings(caplog)
    assert len(warnings) == 3
    assert ['valid UTF-8' in warnings[0], 'byte limit' in warnings[1],
            'valid UTF-8' in warnings[2]] == [True, True, True]


def test_unreadable_dotenv_warning_memory_is_bounded(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sm, '_DOTENV_WARNING_MEMORY', 2)
    projects = []
    for index in range(3):
        project = tmp_path / f'project-{index}'
        project.mkdir()
        (project / '.env').write_bytes(b'\xff\n')
        projects.append(str(project))

    with caplog.at_level(logging.WARNING, logger='server_manager'):
        for project in projects:
            sm.project_server_env(project)
        sm.project_server_env(projects[2])
        # The oldest remembered failure was evicted, so it warns again.
        sm.project_server_env(projects[0])

    warnings = _unreadable_dotenv_warnings(caplog)
    assert len(warnings) == 4
    assert projects[0] in warnings[-1]
    assert len(sm._dotenv_warned_failures) == 2


def test_non_utf8_project_dotenv_keeps_restart_preparation_on_inherited_values(
        tmp_path, monkeypatch):
    from serverctl_pkg import native_engine
    project = _project(tmp_path)
    (project / 'serverctl.py').write_text('# isolated preflight\n')
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setenv('CODEX_HOME', _STALE_CODEX_HOME)
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    _write_unusable_dotenv(
        project / '.env', 'not-utf8',
        f'CODEX_HOME={_FRESH_CODEX_HOME}\nTOFUDB_BIN=/dotenv/engine\n')
    prepared = []

    def prepare(command, **kwargs):
        prepared.append(dict(kwargs['environment']))
        return ''

    # The real preflight also re-reads the .env around the child to refresh
    # managed engine pins; both reads must take the same fallback.
    monkeypatch.setattr(sm, 'run_frontend_preflight', REAL_RUN_FRONTEND_PREFLIGHT)
    monkeypatch.setattr(native_engine, '_run', prepare)
    monkeypatch.setattr(
        manager, 'stop', lambda **_: {'ok': False, 'message': 'stop refused by test'})

    assert manager.restart(source='application-restart') == {
        'ok': False, 'message': 'stop refused by test'}
    assert [environment['CODEX_HOME'] for environment in prepared] == [
        _STALE_CODEX_HOME]
    assert 'TOFUDB_BIN' not in prepared[0]


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


@pytest.mark.skipif(os.name == 'nt', reason='POSIX flock contract')
def test_storage_lease_status_normalizes_tofudb_stamp_to_sidecar(tmp_path):
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
        'owner_kind': 'tofudb',
        'owner_label': 'Tofu-DB',
    }), encoding='utf-8')

    with lock_path.open('r+b') as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = sm.read_storage_lease_status(data_dir)

    assert held['held'] is True
    assert held['kind'] == 'storage_authority_sql'


def test_legacy_storage_lease_owner_recognizes_native_tofudb_serve():
    kind, label = sm._legacy_storage_lease_owner(
        '/repo/packages/tofu-db/target/certified/tofu-db serve '
        '--data-dir /repo/data/tofudb-authority --owner-id 1 --tenant-id 1')
    assert (kind, label) == ('storage_authority_sql', 'Storage sidecar')

    kind, label = sm._legacy_storage_lease_owner(
        'python -m lib.storage_authority_sql')
    assert (kind, label) == ('storage_authority_sql', 'Storage sidecar')

    # A bare tofu-db process that is NOT the serving authority stays unknown.
    kind, _ = sm._legacy_storage_lease_owner(
        '/repo/packages/tofu-db/target/certified/tofu-db init --data-dir /x')
    assert kind == 'unknown'

    kind, _ = sm._legacy_storage_lease_owner('/usr/bin/postgres -D /data')
    assert kind == 'unknown'


def test_terminate_timeout_mirrors_worker_shutdown_budget(
        tmp_path, monkeypatch):
    # Killing the worker before its own hard deadline skips the bounded
    # storage-sidecar stop and orphans the project lease (2026-09-21 restart
    # deadlock).  The manager escalation must stay the outer backstop — and
    # deployments may raise TOFU_SHUTDOWN_HARD_SECS far past the 30s default,
    # so the manager mirrors the worker's configured budget plus a margin.
    import inspect

    default = inspect.signature(
        sm.LifecycleManager._terminate).parameters['timeout'].default
    assert default is None

    monkeypatch.delenv('TOFU_SHUTDOWN_HARD_SECS', raising=False)
    project = _project(tmp_path)
    manager = sm.LifecycleManager(str(project))
    assert manager._worker_shutdown_hard_seconds() == 30.0
    assert manager._terminate_timeout_seconds() == 35.0

    (project / '.env').write_text(
        'TOFU_SHUTDOWN_HARD_SECS=60\n', encoding='utf-8')
    manager._project_env = sm.project_server_env(str(project))
    assert manager._worker_shutdown_hard_seconds() == 60.0
    assert manager._terminate_timeout_seconds() == 65.0

    # The spawn-time serverEnv overlay wins over the project .env.
    manager._state['serverEnv'] = {'TOFU_SHUTDOWN_HARD_SECS': '120'}
    assert manager._terminate_timeout_seconds() == 125.0

    # Garbage falls back to the 30s worker default, still plus the margin.
    manager._state['serverEnv'] = {'TOFU_SHUTDOWN_HARD_SECS': 'banana'}
    monkeypatch.delenv('TOFU_SHUTDOWN_HARD_SECS', raising=False)
    (project / '.env').write_text('', encoding='utf-8')
    manager._project_env = {}
    assert manager._terminate_timeout_seconds() == 35.0


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal contract')
def test_terminate_logs_graceful_stop_elapsed(tmp_path, monkeypatch, caplog):
    child = subprocess.Popen(['sleep', '300'])
    try:
        manager = sm.LifecycleManager(str(_project(tmp_path)))
        # The timing contract is under test, not identity enforcement.
        monkeypatch.setattr(manager, '_identity_error', lambda _s: None)
        status = {
            'pid': child.pid,
            'running': True,
            'host': sm._hostname(),
            'sameHost': True,
            'cmdline': 'python server.py',
            'projectMatches': True,
        }
        with caplog.at_level(logging.INFO, logger='server_manager'):
            ok, killed, message = manager._terminate(status)
        assert ok and not killed and message == 'stopped cleanly'
        texts = [r.getMessage() for r in caplog.records]
        assert any('SIGTERM sent' in text for text in texts)
        assert any('exited cleanly' in text and 'after SIGTERM' in text
                   for text in texts)
    finally:
        child.kill()
        child.wait()


def test_frontend_preflight_logs_stage_elapsed(tmp_path, monkeypatch, caplog):
    from serverctl_pkg import native_engine
    project = _project(tmp_path)
    (project / 'serverctl.py').write_text('# stub\n', encoding='utf-8')

    monkeypatch.setattr(
        native_engine, '_run', lambda *a, **k: '')
    with caplog.at_level(logging.INFO, logger='server_manager'):
        ready, error = REAL_RUN_FRONTEND_PREFLIGHT(
            str(project), sys.executable, {}, 'manager test')
    assert ready and not error
    texts = [r.getMessage() for r in caplog.records]
    assert any('frontend preflight starting' in text for text in texts)
    assert any('frontend preflight passed in' in text for text in texts)


def test_reconcile_logs_worker_ready_with_boot_elapsed(
        tmp_path, monkeypatch, caplog):
    project = _project(tmp_path)
    monkeypatch.setattr(
        sm, 'read_lock_status',
        lambda _p: _status(project, running=True, pid=4321))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._state['observed'] = 'starting'
    manager._state['launchSource'] = 'manager-restart'
    manager._state['worker'] = {
        'pid': 4321,
        'host': sm._hostname(),
        'spawnedAt': time.time() - 21.0,
    }
    monkeypatch.setattr(
        manager, '_http_probe',
        lambda _pid: {'liveness': True, 'ready': True})
    with caplog.at_level(logging.INFO, logger='server_manager'):
        manager.reconcile()
    assert manager._state['observed'] == 'running'
    ready_lines = [r.getMessage() for r in caplog.records
                   if r.getMessage().startswith('worker ready pid=4321')]
    assert len(ready_lines) == 1
    assert 'source=manager-restart' in ready_lines[0]
    assert 'bootTime=2' in ready_lines[0]

    # A steady-state tick must not re-log the ready line — and neither may a
    # later degraded→running blip of the same generation (2026-09-21: the
    # line re-fired hours after spawn with a bogus multi-hour bootTime).
    for blip in ('running', 'degraded'):
        caplog.clear()
        manager._state['observed'] = blip
        with caplog.at_level(logging.INFO, logger='server_manager'):
            manager.reconcile()
        assert not [r for r in caplog.records
                    if r.getMessage().startswith('worker ready pid=')]

    # A fresh spawn resets the marker so the next generation logs again.
    manager._state['workerReadyLoggedAt'] = 0.0
    manager._state['worker']['spawnedAt'] = time.time() - 7.0
    manager._state['observed'] = 'starting'
    caplog.clear()
    with caplog.at_level(logging.INFO, logger='server_manager'):
        manager.reconcile()
    assert len([r for r in caplog.records
                if r.getMessage().startswith('worker ready pid=')]) == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal contract')
def test_reap_orphaned_storage_authority_sql_sigterms_verified_holder(
        tmp_path, monkeypatch):
    child = subprocess.Popen(['sleep', '300'])
    try:
        lease = {
            'held': True,
            'kind': 'storage_authority_sql',
            'label': 'Storage sidecar',
            'pid': child.pid,
            'host': sm._hostname(),
            'holderVerified': True,
        }
        monkeypatch.setattr(
            sm, 'proc_cmdline',
            lambda pid: (
                '/repo/tofu-db serve --data-dir /repo/data/tofudb-authority'
                if int(pid) == child.pid else None))
        manager = sm.LifecycleManager(str(_project(tmp_path)))
        monkeypatch.setattr(
            manager, '_active_storage_lease',
            lambda: (dict(lease) if sm.pid_is_alive(child.pid)
                     else {'held': False, 'kind': None, 'pid': None}))

        released = manager._reap_orphaned_storage_authority_sql(dict(lease))

        assert released['held'] is False
        assert not sm.pid_is_alive(child.pid)
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal contract')
def test_reap_orphaned_storage_authority_sql_refuses_foreign_identity(
        tmp_path, monkeypatch):
    child = subprocess.Popen(['sleep', '300'])
    try:
        lease = {
            'held': True,
            'kind': 'storage_authority_sql',
            'label': 'Storage sidecar',
            'pid': child.pid,
            'host': sm._hostname(),
            'holderVerified': True,
        }
        # PID-reuse guard: the holder no longer looks like a sidecar.
        monkeypatch.setattr(
            sm, 'proc_cmdline', lambda pid: '/usr/bin/other-daemon --serve')
        manager = sm.LifecycleManager(str(_project(tmp_path)))
        monkeypatch.setattr(
            manager, '_active_storage_lease', lambda: dict(lease))

        blocked = manager._reap_orphaned_storage_authority_sql(dict(lease))

        assert blocked['held'] is True
        assert sm.pid_is_alive(child.pid)
    finally:
        child.kill()
        child.wait()


def test_wait_for_stopped_sidecar_release_reaps_after_drain_grace(
        tmp_path, monkeypatch):
    manager = sm.LifecycleManager(str(_project(tmp_path)))
    held_lease = {
        'held': True,
        'kind': 'storage_authority_sql',
        'label': 'Storage sidecar',
        'pid': 4321,
        'host': sm._hostname(),
        'holderVerified': True,
    }
    monkeypatch.setattr(
        manager, '_active_storage_lease', lambda: dict(held_lease))
    reaped = []
    monkeypatch.setattr(
        manager, '_reap_orphaned_storage_authority_sql',
        lambda lease, **_kwargs: reaped.append(lease) or {
            'held': False, 'kind': None, 'pid': None})

    lease = manager._wait_for_stopped_sidecar_release(timeout=0.2)

    assert reaped and reaped[0]['pid'] == 4321
    assert lease['held'] is False


def test_wait_for_stopped_sidecar_release_does_not_reap_foreign_holders(
        tmp_path, monkeypatch):
    manager = sm.LifecycleManager(str(_project(tmp_path)))
    for foreign_kind in ('unknown', 'offline_maintenance', 'storage_operation'):
        monkeypatch.setattr(
            manager, '_active_storage_lease',
            lambda kind=foreign_kind: {
                'held': True, 'kind': kind, 'pid': 4321,
                'host': sm._hostname(), 'holderVerified': True})
        monkeypatch.setattr(
            manager, '_reap_orphaned_storage_authority_sql',
            lambda *_args, **_kwargs: pytest.fail(
                'non-sidecar holders must stay fail-closed'))

        lease = manager._wait_for_stopped_sidecar_release(timeout=0.05)

        assert lease['held'] is True
        assert lease['kind'] == foreign_kind


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
        'kind': 'storage_authority_sql',
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
    prepared = starts[0].pop('_prepared_environment')
    assert prepared['TOFU_PROJECT_PATH'] == str(project)
    assert starts[0].pop('_prepared_storage_plan') is None
    assert starts[0].pop('_prepared_execution_plan') is None
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


@pytest.mark.parametrize('case,expected', [
    ('ready', 'running'),
    ('unready', 'starting'),
    ('wrong_pid', 'starting'),
    ('stopped', 'stopping'),
    ('identity_conflict', 'conflict'),
    ('port_conflict', 'conflict'),
    ('no_probe', 'starting'),
])
def test_status_verified_readiness_resolves_inconclusive_tcp_probe(
        tmp_path, monkeypatch, case, expected):
    project = _project(tmp_path)
    live = _status(project, running=True, pid=42)
    if case == 'identity_conflict':
        live['projectMatches'] = False
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: live)
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [99] if case == 'port_conflict' else [42])
    monkeypatch.setattr(sm, 'proc_env_value', lambda *_a: None)
    calls = []

    def read_json(url, **_kwargs):
        calls.append(url)
        return 200, {
            'ok': True, 'pid': 43 if case == 'wrong_pid' else 42,
            'ready': case != 'unready', 'state': 'ready',
        }

    monkeypatch.setattr(sm, '_read_probe_json', read_json)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'stopped' if case == 'stopped' else 'running'
    previous = dict(manager._state)

    status = manager.status(probe_health=case != 'no_probe')

    assert status['observed'] == expected
    assert status['pid'] == 42
    assert manager._state == previous
    if case in {'no_probe', 'port_conflict'}:
        assert not calls
        assert status['ready'] is None
    else:
        assert calls
        assert status['ready'] is (case not in {'unready', 'wrong_pid'})
        assert status['liveness'] is (case != 'wrong_pid')


@pytest.mark.parametrize('probe_liveness', [True, False])
def test_live_but_unready_worker_degrades_without_wedge_restart(
        tmp_path, monkeypatch, probe_liveness):
    project = _project(tmp_path)
    live = _status(project, running=True, pid=4321)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: live)
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [4321])
    manager = sm.LifecycleManager(str(project))
    probe = {
        'health': probe_liveness,
        'liveness': probe_liveness,
        'ready': False,
        'scheme': 'http',
        'livenessError': '' if probe_liveness else 'identity probe timed out',
        'readinessError': (
            'application readiness failed: lifecycle=starting, '
            'storage=restarting, HTTP 503'),
        'readinessState': 'starting',
        'storageState': 'restarting',
    }
    monkeypatch.setattr(manager, '_http_probe', lambda _pid: dict(probe))
    monkeypatch.setattr(
        manager, '_heartbeat_stale_age',
        lambda _pid: sm.DEFAULT_WEDGE_STALE + 1000)
    monkeypatch.setattr(
        manager, '_terminate',
        lambda *_a, **_k: pytest.fail(
            'dependency readiness must not trigger wedge termination'))
    monkeypatch.setattr(
        manager, '_spawn', lambda *_a, **_k: pytest.fail(
            'a live worker must not be replaced during dependency recovery'))
    clock = {'now': 10000.0}
    monkeypatch.setattr(sm, '_now', lambda: clock['now'])
    manager._state['wedgeSince'] = clock['now'] - sm.DEFAULT_WEDGE_STREAK - 1
    initial_failures = list(manager._state['failureHistory'])
    initial_restarts = manager._state['restartCount']

    status = manager.status(probe_health=True)
    for _ in range(3):
        manager.reconcile()
        clock['now'] += 3600
        assert manager._state['observed'] == 'degraded'
        assert manager._state['worker']['pid'] == 4321
        assert manager._state['failureHistory'] == initial_failures
        assert manager._state['restartCount'] == initial_restarts

    assert status['observed'] == 'degraded'
    assert status['health'] is probe_liveness
    assert status['liveness'] is probe_liveness
    assert status['ready'] is False
    assert status['readinessState'] == 'starting'
    assert status['storageState'] == 'restarting'
    if probe_liveness:
        assert 'storage=restarting' in status['lastError']
        assert manager._state['wedgeSince'] == 0.0
        assert 'storage=restarting' in manager._state['lastError']
    else:
        assert 'timed out' in status['lastError']
        assert manager._state['wedgeSince'] > 0
        assert 'preserved' in manager._state['lastError']

    probe.update(health=True, liveness=True, ready=True, livenessError='',
                 readinessError='', readinessState='ready', storageState='ready')
    manager.reconcile()
    recovered = manager.status(probe_health=True)
    assert recovered['observed'] == 'running'
    assert recovered['pid'] == 4321
    assert recovered['ready'] is True
    assert manager._state['wedgeSince'] == 0.0
    assert manager._state['lastError'] == ''
    assert manager._state['restartCount'] == initial_restarts


def test_cgroup_oom_counter_reader_supports_events_file(tmp_path, monkeypatch):
    events = tmp_path / 'memory.events'
    events.write_text('low 0\noom 4\noom_kill 3\n')
    monkeypatch.setattr(sm, 'CGROUP_OOM_EVENT_PATHS', (str(events),))

    assert sm.cgroup_oom_kill_count() == 3


def test_reconcile_never_recycles_a_healthy_worker_whose_rss_reads_enormous(
        tmp_path, monkeypatch):
    project = _project(tmp_path)
    # Retired RSS ceilings left in an old project environment have no effect.
    (project / '.env').write_text(
        'TOFU_PROCESS_RSS_RECYCLE_MB=1\nTOFU_PROCESS_RSS_RELIEF_MB=1\n')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '1')
    real_path_type = type(sm.Path())

    class _EnormousResidentSet(real_path_type):
        def read_text(self, *args, **kwargs):
            if self.name == 'statm' and str(self).startswith('/proc/'):
                return f'{1 << 50} {1 << 50} 0 0 0 0 0\n'
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(sm, 'Path', _EnormousResidentSet)
    monkeypatch.setattr(
        sm, 'read_lock_status', lambda _p: _status(project, running=True, pid=4321))
    monkeypatch.setattr(sm, 'listener_pids', lambda _port: [4321])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: True)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 7)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_http_probe', lambda _pid: {
        'health': True, 'liveness': True, 'ready': True, 'scheme': 'http'})
    monkeypatch.setattr(manager, '_terminate', lambda *_a, **_k: pytest.fail(
        'memory must never terminate a healthy worker'))
    monkeypatch.setattr(manager, '_spawn', lambda *_a, **_k: pytest.fail(
        'memory must never replace a healthy worker'))

    for _ in range(3):
        manager.reconcile()

    assert manager._state['observed'] == 'running'
    assert manager._state['worker']['pid'] == 4321
    assert manager._state['failureHistory'] == []
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['restartCount'] == 0
    assert manager._state['lastExitCause'] == ''
    status = manager.status()
    assert not [key for key in status if 'Rss' in key or 'MemoryRecycle' in key]


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


def test_status_crashloop_ignores_a_stale_consecutive_count(
        tmp_path, monkeypatch):
    """status() must use the windowed latch, not a stale consecutive counter."""
    clock = [1000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    manager._state['observed'] = 'starting'
    # Many lifetime failures, none inside the window: recovery is still
    # retrying, so the manager must not advertise a terminal crashloop.
    manager._state['consecutiveFailures'] = manager.max_failures * 4
    manager._state['failureHistory'] = []

    status = manager.status()

    assert status['recentFailureCount'] == 0
    assert status['observed'] == 'starting'


def test_retry_spawn_skips_while_failed_generation_still_holds_the_lock(
        tmp_path, monkeypatch):
    """The retry path must not race a still-live (slow-booting) generation."""
    project = _project(tmp_path)
    lock = {'running': True}
    monkeypatch.setattr(
        sm, 'read_lock_status',
        lambda _p: _status(project, running=lock['running'], pid=987654))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    manager._state['desired'] = 'running'
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source: spawned.append(source) or {'ok': True})

    manager._state['nextRetryAt'] = time.time() - 1
    manager._record_failure()
    assert spawned == []

    manager._state['nextRetryAt'] = time.time() - 1
    lock['running'] = False
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


def test_graceful_signal_after_ready_restarts_without_spending_failure_budget(
        tmp_path, monkeypatch):
    """An external drain of a generation that had served is not a crash.

    On a slow FUSE host a single boot can take 500-1000 s; charging an
    operator/agent SIGTERM against the crash-loop budget can push a healthy
    worker toward the terminal crashloop latch. The ready certificate
    (workerReadyLoggedAt) distinguishes a served generation from a booting one.
    """
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 0)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    marker = _install_clean_worker_marker(manager, project, reason='signal')
    # This generation reached readiness before the external signal.
    manager._state['workerReadyLoggedAt'] = time.time() - 0.5
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source: spawned.append(source) or {'ok': True},
    )

    manager.reconcile()

    assert spawned == ['external-drain-recovery']
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['failureHistory'] == []
    assert manager._state['lastWorkerExitReason'] == 'signal'
    # The certificate's own timestamp is the exit time, not the tick that
    # happened to read it.
    assert manager._state['lastWorkerExitAt'] == (
        json.loads(marker.read_text())['clean_ts'])


def test_signal_before_ready_still_spends_failure_budget(tmp_path, monkeypatch):
    """A signal to a generation that never reached readiness is still a crash.

    Guards the exemption's boundary: an external kill loop against a booting
    worker must keep spending budget toward the crashloop latch.
    """
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 0)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    _install_clean_worker_marker(manager, project, reason='signal')
    manager._state['workerReadyLoggedAt'] = 0.0

    manager.reconcile()

    assert manager._state['plannedExitCount'] == 0
    assert manager._state['workerFailureCount'] == 1
    assert manager._state['lastExitCause'] == 'unintended_signal'


def test_booting_heartbeat_is_judged_by_boot_grace_not_wedge_stale(
        tmp_path, monkeypatch):
    """A booting-phase heartbeat gets the boot grace, not the 180 s wedge."""
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    manager = sm.LifecycleManager(str(project))
    heartbeat_dir = tmp_path / 'heartbeat'
    heartbeat_dir.mkdir()
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(heartbeat_dir))
    monkeypatch.setenv('TOFU_BOOT_HEARTBEAT_GRACE_SECS', '900')
    heartbeat = heartbeat_dir / 'server.heartbeat'
    pid = 987654

    def _write(age: float, phase: str) -> None:
        heartbeat.write_text(json.dumps({
            'pid': pid, 'ts': time.time() - age, 'phase': phase}))

    # 250 s: stale for a serving loop, fresh for a booting one.
    _write(250, 'booting')
    assert manager._heartbeat_stale_age(pid) is None
    _write(250, 'serving')
    assert manager._heartbeat_stale_age(pid) is not None
    # Past the boot grace, a booting generation is wedge again.
    _write(1000, 'booting')
    assert manager._heartbeat_stale_age(pid) is not None
    # A heartbeat for another pid never judges this one.
    heartbeat.write_text(json.dumps({
        'pid': pid + 1, 'ts': time.time() - 1000, 'phase': 'booting'}))
    assert manager._heartbeat_stale_age(pid) is None


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
        {'held': True, 'kind': 'storage_authority_sql', 'pid': 222},
        {'held': True, 'kind': 'storage_authority_sql', 'pid': 222},
        {'held': False},
    ])
    monkeypatch.setattr(
        manager, '_active_storage_lease',
        lambda: next(lease_states, {'held': False}))
    spawned = []

    def _spawn(source):
        manager._state['pendingRecoverySource'] = ''
        spawned.append(source)
        return {'ok': True}

    monkeypatch.setattr(manager, '_spawn', _spawn)

    # The drain now happens in-band: one reconcile polls the lease until the
    # orphaned sidecar releases it, then spawns exactly one fresh generation.
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


def _unsupervised_departure(tmp_path, monkeypatch, *, marker_reason=None,
                            ready=True, desired='running'):
    """A supervised generation that exited while no manager observed it.

    The previous Supervisor stopped together with its worker; a new manager
    loads the persisted record of that dead generation (pid 987654).
    """
    clock = [50_000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    project = _project(tmp_path)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    monkeypatch.setattr(sm, 'cgroup_oom_kill_count', lambda: 0)
    manager = sm.LifecycleManager(str(project))
    monkeypatch.setattr(manager, '_storage_data_dir', lambda: project / 'data')
    monkeypatch.setattr(manager, '_active_storage_lease', lambda: {'held': False})
    exited_at = clock[0] - 2040.0
    if marker_reason:
        _install_clean_worker_marker(
            manager, project, reason=marker_reason, clean_ts=exited_at)
    else:
        manager._state['worker'] = {
            'pid': 987654, 'host': sm._hostname(), 'processStartTime': 42,
            'processCwd': str(project.resolve()),
        }
    manager._state['worker']['spawnedAt'] = exited_at - 7200.0
    manager._state['workerReadyLoggedAt'] = (
        exited_at - 7000.0 if ready else 0.0)
    manager._state['desired'] = desired
    manager._save()
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source, **_kwargs: spawned.append(source) or {'ok': True})
    return manager, spawned, exited_at


def test_explicit_start_settles_a_generation_that_departed_unsupervised(
        tmp_path, monkeypatch):
    """A start that outruns every reconcile tick must not erase an exit.

    The Supervisor was stopped together with its served worker; the new
    Supervisor's first act is an explicit start, which used to overwrite the
    dead generation's record so status kept reporting an older exit.
    """
    manager, spawned, exited_at = _unsupervised_departure(
        tmp_path, monkeypatch, marker_reason='signal')

    assert manager.start(source='serverctl')['ok'] is True

    assert spawned == ['serverctl']
    status = manager.status()
    assert status['plannedExitCount'] == 1
    assert status['workerFailureCount'] == 0
    assert status['lastWorkerExitKind'] == 'planned'
    assert status['lastWorkerExitReason'] == 'signal'
    assert status['lastWorkerExitAt'] == exited_at
    persisted = json.loads(manager.state_path.read_text())
    assert persisted['plannedExitCount'] == 1
    assert persisted['lastConsumedExitMarker'].startswith('987654:')
    # The start itself is the recovery; no stale recovery baton survives.
    assert persisted['pendingRecoverySource'] == ''


def test_explicit_start_records_an_unclean_departure_then_resets_the_budget(
        tmp_path, monkeypatch):
    manager, spawned, _ = _unsupervised_departure(tmp_path, monkeypatch)

    assert manager.start(source='serverctl')['ok'] is True

    assert spawned == ['serverctl']
    status = manager.status()
    assert status['workerFailureCount'] == 1
    assert status['plannedExitCount'] == 0
    assert status['lastWorkerExitKind'] == 'failure'
    assert status['lastExitCause'] == 'unexpected_exit'
    # Recorded first, then the explicit start clears crash-loop gating.
    assert status['recentFailureCount'] == 0
    assert status['consecutiveFailures'] == 0
    assert status['nextRetryAt'] == 0


def test_stop_settles_a_generation_that_departed_unsupervised(
        tmp_path, monkeypatch):
    manager, spawned, exited_at = _unsupervised_departure(
        tmp_path, monkeypatch, marker_reason='signal')

    assert manager.stop(source='serverctl')['ok'] is True

    assert spawned == []
    status = manager.status()
    assert status['desired'] == 'stopped'
    assert status['worker'] == {}
    assert status['plannedExitCount'] == 1
    assert status['lastWorkerExitReason'] == 'signal'
    assert status['lastWorkerExitAt'] == exited_at


def test_stopped_reconcile_settles_a_stop_its_crashed_manager_left_open(
        tmp_path, monkeypatch):
    """stop() persists desired=stopped and its PID intent before the signal;
    a manager that dies before recording the exit leaves that proof for the
    next manager, which records a manual stop rather than forgetting it."""
    manager, spawned, exited_at = _unsupervised_departure(
        tmp_path, monkeypatch, marker_reason='signal', ready=False,
        desired='stopped')
    manager._state['pendingWorkerExitIntent'] = {
        'pid': 987654, 'host': sm._hostname(), 'reason': 'manual',
        'requestedAt': exited_at - 0.5, 'recoverySource': '',
    }

    manager.reconcile()

    assert spawned == []
    assert manager._state['desired'] == 'stopped'
    assert manager._state['observed'] == 'stopped'
    assert manager._state['worker'] == {}
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['lastWorkerExitReason'] == 'manual'


def test_stopped_reconcile_records_its_own_termination_as_a_manual_stop(
        tmp_path, monkeypatch):
    """Completing a persisted stop is manager-authored: its PID intent
    precedes the signal as in stop(), so even a still-booting generation's
    drain is a manual stop, not an unintended signal."""
    manager, spawned, _ = _unsupervised_departure(
        tmp_path, monkeypatch, ready=False, desired='stopped')
    project = Path(manager.project)
    live = {'running': True}
    monkeypatch.setattr(
        sm, 'read_lock_status',
        lambda _p: _status(project, running=live['running'], pid=987654))

    def drain(_status_value, **_kwargs):
        (project / 'data' / '.server_shutdown.json').write_text(json.dumps({
            'state': 'clean', 'pid': 987654, 'host': sm._hostname(),
            'clean_ts': sm._now(), 'reason': 'signal'}))
        live['running'] = False
        return True, False, 'stopped cleanly'

    monkeypatch.setattr(manager, '_terminate', drain)

    manager.reconcile()

    assert spawned == []
    assert manager._state['observed'] == 'stopped'
    assert manager._state['worker'] == {}
    assert manager._state['plannedExitCount'] == 1
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['lastWorkerExitReason'] == 'manual'


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
        'kind': 'storage_authority_sql',
        'label': 'Storage sidecar',
        'pid': 222,
    })
    # Keep the drain/reap wait out of this accounting contract, and prove the
    # failure is already counted by the time any sidecar-lease waiting runs.
    failure_count_at_wait = []

    def _wait():
        failure_count_at_wait.append(manager._state.get('workerFailureCount'))
        return manager._active_storage_lease()

    monkeypatch.setattr(manager, '_wait_for_stopped_sidecar_release', _wait)

    manager.reconcile()

    assert failure_count_at_wait == [1]

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


def test_retired_resource_profile_is_not_forwarded_to_the_worker(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / '.env').write_text(
        'TOFU_RESOURCE_PROFILE=throughput\nTOFU_DEPLOYMENT_MODE=distributed\n')
    monkeypatch.setattr(sm, 'read_lock_status', lambda _p: _status(project))
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    manager = sm.LifecycleManager(str(project))
    assert 'TOFU_RESOURCE_PROFILE' not in sm.SERVER_ENV_KEYS
    assert 'TOFU_RESOURCE_PROFILE' not in manager._state['serverEnv']
    assert manager._state['serverEnv']['TOFU_DEPLOYMENT_MODE'] == 'distributed'


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

        preparations = []

        def prepare_while_serving(_project, _python, environment, operation):
            # A source edit during shutdown must not cause another build after
            # the only serving worker has already exited.
            preparations.append(operation)
            if not sm.pid_is_alive(first_pid):
                return False, 'Engine source changed during preparation'
            assert sm.port_accepts(port)
            environment['TOFU_TEST_PREPARED_GENERATION'] = 'admitted'
            return True, ''

        monkeypatch.setattr(sm, 'run_frontend_preflight', prepare_while_serving)
        real_popen = sm.subprocess.Popen
        def launch_admitted_generation(*args, **kwargs):
            if len(args[0]) > 1 and args[0][1] == 'server.py':
                assert kwargs['env']['TOFU_TEST_PREPARED_GENERATION'] == 'admitted'
            return real_popen(*args, **kwargs)
        monkeypatch.setattr(sm.subprocess, 'Popen', launch_admitted_generation)
        restarted = manager.restart(source='integration')
        assert restarted['ok'] is True, restarted
        assert len(preparations) == 1
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
@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Linux exit evidence')
@pytest.mark.parametrize('retirement', ['sigkill', 'stop'])
def test_real_worker_exit_status_survives_the_managers_own_subprocesses(
        tmp_path, monkeypatch, caplog, retirement):
    """Reconcile runs ``ss`` (and preflights spawn children); none may reap
    the worker before its exit status is recorded."""
    caplog.set_level(logging.INFO, logger='server_manager')
    port = _free_port()
    project = _project(tmp_path, _WORKER)
    monkeypatch.setenv('PORT', str(port))
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    manager = sm.LifecycleManager(str(project), sys.executable,
                                  monitor_interval=0.2)
    pid = None
    try:
        assert manager.start(source='exit-evidence')['ok'] is True
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current = manager.status(probe_health=True)
            if current.get('running') and current.get('health'):
                break
            time.sleep(0.1)
        assert current.get('health') is True
        pid = int(current['pid'])

        if retirement == 'stop':
            assert manager.stop(source='exit-evidence')['ok'] is True
            expected = {'pid': pid, 'exitCode': 0, 'signal': None}
        else:
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 20.0
            while (manager.status()['workerFailureCount'] == 0
                   and time.monotonic() < deadline):
                manager.reconcile()
                time.sleep(0.05)
            assert manager.status()['lastExitCause'] == 'fatal_signal'
            expected = {'pid': pid, 'exitCode': None,
                        'signal': int(signal.SIGKILL), 'signalName': 'SIGKILL'}

        evidence = manager.status()['lastWorkerProcessExit']
        assert {key: evidence.get(key) for key in expected} == expected
        assert json.loads(manager.state_path.read_text())[
            'lastWorkerProcessExit'] == evidence
        assert 'worker process exit observation' in caplog.text
        # Recorded, then reaped: no zombie outlives the retired generation.
        assert not Path(f'/proc/{pid}').exists()
    finally:
        status = sm.read_lock_status(str(project))
        if status.get('running'):
            try:
                os.kill(int(status['pid']), signal.SIGKILL)
            except OSError:
                pass
        manager.close()


def _shared_checkout_state(project, clock, *, worker, desired='running',
                           pending_recovery=''):
    state_path = project / 'data' / 'server-manager-state.json'
    state_path.write_text(json.dumps({
        'version': sm.STATE_VERSION,
        'desired': desired,
        'observed': 'running',
        'worker': worker,
        'pendingRecoverySource': pending_recovery,
        'consecutiveFailures': 0,
        'workerFailureCount': 0,
        'updatedAt': clock[0],
    }))
    return state_path


# Beyond any Linux pid_max, so the peer host never sees this PID alive.
_PEER_INVISIBLE_PID = 4_194_305


def test_manager_never_judges_or_replaces_a_worker_recorded_on_another_host(
        tmp_path, monkeypatch):
    """Two hosts share one checkout. A peer Supervisor that cannot see the
    owner host's worker PID must not read that absence as an exit, start a
    second worker, or write the shared state the owner host reloads."""
    clock = [10_000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    monkeypatch.setattr(sm, '_hostname', lambda: 'peer-host')
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    project = _project(tmp_path)
    (project / 'data' / '.server.lock').write_text(
        f'{_PEER_INVISIBLE_PID}@owner-host\n')
    state_path = _shared_checkout_state(project, clock, worker={
        'pid': _PEER_INVISIBLE_PID, 'host': 'owner-host',
        'processStartTime': 41500777, 'processCwd': str(project.resolve()),
        'spawnedAt': clock[0] - 3600.0,
    })
    persisted = state_path.read_bytes()
    manager = sm.LifecycleManager(str(project))
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source, **_kwargs: spawned.append(source) or {'ok': True})

    for _ in range(4):
        manager.reconcile()
        clock[0] += 61.0
    status = manager.status()

    assert spawned == []
    assert status['workerFailureCount'] == 0
    assert status['recentFailureCount'] == 0
    assert status['observed'] == 'conflict'
    assert 'owner-host' in status['lastError']
    assert status['worker']['pid'] == _PEER_INVISIBLE_PID

    # Ownership loss on the peer retires through stop(); a persisted
    # desired=stopped would stop the owner host's worker at its next reload.
    stopped = manager.stop(source='supervisor-ownership-lost')
    assert stopped['ok'] is False
    assert 'owner-host' in stopped['message']
    assert manager.status()['desired'] == 'running'
    assert state_path.read_bytes() == persisted

    # Moving the service (or recovering a host recreated under a new name)
    # stays an explicit operator start, and never records the owner host's
    # worker as exited.
    assert manager.start(source='serverctl')['ok'] is True
    assert spawned == ['serverctl']
    assert manager._state['workerFailureCount'] == 0
    assert manager._state['plannedExitCount'] == 0


@pytest.mark.parametrize('pending_recovery', ['', 'manager-restart'])
def test_foreign_instance_lock_blocks_automatic_spawn(
        tmp_path, monkeypatch, pending_recovery):
    clock = [10_000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    monkeypatch.setattr(sm, '_hostname', lambda: 'peer-host')
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    project = _project(tmp_path)
    (project / 'data' / '.server.lock').write_text(
        f'{_PEER_INVISIBLE_PID}@owner-host\n')
    _shared_checkout_state(
        project, clock, worker={}, pending_recovery=pending_recovery)
    manager = sm.LifecycleManager(str(project))
    spawned = []
    monkeypatch.setattr(
        manager, '_spawn',
        lambda source, **_kwargs: spawned.append(source) or {'ok': True})

    for _ in range(4):
        manager.reconcile()
        clock[0] += 61.0
    status = manager.status()

    assert spawned == []
    assert status['workerFailureCount'] == 0
    assert status['observed'] == 'conflict'
    assert 'owner-host' in status['lastError']


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Linux /proc identity')
def test_live_generation_without_the_instance_lock_is_never_replaced(
        tmp_path, monkeypatch):
    """A worker that is still alive past boot grace is not an exit: counting
    it as failed would start a second generation beside it."""
    clock = [10_000.0]
    monkeypatch.setattr(sm, '_now', lambda: clock[0])
    monkeypatch.setattr(sm, 'listener_pids', lambda _p: [])
    monkeypatch.setattr(sm, 'port_accepts', lambda *_a, **_k: False)
    project = _project(tmp_path, 'import time\ntime.sleep(120)\n')
    worker = subprocess.Popen([sys.executable, 'server.py'], cwd=project)
    try:
        manager = sm.LifecycleManager(str(project))
        manager._state['desired'] = 'running'
        manager._state['worker'] = {
            'pid': worker.pid, 'host': sm._hostname(),
            'processStartTime': sm.proc_start_ticks(worker.pid),
            'processCwd': str(project.resolve()),
            'spawnedAt': clock[0] - sm.DEFAULT_BOOT_GRACE - 60.0,
        }
        spawned = []
        monkeypatch.setattr(
            manager, '_spawn',
            lambda source, **_kwargs: spawned.append(source) or {'ok': True})

        for _ in range(4):
            manager.reconcile()
            clock[0] += 61.0
        status = manager.status()

        assert spawned == []
        assert status['workerFailureCount'] == 0
        assert status['worker']['pid'] == worker.pid
        assert status['observed'] == 'degraded'
        assert 'instance lock' in status['lastError']
        assert worker.poll() is None
    finally:
        worker.kill()
        worker.wait(timeout=5)


@pytest.mark.serial
@pytest.mark.parametrize("entry", ["serverctl.py", "server-entry.py"])
def test_serverctl_process_roundtrip(tmp_path, entry):
    """Exercise the real CLI → detached manager → worker control chain."""
    root = Path(__file__).parents[2]
    project = tmp_path / 'cli-project'
    project.mkdir()
    (project / 'data').mkdir()
    (project / 'logs').mkdir()
    for name in (
            'serverctl.py', 'server_manager.py', 'supervisor.py',
            'supervisor.sh', 'supervisor_protocol.py', 'tofu_dotenv.py',
            'runtime_guards.py', 'tofu_db/__init__.py', 'tofu_db/_loopback.py'):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / name, project / name)
    (project / 'bootstrap_pkg').mkdir()
    for name in ('__init__.py', 'resource_policy_generated.py', 'process_roles.py', 'server_entry.py'):
        shutil.copy(root / 'bootstrap_pkg' / name, project / 'bootstrap_pkg' / name)
    shutil.copy(root / 'server.py', project / 'server-entry.py')
    (project / 'server.py').write_text(_WORKER)
    # The worker fixture borrows this checkout's verified frontend graph via
    # PYTHONPATH; retain the real CLI preflight entry point in its project.
    (project / 'scripts').mkdir()
    shutil.copy(root / 'scripts/verify_frontend_dist.py', project / 'scripts/verify_frontend_dist.py')
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
        # The copied CLI imports lib from PYTHONPATH. Without it an editable
        # install resolves lib to whichever checkout the venv was built from,
        # so a worktree run would validate another tree's frontend graph.
        'PYTHONPATH': os.pathsep.join(
            filter(None, (str(root), os.environ.get('PYTHONPATH', '')))),
        'TOFU_SUPERVISOR_PORT': str(manager_port),
        'TOFU_SUPERVISOR_HOST': '127.0.0.1',
        'TOFU_SUPERVISOR_PYTHON': sys.executable,
        # The helper exits on pipe EOF. Normal test teardown closes it, while
        # an abruptly killed pytest worker closes the same non-inherited FD.
        # Either way the detached watchdog has a per-test ownership boundary.
        'TOFU_SUPERVISOR_OWNER_PID': str(owner_lease.pid),
        'TOFU_PROJECT_PATH': str(project),
        # A per-test storage authority outside the project, which is renamed below.
        'TOFU_DATA_DIR': str(tmp_path / 'cli-storage'),
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
            ([sys.executable, entry, 'start', '--wait', '10']
             if entry == 'serverctl.py' else
             [sys.executable, entry, '--port', str(worker_port), '--no-tls']),
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

        if entry == 'server-entry.py':
            # A new human launch supersedes the durable stop through the same
            # manager command boundary, before an instance lock is acquired.
            resumed = subprocess.run(
                [sys.executable, entry, '--port', str(worker_port), '--no-tls'],
                cwd=project, env=env, capture_output=True, text=True, timeout=30)
            assert resumed.returncode == 0, resumed.stdout + resumed.stderr
            persisted = json.loads((project / 'data/server-manager-state.json').read_text())
            assert persisted['desired'] == 'running'
            assert persisted['launchSource'] == 'python-server.py'
            assert sm.read_lock_status(str(project))['running'] is True
            final_stop = subprocess.run(
                [sys.executable, 'serverctl.py', 'stop'], cwd=project, env=env,
                capture_output=True, text=True, timeout=30)
            assert final_stop.returncode == 0, final_stop.stdout + final_stop.stderr

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
