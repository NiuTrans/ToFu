"""Lifecycle-manager contracts for worker runtime replicas.

The manager chooses the interpreter for every worker spawn, owns the leases
that keep that replica alive, builds missing replicas only after a worker is
ready, and attributes pre-readiness failures before discarding a replica.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import server_manager as sm
import supervisor_protocol
from serverctl_pkg import python_bytecode_cache as cache
from serverctl_pkg import application_restart
from serverctl_pkg import runtime_replica as rr


pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name == 'nt', reason='POSIX lease contract'),
]


@pytest.fixture
def spawnable(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    (project / 'server.py').write_text('raise SystemExit(0)\n')
    for name in ('_TOFU_RUNTIME_PORT', 'PORT', 'TOFU_HEARTBEAT_DIR', 'PYTHONPYCACHEPREFIX'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_DATA_DIR', str(project))
    monkeypatch.setattr(sm, 'run_frontend_preflight', lambda *_args, **_kwargs: (True, ''))
    monkeypatch.setattr(cache, 'prepare_server_python_cache', lambda *_args, **_kwargs: (
        cache.ServerPythonCacheActivation(selected=False, managed=False, reason='test',
                                          mode='off')))
    monkeypatch.setattr(sm, 'read_lock_status', lambda _path: {
        'running': False, 'projectPath': str(project.resolve())})
    monkeypatch.setattr(sm, 'port_accepts', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(sm, 'listener_pids', lambda _port: [])
    monkeypatch.setattr(sm, 'proc_start_ticks', lambda _pid: 99)
    launched = []

    class _Process:
        pid = 4321

    def popen(argv, **options):
        launched.append((list(argv), options))
        return _Process()

    monkeypatch.setattr(sm.subprocess, 'Popen', popen)
    return SimpleNamespace(project=project, launched=launched)


def _selection(monkeypatch, runtime_for):
    calls = []

    def select(python, environ, *, avoid=''):
        calls.append({'python': python, 'avoid': avoid,
                      'mode': environ.get('TOFU_RUNTIME_REPLICA')})
        return runtime_for(python)

    monkeypatch.setattr(rr, 'select_worker_runtime', select)
    return calls


def test_spawn_runs_the_selected_replica_and_builds_only_after_readiness(
        spawnable, tmp_path, monkeypatch):
    leases = [os.open(tmp_path / name, os.O_RDWR | os.O_CREAT, 0o600)
              for name in ('venv.lease', 'base.lease')]
    calls = _selection(monkeypatch, lambda _python: rr.WorkerRuntime(
        '/replicas/venv/g-1/bin/python', True, 'replica',
        generations=['/replicas/venv/g-1', '/replicas/base/g-1'], lease_fds=list(leases),
        build_root='/replicas', build_pythons=['/slow/env/bin/python'],
        status={'mode': 'auto'}))
    builds = []
    monkeypatch.setattr(rr, 'request_build', lambda pythons, root, *, launcher=None: (
        builds.append((list(pythons), str(root), launcher)) or True))
    manager = sm.LifecycleManager(str(spawnable.project))

    assert manager._spawn('test')['ok'] is True

    argv, _options = spawnable.launched[0]
    assert argv[:2] == ['/replicas/venv/g-1/bin/python', 'server.py']
    assert calls == [{'python': manager.python, 'avoid': '', 'mode': 'off'}]
    runtime = manager.status()['workerRuntime']
    assert runtime['replica'] is True and runtime['reason'] == 'replica'
    assert runtime['generationPaths'] == ['/replicas/venv/g-1', '/replicas/base/g-1']
    assert builds == [], 'copying must not compete with the worker boot'
    manager._request_pending_runtime_build()
    manager._request_pending_runtime_build()
    assert builds == [(['/slow/env/bin/python'], '/replicas', manager.python)]
    for lease in leases:
        os.fstat(lease)
    manager._release_worker_leases()
    for lease in leases:
        with pytest.raises(OSError):
            os.fstat(lease)


def test_selection_failure_never_blocks_a_spawn(spawnable, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError('replica helper broken')

    monkeypatch.setattr(rr, 'select_worker_runtime', broken)
    manager = sm.LifecycleManager(str(spawnable.project))

    assert manager._spawn('test')['ok'] is True
    assert spawnable.launched[0][0][0] == manager.python
    assert manager.status()['workerRuntime']['reason'] == 'runtime-helper-unavailable'


def test_a_pre_ready_replica_failure_is_confirmed_on_the_source_before_rejection(
        spawnable, monkeypatch):
    rejected = []
    monkeypatch.setattr(rr, 'reject_generation',
                        lambda generation, reason: rejected.append((generation, reason)))
    calls = _selection(monkeypatch, lambda python: rr.WorkerRuntime(
        python, False, 'replica-suspect', status={}))
    manager = sm.LifecycleManager(str(spawnable.project))
    manager._state['workerRuntime'] = {'replica': True, 'generationPaths': ['/replicas/g-1']}
    manager._state['workerReadyLoggedAt'] = 0.0

    manager._note_unready_runtime('ImportError: libfoo.so')
    assert manager.status()['runtimeSuspect']['generationPath'] == '/replicas/g-1'
    assert rejected == []

    assert manager._spawn('retry')['ok'] is True
    assert calls[-1]['avoid'] == '/replicas/g-1'
    assert spawnable.launched[-1][0][0] == manager.python
    manager._settle_runtime_suspect()  # the source-backed worker became ready
    assert rejected == [('/replicas/g-1', 'ImportError: libfoo.so')]
    assert manager._state['runtimeSuspect'] == {}


def test_a_failure_shared_by_the_source_interpreter_keeps_the_replica(spawnable, monkeypatch):
    rejected = []
    monkeypatch.setattr(rr, 'reject_generation', lambda *args: rejected.append(args))
    manager = sm.LifecycleManager(str(spawnable.project))
    manager._state['runtimeSuspect'] = {'generationPath': '/replicas/g-1', 'reason': 'boom'}
    manager._state['workerRuntime'] = {'replica': False, 'generationPaths': []}
    manager._state['workerReadyLoggedAt'] = 0.0

    manager._note_unready_runtime('port in use')
    manager._settle_runtime_suspect()

    assert manager._state['runtimeSuspect'] == {}
    assert rejected == []


def test_failures_after_readiness_are_not_attributed_to_the_runtime(spawnable):
    manager = sm.LifecycleManager(str(spawnable.project))
    manager._state['workerRuntime'] = {'replica': True, 'generationPaths': ['/replicas/g-1']}
    manager._state['workerReadyLoggedAt'] = 1234.0

    manager._note_unready_runtime('wedged after an hour')

    assert manager._state['runtimeSuspect'] == {}


def test_a_newer_generation_supersedes_the_suspect(spawnable, tmp_path, monkeypatch):
    lease = os.open(tmp_path / 'lease', os.O_RDWR | os.O_CREAT, 0o600)
    _selection(monkeypatch, lambda _python: rr.WorkerRuntime(
        '/replicas/g-2/bin/python', True, 'replica', generations=['/replicas/g-2'],
        lease_fds=[lease], status={}))
    manager = sm.LifecycleManager(str(spawnable.project))
    manager._state['runtimeSuspect'] = {'generationPath': '/replicas/g-1', 'reason': 'boom'}

    assert manager._spawn('test')['ok'] is True

    assert manager._state['runtimeSuspect'] == {}
    manager._release_worker_leases()


def _fake_replica(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / 'env' / 'bin' / 'python'
    source.parent.mkdir(parents=True)
    source.write_text('#!/bin/sh\n')
    source.chmod(0o755)
    generation = tmp_path / 'replicas' / 'env-0' / 'g-1'
    (generation / 'bin').mkdir(parents=True)
    (generation / 'bin' / 'python').write_text('')
    (generation / rr.MANIFEST_NAME).write_text(json.dumps({
        'schema': rr.SCHEMA, 'sourcePrefix': str((tmp_path / 'env').resolve()),
        'sourcePython': str(source)}))
    return source, generation / 'bin' / 'python'


def test_the_manager_never_pins_a_replica_interpreter(spawnable, tmp_path):
    source, replica_python = _fake_replica(tmp_path)

    manager = sm.LifecycleManager(str(spawnable.project), str(replica_python))

    assert manager.python == str(source)


def test_launchers_started_from_a_replica_hand_off_the_source_interpreter(
        tmp_path, monkeypatch):
    source, replica_python = _fake_replica(tmp_path)
    monkeypatch.setattr(sys, 'executable', str(replica_python))
    supervisor_environment = {}

    def run_launcher(_argv, **options):
        supervisor_environment.update(options['env'])
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(supervisor_protocol.subprocess, 'run', run_launcher)
    supervisor_protocol._launch_supervisor(str(tmp_path), {})
    restart_commands = []

    def run_control(command, **_options):
        restart_commands.append(command)
        return application_restart.RESULT_PREFIX + json.dumps({'ok': True})

    monkeypatch.setattr(application_restart, '_run', run_control)
    application_restart.run_application_restart_control(str(tmp_path), stage='delegate')

    assert supervisor_environment['TOFU_SUPERVISOR_PYTHON'] == str(source)
    assert restart_commands[0][0] == str(source)
