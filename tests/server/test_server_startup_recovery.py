"""The sidecar-ready startup sequence restores every process-local owner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_engine_preparation(monkeypatch):
    # These tests own boot ordering/retries, not an installation's artifacts.
    import serverctl
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact', lambda *a, **k: '')


def test_init_database_runs_complete_recovery_sequence(monkeypatch):
    import server
    import lib.orchestration.startup_recovery as orchestration_recovery
    import lib.conversations.project_brain_startup as brain_startup
    import lib.shutdown_marker as shutdown_marker
    import lib.swarm.integration as swarm_integration
    import lib.tasks_pkg.manager as task_manager
    import lib.turn_lifecycle as turns

    calls = []
    previous_shutdown = {'verdict': 'unclean'}


    from lib.storage import warmup
    monkeypatch.setattr(
        warmup, 'run_warmup_pass',
        lambda *_a, **_k: pytest.fail('cache warming must not block readiness'))
    monkeypatch.setattr(
        warmup, 'start_keep_warm',
        lambda **kwargs: calls.append(('warmup', kwargs['initial_pass'])))
    monkeypatch.setattr(
        brain_startup, 'ensure_project_brain_cutover',
        lambda: calls.append('brain-cutover') or {'complete': True})
    monkeypatch.setattr(
        brain_startup, 'recover_active_work_items',
        lambda: calls.append('brain-recovery') or 0)

    monkeypatch.setattr(
        shutdown_marker, 'report_and_arm',
        lambda: calls.append('shutdown-marker') or previous_shutdown)
    monkeypatch.setattr(
        task_manager, 'recover_stale_tasks_on_startup',
        lambda *, prev_shutdown: calls.append(('task-recovery', prev_shutdown)))
    monkeypatch.setattr(
        turns, 'cleanup_superseded_attempts',
        lambda: calls.append('attempt-cleanup'))

    monkeypatch.setattr(
        orchestration_recovery,
        'retire_interrupted_orchestration_runs_detailed',
        lambda *, error: calls.append(
            ('orchestration-recovery', error['kind']))
            or orchestration_recovery.BootRetirement(retired=2, candidates=()),
    )

    monkeypatch.setattr(
        swarm_integration, 'rehydrate_swarms_on_startup',
        lambda: calls.append('swarm-rehydrate'))
    monkeypatch.setattr(
        swarm_integration, 'start_swarm_output_cleanup',
        lambda: calls.append('swarm-output-cleanup'))
    monkeypatch.setattr(
        server, '_start_log_aggregate_runtime_after_recovery',
        lambda: calls.append('log-aggregate-flusher'))

    server._init_database()

    # Readiness-critical recovery stays in the boot phase; reconstructible
    # best-effort work moved behind the readiness gate
    # (_run_post_ready_deferred).
    assert calls == [
        'brain-cutover',
        'shutdown-marker',
        ('task-recovery', previous_shutdown),
        'brain-recovery',
        ('orchestration-recovery', 'worker_lost'),
        'log-aggregate-flusher',
    ]

    import lib.server_assembly as assembly
    assembly._run_post_ready_deferred()
    assert calls[-4:] == [
        ('warmup', True),
        'attempt-cleanup',
        'swarm-rehydrate',
        'swarm-output-cleanup',
    ]



def _retryable_storage_error(message='storage.v2 authority is unavailable'):
    from lib.storage.errors import StorageError
    return StorageError('database_unavailable', message, True, 10)


def test_readiness_critical_step_rides_out_transient_storage_error(monkeypatch):
    """A sidecar recycle that transiently fails one step must not fail boot."""
    import lib.server_assembly as assembly

    monkeypatch.setattr(assembly.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(assembly, '_server_log', __import__('logging').getLogger('test'))
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise _retryable_storage_error()
        return 'recovered'

    assert assembly._run_readiness_critical_storage_step(
        'flaky', flaky, deadline_s=30.0) == 'recovered'
    assert len(attempts) == 3


def test_readiness_critical_step_fails_fast_on_non_retryable(monkeypatch):
    import lib.server_assembly as assembly

    def broken():
        raise RuntimeError('Project Brain cutover verification failed')

    with pytest.raises(RuntimeError):
        assembly._run_readiness_critical_storage_step(
            'broken', broken, deadline_s=30.0)


def test_readiness_critical_step_reraises_after_budget(monkeypatch):
    import lib.server_assembly as assembly

    monkeypatch.setattr(assembly.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(assembly, '_server_log', __import__('logging').getLogger('test'))

    def never():
        raise _retryable_storage_error()

    with pytest.raises(Exception):
        assembly._run_readiness_critical_storage_step(
            'never', never, deadline_s=0.0)


def test_start_storage_retries_while_previous_owner_releases_lease(monkeypatch):
    """Phase 3 must not hard-fail the lifespan on a restart data-root race."""
    import lib.server_assembly as assembly
    import lib.storage as storage_pkg

    monkeypatch.setattr(assembly, '_server_log', __import__('logging').getLogger('test'))
    monkeypatch.setattr(assembly, '_boot', lambda *_a, **_k: None)
    monkeypatch.setattr(assembly.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(assembly, '_SIDECAR_START_BACKOFF_S', (0.0,) * 5)
    monkeypatch.setattr(assembly, '_SIDECAR_START_ATTEMPTS', 6)

    calls = []

    def contended_start():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(
                'storage sidecar closed its readiness channel before '
                'startup (exit 1)')

    monkeypatch.setattr(storage_pkg, 'start_storage', contended_start)
    monkeypatch.setattr(
        storage_pkg, 'storage_status',
        lambda: {'ready': True, 'backend': 'tofudb', 'pid': 4242})

    assembly._start_storage_sidecar()
    assert len(calls) == 3


def test_start_storage_raises_after_exhausting_retries(monkeypatch):
    import lib.server_assembly as assembly
    import lib.storage as storage_pkg

    monkeypatch.setattr(assembly, '_server_log', __import__('logging').getLogger('test'))
    monkeypatch.setattr(assembly, '_boot', lambda *_a, **_k: None)
    monkeypatch.setattr(assembly.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(assembly, '_SIDECAR_START_BACKOFF_S', (0.0,) * 5)
    monkeypatch.setattr(assembly, '_SIDECAR_START_ATTEMPTS', 3)

    def always_contended():
        raise RuntimeError('another tofu-db process owns the data directory')

    monkeypatch.setattr(storage_pkg, 'start_storage', always_contended)

    with pytest.raises(RuntimeError):
        assembly._start_storage_sidecar()


def test_start_storage_does_not_retry_typed_integrity_failure(monkeypatch):
    import lib.server_assembly as assembly
    import lib.storage as storage_pkg
    from lib.storage.errors import StorageError
    import serverctl
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact', lambda *a, **k: '')
    monkeypatch.setattr(assembly, '_boot', lambda *a, **k: None)
    monkeypatch.setattr(assembly.time, 'sleep', lambda _: pytest.fail('integrity failure must fail immediately'))
    calls = []
    failure = StorageError('database_integrity', 'search projection: CONTROL points beyond active WAL', retryable=False)
    def fail():
        calls.append('start')
        raise failure
    monkeypatch.setattr(storage_pkg, 'start_storage', fail)
    with pytest.raises(StorageError) as result:
        assembly._start_storage_sidecar()
    assert result.value is failure
    assert calls == ['start']


def test_distributed_preview_skips_all_task_recovery(monkeypatch):
    import lib.server_assembly as assembly

    calls = []
    monkeypatch.setattr(
        assembly,
        '_DEPLOYMENT_CONFIGURATION',
        SimpleNamespace(
            process_role='worker',
            distributed_preview_read_only=True,
        ),
    )
    monkeypatch.setattr(assembly, '_boot', lambda *_args: calls.append('boot'))
    monkeypatch.setattr(
        assembly,
        '_start_log_aggregate_runtime_after_recovery',
        lambda: calls.append('log-aggregate-flusher'),
    )

    assembly._init_database()

    assert calls == ['boot', 'log-aggregate-flusher']


@pytest.mark.parametrize('error', [
    RuntimeError('invalid recovery payload'),
    pytest.param(None, id='non-retryable-storage-protocol-error'),
])
def test_best_effort_recovery_does_not_sleep_on_permanent_errors(monkeypatch, error):
    import lib.server_assembly as assembly
    from lib.storage.errors import StorageError

    if error is None:
        error = StorageError('database_protocol_error', 'invalid recovery payload', False)
    calls, sleeps = [], []
    monkeypatch.setattr(assembly.time, 'sleep', sleeps.append)

    def broken():
        calls.append(1)
        raise error

    assert assembly._run_boot_recovery_step('test', broken, attempts=6) is None
    assert len(calls) == 1
    assert sleeps == []


def test_best_effort_recovery_preserves_transient_storage_retries(monkeypatch):
    import lib.server_assembly as assembly

    calls, sleeps = [], []
    monkeypatch.setattr(assembly.time, 'sleep', sleeps.append)

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _retryable_storage_error()
        return {'recovered': 1}

    assert assembly._run_boot_recovery_step('test', flaky, attempts=6) == {'recovered': 1}
    assert len(calls) == 3
    assert sleeps == [2.0, 5.0]


@pytest.mark.parametrize('kind', ['protocol', 'programming'])
def test_task_recovery_failure_blocks_readiness_without_retry_sleep(monkeypatch, caplog, kind):
    import lib.server_assembly as assembly
    import lib.conversations.project_brain_startup as brain
    import lib.shutdown_marker as marker
    import lib.tasks_pkg.manager as manager
    from lib.storage.errors import StorageError

    monkeypatch.setattr(assembly, '_DEPLOYMENT_CONFIGURATION', SimpleNamespace(
        process_role='all', distributed_preview_read_only=False))
    monkeypatch.setattr(assembly, '_boot', lambda *_a: None)
    monkeypatch.setattr(brain, 'ensure_project_brain_cutover', lambda: None)
    monkeypatch.setattr(marker, 'report_and_arm', lambda: {'verdict': 'clean'})
    calls, sleeps = [], []
    monkeypatch.setattr(assembly.time, 'sleep', sleeps.append)
    error = (StorageError('database_protocol_error', 'invalid recovery cursor')
             if kind == 'protocol' else RuntimeError('invalid recovery cursor'))

    def fail(**_kwargs):
        calls.append('recovery')
        raise error

    monkeypatch.setattr(manager, 'recover_stale_tasks_on_startup', fail)
    monkeypatch.setattr(manager, 'record_boot_interrupted_conversations',
                        lambda *_a: pytest.fail('must not publish failed settlement'))
    monkeypatch.setattr(brain, 'recover_active_work_items',
                        lambda: pytest.fail('must not open readiness after failure'))
    with pytest.raises(type(error), match='invalid recovery cursor'):
        assembly._init_database()
    assert calls == ['recovery']
    assert sleeps == []
    assert 'task and turn recovery failed' in caplog.text
    assert 'retryable=False' in caplog.text
