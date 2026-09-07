"""Process-level Timer Watcher admission and rollback contracts."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


def test_live_registry_rejects_work_above_resource_budget(monkeypatch):
    from lib.scheduler.contract import TimerCapacityError
    from lib.scheduler.timer import _loop

    monkeypatch.setenv('TOFU_TIMER_LIVE_CAP', '1')
    monkeypatch.setattr(
        _loop,
        '_get_timer_row',
        lambda timer_id, *, user_id: {
            'id': timer_id, 'poll_interval': 60, 'max_polls': 1},
    )
    monkeypatch.setattr(_loop.threading, 'Thread', _FakeThread)
    with _loop._timers_lock:
        _loop._active_timers.clear()
    try:
        assert _loop.start_timer_loop('timer-a', user_id=1) is True
        with pytest.raises(TimerCapacityError):
            _loop.start_timer_loop('timer-b', user_id=2)
        assert set(_loop._active_timers) == {'timer-a'}
    finally:
        with _loop._timers_lock:
            _loop._active_timers.clear()


def test_thread_start_failure_releases_registry_slot(monkeypatch):
    from lib.scheduler.timer import _loop

    class _FailingThread(_FakeThread):
        def start(self):
            raise RuntimeError('thread creation failed')

    monkeypatch.setenv('TOFU_TIMER_LIVE_CAP', '2')
    monkeypatch.setattr(
        _loop,
        '_get_timer_row',
        lambda timer_id, *, user_id: {
            'id': timer_id, 'poll_interval': 60, 'max_polls': 1},
    )
    monkeypatch.setattr(_loop.threading, 'Thread', _FailingThread)
    with _loop._timers_lock:
        _loop._active_timers.clear()

    with pytest.raises(RuntimeError, match='thread creation failed'):
        _loop.start_timer_loop('timer-failed', user_id=1)
    assert 'timer-failed' not in _loop._active_timers


def test_tool_rolls_back_durable_timer_when_start_fails(monkeypatch):
    import lib.scheduler.timer as timer
    from lib.scheduler.executor._timer import _execute_timer_create

    cancelled = []
    monkeypatch.setattr(
        timer,
        'create_timer',
        lambda **_kwargs: {'id': 'timer-created'},
    )
    monkeypatch.setattr(
        timer,
        'start_timer_loop',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            timer.TimerCapacityError('occupied')),
    )
    monkeypatch.setattr(
        timer,
        'cancel_timer',
        lambda timer_id, *, user_id: cancelled.append((timer_id, user_id)),
    )

    result = _execute_timer_create({
        '_user_id': 7,
        '_source_conv_id': 'conv-1',
        '_source_task_id': 'task-1',
        'check_instruction': 'is it ready?',
        'continuation_message': 'continue',
    })

    assert cancelled == [('timer-created', 7)]
    assert 'capacity' in result.lower()


def test_live_budget_override_has_hard_ceiling():
    from runtime_guards import resolve_resource_budget

    assert resolve_resource_budget(
        'TOFU_TIMER_LIVE_CAP',
        {'TOFU_TIMER_LIVE_CAP': '999999'},
        maximum=64,
    ) == 64
