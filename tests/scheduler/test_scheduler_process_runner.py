"""The scheduler's one timeout/cancellation wrapper owns child processes."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from lib.identity import PrincipalContext
from lib.scheduler.process_runner import run_bounded_process


# serial: process-tree kill/reap under wall-clock timeouts — cannot
# safely overlap the parallel unit lane (TESTING_STRATEGY serial policy).
pytestmark = [pytest.mark.unit, pytest.mark.serial]


def _user_principal() -> PrincipalContext:
    return PrincipalContext.user(
        subject_id='scheduled-test-user', owner_user_id=1,
        scopes={'agents:scheduler'})


def test_bounded_process_captures_a_successful_child():
    result = run_bounded_process(
        [sys.executable, '-c', 'print("maintenance-ok")'],
        max_runtime=10,
        job_id='job-success',
        job_type='test',
        principal=_user_principal(),
    )

    assert result.ok is True
    assert result.stdout.strip() == 'maintenance-ok'


def test_bounded_process_requires_explicit_principal():
    with pytest.raises(TypeError, match='PrincipalContext'):
        run_bounded_process(
            [sys.executable, '-c', 'pass'],
            max_runtime=10,
            job_id='job-ownerless',
            job_type='test',
            principal=None,
        )

    with pytest.raises(PermissionError, match='scope'):
        run_bounded_process(
            [sys.executable, '-c', 'pass'],
            max_runtime=10,
            job_id='job-unscoped',
            job_type='test',
            principal=PrincipalContext.system(
                subject_id='unscoped-system', scopes=set()),
        )


def test_bounded_process_terminates_at_deadline():
    result = run_bounded_process(
        [sys.executable, '-c', 'import time; time.sleep(5)'],
        max_runtime=1,
        job_id='job-timeout',
        job_type='test',
        principal=_user_principal(),
    )

    assert result.ok is False
    assert result.timed_out is True
    assert result.error == 'Timed out after 1s'


def test_bounded_process_observes_lifecycle_cancellation():
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_bounded_process(
            [sys.executable, '-c', 'import time; time.sleep(30)'],
            max_runtime=60,
            job_id='job-cancel',
            job_type='test',
            principal=_user_principal(),
            cancel_event=cancel,
        )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 3
    assert result.cancelled is True
    assert result.error == 'Cancelled during scheduler shutdown'


def test_maintenance_dispatch_does_not_block_scheduler_tick(monkeypatch):
    from lib.scheduler.manager import ScheduledTaskManager

    manager = ScheduledTaskManager()
    started = threading.Event()
    release = threading.Event()

    def wait_for_release(_task):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, '_run_and_record', wait_for_release)
    task = {'id': 'maintenance-task', 'task_type': 'storage_backup', 'user_id': 1}

    before = time.monotonic()
    assert manager._dispatch_maintenance_task(task) is True
    assert time.monotonic() - before < 0.5
    assert started.wait(1)
    assert manager._dispatch_maintenance_task(task) is False

    release.set()
    assert manager.stop(timeout=2) is True


def test_child_output_limit_bounds_memory_while_draining_both_pipes():
    import tracemalloc
    tracemalloc.start()
    try:
        result = run_bounded_process(
            [sys.executable, '-c',
             'import os; chunk=b"x"*8192; '
             '[(os.write(1,chunk),os.write(2,chunk)) for _ in range(1024)]'],
            max_runtime=10, job_id='bounded-output', job_type='test',
            principal=_user_principal())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result.ok
    assert result.stdout == 'x' * 50_000
    assert result.stderr == 'x' * 10_000
    assert peak < 2 * 1024 * 1024


def test_child_output_limit_preserves_multibyte_character_bound():
    result = run_bounded_process(
        [sys.executable, '-c', 'import os; os.write(1,("😀"*60000).encode())'],
        max_runtime=10, job_id='unicode-output', job_type='test',
        principal=_user_principal())
    assert result.ok
    assert result.stdout == '😀' * 50_000


def test_output_flood_does_not_starve_cancellation():
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_bounded_process(
            [sys.executable, '-c',
             'import os\nwhile True: os.write(1,b"x"*8192)'],
            max_runtime=30, job_id='flood-cancel', job_type='test',
            principal=_user_principal(), cancel_event=cancel)
    finally:
        timer.cancel()
    assert result.cancelled
    assert time.monotonic() - started < 3


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux /proc descendant liveness evidence')
def test_deadline_kills_inherited_pipe_child_after_leader_exits(tmp_path):
    import os
    import signal
    pid_path = tmp_path / 'descendant.pid'
    # The leader exits immediately. Its child keeps both output pipes open and
    # ignores graceful termination, so cleanup must address the process group.
    child = ('import os,signal,time,pathlib; '
             'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             f'pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); '
             'time.sleep(60)')
    leader = ('import subprocess,sys; '
              f'subprocess.Popen([sys.executable,"-c",{child!r}])')
    pid = None
    try:
        result = run_bounded_process([sys.executable, '-c', leader],
            max_runtime=1, job_id='inherited-pipes', job_type='test',
            principal=_user_principal())
        assert result.timed_out
        pid = int(pid_path.read_text())
        def alive():
            try:
                # A reparented zombie has already stopped; init owns reaping.
                from pathlib import Path
                stat = Path(f'/proc/{pid}/stat').read_text()
                return stat.rsplit(')', 1)[1].strip().split()[0] != 'Z'
            except FileNotFoundError:
                return False
        until = time.monotonic() + 1
        while alive() and time.monotonic() < until:
            time.sleep(.02)
        assert not alive(), 'scheduler left the inherited-pipe child running'
    finally:
        if pid is None and pid_path.exists():
            pid = int(pid_path.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
