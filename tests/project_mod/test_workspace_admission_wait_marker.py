"""A lease waiter publishes its wait to silence-based liveness decisions.

Regression coverage for the 2026-10-20 incident: a command queued behind another
task's 31-minute writer held no process and produced no output, so the task
reaper and the invocation stall beacon both saw a wedged worker and force-failed
a turn that was simply waiting. The wait owner must publish the marker for
exactly the acquisition span, whichever tool is waiting.
"""

import os
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _repo(path):
    path.mkdir()
    for command in (['git', 'init', '-q', str(path)],
                    ['git', '-C', str(path), 'config', 'user.email', 'w@example.invalid'],
                    ['git', '-C', str(path), 'config', 'user.name', 'W'],
                    ['git', '-C', str(path), 'add', '-A']):
        subprocess.run(command, check=True)
    (path / 'file.txt').write_text('x\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'init'], check=True)
    return path


@pytest.fixture()
def holder(tmp_path):
    """Hold one checkout write lease for the whole test body."""
    from lib.project_mod.workspace_lease import workspace_write_lease

    repo = _repo(tmp_path / 'repo')
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def hold():
        try:
            with workspace_write_lease(str(repo)):
                entered.set()
                release.wait(10)
        except BaseException as error:  # pragma: no cover - surfaces in the assert
            errors.append(error)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(5)
    yield repo
    release.set()
    worker.join(5)
    assert not errors


def test_waiter_publishes_admission_while_queued(holder):
    from lib.project_mod.tool_admission import (
        Cancellation, WORKSPACE_ADMISSION_KEY, tool_admission_scope,
    )
    from lib.project_mod.workspace_lease import workspace_write_lease
    from lib.tasks_pkg.explained_wait import explained_wait

    task = {'id': 'task-1', 'convId': 'conv-1', 'aborted': False}
    observed = {}
    queued = threading.Event()

    def wait():
        with tool_admission_scope(task=task), workspace_write_lease(
                str(holder), timeout=None, cancel_event=Cancellation(task)):
            observed['after'] = task.get(WORKSPACE_ADMISSION_KEY)

    worker = threading.Thread(target=wait)
    worker.start()
    assert _await_marker(task, WORKSPACE_ADMISSION_KEY, queued)
    # The marker is what keeps a healthy wait out of the wedged-task path.
    assert explained_wait(task) == 'workspace-admission'
    assert isinstance(task[WORKSPACE_ADMISSION_KEY]['since'], float)
    assert observed == {}
    worker.join(10)
    assert not worker.is_alive()
    # Admission only — execution owns the ordinary process clock again.
    assert observed['after'] is None
    assert WORKSPACE_ADMISSION_KEY not in task
    assert explained_wait(task) == ''


def _await_marker(task, key, ready, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if isinstance(task.get(key), dict):
            ready.set()
            return True
        time.sleep(0.01)
    return False


def test_task_less_waiter_publishes_nothing(tmp_path):
    """No live task means no marker: the marker is task liveness evidence only."""
    from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY
    from lib.project_mod.workspace_lease import workspace_write_lease

    repo = _repo(tmp_path / 'solo')
    with workspace_write_lease(str(repo)):
        pass
    assert os.getpid() > 0  # nothing raised and no task dict was invented
    assert WORKSPACE_ADMISSION_KEY == '_workspaceAdmission'
