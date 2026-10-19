"""OS-backed exit evidence remains distinct from health-probe guesses."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import server_manager as manager_module

pytestmark = pytest.mark.unit


@pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Linux exit evidence')
@pytest.mark.parametrize('action,signum,exit_code', [
    ('import ctypes; ctypes.string_at(0)', signal.SIGSEGV, None),
    ('import os; os._exit(7)', None, 7),
])
def test_owned_child_exit_evidence_survives_without_reaping(action, signum, exit_code):
    # One byte activates Linux's pipe-collector recursion guard; zero is
    # ignored by piped collectors and would rotate real incident evidence.
    # Only this disposable child's policy changes.
    code = 'import resource; resource.setrlimit(resource.RLIMIT_CORE, (1, 1)); ' + action
    child = subprocess.Popen([sys.executable, '-c', code],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ticks = manager_module.proc_start_ticks(child.pid)
        worker = {'pid': child.pid, 'processStartTime': ticks}
        deadline = time.monotonic() + 5
        observation = {}
        while time.monotonic() < deadline:
            observation = manager_module.proc_exit_observation(worker)
            if observation:
                break
            time.sleep(0.01)
        assert observation['signal'] == signum
        assert observation['exitCode'] == exit_code
        assert observation['pid'] == child.pid
        assert observation['coreDumpFlag'] is False
        assert manager_module.proc_exit_observation({
            'pid': child.pid, 'processStartTime': ticks + 1}) == {}
        # Observation does not consume the parent's wait status.
        assert child.wait(timeout=2) == (-signum if signum else exit_code)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


def test_unknown_or_running_identity_never_invents_an_exit():
    assert manager_module.proc_exit_observation({}) == {}
    assert manager_module.proc_exit_observation({'pid': -1, 'processStartTime': 1}) == {}
    assert manager_module.proc_exit_observation({
        'pid': os.getpid(), 'processStartTime': manager_module.proc_start_ticks(os.getpid())}) == {}


def test_manager_retains_fatal_signal_instead_of_a_stale_probe_error(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'server.py').write_text('# disposable project\n')
    manager = manager_module.LifecycleManager(str(project))
    manager._state['lastError'] = 'HTTPS identity probe timed out; heartbeat not stale'
    evidence = {'pid': 123, 'signal': 11, 'signalName': 'SIGSEGV',
                'exitCode': None, 'rawStatus': 11, 'coreDumpFlag': False}
    monkeypatch.setattr(manager_module, 'proc_exit_observation', lambda _worker: evidence)
    monkeypatch.setattr(manager_module, 'cgroup_oom_kill_count', lambda: 0)
    manager._record_failure()
    assert manager._state['lastExitCause'] == 'fatal_signal'
    assert 'SIGSEGV' in manager._state['lastFailureReason']
    assert 'signal sender unknown' in manager._state['lastFailureReason']
    assert manager.status(probe_health=False)['lastWorkerProcessExit'] == evidence


def test_new_unknown_failure_does_not_reuse_old_signal_evidence(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'server.py').write_text('# disposable project\n')
    manager = manager_module.LifecycleManager(str(project))
    manager._state['lastWorkerProcessExit'] = {'signal': 11}
    monkeypatch.setattr(manager_module, 'proc_exit_observation', lambda _worker: {})
    monkeypatch.setattr(manager_module, 'cgroup_oom_kill_count', lambda: 0)
    manager._record_failure()
    assert manager._state['lastWorkerProcessExit'] == {}
    assert manager._state['lastExitCause'] == 'unexpected_exit'


def test_frontend_preflight_passes_worker_interpreter_to_nested_canary(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'serverctl.py').write_text(
        'import os,sys\n'
        'assert os.environ["TOFU_PYTHON"] == os.environ["TOFU_EXPECTED_PREFLIGHT_PYTHON"]\n'
        'assert sys.argv[1] == "prepare-frontend"\n')
    ok, error = manager_module.run_frontend_preflight(
        str(project), sys.executable,
        {'PATH': '/usr/bin:/bin', 'TOFU_PYTHON': '/missing/stale/python',
         'TOFU_EXPECTED_PREFLIGHT_PYTHON': sys.executable},
        'isolated crash-recovery contract')
    assert ok is True
    assert error == ''
