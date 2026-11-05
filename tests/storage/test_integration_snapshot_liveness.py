"""Isolated-task admission: a slow snapshot completes, a hung one retires.

The input snapshot runs whole-tree Git commands on the writer's checkout. On a
network volume their duration follows the volume, not the work, so a slow but
progressing command must never be refused, while a command blocked on an
unresponsive volume must still end admission. A fake ``git`` stands in for the
volume: it delays (``slow``) or blocks forever (``hung``) before executing the
real Git for the first call of one subcommand.

Deadlines are dilated instead of waiting minutes: every snapshot deadline, the
historical wall-clock ``subprocess.run`` timeouts as well as the stall window,
is divided by ``DILATION``. That is a volume ``DILATION`` times slower than
those deadlines assume.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state

pytestmark = pytest.mark.unit

DILATION = 30.0
WINDOW = 90.0 / DILATION


def git(root, *args):
    cp = subprocess.run(['git', '-C', str(root), *args], text=True,
                        capture_output=True, timeout=20)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **k: None)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', '')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Snapshot liveness')
    git(root, 'config', 'user.email', 'liveness@example.invalid')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    (root / 'base.txt').write_text('uncommitted user edit\n')
    (root / 'untracked.txt').write_text('borrowed context\n')
    try:
        yield root
    finally:
        install_runtime_for_test(None)


@pytest.fixture
def dilated(monkeypatch):
    real = subprocess

    class Dilated(types.ModuleType):
        def __getattr__(self, name):
            return getattr(real, name)

    def run(*args, timeout=None, **kwargs):
        return real.run(*args, timeout=None if timeout is None else timeout / DILATION, **kwargs)

    proxy = Dilated('subprocess')
    proxy.run = run
    monkeypatch.setattr(ic, 'subprocess', proxy)
    # Optional so this specification also runs, and fails for the real
    # reason, against code that only has wall-clock deadlines.
    monkeypatch.setattr(ic, '_SNAPSHOT_STALL_SECONDS', WINDOW, raising=False)


def fake_git(tmp_path, monkeypatch, mode, subcommand, seconds=0.0):
    """Install a git that is slow or hung for the first call of one subcommand.

    ``hung`` blocks in one request that never completes; ``slow`` keeps
    completing small requests for ``seconds`` before running the real Git.
    Every other call executes the real Git immediately.
    """
    real = shutil.which('git')
    marker = tmp_path / f'{mode}-{subcommand}.pid'
    slow = tmp_path / 'slow_volume.py'
    slow.write_text(f'''import os, sys, time
deadline = time.monotonic() + {seconds!r}
while time.monotonic() < deadline:
    time.sleep(0.02)
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
''')
    delay = ('exec sleep 1000000' if mode == 'hung'
             else f'exec "{sys.executable}" "{slow}" "$@"')
    script = tmp_path / f'git-{mode}-{subcommand}'
    script.write_text(f'''#!/bin/sh
skip=
for arg in "$@"; do
  if [ -n "$skip" ]; then skip=; continue; fi
  if [ "$arg" = "-c" ]; then skip=1; continue; fi
  if [ "$arg" = "--no-optional-locks" ]; then continue; fi
  if [ "$arg" = "{subcommand}" ] && [ ! -e "{marker}" ]; then
    echo $$ > "{marker}"
    {delay}
  fi
  break
done
exec "{real}" "$@"
''')
    script.chmod(0o755)
    monkeypatch.setattr(ic, 'git_executable', lambda: str(script))
    return marker


def assert_retired(marker):
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def assert_not_admitted(root, tmp_path, task_id):
    assert state.find_workspace(str(root), task_id, user_id=1) is None
    assert not any(task_id in path.name for path in (tmp_path / 'managed').rglob('*'))


@pytest.mark.parametrize('subcommand', ['ls-files', 'read-tree', 'diff', 'add', 'write-tree'])
def test_slow_snapshot_git_outlives_every_deadline_and_admits_the_task(
        repo, tmp_path, monkeypatch, dilated, subcommand):
    root = repo
    status = git(root, 'status', '--porcelain')
    index = root / '.git' / 'index'
    before = index.read_bytes()
    marker = fake_git(tmp_path, monkeypatch, 'slow', subcommand, seconds=1.2 * WINDOW)
    started = time.monotonic()
    created = ic.create_task_workspace(str(root), 'slow-task', user_id=1, conversation_id='conv')
    assert time.monotonic() - started > 1.2 * WINDOW
    assert marker.exists()
    work = Path(created['workspacePath'])
    assert (work / 'base.txt').read_text() == 'uncommitted user edit\n'
    assert (work / 'untracked.txt').read_text() == 'borrowed context\n'
    assert state.find_workspace(str(root), 'slow-task', user_id=1) is not None
    assert index.read_bytes() == before
    assert git(root, 'status', '--porcelain') == status


@pytest.mark.parametrize('subcommand', ['add', 'diff'])
def test_hung_snapshot_git_is_retired_after_one_stall_window(
        repo, tmp_path, monkeypatch, dilated, subcommand):
    root = repo
    marker = fake_git(tmp_path, monkeypatch, 'hung', subcommand)
    started = time.monotonic()
    with pytest.raises(ic.IntegrationError, match='made no progress'):
        ic.create_task_workspace(str(root), 'hung-task', user_id=1, conversation_id='conv')
    # Bounded: one stall window plus process-group retirement, never forever.
    assert WINDOW <= time.monotonic() - started < WINDOW + 30
    assert_retired(marker)
    assert_not_admitted(root, tmp_path, 'hung-task')


def test_stop_retires_a_blocked_snapshot_git_without_waiting_for_the_window(
        repo, tmp_path, monkeypatch, dilated):
    root = repo
    monkeypatch.setattr(ic, '_SNAPSHOT_STALL_SECONDS', 20 * WINDOW, raising=False)
    marker = fake_git(tmp_path, monkeypatch, 'hung', 'add')
    task = {'aborted': False}
    stopped = []

    def stop_when_blocked():
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped.append(time.monotonic())
        task['aborted'] = True

    stopper = threading.Thread(target=stop_when_blocked)
    stopper.start()
    try:
        with pytest.raises(ic.IntegrationError, match='cancelled'):
            ic.create_task_workspace(str(root), 'stopped-task', user_id=1,
                                     conversation_id='conv', task=task)
    finally:
        stopper.join(30)
    assert time.monotonic() - stopped[0] < 10 * WINDOW
    assert_retired(marker)
    assert_not_admitted(root, tmp_path, 'stopped-task')
