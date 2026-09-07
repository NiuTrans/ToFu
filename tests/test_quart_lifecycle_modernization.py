"""Native Quart application lifecycle and Hypercorn configuration contracts."""

from __future__ import annotations

import asyncio
import threading

import pytest

from lib.app_lifecycle import (
    add_shutdown_handler,
    add_startup_handler,
    register_app_lifecycle,
)
from lib.app_factory import create_base_app
from lib.hypercorn_runtime import build_hypercorn_config


pytestmark = pytest.mark.unit


def _run_async(awaitable):
    """Run an awaitable even after Playwright leaves a loop on this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result = []
    failure = []

    def runner():
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=runner, name='lifecycle-test-loop')
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), 'isolated lifecycle test loop did not finish'
    if failure:
        raise failure[0]
    return result[0] if result else None


class _FakeApp:
    def __init__(self):
        self.extensions = {}
        self.startup = []
        self.shutdown = []

    def before_serving(self, fn):
        self.startup.append(fn)
        return fn

    def after_serving(self, fn):
        self.shutdown.append(fn)
        return fn


def test_lifecycle_registration_is_idempotent_and_tracks_native_lifespan():
    app = _FakeApp()
    register_app_lifecycle(app)
    register_app_lifecycle(app)
    assert len(app.startup) == len(app.shutdown) == 1

    async def exercise():
        await app.startup[0]()
        state = app.extensions['tofu_lifecycle']
        assert state['status'] == 'serving'
        assert state['loop'] is asyncio.get_running_loop()
        await app.shutdown[0]()
        assert state['status'] == 'stopped'
        assert state['loop'] is None

    _run_async(exercise())


def test_base_app_factory_owns_native_shell_and_lifespan():
    app = create_base_app('tofu-factory-test', {'TESTING': True})
    assert app.config['TESTING'] is True
    assert app.config['PROVIDE_AUTOMATIC_OPTIONS'] is True
    assert app.static_folder is None
    assert app.extensions['tofu_lifecycle']['status'] == 'created'

    async def exercise():
        async with app.test_app():
            state = app.extensions['tofu_lifecycle']
            assert state['status'] == 'serving'
            assert state['loop'] is asyncio.get_running_loop()
        assert app.extensions['tofu_lifecycle']['status'] == 'stopped'

    _run_async(exercise())


def test_lifecycle_runs_named_handlers_on_serving_loop_and_shutdown_in_reverse():
    app = _FakeApp()
    register_app_lifecycle(app)
    calls = []

    async def start_async():
        calls.append(('start-async', asyncio.get_running_loop()))

    def start_sync():
        calls.append(('start-sync', asyncio.get_running_loop()))

    async def stop_first():
        calls.append(('stop-first', asyncio.get_running_loop()))

    def stop_last():
        calls.append(('stop-last', asyncio.get_running_loop()))

    assert add_startup_handler(app, start_sync, name='start-sync') is True
    assert add_startup_handler(app, start_async, name='start-async') is True
    assert add_startup_handler(app, start_async, name='start-async') is False
    add_shutdown_handler(app, stop_first, name='stop-first')
    add_shutdown_handler(app, stop_last, name='stop-last')

    async def exercise():
        loop = asyncio.get_running_loop()
        await app.startup[0]()
        assert app.extensions['tofu_lifecycle']['startup_completed'] == (
            'start-sync', 'start-async')
        await app.shutdown[0]()
        assert all(call_loop is loop for _, call_loop in calls)

    _run_async(exercise())
    assert [name for name, _ in calls] == [
        'start-sync', 'start-async', 'stop-last', 'stop-first']


def test_startup_failure_is_visible_and_stops_later_handlers():
    app = _FakeApp()
    register_app_lifecycle(app)
    calls = []

    async def fail():
        calls.append('fail')
        raise RuntimeError('injected startup failure')

    add_startup_handler(app, fail, name='fail')
    add_startup_handler(app, lambda: calls.append('too-late'), name='too-late')

    with pytest.raises(RuntimeError, match='injected startup failure'):
        _run_async(app.startup[0]())
    state = app.extensions['tofu_lifecycle']
    assert state['status'] == 'startup_failed'
    assert state['startup_completed'] == ()
    assert calls == ['fail']


def test_startup_failure_rolls_back_shutdown_handlers_once():
    app = _FakeApp()
    register_app_lifecycle(app)
    calls = []

    add_startup_handler(
        app, lambda: calls.append('started'), name='started')

    def fail_start():
        calls.append('start-failed')
        raise RuntimeError('startup exploded')

    def fail_cleanup():
        calls.append('cleanup-failed')
        raise ValueError('cleanup exploded')

    add_startup_handler(app, fail_start, name='start-failed')
    add_shutdown_handler(
        app, lambda: calls.append('cleanup-first'), name='cleanup-first')
    add_shutdown_handler(app, fail_cleanup, name='cleanup-failed')

    async def exercise():
        with pytest.raises(RuntimeError, match='startup exploded'):
            await app.startup[0]()
        # A framework shutdown after the failed lifespan must be idempotent.
        await app.shutdown[0]()

    _run_async(exercise())
    state = app.extensions['tofu_lifecycle']
    assert state['status'] == 'startup_failed'
    assert state['shutdown_completed'] == ('cleanup-first',)
    assert state['shutdown_errors'][0][0] == 'cleanup-failed'
    assert calls == [
        'started', 'start-failed', 'cleanup-failed', 'cleanup-first',
    ]


def test_shutdown_failure_does_not_skip_remaining_cleanup():
    app = _FakeApp()
    register_app_lifecycle(app)
    calls = []

    def first():
        calls.append('first')

    def fail_last():
        calls.append('fail-last')
        raise RuntimeError('injected shutdown failure')

    add_shutdown_handler(app, first, name='first')
    add_shutdown_handler(app, fail_last, name='fail-last')

    async def exercise():
        await app.startup[0]()
        with pytest.raises(RuntimeError, match='injected shutdown failure'):
            await app.shutdown[0]()

    _run_async(exercise())
    state = app.extensions['tofu_lifecycle']
    assert state['status'] == 'stopped'
    assert state['shutdown_completed'] == ('first',)
    assert calls == ['fail-last', 'first']


def test_hypercorn_config_defaults_overrides_and_tls():
    config = build_hypercorn_config(
        '127.0.0.1', 15000, keep_alive_timeout=30,
        tls_cert='/tmp/cert.pem', tls_key='/tmp/key.pem',
        environ={'TOFU_GRACEFUL_TIMEOUT': '4.5',
                 'TOFU_LISTEN_BACKLOG': '2048'},
    )
    assert config.bind == ['127.0.0.1:15000']
    assert config.keep_alive_timeout == 30
    assert config.websocket_max_message_size == 64 * 1024
    assert config.graceful_timeout == 4.5
    assert config.backlog == 2048
    assert config.certfile == '/tmp/cert.pem'
    assert config.keyfile == '/tmp/key.pem'


def test_hypercorn_config_invalid_numbers_fall_back():
    config = build_hypercorn_config(
        '0.0.0.0', 15000, keep_alive_timeout=15,
        environ={'TOFU_GRACEFUL_TIMEOUT': 'bad',
                 'TOFU_LISTEN_BACKLOG': '-2'},
    )
    assert config.graceful_timeout == 3.0
    assert config.backlog == 1024
