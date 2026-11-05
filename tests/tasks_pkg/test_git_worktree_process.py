"""Controlled materialization renews only real progress and retires children."""
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from lib.project_mod.worktree_process import run_worktree_add
from lib.project_mod.tool_admission import Cancellation

pytestmark = pytest.mark.unit


def run(tmp_path, script, *, idle=None, cancel=None):
    """Run a child under the product idle budget unless a test times the stall.

    Interpreter start-up counts against the idle window, so a short budget is
    only for tests whose child must stall; others keep the product contract.
    """
    budget = {} if idle is None else {'idle_timeout': idle}
    return run_worktree_add([sys.executable, '-u', '-c', script],
                            cwd=tmp_path, env=os.environ.copy(),
                            cancellation=cancel, **budget)


class ProgressClock:
    """Materialization clock that advances ``step`` seconds when the wait loop
    first observes a newly accepted progress frame; only then may the child
    print the next one (``ack-<n>``). Start-up and scheduling delays take no
    time, so whether the idle budget runs out is decided by renewal alone."""

    def __init__(self, step, acks):
        self.step, self.acks = step, acks
        self.now, self.accepted, self.observed = 0.0, 0, 0

    def monotonic(self):
        if threading.current_thread().name == 'git-worktree-output':
            self.accepted += 1
        elif self.accepted > self.observed:
            self.observed = self.accepted
            self.now += self.step
            (self.acks / f'ack-{self.observed}').touch()
        return self.now


@pytest.mark.skipif(os.name != 'posix', reason='POSIX native progress emitter')
@pytest.mark.parametrize('label', ['Updating', 'Checking out'])
def test_slow_healthy_progress_can_outlive_original_deadline(tmp_path, monkeypatch, label):
    from lib.project_mod import worktree_process
    clock = ProgressClock(step=0.6, acks=tmp_path)
    monkeypatch.setattr(worktree_process, 'time', SimpleNamespace(monotonic=clock.monotonic))
    result = run(tmp_path, f"""
import os, sys, time
for n in range(1, 9):
    sys.stderr.write(f'{label} files: {{n*10}}% ({{n}}/8)\\r')
    sys.stderr.flush()
    while not os.path.exists(os.path.join({str(tmp_path)!r}, f'ack-{{n}}')):
        time.sleep(0.01)
""", idle=1.0)
    assert result.returncode == 0
    assert '8/8' in result.stderr
    # Eight frames 0.6 s apart outlive the 1 s budget only because each renews it.
    assert clock.now == pytest.approx(8 * 0.6)


@pytest.mark.parametrize('frame', ['noise', 'Updating files: 10% (1/8)',
                                  'Updating files: 200% (99/8)'])
def test_output_noise_repeated_or_invalid_count_does_not_renew(tmp_path, frame):
    with pytest.raises(RuntimeError, match='stalled'):
        run(tmp_path, f"""
import sys, time
while True:
    sys.stderr.write({frame!r} + '\\r'); sys.stderr.flush(); time.sleep(0.03)
""", idle=0.4)


def test_cancellation_reaps_child_and_closes_reader(tmp_path):
    task = {'aborted': False}
    signal = threading.Timer(0.2, lambda: task.update(aborted=True))
    signal.start()
    try:
        with pytest.raises(RuntimeError, match='cancelled'):
            run(tmp_path, 'import time; time.sleep(30)', cancel=Cancellation(task))
    finally:
        signal.join()
    assert not any(t.name == 'git-worktree-output' for t in threading.enumerate())


@pytest.mark.skipif(os.name != 'posix', reason='POSIX inherited-pipe helper')
def test_leader_exit_retires_pipe_holding_descendant(tmp_path):
    from lib.project_mod.process_group import group_has_live_members
    result = run(tmp_path, """
import os, subprocess, sys
print('owned-group=' + str(os.getpgrp()), flush=True)
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
""")
    assert result.returncode == 0
    pgid = int(result.stderr.split('owned-group=')[1].split()[0])
    assert not group_has_live_members(pgid)


def test_failure_keeps_bounded_diagnostic_tail(tmp_path):
    # Output without progress frames never renews the idle window, so the
    # 0.4 s stall default raced interpreter start-up on a loaded host.
    result = run(tmp_path, "import sys; print('x' * 100000); print('terminal error'); sys.exit(7)",
                 idle=10)
    assert result.returncode == 7
    assert len(result.stderr) <= 8192
    assert 'terminal error' in result.stderr


@pytest.mark.parametrize('idle', [0, -1, float('inf'), float('nan')])
def test_invalid_budget_refuses_before_launch(tmp_path, idle):
    with pytest.raises(ValueError, match='positive'):
        run(tmp_path, "raise AssertionError('must not execute')", idle=idle)


def test_late_reader_after_exit_is_not_mistaken_for_a_pipe_holder(tmp_path, monkeypatch):
    """After the owned group is retired only buffered bytes remain. How late
    the output reader runs is host scheduling, never pipe-holder evidence."""
    from lib.project_mod import worktree_process

    class LateEndOfFile:
        def __getattr__(self, name):
            return getattr(os, name)

        def read(self, fd, size):
            chunk = os.read(fd, size)
            if not chunk and threading.current_thread().name == 'git-worktree-output':
                time.sleep(1.5)  # a reader descheduled past every former join budget
            return chunk

    monkeypatch.setattr(worktree_process, 'os', LateEndOfFile())
    result = run(tmp_path, "print('Updating files: 100% (1/1), done.'); print('ok')")
    assert result.returncode == 0
    assert 'ok' in result.stderr
    assert not any(t.name == 'git-worktree-output' for t in threading.enumerate())


@pytest.mark.skipif(os.name != 'posix', reason='POSIX escaped helper')
def test_escaped_pipe_holder_cannot_hang_teardown(tmp_path):
    import signal
    from lib.project_mod.worktree_process import WorktreeRetirementUnproven
    marker = tmp_path / 'escaped.pid'
    script = f"""
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'], start_new_session=True)
open({str(marker)!r}, 'w').write(str(child.pid))
"""
    try:
        with pytest.raises(WorktreeRetirementUnproven, match='pipe remains held'):
            run(tmp_path, script)
        # Teardown returned while the holder still runs: it never waited for it.
        os.kill(int(marker.read_text()), 0)
        assert not any(t.name == 'git-worktree-output' for t in threading.enumerate())
    finally:
        if marker.exists():
            os.killpg(int(marker.read_text()), signal.SIGKILL)


def test_retirement_failure_remains_explicit_and_joins_reader(tmp_path, monkeypatch):
    from lib.project_mod import process_group
    from lib.project_mod.worktree_process import WorktreeRetirementUnproven
    original = process_group.retire_process_group
    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise process_group.ProcessGroupRetirementError('injected uncertain retirement')
    monkeypatch.setattr(process_group, 'retire_process_group', uncertain)
    with pytest.raises(WorktreeRetirementUnproven, match='injected uncertain retirement'):
        run(tmp_path, 'print("done")')
    assert not any(t.name == 'git-worktree-output' for t in threading.enumerate())
