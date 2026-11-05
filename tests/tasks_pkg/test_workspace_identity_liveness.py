"""Task admission: a slow repository read completes, a hung one retires.

Before a worker may run, every task start reads its root's Git identity
(``rev-parse --is-bare-repository --git-common-dir`` and ``config
core.worktree`` through the explicit Git directory) and workspace policy
(``config --local tofu.workspaceMode``). These reads are constant-size, yet on
the shared network checkout they wait as long as the volume does: on
2026-11-03 their fixed 10 s deadline timed out 155 times for about two dozen
tasks in four and a half hours, while stall-supervised snapshot commands in
the same log ran for minutes without one stall-window retirement. A slow but
progressing read must therefore never refuse a start, a read blocked on an
unresponsive volume must still end admission, and Stop must end it promptly as
a cancellation. A fake ``git`` stands in for the volume: it delays (``slow``)
or blocks forever (``hung``) before executing the real Git for the first call
that carries one argument (``rev-parse``, ``core.worktree`` or
``tofu.workspaceMode``).

Deadlines are dilated instead of waiting minutes: the stall window and every
historical wall-clock ``subprocess.run`` timeout of the admission modules are
divided by ``DILATION``, a volume ``DILATION`` times slower than they assume.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import types

import pytest

from lib.project_mod import workspace_policy
from lib.project_mod.activity_supervised_process import ProcessStalled
from lib.tasks_pkg import workspace_delivery as delivery

pytestmark = pytest.mark.unit

DILATION = 30.0
WINDOW = 90.0 / DILATION
TASK_ID = 'identity-liveness-task'
ADMISSION_MODULES = (delivery, workspace_policy)


def git(root, *args):
    cp = subprocess.run(['git', '-C', str(root), *args], text=True,
                        capture_output=True, timeout=20)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith('GIT_'):
            monkeypatch.delenv(name)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.delenv('TOFU_WORKSPACE_MODE', raising=False)
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope',
                        lambda *a, **kw: None)
    from lib import integration_state_repository as state
    monkeypatch.setattr(state, 'find_workspace', lambda *a, **kw: None)
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Identity liveness')
    git(root, 'config', 'user.email', 'liveness@example.invalid')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    try:
        yield root
    finally:
        import lib.project_mod as project_mod
        project_mod.config.clear_conv_state(TASK_ID)


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
    for module in ADMISSION_MODULES:
        # Optional so this specification also runs, and fails for the real
        # reason, against code that only has wall-clock deadlines.
        monkeypatch.setattr(module, 'subprocess', proxy, raising=False)
        monkeypatch.setattr(module, 'GIT_STALL_SECONDS', WINDOW, raising=False)


def fake_git(tmp_path, monkeypatch, mode, needle, seconds=0.0):
    """Install a git that is slow or hung for the first call carrying ``needle``.

    ``hung`` blocks in one request that never completes; ``slow`` keeps
    completing small requests for ``seconds`` before running the real Git.
    Every other call executes the real Git immediately.
    """
    real = shutil.which('git')
    marker = tmp_path / f'{mode}-{needle}.pid'
    slow = tmp_path / 'slow_volume.py'
    slow.write_text(f'''import os, sys, time
deadline = time.monotonic() + {seconds!r}
while time.monotonic() < deadline:
    time.sleep(0.02)
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
''')
    delay = ('exec sleep 1000000' if mode == 'hung'
             else f'exec "{sys.executable}" "{slow}" "$@"')
    script = tmp_path / f'git-{mode}-{needle}'
    script.write_text(f'''#!/bin/sh
for arg in "$@"; do
  if [ "$arg" = "{needle}" ] && [ ! -e "{marker}" ]; then
    echo $$ > "{marker}"
    {delay}
  fi
done
exec "{real}" "$@"
''')
    script.chmod(0o755)
    for module in ADMISSION_MODULES:
        monkeypatch.setattr(module, 'git_executable', lambda: str(script))
    return marker


def _task(root, **values):
    return {'id': TASK_ID, 'convId': 'conversation', '_userId': 7, 'status': 'running',
            'config': {'projectPath': str(root)}, **values}


def assert_retired(marker):
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize('needle', ['rev-parse', 'core.worktree', 'tofu.workspaceMode'])
def test_slow_admission_read_outlives_every_deadline_and_starts_the_task(
        repo, tmp_path, monkeypatch, dilated, needle):
    marker = fake_git(tmp_path, monkeypatch, 'slow', needle, seconds=1.2 * WINDOW)
    task = _task(repo)
    started = time.monotonic()

    assert delivery.bind_worker_workspace(task) is None

    assert time.monotonic() - started > 1.2 * WINDOW
    assert marker.exists()
    assert task['_workspaceExecutionModes'] == {os.path.realpath(repo): 'direct'}
    assert not delivery.execution_repositories(task)


@pytest.mark.parametrize('needle, message', [
    ('rev-parse', 'metadata inspection made no progress'),
    ('core.worktree', 'metadata inspection made no progress'),
    ('tofu.workspaceMode', 'Workspace policy read failed'),
])
def test_hung_admission_read_is_retired_after_one_stall_window(
        repo, tmp_path, monkeypatch, dilated, needle, message):
    marker = fake_git(tmp_path, monkeypatch, 'hung', needle)
    task = _task(repo)
    config = dict(task['config'])
    started = time.monotonic()

    with pytest.raises(delivery.WorkspaceBindingError, match=message) as raised:
        delivery.bind_worker_workspace(task)

    # Bounded: one stall window plus process-group retirement, never forever.
    assert WINDOW <= time.monotonic() - started < WINDOW + 30
    assert_retired(marker)
    assert isinstance(raised.value.__cause__, (ProcessStalled, workspace_policy.WorkspacePolicyError))
    assert task['config'] == config
    assert '_workspaceRootPolicy' not in task


@pytest.mark.parametrize('needle', ['rev-parse', 'core.worktree', 'tofu.workspaceMode'])
def test_stop_retires_a_blocked_admission_read_without_waiting_for_the_window(
        repo, tmp_path, monkeypatch, dilated, needle):
    from lib.llm.llm_errors import AbortedError
    for module in ADMISSION_MODULES:
        monkeypatch.setattr(module, 'GIT_STALL_SECONDS', 20 * WINDOW, raising=False)
    marker = fake_git(tmp_path, monkeypatch, 'hung', needle)
    task = _task(repo, abort_event=threading.Event())
    stopped = []

    def stop_when_blocked():
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped.append(time.monotonic())
        task['abort_event'].set()  # TaskRuntime.abort signals only the event.

    stopper = threading.Thread(target=stop_when_blocked)
    stopper.start()
    try:
        with pytest.raises(AbortedError, match='cancelled during workspace admission'):
            delivery.bind_worker_workspace(task)
    finally:
        stopper.join(30)

    assert time.monotonic() - stopped[0] < 10 * WINDOW
    assert_retired(marker)
    assert '_workspaceRootPolicy' not in task
