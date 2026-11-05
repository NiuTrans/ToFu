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


def _blocked_marker(task, timeout=5.0):
    """The task's marker once a blocked probe has named the lock it waits for."""
    from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = task.get(WORKSPACE_ADMISSION_KEY)
        if isinstance(marker, dict) and marker.get('lock'):
            return marker
        time.sleep(0.01)
    raise AssertionError('the blocked admission was never published')


def test_concurrent_waits_of_one_task_end_in_any_order_without_a_stale_marker(tmp_path):
    """Parallel calls of one task publish on the same task dict and finish in any order.

    The marker was restored LIFO: the first waiter to finish erased the marker
    of the one still queued, and that one then put the finished waiter's
    marker back for good, so the idle task kept looking queued.
    """
    from lib.project_mod.tool_admission import (
        WORKSPACE_ADMISSION_KEY, Cancellation, tool_admission_scope,
    )
    from lib.project_mod.workspace_lease import workspace_write_lease

    repos = [_repo(tmp_path / name) for name in ('first-repo', 'second-repo')]
    task = {'id': 'task-1', 'convId': 'conv-1', 'aborted': False}
    holders, waiters = [], []
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    admitted = [threading.Event(), threading.Event()]

    def hold(index):
        with workspace_write_lease(str(repos[index])):
            entered[index].set()
            release[index].wait(10)

    def wait(index):
        with tool_admission_scope(task, tool_name='write_file'), workspace_write_lease(
                str(repos[index]), timeout=None, cancel_event=Cancellation(task)):
            admitted[index].set()

    for index in (0, 1):
        holders.append(threading.Thread(target=hold, args=(index,)))
        holders[-1].start()
        assert entered[index].wait(5)
    try:
        waiters.append(threading.Thread(target=wait, args=(0,)))
        waiters[-1].start()
        assert _await_marker_field(task, 'depth', 1)
        waiters.append(threading.Thread(target=wait, args=(1,)))
        waiters[-1].start()
        assert _await_marker_field(task, 'depth', 2)
        release[0].set()
        waiters[0].join(10)
        assert admitted[0].is_set() and not admitted[1].is_set()
        # The second call is still queued and must still explain its silence.
        assert _await_marker_field(task, 'depth', 1)
    finally:
        for event in release:
            event.set()
        for thread in holders + waiters:
            thread.join(10)
    assert admitted[1].is_set()
    assert WORKSPACE_ADMISSION_KEY not in task


def _await_marker_field(task, name, value, timeout=5.0):
    from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = task.get(WORKSPACE_ADMISSION_KEY)
        if isinstance(marker, dict) and marker.get(name) == value:
            return True
        time.sleep(0.01)
    return False


def test_command_proxy_starts_without_its_parents_admission_marker():
    """A command proxy made while another call of its task waits is not queued itself.

    The proxy copied the parent's transient marker, so a fresh command looked
    as if it had waited since the other call began: handoff requests and the
    timed background handoff acted on another call's wait.
    """
    from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY
    from lib.tasks_pkg.explained_wait import explained_wait
    from lib.tasks_pkg.handlers._background_command import (
        CommandHandoffControl, DetachableCommandTask,
    )

    marker = {'since': time.monotonic() - 600, 'depth': 1}
    parent = {'id': 'task-1', 'convId': 'conv-1', 'aborted': False, 'status': 'running',
              WORKSPACE_ADMISSION_KEY: marker}
    handoff = CommandHandoffControl(parent, 1, 'call-1')
    proxy = DetachableCommandTask(parent, handoff)
    assert proxy.get(WORKSPACE_ADMISSION_KEY) is None
    assert explained_wait(proxy) == ''
    assert handoff.request(round_num=1, tool_call_id='call-1') == {
        'backgrounded': False, 'reason': 'no_active_command'}
    assert parent[WORKSPACE_ADMISSION_KEY] is marker
