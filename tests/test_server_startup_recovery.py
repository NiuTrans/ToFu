"""The sidecar-ready startup sequence restores every process-local owner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


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
        'retire_interrupted_orchestration_runs',
        lambda *, error: calls.append(
            ('orchestration-recovery', error['kind'])) or 2,
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

    assert calls == [
        'brain-cutover',
        'shutdown-marker',
        ('task-recovery', previous_shutdown),
        'brain-recovery',
        'attempt-cleanup',
        ('orchestration-recovery', 'worker_lost'),
        'swarm-rehydrate',
        'swarm-output-cleanup',
        'log-aggregate-flusher',
    ]



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
