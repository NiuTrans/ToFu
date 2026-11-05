"""A command batch observes completion independently of handle order."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.tasks_pkg import background_commands as commands
from lib.tasks_pkg.handlers import poll

pytestmark = pytest.mark.unit


@pytest.fixture
def batch(monkeypatch):
    task = {'id': 'batch-owner', '_userId': 777777, 'aborted': False}
    clock = SimpleNamespace(now=0.0)
    completions = {}
    with commands._lock:
        commands._registry.clear()
    for handle in ('bg_slow', 'bg_fast'):
        commands.register_handed_off_command(
            command_id=handle, task=task, command=handle,
            done=threading.Event())
    real_check = commands.check_background_command

    def check(handle, **kwargs):
        wait = kwargs.pop('wait_s', 0)
        current = real_check(handle, wait_s=0, **kwargs)
        if wait and current and current['status'] != 'completed':
            clock.now += wait
            for candidate, due in list(completions.items()):
                if due <= clock.now:
                    commands.finish_handed_off_command(
                        command_id=candidate, result=f'{candidate} result', error=None)
                    del completions[candidate]
        return real_check(handle, wait_s=0, **kwargs)

    monkeypatch.setattr(commands, 'check_background_command', check)
    monkeypatch.setattr(poll, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(poll, '_finalize_tool_round', Mock())
    monkeypatch.setattr(poll, '_poll_wait_cap_s', lambda: 1500)

    def invoke(ids=('bg_slow', 'bg_fast'), **args):
        return poll._handle_poll(
            task, {}, 'poll', 'batch-call', {'ids': list(ids), 'wait_s': 1500, **args},
            1, {}, {}, None, False)[1]

    yield task, clock, completions, invoke
    with commands._lock:
        commands._registry.clear()


@pytest.mark.parametrize('ids', [('bg_slow', 'bg_fast'), ('bg_fast', 'bg_slow')])
def test_any_returns_an_already_completed_command_without_waiting(batch, ids):
    _task, clock, _completions, invoke = batch
    commands.finish_handed_off_command(
        command_id='bg_fast', result='fast result', error=None)

    result = invoke(ids)

    assert clock.now == 0, 'a completed command must not wait behind a running sibling'
    assert 'fast result' in result and 'bg_slow' in result
    assert 'Wait cap reached' not in result


def test_any_observes_a_later_handles_completion_during_the_wait(batch):
    task, clock, completions, invoke = batch
    completions['bg_fast'] = 1

    result = invoke(mode='any')

    assert clock.now == 1
    assert 'bg_fast result' in result
    assert commands.check_background_command('bg_slow', user_id=task['_userId'])['status'] == 'running'
    assert 'Wait cap reached' not in result


def test_all_waits_for_each_command_under_one_deadline(batch):
    _task, clock, completions, invoke = batch
    completions.update(bg_fast=1, bg_slow=2)

    result = invoke(mode='all')

    assert clock.now == 2
    assert 'bg_fast result' in result and 'bg_slow result' in result
    assert 'Wait cap reached' not in result


def test_batch_deadline_is_shared_and_does_not_stop_commands(batch):
    task, clock, _completions, invoke = batch

    result = invoke(wait_s=2, mode='all')

    assert clock.now == 2
    assert result.count('Wait cap reached') == 2
    assert not task['aborted']


def test_pending_input_returns_before_waiting_on_any_command(batch, monkeypatch):
    task, clock, _completions, invoke = batch
    monkeypatch.setattr(poll, '_wait_interruption', lambda *args: lambda: 'pending_input')

    result = invoke(mode='all')

    assert clock.now == 0
    assert 'Wait ended early: new input is waiting' in result
    assert not task['aborted']


def test_cancellation_during_a_batch_wait_keeps_commands_running(batch, monkeypatch):
    task, clock, _completions, invoke = batch
    monkeypatch.setattr(poll, '_wait_interruption', lambda *args: lambda:
                        'cancelled' if clock.now >= 1 else '')

    result = invoke(mode='all')

    assert clock.now == 1
    assert result.count('the command remains running') == 2
    assert all(commands.check_background_command(handle, user_id=task['_userId'])[
        'status'] == 'running' for handle in ('bg_slow', 'bg_fast'))


@pytest.mark.parametrize('leaf_scope', [False, True])
def test_a_disappeared_command_is_unavailable_instead_of_using_stale_state(
        batch, monkeypatch, leaf_scope):
    task, clock, _completions, invoke = batch
    check = commands.check_background_command
    if leaf_scope:
        monkeypatch.setattr(poll, '_own_command_scope', lambda task: task['id'])
        commands.register_handed_off_command(
            command_id='bg_foreign', task={**task, 'id': 'sibling-worker'},
            command='sibling command', done=threading.Event())

    def disappearing_check(handle, **kwargs):
        if handle == 'bg_fast' and clock.now >= 1:
            return None
        return check(handle, **kwargs)

    monkeypatch.setattr(commands, 'check_background_command', disappearing_check)

    result = invoke(mode='all')

    assert clock.now == 1
    assert 'Unknown handle(s): bg_fast' in result
    assert '== bg_fast (background command) ==' not in result
    assert 'bg_foreign' not in result


def test_zero_wait_returns_a_snapshot_without_waiting(batch):
    _task, clock, _completions, invoke = batch

    result = invoke(wait_s=0)

    assert clock.now == 0
    assert result.count('still running') == 2
    assert 'Wait cap reached' not in result
