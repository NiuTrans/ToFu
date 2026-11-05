"""Preparation budgets and failed builders preserve a restartable old service."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import serverctl
import server_manager
from serverctl_pkg import application_restart, native_engine

pytestmark = pytest.mark.unit


def _wait_until_dead(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        stat = Path(f'/proc/{pid}/stat')
        if not stat.exists() or stat.read_text().split(') ')[1].startswith('Z '):
            return
        time.sleep(0.02)
    pytest.fail('compiler descendant survived preparation cancellation')


def test_outer_restart_budgets_cover_all_preparation_stages(monkeypatch):
    inner = (native_engine.PREPARATION_TOTAL_TIMEOUT_SECONDS + 30
             + serverctl._FRONTEND_BUILD_LOCK_TIMEOUT_SECONDS
             + serverctl._FRONTEND_REPAIR_TIMEOUT_SECONDS)
    assert server_manager.DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT > inner
    assert application_restart.PREPARATION_TIMEOUT_SECONDS >= server_manager.DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT
    calls = []
    monkeypatch.setattr(serverctl, 'ensure_manager', lambda: None)
    monkeypatch.setattr(serverctl, '_request', lambda path, body, timeout: calls.append(timeout) or {})
    for action in ('start', 'restart'):
        serverctl._post(action)
    assert all(timeout > server_manager.DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT + 305 + 10 for timeout in calls)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX compiler descendants')
def test_frontend_timeout_joins_descendants_and_releases_build_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', lambda: (_ for _ in ()).throw(RuntimeError('stale')))
    monkeypatch.setattr(serverctl, '_FRONTEND_REPAIR_TIMEOUT_SECONDS', 0.7)
    pid_file = tmp_path / 'builder.pid'
    child = ('import os,signal,time; from pathlib import Path; '
             'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
             'print("frontend compiler started",flush=True); '
             f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)')
    program = ('import subprocess,sys,time; '
               f'subprocess.Popen([sys.executable,"-c",{child!r}]); '
               'time.sleep(60)')
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: [sys.executable, '-c', program])
    real_clock = time.monotonic
    started = real_clock()
    def deadline_after_startup():
        try:
            ready = pid_file.read_text().isdigit()
        except FileNotFoundError:
            ready = False
        # Test the timeout only after the descendant can receive it. A real
        # watchdog still bounds a broken fixture's startup independently.
        return started + 1 if ready or real_clock() - started > 20 else started
    try:
        with monkeypatch.context() as clock:
            clock.setattr(native_engine.time, 'monotonic', deadline_after_startup)
            error = serverctl.prepare_source_frontend_artifact('test', capture_output=True)
        assert 'timed out' in error
        assert 'frontend compiler started' in error
        assert pid_file.exists(), 'fixture must reach descendant startup'
        pid = int(pid_file.read_text())
        _wait_until_dead(pid)
        import fcntl
        with (tmp_path / 'data/.frontend-build.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_frontend_output_budget_rejects_noisy_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    calls = []
    def validate():
        calls.append(True)
        if len(calls) <= 2:
            raise RuntimeError('stale')
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', validate)
    monkeypatch.setattr(native_engine, 'MAX_LOG_BYTES', 128)
    monkeypatch.setattr(serverctl, '_source_frontend_build_command',
                        lambda: [sys.executable, '-c', 'print("x"*1024)'])
    assert 'log budget' in serverctl.prepare_source_frontend_artifact('test', capture_output=True)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux descendant liveness proof')
def test_preparation_cli_sigterm_joins_separately_sessioned_builder(tmp_path):
    pid_file = tmp_path / 'compiler.pid'
    child = ('import os,signal,time; from pathlib import Path; '
             'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
             f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)')
    program = (
        'import argparse,os,sys; from pathlib import Path; import serverctl; '
        'from serverctl_pkg.native_engine import _run; '
        'serverctl._repair_source_frontend_artifact = lambda *_: '
        f'_run([sys.executable,"-c",{child!r}],cwd=Path({str(tmp_path)!r}),'
        'environment=dict(os.environ),timeout=60,termination_grace=0.1); '
        'raise SystemExit(serverctl.cmd_prepare_frontend(argparse.Namespace(operation="test")))')
    process = subprocess.Popen([sys.executable, '-c', program],
                               cwd=Path(serverctl.__file__).parent,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        assert pid_file.exists(), 'fixture must reach builder startup'
        process.terminate()
        _, error = process.communicate(timeout=10)
        assert process.returncode == 1 and 'cancelled' in error
        assert 'Traceback' not in error
        _wait_until_dead(int(pid_file.read_text()))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
