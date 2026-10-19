#!/usr/bin/env python3
"""Contract for the durable cross-replica abort signal.

The route plants a flag in ``runtime_state_store`` when a task misses every
local registry; the shared watcher delivers it through the runtime's own
``abort_owned``. Personal mode must stay byte-equivalent (no plant, no
watcher thread).

    python3 -m pytest tests/tasks_pkg/test_task_abort_signal.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.agent_core import task_abort_signal  # noqa: E402
from lib.agent_core.task_runtime import TaskRuntime  # noqa: E402
from lib.runtime.runtime_state_store import get_store, reset_for_test  # noqa: E402


pytestmark = pytest.mark.unit


@pytest.fixture
def distributed(monkeypatch):
    monkeypatch.setattr(task_abort_signal, '_distributed_mode',
                        lambda: True)
    reset_for_test()
    yield
    reset_for_test()


def test_personal_mode_plant_refused():
    assert task_abort_signal.plant_durable_abort(
        'some-task', user_id=1, source='test') is False


def test_personal_mode_starts_no_watcher():
    task_abort_signal.ensure_watcher()
    assert task_abort_signal._watcher_thread is None


def test_plant_and_sweep_aborts_owner_task(distributed):
    rt = TaskRuntime('signal-test', ttl=60)
    task = rt.create(user_id=1)
    assert task_abort_signal.plant_durable_abort(
        task['id'], user_id=1, source='test') is True

    task_abort_signal._sweep_once()

    assert task['abort_event'].is_set()
    assert task['_abortSource'] == 'durable_abort_signal'
    assert get_store().get_value('task-abort', task['id']) is None


def test_foreign_plant_is_never_honored(distributed):
    rt = TaskRuntime('signal-test-foreign', ttl=60)
    task = rt.create(user_id=1)
    assert task_abort_signal.plant_durable_abort(
        task['id'], user_id=2, source='test') is True

    task_abort_signal._sweep_once()

    assert not task['abort_event'].is_set()
    assert get_store().get_value('task-abort', task['id']) == '2'


def test_interrupt_plant_and_sweep_stamps_live_pid(distributed):
    rt = TaskRuntime('signal-test-interrupt', ttl=60)
    task = rt.create(user_id=1)
    task['_subprocess_pid'] = 4321
    assert task_abort_signal.plant_durable_interrupt(
        task['id'], user_id=1, source='test') is True

    task_abort_signal._sweep_once()

    flag = task.get('_cmd_interrupt')
    assert flag is not None
    assert flag['pid'] == 4321
    assert flag['source'] == 'user'
    assert get_store().get_value('cmd-interrupt', task['id']) is None


def test_interrupt_without_active_command_is_dropped(distributed):
    rt = TaskRuntime('signal-test-interrupt-idle', ttl=60)
    task = rt.create(user_id=1)
    assert task_abort_signal.plant_durable_interrupt(
        task['id'], user_id=1, source='test') is True

    task_abort_signal._sweep_once()

    assert '_cmd_interrupt' not in task
    # Consumed, not left for a LATER command to trip over.
    assert get_store().get_value('cmd-interrupt', task['id']) is None


def test_foreign_interrupt_is_never_honored(distributed):
    rt = TaskRuntime('signal-test-interrupt-foreign', ttl=60)
    task = rt.create(user_id=1)
    task['_subprocess_pid'] = 4321
    assert task_abort_signal.plant_durable_interrupt(
        task['id'], user_id=2, source='test') is True

    task_abort_signal._sweep_once()

    assert '_cmd_interrupt' not in task
    assert get_store().get_value('cmd-interrupt', task['id']) == '2'


def test_personal_mode_interrupt_plant_refused():
    assert task_abort_signal.plant_durable_interrupt(
        'some-task', user_id=1, source='test') is False


def test_sweep_ignores_terminal_tasks(distributed):
    rt = TaskRuntime('signal-test-terminal', ttl=60)
    task = rt.create(user_id=1)
    rt.finish(task['id'], result='ok')
    assert task_abort_signal.plant_durable_abort(
        task['id'], user_id=1, source='test') is True

    task_abort_signal._sweep_once()

    assert task['status'] == 'done'
    assert get_store().get_value('task-abort', task['id']) == '1'
