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
        return _Watchdog('watchdog', calls)

    monkeypatch.setattr(lifecycle, '_create_watchdog', watchdog)
    monkeypatch.setattr(
        lifecycle, '_load_write_freshness_snapshot',
        lambda: calls.append(('snapshot', {})))
    monkeypatch.setattr(
        lifecycle, '_start_auto_restart',
        lambda _event: calls.append(('auto-start', {})) or True)
    monkeypatch.setattr(
        lifecycle, '_stop_auto_restart',
        lambda: calls.append(('auto-stop', {})))
    monkeypatch.setattr(
        lifecycle, '_run_deferred_dispatch',
        lambda descriptor, _event: calls.append(('deferred', descriptor)))
    monkeypatch.setattr(
        lifecycle, '_redispatch_orphaned_queue',
        lambda: calls.append(('orphan', {})) or ['task-1'])


def test_loop_owners_start_on_serving_loop_and_recovery_reads_after_gate(
        monkeypatch):
    app = create_base_app('serving-loop-lifecycle', {'TESTING': True})
    calls = []
    runtime = _Runtime(calls)
    _install_fakes(monkeypatch, calls, runtime)
    stop = threading.Event()
    descriptor = {'value': 'before-db'}

    assert register_serving_loop_lifecycle(
        app,
        shutdown_requested=stop,
        host='127.0.0.1',
        port=16000,
        hooks=object(),
        deferred_dispatch_provider=lambda: descriptor['value'],
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
            assert not any(name == 'deferred' for name, _ in calls)
            descriptor['value'] = 'after-db'
            state['gate'].set()
            for _ in range(50):
                if any(name == 'orphan' for name, _ in calls) and any(
                        name == 'deferred' for name, _ in calls):
                    break
                await asyncio.sleep(0.01)
            assert ('deferred', 'after-db') in calls
            assert ('orphan', {}) in calls
        assert loop.get_exception_handler() is previous

    asyncio.run(exercise())
    assert stop.is_set()
    assert app.extensions['tofu_serving_loop_lifecycle']['status'] == 'stopped'
    assert ('task', 'tofu-deferred-boot-dispatch') in calls
    assert ('task', 'tofu-orphan-queue-redispatch') in calls
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


def test_server_runtime_factory_shares_one_event_and_handler_order():
    import server

    app = server.create_production_app({'TESTING': True})
    event = app.extensions['tofu_shutdown_requested']
    assert event is app.extensions['tofu_production_lifecycle'][
        'shutdown_requested']
    assert app.extensions['tofu_lifecycle']['startup_handlers'][-2:] == (
        'tofu.serving-loop.startup', 'tofu.production.startup')
    assert app.extensions['tofu_lifecycle']['shutdown_handlers'][-2:] == (
        'tofu.serving-loop.shutdown', 'tofu.production.shutdown')
