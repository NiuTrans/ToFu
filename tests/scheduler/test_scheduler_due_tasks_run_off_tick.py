"""Claimed scheduled tasks run off the 30-second tick, single-flight per task.

The tick used to execute command, python, prompt, optimizer and proactive
agent tasks inline, so one slow task delayed every other due task in the same
sweep, and because ``cron_matches`` only checks the current minute, cron
occurrences that came due while the tick was blocked were lost. Every claimed
occurrence now runs on its task's own thread. An occurrence claimed while the
same task is still running is coalesced into one rerun after it finishes, so a
task never overlaps itself and its due work is not dropped.
"""
from __future__ import annotations

import threading
import time

import pytest

import lib.scheduler.manager as manager_module
from lib.identity import PrincipalContext
from lib.scheduler.manager import ScheduledTaskManager


pytestmark = pytest.mark.unit


class _DueFeed:
    def __init__(self, rows):
        self.rows = rows

    def query(self, operation, payload, **_kwargs):
        assert operation == 'scheduler.task.list_all'
        return [dict(row) for row in self.rows]

    def command(self, operation, payload, key=None, **_kwargs):
        if operation == 'scheduler.task.claim_due':
            return {'claimed': True}
        if operation == 'scheduler.task.record_result':
            return {}
        raise AssertionError(operation)


def _manager():
    manager = ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='scheduler-dispatch-test', owner_user_id=1,
        scopes={'scheduler:run'})
    return manager


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_slow_task_never_delays_other_due_tasks(monkeypatch):
    rows = [
        {'id': 'slow-command', 'user_id': 1, 'name': 'slow', 'schedule': '* * * * *',
         'run_count': 0, 'task_type': 'command', 'command': 'sleep 90', 'last_run': None},
        {'id': 'every-minute-report', 'user_id': 1, 'name': 'report',
         'schedule': '* * * * *', 'run_count': 0, 'task_type': 'prompt',
         'command': 'summarize', 'last_run': None},
    ]
    monkeypatch.setattr(manager_module, '_scheduler_client',
                        lambda *, write=False: _DueFeed(rows))
    monkeypatch.setattr(manager_module, '_scheduler_wire_task', lambda task: task)
    manager = _manager()
    release = threading.Event()
    started = {}

    def execute(task):
        started[task['id']] = time.monotonic()
        if task['id'] == 'slow-command':
            release.wait(5)
        return True, 'ok'

    monkeypatch.setattr(manager, '_execute_task', execute)
    try:
        began = time.monotonic()
        manager._check_and_run_due_tasks()
        assert time.monotonic() - began < 1.0, 'the tick waited for a task to finish'
        assert _wait_until(lambda: 'every-minute-report' in started)
        assert started['every-minute-report'] - began < 1.0
    finally:
        release.set()
        assert manager.stop(timeout=5)


def test_proactive_polls_run_off_the_tick(monkeypatch):
    manager = _manager()
    entered = threading.Event()
    release = threading.Event()

    def poll(_task):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(manager, '_run_proactive_poll', poll)
    try:
        began = time.monotonic()
        assert manager._dispatch_claimed_task(
            {'id': 'agent-task', 'task_type': 'agent', 'user_id': 1}) is True
        assert time.monotonic() - began < 0.5
        assert entered.wait(2)
    finally:
        release.set()
        assert manager.stop(timeout=5)


def test_an_occurrence_due_while_the_task_runs_reruns_once_after_it(monkeypatch):
    manager = _manager()
    release = threading.Event()
    first_started = threading.Event()
    guard = threading.Lock()
    runs = []
    active = {'now': 0, 'peak': 0}

    def run(task):
        with guard:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            runs.append(task['id'])
            first = len(runs) == 1
        if first:
            first_started.set()
            release.wait(5)
        with guard:
            active['now'] -= 1

    monkeypatch.setattr(manager, '_run_and_record', run)
    task = {'id': 'slow-every-minute', 'task_type': 'command', 'user_id': 1}
    try:
        assert manager._dispatch_claimed_task(dict(task)) is True
        assert first_started.wait(2)
        assert manager._dispatch_claimed_task(dict(task)) is False
        assert manager._dispatch_claimed_task(dict(task)) is False
        release.set()
        assert _wait_until(lambda: len(runs) == 2)
        time.sleep(0.1)
        assert len(runs) == 2, 'claims made during one run coalesce into one rerun'
        assert active['peak'] == 1, 'a task never overlaps itself'
    finally:
        release.set()
        assert manager.stop(timeout=5)


def test_stop_drops_a_pending_rerun_and_joins_the_run(monkeypatch):
    manager = _manager()
    release = threading.Event()
    first_started = threading.Event()
    runs = []

    def run(task):
        runs.append(task['id'])
        first_started.set()
        release.wait(5)

    monkeypatch.setattr(manager, '_run_and_record', run)
    task = {'id': 'slow-backup', 'task_type': 'storage_backup', 'user_id': 1}
    assert manager._dispatch_claimed_task(dict(task)) is True
    assert first_started.wait(2)
    assert manager._dispatch_claimed_task(dict(task)) is False

    stopper = threading.Thread(target=lambda: runs.append(manager.stop(timeout=5)))
    stopper.start()
    assert _wait_until(manager._stop_event.is_set)
    release.set()
    stopper.join(5)
    assert runs == ['slow-backup', True]
