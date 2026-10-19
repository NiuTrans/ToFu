"""Shared CLI/ASGI production lifespan contracts."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from quart import current_app
from quart.testing.app import LifespanError

from lib.app.app_factory import create_base_app
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
            bootstrap_model_routing=phase('model-routing'),
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
            bootstrap_model_routing=lambda: None,
            validate_imports=lambda: None,
            start_workers=lambda _app: None,
        ),
    ) is False

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'ready'
            state = app.extensions['tofu_production_lifecycle']
            assert state['status'] == 'ready'
            assert state['mcp_config'] == {'servers': {'search': {}}}
            assert state['feishu_ok'] is True
            assert not stop.is_set()
        assert stop.is_set()
        assert app.extensions['tofu_production_lifecycle']['status'] == 'stopped'

    asyncio.run(exercise())
    assert [name for name, _ in calls] == [
        'assets', 'boundary', 'storage', 'database', 'model-routing',
        'imports', 'workers', 'optional', 'announce', 'shutdown',
    ]
    assert calls[6][1] == (app,)
    assert calls[-1][1][:2] == (stop, app)
    assert any(
        line.startswith('[startup phase 1/9] start | Frontend assets')
        for line in boot_lines)
    assert any(
        line.startswith('[startup phase 9/9] done | Readiness announcement | ')
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
            bootstrap_model_routing=lambda: calls.append('model-routing'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        optional_services=lambda **_kwargs: calls.append('optional'),
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'interrupted'
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


def test_inline_phase_failure_uses_native_rollback_cleanup():
    """A listener-critical (inline) phase failure still fails startup fast."""
    app = create_base_app('production-lifecycle-rollback', {'TESTING': True})
    stop = threading.Event()
    calls = []

    def fail_assets():
        calls.append('assets')
        raise RuntimeError('artifact validation failed')

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
            bootstrap_model_routing=lambda: calls.append('model-routing'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        with pytest.raises(LifespanError, match='artifact validation failed'):
            async with app.test_app():
                pass

    asyncio.run(exercise())
    assert stop.is_set()
    assert calls == ['assets', 'shutdown']
    assert app.extensions['tofu_lifecycle']['status'] == 'startup_failed'


def test_background_phase_failure_latches_boot_failed_and_retries():
    """A backgrounded required phase failure keeps the process up (owner
    ruling 2026-09-20): status latches boot_failed, later phases never run,
    and an operator retry re-runs from the failed phase only."""
    app = create_base_app('production-boot-failure', {'TESTING': True})
    stop = threading.Event()
    calls = []
    fail_database = {'enabled': True}

    def maybe_fail_database():
        calls.append('database')
        if fail_database['enabled']:
            raise RuntimeError('database bootstrap failed')

    async def shutdown_runtime(event, **_kwargs):
        calls.append('shutdown')

    register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=lambda: calls.append('assets'),
            validate_storage_boundary=lambda: calls.append('boundary'),
            init_database=maybe_fail_database,
            start_storage=lambda: calls.append('storage'),
            bootstrap_model_routing=lambda: calls.append('model-routing'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        optional_services=lambda **_kwargs: ({}, False),
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'failed'
            assert app.extensions['tofu_production_lifecycle'][
                'status'] == 'boot_failed'
            assert calls == ['assets', 'boundary', 'storage', 'database']
            # Retry while failed re-runs from the failed phase only.
            fail_database['enabled'] = False
            assert runner.request_retry() == 'started'
            assert await runner.wait_done() == 'ready'
            assert app.extensions['tofu_production_lifecycle'][
                'status'] == 'ready'

    asyncio.run(exercise())
    assert calls == [
        'assets', 'boundary', 'storage', 'database',
        'database', 'model-routing', 'imports', 'workers',
        'shutdown',
    ]


def _register_flaky_database_app(app, stop, calls, *, should_fail):
    """Wire a lifecycle whose database phase fails retryably per should_fail().

    The raised error is the classified-retryable typed failure the storage
    client raises when the authority is starved by host memory pressure.
    """
    from lib.storage.errors import StorageError

    def maybe_fail_database():
        calls.append('database')
        if should_fail():
            raise StorageError(
                'database_unavailable',
                'storage.v2 authority is unavailable',
                True,
                100,
            )

    async def shutdown_runtime(event, **_kwargs):
        calls.append('shutdown')

    register_production_lifecycle(
        app,
        steps=ProductionStartupSteps(
            build_assets=lambda: calls.append('assets'),
            validate_storage_boundary=lambda: calls.append('boundary'),
            init_database=maybe_fail_database,
            start_storage=lambda: calls.append('storage'),
            bootstrap_model_routing=lambda: calls.append('model-routing'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        optional_services=lambda **_kwargs: ({}, False),
        shutdown_runtime=shutdown_runtime,
    )


def test_retryable_background_phase_failure_auto_recovers_without_operator(
        monkeypatch):
    """A classified-retryable phase failure re-runs in place with bounded
    backoff: a transient authority-starvation window self-heals and the boot
    reaches ready with no operator retry."""
    monkeypatch.setattr(
        'lib.production_lifecycle._BOOT_PHASE_RETRY_BACKOFF_S',
        (0.01, 0.01, 0.01, 0.01))
    app = create_base_app('production-boot-auto-retry', {'TESTING': True})
    stop = threading.Event()
    calls = []
    failures_left = {'count': 2}

    def should_fail():
        if failures_left['count'] <= 0:
            return False
        failures_left['count'] -= 1
        return True

    _register_flaky_database_app(app, stop, calls, should_fail=should_fail)

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'ready'
            assert app.extensions['tofu_production_lifecycle'][
                'status'] == 'ready'
            assert runner.snapshot()['error'] is None

    asyncio.run(exercise())
    assert calls == [
        'assets', 'boundary', 'storage',
        'database', 'database', 'database',
        'model-routing', 'imports', 'workers',
        'shutdown',
    ]


def test_retryable_phase_surfaces_transient_reason_while_retrying(
        monkeypatch):
    """While auto-retrying, the running phase must carry the transient reason
    and retry ordinal so the cold-start splash never shows a silent timer."""
    monkeypatch.setattr(
        'lib.production_lifecycle._BOOT_PHASE_RETRY_BACKOFF_S',
        (0.5, 0.5, 0.5, 0.5))
    app = create_base_app('production-boot-retry-visible', {'TESTING': True})
    stop = threading.Event()
    calls = []
    failures_left = {'count': 1}

    def should_fail():
        if failures_left['count'] <= 0:
            return False
        failures_left['count'] -= 1
        return True

    _register_flaky_database_app(app, stop, calls, should_fail=should_fail)

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            for _ in range(400):
                phase = runner.snapshot().get('currentPhase') or {}
                if phase.get('retry'):
                    assert phase['status'] == 'running'
                    assert 'unavailable' in (phase.get('error') or '')
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail('boot never exposed a retrying phase')
            assert await runner.wait_done() == 'ready'

    asyncio.run(exercise())


def test_retryable_background_phase_failure_latches_after_budget_exhausted(
        monkeypatch):
    """The auto-retry window is bounded (never an unbounded repair loop):
    a permanently starving authority still latches boot_failed with the
    error, and later phases never run until the operator retry. A zero
    window latches on the first failure without a single re-run."""
    monkeypatch.setattr(
        'lib.production_lifecycle._BOOT_PHASE_RETRY_BACKOFF_S',
        (0.01, 0.01, 0.01, 0.01))
    monkeypatch.setattr(
        'lib.production_lifecycle._BOOT_PHASE_AUTO_RETRY_WINDOW_S', 0.0)
    app = create_base_app('production-boot-auto-retry-budget',
                          {'TESTING': True})
    stop = threading.Event()
    calls = []
    failing = {'enabled': True}

    _register_flaky_database_app(
        app, stop, calls, should_fail=lambda: failing['enabled'])

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'failed'
            assert app.extensions['tofu_production_lifecycle'][
                'status'] == 'boot_failed'
            assert 'storage.v2 authority is unavailable' in (
                runner.snapshot()['error'])
            # Zero window: the initial attempt latches immediately.
            assert calls == [
                'assets', 'boundary', 'storage', 'database',
            ]
            failing['enabled'] = False
            assert runner.request_retry() == 'started'
            assert await runner.wait_done() == 'ready'

    asyncio.run(exercise())
    assert calls == [
        'assets', 'boundary', 'storage', 'database',
        'database', 'model-routing', 'imports', 'workers',
        'shutdown',
    ]


def test_retry_window_outwaits_a_sustained_starvation_episode(monkeypatch):
    """2026-09-22 evening: the pressure episode starved the authority for
    ~10 minutes — an ATTEMPT-boxed budget latched mid-episode while the
    authority was still legitimately recovering. The time-boxed window
    keeps re-running for its full duration, then still latches (bounded)."""
    monkeypatch.setattr(
        'lib.production_lifecycle._BOOT_PHASE_RETRY_BACKOFF_S',
        (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(
        'lib.production_lifecycle._BOOT_PHASE_AUTO_RETRY_WINDOW_S', 0.3)
    app = create_base_app('production-boot-retry-window',
                          {'TESTING': True})
    stop = threading.Event()
    calls = []
    failing = {'enabled': True}

    _register_flaky_database_app(
        app, stop, calls, should_fail=lambda: failing['enabled'])

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'failed'
            database_attempts = calls.count('database')
            # The window allowed several re-runs (not just one), yet stayed
            # bounded: no unbounded repair loop even with tiny backoff.
            assert database_attempts >= 3
            assert database_attempts <= 20
            assert 'model-routing' not in calls
            failing['enabled'] = False
            assert runner.request_retry() == 'started'
            assert await runner.wait_done() == 'ready'

    asyncio.run(exercise())


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
            bootstrap_model_routing=lambda: calls.append('model-routing'),
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


def test_storage_handshake_failure_blocks_workers_without_killing_listener():
    """The sidecar handshake remains a hard readiness gate: a failed
    handshake never runs later phases and readiness never opens — but the
    listener stays up so the browser can see the truthful boot failure."""
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
            bootstrap_model_routing=lambda: calls.append('model-routing'),
            validate_imports=lambda: calls.append('imports'),
            start_workers=lambda _app: calls.append('workers'),
        ),
        shutdown_requested=stop,
        shutdown_runtime=shutdown_runtime,
    )

    async def exercise():
        async with app.test_app():
            runner = app.extensions['tofu_boot_runner']
            assert await runner.wait_done() == 'failed'
            assert app.extensions['tofu_production_lifecycle'][
                'status'] == 'boot_failed'
            assert runner.snapshot()['failedPhase']['key'] == (
                'storage_authority_sql')

    asyncio.run(exercise())
    assert stop.is_set()
    assert calls == ['assets', 'boundary', 'storage', 'shutdown']


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
    assert steps.start_storage is server_module._start_storage_authority_sql
    from lib.model_routing import bootstrap_personal_model_routing
    assert steps.bootstrap_model_routing is bootstrap_personal_model_routing
    assert (
        steps.validate_storage_boundary
        is server_module._validate_storage_cutover_boundary
    )


def test_slow_storage_boundary_leaves_shell_and_boot_available(monkeypatch):
    """Required storage admission may block; it must not hold HTTP startup."""
    from lib.http_transport.request_lifecycle import register_request_lifecycle
    from routes.api_v4.bootstrap import api_v4_bp

    app = create_base_app('early-shell-storage-boundary', {'TESTING': True})
    register_request_lifecycle(app)
    app.register_blueprint(api_v4_bp)
    monkeypatch.setattr('lib.storage.storage_status', lambda: {'ready': True})
    started, release = threading.Event(), threading.Event()
    calls = []

    @app.route('/')
    async def shell():
        return '<main>Tofu shell</main>'

    @app.route('/api/send', methods=['POST'])
    async def send():
        calls.append('send')
        return {'ok': True}

    def boundary():
        started.set()
        assert release.wait(5), 'test did not release its storage gate'

    async def shutdown(*_args, **_kwargs):
        release.set()

    register_production_lifecycle(app, steps=ProductionStartupSteps(
        build_assets=lambda: None, validate_storage_boundary=boundary,
        start_storage=lambda: calls.append('storage'),
        init_database=lambda: None, bootstrap_model_routing=lambda: None,
        validate_imports=lambda: None, start_workers=lambda _: None,
    ), shutdown_runtime=shutdown, optional_services=lambda **_: ({}, False))

    async def exercise():
        try:
            async with app.test_app():
                assert await asyncio.to_thread(started.wait, 1)
                async with app.test_client() as client:
                    assert (await asyncio.wait_for(client.get('/'), 1)).status_code == 200
                    response = await asyncio.wait_for(client.get('/api/v4/boot'), 1)
                    body = await response.get_json()
                    assert body['currentPhase']['key'] == 'storage_boundary'
                    assert (await client.post('/api/send')).status_code == 503
                    assert calls == []
                    release.set()
                    assert await app.extensions['tofu_boot_runner'].wait_done() == 'ready'
                    assert (await client.post('/api/send')).status_code == 200
        finally:
            release.set()
    asyncio.run(exercise())
    assert calls == ['storage', 'send']
