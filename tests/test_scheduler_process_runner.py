"""The scheduler's one timeout/cancellation wrapper owns child processes."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from lib.identity import PrincipalContext
from lib.scheduler.process_runner import run_bounded_process


pytestmark = pytest.mark.unit


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
