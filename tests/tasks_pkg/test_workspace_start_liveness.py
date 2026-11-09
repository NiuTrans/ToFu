"""Task start never fails on a slow Git command; only an unresponsive one ends it.

2026-11-06, start-path family. Lane 3c2ffb4b (worker-d6b8d301531df8339c2233f1)
spent 1,697 s preparing its workspace and then failed binding: ``git worktree
list --porcelain`` ran under the fixed 30 s wall-clock default of
``integration_control._git``, inside ``delivery._canonical`` /
``bind_task_workspace``. The 2026-11-03 decision already requires every Git read
that admission depends on to run under the shared ``GIT_STALL_SECONDS`` window
with no total deadline; these reads had been missed. Lane c7081533
(worker-a0c7278326a438c219e34dd4) failed with "Git worktree materialization
stalled for 90s (files 0/unknown)". Its window renewed only on file-count
progress lines, which registration never prints and a checkout prints only
after it has read its tree.

A fake ``git`` stands in for the contended volume. Deadlines are dilated, as
in ``test_workspace_identity_liveness.py``: the volume is ``DILATION`` times
slower than they assume.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import types

import pytest

from lib import integration_control as ic
from lib.project_mod import delivery
from lib.project_mod.worktree_process import run_worktree_add

pytestmark = pytest.mark.unit

DILATION = 30.0
WINDOW = 90.0 / DILATION
OLD_DEADLINE = 30.0 / DILATION


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
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Start liveness')
    git(root, 'config', 'user.email', 'liveness@example.invalid')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    return root


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
    monkeypatch.setattr(ic, 'GIT_STALL_SECONDS', WINDOW)


def fake_git(tmp_path, monkeypatch, mode, needle, seconds=0.0):
    """A git that is slow (still executing) or hung for the first ``needle`` call."""
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
    monkeypatch.setattr(ic, 'git_executable', lambda: str(script))
    return marker


@pytest.mark.parametrize('needle', ['worktree', '--git-common-dir'])
def test_slow_binding_identity_read_outlives_the_old_wall_clock_deadline(
        repo, tmp_path, monkeypatch, dilated, needle):
    marker = fake_git(tmp_path, monkeypatch, 'slow', needle, seconds=1.5 * OLD_DEADLINE)
    started = time.monotonic()

    root, workspace = delivery._canonical(str(repo))

    assert time.monotonic() - started > 1.5 * OLD_DEADLINE
    assert marker.exists()
    assert root == workspace == repo.resolve()


def test_stop_retires_a_blocked_binding_read_without_waiting_for_the_window(
        repo, tmp_path, monkeypatch, dilated):
    monkeypatch.setattr(ic, 'GIT_STALL_SECONDS', 20 * WINDOW)
    marker = fake_git(tmp_path, monkeypatch, 'hung', 'worktree')
    stop = threading.Event()

    def stop_when_blocked():
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()

    stopper = threading.Thread(target=stop_when_blocked)
    stopper.start()
    started = time.monotonic()
    try:
        with pytest.raises(ic.IntegrationError, match='cancelled'):
            delivery._canonical(str(repo), cancel=stop)
    finally:
        stopper.join()
    assert time.monotonic() - started < 20 * WINDOW


BUSY_WITHOUT_OUTPUT = ('import time\n'
                       'end = time.monotonic() + {seconds}\n'
                       'while time.monotonic() < end:\n'
                       '    sum(range(1000))\n')


def test_materialization_lives_while_git_executes_without_file_progress(tmp_path):
    idle = 3.0
    started = time.monotonic()

    completed = run_worktree_add(
        [sys.executable, '-c', BUSY_WITHOUT_OUTPUT.format(seconds=1.5 * idle)],
        cwd=tmp_path, env=dict(os.environ), idle_timeout=idle)

    assert completed.returncode == 0
    assert time.monotonic() - started > 1.5 * idle


def test_materialization_still_retires_a_git_that_never_runs(tmp_path):
    idle = 3.0
    started = time.monotonic()

    with pytest.raises(RuntimeError, match='stalled'):
        run_worktree_add(['sleep', '600'], cwd=tmp_path, env=dict(os.environ),
                         idle_timeout=idle)

    # One window, plus sampling and process-group retirement; never forever.
    assert idle <= time.monotonic() - started < idle + 30
