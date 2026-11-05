"""A stalled internal lease wait is diagnosable from one line: both sides are named.

Since 2026-10-28 model tool calls take no workspace lease; the waits left are
those of Tofu's own whole-workspace operations (delivery, integration
snapshots, checkpoints, dependency setup). The wait line and the AdmissionWait
evidence name the waiting request's claims and the blocker's mode, claims,
purpose (for its own owner), task/conversation and age.
"""

import contextlib
import logging
import subprocess
import threading
import time

import pytest

from lib.project_mod import workspace_lease as leases
from lib.project_mod.tool_admission import Cancellation, tool_admission_scope

pytestmark = pytest.mark.unit

_HOLDER_COMMAND = 'cd A && python -m pytest -n 6'
_WAITER_COMMAND = 'git log -i -E --grep=cache'


@pytest.fixture
def layout(tmp_path, monkeypatch):
    namespace = tmp_path / 'leases'
    namespace.mkdir(mode=0o700)
    monkeypatch.setattr(leases, '_tree_lock_namespace', lambda: namespace)
    root = (tmp_path / 'root').resolve()
    root.mkdir()
    for command in (['git', 'init', '-q', str(root)],
                    ['git', '-C', str(root), 'config', 'user.email', 'm@example.invalid'],
                    ['git', '-C', str(root), 'config', 'user.name', 'M']):
        subprocess.run(command, check=True)
    (root / 'file.txt').write_text('x\n')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'init'], check=True)
    worktree = root / '.tofu' / 'worktrees' / 'A'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-q', str(worktree), '-b', 'wa'],
                   check=True)
    return str(root), str(worktree)


def _task(name, owner=1):
    return {'id': f'{name}-task', 'convId': f'{name}-conv', 'aborted': False, '_userId': owner}


@contextlib.contextmanager
def _nested_holder(worktree, owner=1):
    """A holder whose tree claim covers nested worktree A."""
    entered, release, errors = threading.Event(), threading.Event(), []
    task = _task('holder', owner)

    def hold():
        try:
            with tool_admission_scope(task, tool_name='run_command'), leases.workspace_claims_lease(
                    [('tree', worktree)], cancel_event=Cancellation(task), purpose=_HOLDER_COMMAND):
                entered.set()
                release.wait(30)
        except BaseException as error:  # pragma: no cover - surfaces below
            errors.append(error)
            entered.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert entered.wait(10) and not errors, errors
    try:
        yield
    finally:
        release.set()
        thread.join(10)
    assert not errors, errors


def _waiter_claims(root):
    """A whole-checkout request: tree and metadata on ROOT."""
    return [('tree', root), ('metadata', root)]


def _wait_in_thread(root, task, timeout=None):
    claims = _waiter_claims(root)
    outcome = []

    def wait():
        try:
            with tool_admission_scope(task, tool_name='run_command'), leases.workspace_claims_lease(
                    claims, timeout=timeout, cancel_event=Cancellation(task),
                    purpose=_WAITER_COMMAND):
                outcome.append('admitted')
        except leases.WorkspaceBusyError as error:
            outcome.append(error)

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    return thread, outcome


def test_wait_log_line_names_waiter_and_holder_requests(layout, monkeypatch, caplog):
    root, worktree = layout
    monkeypatch.setattr(leases, '_ADMISSION_NOTICE_SECONDS', 0.05)
    monkeypatch.setattr(leases, '_ADMISSION_NOTICE_INTERVAL_SECONDS', 60.0)
    caplog.set_level(logging.WARNING)
    with _nested_holder(worktree):
        thread, outcome = _wait_in_thread(root, _task('waiter'), timeout=0.6)
        thread.join(10)
    assert isinstance(outcome[0], leases.WorkspaceBusyError)
    (line,) = [record.getMessage() for record in caplog.records
               if '[WorkspaceLease] wait' in record.getMessage()]
    assert f'mode=write root=tree:{root} ' in line
    assert (f'tool=run_command task=waiter-task conv=waiter-conv '
            f'claims=tree:{root},metadata:{root} '
            f'cmd="{_WAITER_COMMAND}")') in line
    holder = line.split('[holder: ', 1)[1]
    assert 'held_seconds=' in holder and 'mode=file-write' in holder
    assert 'task=holder-task' in holder and 'conversation=holder-conv' in holder
    assert f'claims=tree:{worktree}' in holder
    assert f'cmd="{_HOLDER_COMMAND}"' in holder


def test_admission_wait_evidence_carries_both_requests(layout, monkeypatch):
    root, worktree = layout
    waits = []
    monkeypatch.setattr(leases, '_admission_wait_observer',
                        lambda task, wait: waits.append(wait))
    with _nested_holder(worktree):
        thread, outcome = _wait_in_thread(root, _task('waiter'))
        time.sleep(0.3)
    thread.join(10)
    assert outcome == ['admitted']
    (wait,) = [wait for wait in waits if (wait.lock_domain, wait.lock_root) == ('tree', 'root')]
    assert wait.mode == 'write' and wait.outcome == 'admitted'
    assert wait.waiter_claims == 'tree:root,metadata:root'
    assert (wait.holder_task, wait.holder_conversation, wait.holder_tool) == (
        'holder-task', 'holder-conv', 'run_command')
    assert wait.holder_mode == 'file-write'
    assert wait.holder_claims == 'tree:A'
    assert wait.holder_command == _HOLDER_COMMAND
    assert wait.holder_held_ms >= 200


def test_refusal_names_holder_request_and_command_only_for_its_owner(layout):
    root, worktree = layout
    claims = _waiter_claims(root)
    for holder_owner, shows_command in ((1, True), (2, False)):
        with _nested_holder(worktree, owner=holder_owner):
            waiter = _task('waiter', owner=1)
            with tool_admission_scope(waiter, tool_name='run_command'):
                with pytest.raises(leases.WorkspaceBusyError) as raised:
                    with leases.workspace_claims_lease(claims, cancel_event=Cancellation(waiter)):
                        pytest.fail('the nested holder was ignored')
        text = str(raised.value)
        assert 'mode=file-write' in text and 'task=holder-task' in text
        assert f'claims=tree:{worktree}' in text
        assert (f'cmd="{_HOLDER_COMMAND}"' in text) is shows_command
        assert 'waiter-task' not in text

