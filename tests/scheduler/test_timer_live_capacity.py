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


def test_live_registry_accepts_work_beyond_retired_resource_budget(monkeypatch):
    from lib.scheduler.timer import _loop

    monkeypatch.setenv('TOFU_TIMER_LIVE_CAP', '1')
    monkeypatch.setattr(
        _loop,
        '_get_timer_row',
        lambda timer_id, *, user_id: {
            'id': timer_id, 'poll_interval': 60, 'max_polls': 1},
    )
    monkeypatch.setattr(_loop, 'TimerWatcherHandle', _FakeThread)
    with _loop._timers_lock:
        _loop._active_timers.clear()
    try:
        for index in range(65):
            assert _loop.start_timer_loop(f'timer-{index}', user_id=index+1) is True
        assert len(_loop._active_timers) == 65
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
    monkeypatch.setattr(_loop, 'TimerWatcherHandle', _FailingThread)
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
            OSError('thread creation failed')),
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
    assert 'startup' in result.lower()


def test_legacy_live_budget_is_not_an_admission_ceiling(monkeypatch):
    from lib.scheduler.contract import timer_live_capacity
    monkeypatch.setenv('TOFU_TIMER_LIVE_CAP', '1')
    assert timer_live_capacity() == 0


def test_many_sleeping_watchers_share_one_loop_and_release_leases(monkeypatch):
    import threading
    from lib.scheduler.timer import _loop, _runtime
    runtime = _runtime.TimerWatcherRuntime()
    monkeypatch.setattr(_runtime, '_runtime', runtime)
    entered = threading.Event()
    released = []
    waiting_count = 0
    monkeypatch.setattr(_loop, '_get_timer_row', lambda timer_id, *, user_id: {
        'id': timer_id, 'poll_interval': 60, 'max_polls': 1})
    monkeypatch.setattr(_loop, 'claim_watcher_lease', lambda timer_id: f'lease:{timer_id}')
    monkeypatch.setattr(_loop, 'release_watcher_lease', lambda timer_id, token: released.append((timer_id, token)))
    async def waiting(*args):
        nonlocal waiting_count
        waiting_count += 1
        if waiting_count == 512:
            entered.set()
        return await _runtime.watcher_delay(60)
    monkeypatch.setattr(_loop, '_sleep_with_lease', waiting)
    ids = [f'timer-demand-{index}' for index in range(512)]
    try:
        for index, timer_id in enumerate(ids):
            assert _loop.start_timer_loop(timer_id, user_id=index+1)
        assert entered.wait(5)
        with _loop._timers_lock:
            handles = [_loop._active_timers[timer_id] for timer_id in ids]
        assert all(handle.is_alive() for handle in handles)
        assert runtime.snapshot() == {'watchers': 512, 'wait_threads': 1}
        assert not runtime._executor._threads
    finally:
        runtime.shutdown()
    assert all(not handle.is_alive() for handle in handles)
    assert runtime.snapshot() == {'watchers': 0, 'wait_threads': 0}
    assert {timer_id for timer_id, _ in released} == set(ids)
