"""Admission-wait evidence is produced by the wait owner, only for real waits.

``lib.project_mod.workspace_lease`` reports every acquisition whose first probe
was blocked to the observer port; the task layer turns it into one
``workspace_admission_wait`` event on the waiting task's execution clock. These
tests use real held checkout leases (no fake lock), the installed executor
observer and the real ``append_event`` chokepoint.
"""

import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _repo(path):
    path.mkdir()
    for command in (['git', 'init', '-q', str(path)],
                    ['git', '-C', str(path), 'config', 'user.email', 'w@example.invalid'],
                    ['git', '-C', str(path), 'config', 'user.name', 'W']):
        subprocess.run(command, check=True)
    (path / 'file.txt').write_text('x\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'init'], check=True)
    return path


class _Holder:
    """A run_command-shaped holder of one checkout write lease."""

    def __init__(self, repo, hold_seconds):
        from lib.project_mod.tool_admission import Cancellation, tool_admission_scope
        from lib.project_mod.workspace_lease import workspace_write_lease
        self.entered, self.errors = threading.Event(), []
        task = {'id': 'holder-task', 'convId': 'holder-conv', 'aborted': False}

        def hold():
            try:
                with tool_admission_scope(task=task, tool_name='run_command'), \
                        workspace_write_lease(str(repo), cancel_event=Cancellation(task)):
                    self.entered.set()
                    time.sleep(hold_seconds)
            except BaseException as error:  # pragma: no cover - surfaces in assert
                self.errors.append(error)

        self.thread = threading.Thread(target=hold)

    def __enter__(self):
        self.thread.start()
        assert self.entered.wait(5)
        return self

    def __exit__(self, *exc):
        self.thread.join(10)
        assert not self.errors


class _Runtime:
    """The ToolExecutionContext attributes the lease layer reads (duck-typed)."""
    tool_call_id = 'call-waiter'
    round_num = 3
    cancellation_requested = False


@pytest.fixture
def observed(monkeypatch):
    from lib.project_mod import workspace_lease
    waits = []
    monkeypatch.setattr(workspace_lease, '_admission_wait_observer',
                        lambda task, wait: waits.append((task, wait)))
    return waits


def _waiter_task():
    from lib.agent_core.execution_clock import ExecutionClock
    return {'id': 'waiter-task', 'convId': 'waiter-conv', 'aborted': False,
            '_executionClock': ExecutionClock.start()}


def test_blocked_acquisition_reports_holder_and_ends_at_admission(tmp_path, observed):
    from lib.project_mod.tool_admission import Cancellation, tool_admission_scope
    from lib.project_mod.workspace_lease import workspace_write_lease

    repo = _repo(tmp_path / 'repo')
    task = _waiter_task()
    with _Holder(repo, 0.4):
        requested = time.monotonic()
        with tool_admission_scope(task, _Runtime(), tool_name='write_file'), \
                workspace_write_lease(str(repo), timeout=None,
                                      cancel_event=Cancellation(task, _Runtime())):
            admitted = time.monotonic()
            time.sleep(0.3)  # body time must not extend the wait
    assert len(observed) == 1
    reported_task, wait = observed[0]
    assert reported_task is task
    assert wait.outcome == 'admitted'
    assert requested <= wait.started_monotonic < wait.ended_monotonic <= admitted
    assert wait.ended_monotonic - wait.started_monotonic >= 0.2
    assert (wait.waiter_tool, wait.tool_call_id, wait.round_num) == ('write_file', 'call-waiter', 3)
    assert (wait.holder_tool, wait.holder_task, wait.holder_conversation) == (
        'run_command', 'holder-task', 'holder-conv')
    assert wait.holder_held_ms >= 200
    assert wait.mode == 'write' and wait.lock_domain == 'workspace'
    assert wait.lock_root == 'repo'  # basename only, never the full path


def test_immediate_admission_reports_nothing(tmp_path, observed):
    from lib.project_mod.tool_admission import Cancellation
    from lib.project_mod.workspace_lease import workspace_write_lease

    repo = _repo(tmp_path / 'free')
    task = _waiter_task()
    with workspace_write_lease(str(repo), cancel_event=Cancellation(task)):
        pass
    assert observed == []


def test_refused_wait_is_reported_and_admission_error_is_preserved(tmp_path, observed):
    from lib.project_mod.tool_admission import Cancellation
    from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_write_lease

    repo = _repo(tmp_path / 'busy')
    task = _waiter_task()
    with _Holder(repo, 0.6):
        with pytest.raises(WorkspaceBusyError):
            with workspace_write_lease(str(repo), timeout=0.2,
                                       cancel_event=Cancellation(task)):
                pytest.fail('a held checkout must not be admitted twice')
    assert [wait.outcome for _, wait in observed] == ['refused']


def test_observer_failure_never_fails_admission(tmp_path, monkeypatch):
    from lib.project_mod import workspace_lease
    from lib.project_mod.tool_admission import Cancellation

    def broken(task, wait):
        raise RuntimeError('synthetic sink failure')

    monkeypatch.setattr(workspace_lease, '_admission_wait_observer', broken)
    repo = _repo(tmp_path / 'sink')
    task = _waiter_task()
    with _Holder(repo, 0.3):
        with workspace_lease.workspace_write_lease(
                str(repo), timeout=None, cancel_event=Cancellation(task)):
            entered = True
    assert entered


def test_escaping_admitted_report_never_strands_the_lease(tmp_path, monkeypatch):
    """The admitted-path report is sent inside the scope that owns the release."""
    from lib.project_mod import workspace_lease
    from lib.project_mod.tool_admission import Cancellation

    class _Escape(BaseException):
        pass

    def escaping(task, wait):
        raise _Escape()

    monkeypatch.setattr(workspace_lease, '_admission_wait_observer', escaping)
    repo = _repo(tmp_path / 'strand')
    task = _waiter_task()
    with _Holder(repo, 0.3):
        with pytest.raises(_Escape):
            with workspace_lease.workspace_write_lease(
                    str(repo), timeout=None, cancel_event=Cancellation(task)):
                pytest.fail('the report precedes the body')
    monkeypatch.setattr(workspace_lease, '_admission_wait_observer', None)
    admitted, errors = [], []

    def other_thread():
        try:
            with workspace_lease.workspace_write_lease(str(repo), timeout=2):
                admitted.append(True)
        except BaseException as error:  # pragma: no cover - surfaces in assert
            errors.append(error)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(10)
    assert (admitted, errors) == ([True], [])


def test_task_less_waiter_is_never_armed(tmp_path, observed):
    from lib.project_mod.workspace_lease import workspace_write_lease

    repo = _repo(tmp_path / 'solo')
    with _Holder(repo, 0.3):
        with workspace_write_lease(str(repo), timeout=None):
            pass
    assert observed == []


def test_installed_observer_appends_schema_valid_event_on_execution_clock(tmp_path, monkeypatch):
    import lib.tasks_pkg.executor  # noqa: F401 - installs the production observer
    from lib.agent_core.events import EventType, validate_event
    from lib.project_mod import workspace_lease
    from lib.project_mod.tool_admission import Cancellation, tool_admission_scope
    from lib.tasks_pkg.executor import _admission_wait
    from lib.tasks_pkg import manager

    assert workspace_lease._admission_wait_observer is _admission_wait.observe_admission_wait
    appended = []
    monkeypatch.setattr(manager, 'append_event', lambda task, event: appended.append(event))
    repo = _repo(tmp_path / 'events')
    task = _waiter_task()
    clock = task['_executionClock']
    with _Holder(repo, 0.4):
        before = clock.now_ms()
        with tool_admission_scope(task, _Runtime(), tool_name='run_command'), \
                workspace_lease.workspace_write_lease(
                    str(repo), timeout=None, cancel_event=Cancellation(task)):
            after = clock.now_ms()
    assert len(appended) == 1
    event = appended[0]
    assert event['type'] == EventType.WORKSPACE_ADMISSION_WAIT
    assert not validate_event(event)
    assert before <= event['waitStartedAtMs'] <= event['waitEndedAtMs'] <= after
    assert event['waitedMs'] == event['waitEndedAtMs'] - event['waitStartedAtMs'] >= 200
    assert (event['toolCallId'], event['roundNum'], event['toolName']) == (
        'call-waiter', 3, 'run_command')
    assert (event['holderTool'], event['holderConversation']) == ('run_command', 'holder-conv')
    assert event['outcome'] == 'admitted' and event['lockRoot'] == 'events'
