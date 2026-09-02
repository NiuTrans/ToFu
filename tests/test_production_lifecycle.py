"""Shared CLI/ASGI production lifespan contracts."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from quart import current_app
from quart.testing.app import LifespanError

from lib.app_factory import create_base_app
from lib.production_lifecycle import (
    ProductionStartupSteps,
    register_production_lifecycle,
    start_optional_production_services,
)


pytestmark = pytest.mark.unit


def test_production_lifecycle_runs_required_optional_and_cleanup_in_order():
    app = create_base_app('production-lifecycle-order', {'TESTING': True})
    stop = threading.Event()
    calls = []
    boot_lines = []

    def phase(name):
        def run(*args):
            assert current_app._get_current_object() is app
            calls.append((name, args))
        return run

    def optional_services(**kwargs):
        assert kwargs['shutdown_requested'] is stop
        calls.append(('optional', ()))
        return {'servers': {'search': {}}}, True

    def announce(mcp_config, feishu_ok):
        calls.append(('announce', (mcp_config, feishu_ok)))

    async def shutdown_runtime(event, *, app, logger):
        calls.append(('shutdown', (event, app, logger.name)))

    registered = register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=phase('assets'),
            validate_storage_boundary=phase('boundary'),
            init_database=phase('database'),
            start_storage=phase('storage'),
            validate_imports=phase('imports'),
            start_workers=phase('workers'),
        ),
        shutdown_requested=stop,
        logger=logging.getLogger('test.production-lifecycle'),
        boot=lambda message, *args: boot_lines.append(
            message % args if args else message),
        announce_ready=announce,
        optional_services=optional_services,
        shutdown_runtime=shutdown_runtime,
    )
    assert registered is True
    assert register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=lambda: None,
            validate_storage_boundary=lambda: None,
            init_database=lambda: None,
            start_storage=lambda: None,
            validate_imports=lambda: None,
            start_workers=lambda _app: None,
        ),
    ) is False

    async def exercise():
        async with app.test_app():
            state = app.extensions['tofu_production_lifecycle']
            assert state['status'] == 'ready'
            assert state['mcp_config'] == {'servers': {'search': {}}}
            assert state['feishu_ok'] is True
            assert not stop.is_set()
        assert stop.is_set()
        assert app.extensions['tofu_production_lifecycle']['status'] == 'stopped'

    asyncio.run(exercise())
    assert [name for name, _ in calls] == [
        'assets', 'boundary', 'storage', 'database', 'imports', 'workers', 'optional',
        'announce', 'shutdown',
    ]
    assert calls[5][1] == (app,)
    assert calls[-1][1][:2] == (stop, app)
    assert any(
        line.startswith('[startup phase 1/8] start | Frontend assets')
        for line in boot_lines)
    assert any(
        line.startswith('[startup phase 8/8] done | Readiness announcement | ')
        for line in boot_lines)


def test_shutdown_checkpoint_skips_post_database_startup():
    app = create_base_app('production-lifecycle-interrupt', {'TESTING': True})
    stop = threading.Event()
    calls = []

    def init_database():
        calls.append('database')
        stop.set()

    async def shutdown_runtime(*_args, **_kwargs):
        calls.append('shutdown')

    register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=lambda: calls.append('assets'),
            validate_storage_boundary=lambda: calls.append('boundary'),
            init_database=init_database,
            start_storage=lambda: calls.append('storage'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        optional_services=lambda **_kwargs: calls.append('optional'),
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        async with app.test_app():
            assert app.extensions['tofu_production_lifecycle'][
                'status'] == 'interrupted'

    asyncio.run(exercise())
    assert calls == ['assets', 'boundary', 'storage', 'database', 'shutdown']



def test_distributed_preview_starts_no_optional_service(monkeypatch):
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
    monkeypatch.setenv('TOFU_DISTRIBUTED_PREVIEW_MODE', 'read-only')

    result = start_optional_production_services(
        shutdown_requested=threading.Event(),
        logger=logging.getLogger('test.distributed-preview'),
        process_role='worker',
    )

    assert result == ({}, False)


def test_startup_failure_uses_native_rollback_cleanup():
    app = create_base_app('production-lifecycle-rollback', {'TESTING': True})
    stop = threading.Event()
    calls = []

    def fail_database():
        calls.append('database')
        raise RuntimeError('database bootstrap failed')

    async def shutdown_runtime(event, **_kwargs):
        calls.append('shutdown')
        assert event.is_set()

    register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=lambda: calls.append('assets'),
            validate_storage_boundary=lambda: calls.append('boundary'),
            init_database=fail_database,
            start_storage=lambda: calls.append('storage'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        with pytest.raises(LifespanError, match='database bootstrap failed'):
            async with app.test_app():
                pass

    asyncio.run(exercise())
    assert stop.is_set()
    assert calls == ['assets', 'boundary', 'storage', 'database', 'shutdown']
    assert app.extensions['tofu_lifecycle']['status'] == 'startup_failed'


def test_frontend_artifact_failure_blocks_readiness_and_runs_rollback():
    app = create_base_app('production-frontend-gate', {'TESTING': True})
    stop = threading.Event()
    readiness_gate = threading.Event()
    app.extensions['tofu_production_startup_gate'] = readiness_gate
    calls = []

    def fail_assets():
        calls.append('assets')
        raise RuntimeError('required frontend artifact missing')

    async def shutdown_runtime(event, **_kwargs):
        calls.append('shutdown')
        assert event.is_set()

    register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=fail_assets,
            validate_storage_boundary=lambda: calls.append('boundary'),
            init_database=lambda: calls.append('database'),
            start_storage=lambda: calls.append('storage'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        shutdown_runtime=shutdown_runtime,
        process_role='api',
    )

    async def exercise():
        with pytest.raises(
                LifespanError, match='required frontend artifact missing'):
            async with app.test_app():
                pass

    asyncio.run(exercise())
    assert stop.is_set()
    assert not readiness_gate.is_set()
    assert calls == ['assets', 'shutdown']
    assert app.extensions['tofu_lifecycle']['status'] == 'startup_failed'


def test_storage_handshake_failure_blocks_workers_and_runs_cleanup():
    app = create_base_app('production-storage-gate', {'TESTING': True})
    stop = threading.Event()
    calls = []

    def fail_storage():
        calls.append('storage')
        raise RuntimeError('sidecar handshake failed')

    async def shutdown_runtime(event, **_kwargs):
        calls.append('shutdown')
        assert event.is_set()

    register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=lambda: calls.append('assets'),
            validate_storage_boundary=lambda: calls.append('boundary'),
            init_database=lambda: calls.append('database'),
            start_storage=fail_storage,
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        with pytest.raises(LifespanError, match='sidecar handshake failed'):
            async with app.test_app():
                pass

    asyncio.run(exercise())
    assert calls == ['assets', 'boundary', 'storage', 'shutdown']
    assert app.extensions['tofu_production_lifecycle'][
        'status'] == 'stopped'


def test_server_composition_wires_required_storage_phase(monkeypatch):
    import server as server_module
    from lib import production_lifecycle

    captured = {}

    def capture_registration(target_app, **kwargs):
        captured['target_app'] = target_app
        captured.update(kwargs)
        return 'registered'

    monkeypatch.setattr(
        production_lifecycle,
        'register_production_lifecycle',
        capture_registration,
    )
    app_sentinel = object()

    assert server_module.register_server_production_lifecycle(
        app_sentinel
    ) == 'registered'
    assert captured['target_app'] is app_sentinel
    steps = captured['steps']
    assert steps.start_storage is server_module._start_storage_sidecar
    assert (
        steps.validate_storage_boundary
        is server_module._validate_storage_cutover_boundary
    )
