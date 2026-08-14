"""Native Quart application lifecycle and Hypercorn configuration contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
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


def test_full_application_factory_is_a_separate_repeatable_boundary():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'server.py').read_text()
    assembly = (root / 'lib/app_assembly.py').read_text()
    assert 'from lib.app_assembly import create_application' in source
    assert 'return create_application(' in source
    assert 'def configure_application(' in assembly
    assert 'register_all(app, start_workers=False)' in assembly
    assert "app.extensions[marker] = True" in assembly


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


def test_production_entry_uses_quart_lifespan_not_a_disposable_startup_loop():
    root = Path(__file__).resolve().parents[1]
    server_source = (root / 'server.py').read_text()
    lifespan_source = (root / 'lib/production_lifecycle.py').read_text()
    loop_source = (root / 'lib/serving_loop_lifecycle.py').read_text()
    asgi_source = (root / 'asgi.py').read_text()
    assert 'asyncio.run(_startup())' not in server_source
    assert "name='tofu.production.startup'" in lifespan_source
    assert "name='tofu.production.shutdown'" in lifespan_source
    assert "name='tofu.serving-loop.startup'" in loop_source
    assert "name='tofu.serving-loop.shutdown'" in loop_source
    assert 'deferred_dispatch_provider()' in loop_source
    assert 'await hypercorn_serve(app, hconfig' in server_source
    assert 'create_production_app' in asgi_source


def test_shutdown_policy_is_owned_outside_server_entrypoint():
    root = Path(__file__).resolve().parents[1]
    server_source = (root / 'server.py').read_text()
    shutdown_source = (root / 'lib/server_shutdown.py').read_text()
    assert 'from lib.server_shutdown import (' in server_source
    assert 'def graceful_shutdown_signals(' not in server_source
    assert 'def _request_graceful_shutdown(' not in server_source
    assert 'def graceful_shutdown_signals(' in shutdown_source
    assert 'def request_graceful_shutdown(' in shutdown_source


def test_listener_network_policy_is_owned_outside_server_entrypoint():
    root = Path(__file__).resolve().parents[1]
    server_source = (root / 'server.py').read_text()
    network_source = (root / 'lib/server_network.py').read_text()
    assert 'from lib.server_network import (' in server_source
    for name in (
        '_detect_reverse_proxy', '_resolve_tls_policy',
        '_find_free_port', '_wait_port_free',
    ):
        assert f'def {name}(' not in server_source
    assert 'def detect_reverse_proxy(' in network_source
    assert 'def resolve_tls_policy(' in network_source
    assert 'def find_free_port(' in network_source
    assert 'def wait_port_free(' in network_source


def test_tls_certificate_owner_is_outside_server_entrypoint():
    root = Path(__file__).resolve().parents[1]
    server_source = (root / 'server.py').read_text()
    tls_source = (root / 'lib/server_tls.py').read_text()
    assert 'ensure_tls_certificates as _ensure_tls_certs' in server_source
    assert 'def _ensure_tls_certs(' not in server_source
    assert 'def ensure_tls_certificates(' in tls_source
    assert 'x509.CertificateBuilder()' in tls_source


def test_http_compression_is_registered_outside_server_assembly():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'server.py').read_text()
    middleware = (root / 'lib/http_compression.py').read_text()
    assembly = (root / 'lib/app_assembly.py').read_text()
    assert 'configure_application(' in source
    assert 'register_http_compression(app)' in assembly
    assert 'async def _compress_response' not in source
    assert 'async def compress_response' in middleware
    assert 'run_in_executor' in middleware


def test_request_observation_is_registered_outside_server_assembly():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'server.py').read_text()
    middleware = (root / 'lib/http_request_lifecycle.py').read_text()
    assembly = (root / 'lib/app_assembly.py').read_text()
    assert 'configure_application(' in source
    assert 'register_request_lifecycle(app)' in assembly
    assert 'async def _assign_req_id_and_log' not in source
    assert 'async def assign_request_id_and_log' in middleware
    assert 'route_template_for_request(request)' in middleware


def test_http_compat_and_cache_policy_are_registered_outside_assembly():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'server.py').read_text()
    middleware = (root / 'lib/http_compat_middleware.py').read_text()
    assembly = (root / 'lib/app_assembly.py').read_text()
    assert 'configure_application(' in source
    assert 'register_method_override(app)' in assembly
    assert 'register_static_cache_headers(app)' in assembly
    assert 'async def method_override' not in source
    assert 'async def add_static_cache_headers' in middleware


def test_global_error_mapping_is_registered_outside_server_assembly():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'server.py').read_text()
    middleware = (root / 'lib/http_error_handlers.py').read_text()
    assembly = (root / 'lib/app_assembly.py').read_text()
    assert 'configure_application(' in source
    assert 'register_http_error_handlers(app)' in assembly
    assert 'async def _handle_uncaught' not in source
    assert 'async def handle_uncaught' in middleware
    assert "retry_after=2, kind='overloaded'" in middleware


def test_static_route_is_registered_outside_server_assembly():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'server.py').read_text()
    serving = (root / 'lib/static_serving.py').read_text()
    assembly = (root / 'lib/app_assembly.py').read_text()
    assert 'configure_application(' in source
    assert 'register_static_route(' in assembly
    assert "@app.route('/static/<path:filename>')" not in source
    assert 'async def _static_route' not in source
    assert 'def register_static_route(' in serving
    assert "app.add_url_rule(" in serving
    assert "endpoint='tofu_static'" in serving
