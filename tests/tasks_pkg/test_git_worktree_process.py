"""Controlled materialization renews only real progress and retires children."""
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from lib.project_mod.worktree_process import run_worktree_add
from lib.project_mod.tool_admission import Cancellation

pytestmark = pytest.mark.unit


def run(tmp_path, script, *, idle=0.4, cancel=None):
    return run_worktree_add([sys.executable, '-u', '-c', script],
                            cwd=tmp_path, env=os.environ.copy(),
                            idle_timeout=idle, cancellation=cancel)


@pytest.mark.parametrize('label', ['Updating', 'Checking out'])
def test_slow_healthy_progress_can_outlive_original_deadline(tmp_path, label):
    result = run(tmp_path, f"""
import sys, time
for n in range(1, 9):
    sys.stderr.write(f'{label} files: {{n*10}}% ({{n}}/8)\\r')
    sys.stderr.flush()
    time.sleep(0.2)
""", idle=1.0)
    assert result.returncode == 0
    assert '8/8' in result.stderr


@pytest.mark.parametrize('frame', ['noise', 'Updating files: 10% (1/8)',
                                  'Updating files: 200% (99/8)'])
def test_output_noise_repeated_or_invalid_count_does_not_renew(tmp_path, frame):
    with pytest.raises(RuntimeError, match='stalled'):
        run(tmp_path, f"""
import sys, time
while True:
    sys.stderr.write({frame!r} + '\\r'); sys.stderr.flush(); time.sleep(0.03)
""")


def test_cancellation_reaps_child_and_closes_reader(tmp_path):
    task = {'aborted': False}
    signal = threading.Timer(0.2, lambda: task.update(aborted=True))
    signal.start()
    try:
        with pytest.raises(RuntimeError, match='cancelled'):
            run(tmp_path, 'import time; time.sleep(30)', idle=10,
                cancel=Cancellation(task))
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
""", idle=10)
    assert result.returncode == 0
    pgid = int(result.stderr.split('owned-group=')[1].split()[0])
    assert not group_has_live_members(pgid)


def test_failure_keeps_bounded_diagnostic_tail(tmp_path):
    result = run(tmp_path, "import sys; print('x' * 100000); print('terminal error'); sys.exit(7)")
    assert result.returncode == 7
    assert len(result.stderr) <= 8192
    assert 'terminal error' in result.stderr


@pytest.mark.parametrize('idle', [0, -1, float('inf'), float('nan')])
def test_invalid_budget_refuses_before_launch(tmp_path, idle):
    with pytest.raises(ValueError, match='positive'):
        run(tmp_path, "raise AssertionError('must not execute')", idle=idle)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX escaped helper')
def test_escaped_pipe_holder_cannot_hang_teardown(tmp_path):
    import signal
    from lib.project_mod.worktree_process import WorktreeRetirementUnproven
    marker = tmp_path / 'escaped.pid'
    script = f"""
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
open({str(marker)!r}, 'w').write(str(child.pid))
"""
    started = time.monotonic()
    try:
        with pytest.raises(WorktreeRetirementUnproven, match='pipe remains held'):
            run(tmp_path, script, idle=10)
        assert time.monotonic() - started < 3
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
