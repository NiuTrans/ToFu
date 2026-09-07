"""Native Quart ownership for serving-loop resources and recovery jobs."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from quart.testing.app import LifespanError

from lib.app_factory import create_base_app
from lib.serving_loop_lifecycle import register_serving_loop_lifecycle


pytestmark = pytest.mark.unit


class _Owner:
    def __init__(self, name, calls):
        self.name = name
        self.calls = calls

    def stop(self, **kwargs):
        self.calls.append((f'{self.name}-stop', kwargs))


class _Watchdog(_Owner):
    async def stop(self, **kwargs):
        self.calls.append((f'{self.name}-stop', kwargs))


class _Runtime(_Owner):
    def __init__(self, calls):
        super().__init__('runtime', calls)
        self.tasks = []

    def create_task(self, awaitable, *, name):
        self.calls.append(('task', name))
        task = asyncio.create_task(awaitable, name=name)
        self.tasks.append(task)
        return task

    async def stop(self):
        self.calls.append(('runtime-stop', {}))
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def _install_fakes(monkeypatch, calls, runtime, *, fail_watchdog=False):
    import lib.auto_restart as auto_restart
    import lib.serving_loop_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle, '_create_debug_guard',
        lambda *_args, **_kwargs: _Owner('debug', calls))
    monkeypatch.setattr(
        lifecycle, '_create_loop_runtime',
        lambda *_args, **_kwargs: runtime)

    def watchdog(*_args, **_kwargs):
        if fail_watchdog:
            raise RuntimeError('watchdog failed')
        calls.append(('watchdog-start', _kwargs))
        return _Watchdog('watchdog', calls)

    monkeypatch.setattr(lifecycle, '_create_watchdog', watchdog)
    monkeypatch.setattr(
        lifecycle, '_load_write_freshness_snapshot',
        lambda: calls.append(('snapshot', {})))
    monkeypatch.setattr(
        auto_restart, 'maybe_start_auto_restart_watch',
        lambda *, shutdown_requested: calls.append(
            ('auto-start', {'shutdown_requested': shutdown_requested})) or True)
    monkeypatch.setattr(
        auto_restart, 'stop_auto_restart_watch',
        lambda *, timeout: calls.append(('auto-stop', {'timeout': timeout})))
    monkeypatch.setattr(
        lifecycle, '_redispatch_orphaned_queue',
        lambda: calls.append(('orphan', {})) or ['task-1'])
    monkeypatch.setattr(
        lifecycle, '_turn_recovery_backstop_body',
        lambda stop_event, gate_open_ms, _logger: calls.append((
            'turn-recovery', {
                'stop_event': stop_event,
                'gate_open_ms': gate_open_ms,
            })))
    monkeypatch.setattr(
        lifecycle, '_recover_dispatchable_attempts',
        lambda created_before_ms: calls.append((
            'attempt-dispatch-recovery', {
                'created_before_ms': created_before_ms,
            })) or {
                'examined': 0,
                'recovered': 0,
                'settledFailed': 0,
            })


def test_loop_owners_start_then_gate_recovery_and_shutdown_every_owner(
        monkeypatch):
    app = create_base_app('serving-loop-lifecycle', {'TESTING': True})
    calls = []
    runtime = _Runtime(calls)
    _install_fakes(monkeypatch, calls, runtime)
    stop = threading.Event()

    assert register_serving_loop_lifecycle(
        app,
        shutdown_requested=stop,
        host='127.0.0.1',
        port=16000,
        hooks=object(),
        logger=logging.getLogger('test.serving-loop'),
        environ={},
    ) is True
    assert register_serving_loop_lifecycle(app, hooks=object()) is False

    async def exercise():
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        async with app.test_app():
            state = app.extensions['tofu_serving_loop_lifecycle']
            assert state['status'] == 'ready'
            assert state['loop'] is loop
            assert state['gate'] is app.extensions[
                'tofu_production_startup_gate']
            assert loop.get_exception_handler() is state['exception_handler']
            assert state['auto_restart_started'] is True
            assert ('auto-start', {'shutdown_requested': stop}) in calls
            watchdog_start = next(
                details for name, details in calls
                if name == 'watchdog-start')
            assert watchdog_start['host'] == '127.0.0.1'
            assert watchdog_start['port'] == 16000
            assert watchdog_start['ready_event'] is state['gate']
            assert not any(
                name in {'orphan', 'turn-recovery'} for name, _ in calls)
            state['gate'].set()
            for _ in range(50):
                if all(any(name == expected for name, _ in calls)
                       for expected in ('orphan', 'turn-recovery')):
                    break
                await asyncio.sleep(0.01)
            assert ('orphan', {}) in calls
            recovery = next(
                details for name, details in calls
                if name == 'turn-recovery')
            assert recovery['stop_event'] is stop
            assert isinstance(recovery['gate_open_ms'], int)
        assert loop.get_exception_handler() is previous

    asyncio.run(exercise())
    assert stop.is_set()
    assert app.extensions['tofu_serving_loop_lifecycle']['status'] == 'stopped'
    assert ('task', 'tofu-orphan-queue-redispatch') in calls
    assert ('task', 'tofu-turn-recovery-backstop') in calls
    assert ('task', 'tofu-attempt-dispatch-recovery') in calls
    assert ('auto-stop', {'timeout': 2.0}) in calls
    stop_names = [name for name, _ in calls if name.endswith('-stop')]
    assert stop_names == [
        'auto-stop', 'watchdog-stop', 'runtime-stop', 'debug-stop']


def test_partial_loop_startup_rolls_back_started_owners(monkeypatch):
    app = create_base_app('serving-loop-rollback', {'TESTING': True})
    calls = []
    runtime = _Runtime(calls)
    _install_fakes(monkeypatch, calls, runtime, fail_watchdog=True)
    stop = threading.Event()
    register_serving_loop_lifecycle(
        app,
        shutdown_requested=stop,
        hooks=object(),
        environ={},
    )

    async def exercise():
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        with pytest.raises(LifespanError, match='watchdog failed'):
            async with app.test_app():
                pass
        assert loop.get_exception_handler() is previous

    asyncio.run(exercise())
    assert stop.is_set()
    assert ('runtime-stop', {}) in calls
    assert ('debug-stop', {}) in calls
    assert app.extensions['tofu_lifecycle']['status'] == 'startup_failed'


def test_api_role_does_not_own_attempt_dispatch_recovery(monkeypatch):
    app = create_base_app('serving-loop-api-role', {'TESTING': True})
    calls = []
    runtime = _Runtime(calls)
    _install_fakes(monkeypatch, calls, runtime)
    stop = threading.Event()
    register_serving_loop_lifecycle(
        app,
        shutdown_requested=stop,
        hooks=object(),
        environ={},
        process_role='api',
    )

    async def exercise():
        async with app.test_app():
            state = app.extensions['tofu_serving_loop_lifecycle']
            assert state['process_role'] == 'api'
            assert state['owns_task_recovery'] is False
            assert state['owns_task_workers'] is False
            assert state['owns_attempt_dispatch_recovery'] is False
            task_names = {
                details for name, details in calls if name == 'task'
            }
            assert task_names.isdisjoint({
                'tofu-orphan-queue-redispatch',
                'tofu-turn-recovery-backstop',
                'tofu-attempt-dispatch-recovery',
            })

    asyncio.run(exercise())


def test_worker_role_owns_attempt_dispatch_recovery(monkeypatch):
    app = create_base_app('serving-loop-worker-role', {'TESTING': True})
    calls = []
    runtime = _Runtime(calls)
    _install_fakes(monkeypatch, calls, runtime)
    stop = threading.Event()
    register_serving_loop_lifecycle(
        app,
        shutdown_requested=stop,
        hooks=object(),
        environ={},
        process_role='worker',
    )

    async def exercise():
        async with app.test_app():
            state = app.extensions['tofu_serving_loop_lifecycle']
            assert state['process_role'] == 'worker'
            assert state['owns_task_recovery'] is True
            assert state['owns_task_workers'] is True
            assert state['owns_attempt_dispatch_recovery'] is True
            task_names = {
                details for name, details in calls if name == 'task'
            }
            assert {
                'tofu-orphan-queue-redispatch',
                'tofu-turn-recovery-backstop',
                'tofu-attempt-dispatch-recovery',
            }.issubset(task_names)

    asyncio.run(exercise())


def test_server_runtime_factory_shares_one_event_and_handler_order(monkeypatch):
    import server
    app = server.create_production_app({'TESTING': True})
    event = app.extensions['tofu_shutdown_requested']
    assert event is app.extensions['tofu_production_lifecycle'][
        'shutdown_requested']
    assert app.extensions['tofu_lifecycle']['startup_handlers'][-2:] == (
        'tofu.serving-loop.startup', 'tofu.production.startup')
    assert app.extensions['tofu_lifecycle']['shutdown_handlers'][-2:] == (
        'tofu.serving-loop.shutdown', 'tofu.production.shutdown')
