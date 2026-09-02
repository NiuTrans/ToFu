"""Unified transient/durable orchestration start facade coverage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.orchestration.runtime_start_service import (
    OrchestrationRuntimeStartService,
    RuntimeStartError,
)
import lib.orchestration.runtime_start_service as start_module


pytestmark = pytest.mark.unit


def _definition() -> dict:
    return {
        'schema': 'tofu.orchestration/v1',
        'name': 'Shared start',
        'nodes': [],
        'edges': [],
    }


class _Definitions:
    def get_definition(self, orchestration_id):
        return {'id': orchestration_id}


class _Runs:
    def __init__(self, run_id='run-1'):
        self.run_id = run_id
        self.created = []
        self.transitions = []

    def create_new(self, **values):
        self.created.append(values)
        return self.run_id

    def transition_status(self, run_id, status, **values):
        self.transitions.append((run_id, status, values))
        return SimpleNamespace(ok=True, reason='accepted', run_status=status)


class _Runtime:
    def __init__(self):
        self.finished = []

    def finish(self, run_id, **values):
        self.finished.append((run_id, values))
        return True


def test_transient_and_durable_starts_share_worker_wiring(monkeypatch):
    runtime = _Runtime()
    runs = _Runs()
    spawns = []

    def fake_spawn(actual_runtime, definition, **options):
        spawns.append((actual_runtime, definition, options))
        return options.get('task_id') or 'live-1'

    monkeypatch.setattr(start_module, 'spawn_runtime_flow', fake_spawn)
    service = OrchestrationRuntimeStartService(
        runtime,
        definition_service=_Definitions,
        run_service=lambda: runs,
    )

    live_id = service.start(
        'ephemeral', _definition(), owner_user_id=41,
        input_text='live input')
    durable_id = service.start(
        'durable', _definition(),
        owner_user_id=41,
        input_text='durable input',
        orchestration_id='flow-1',
        created_by='key-1',
    )

    assert live_id == 'live-1'
    assert durable_id == 'run-1'
    assert runs.created == [{
        'definition': _definition(),
        'input_text': 'durable input',
        'orch_id': 'flow-1',
        'name': 'Shared start',
        'created_by': 'key-1',
    }]
    assert [call[2]['initial_context'] for call in spawns] == [
        'live input', 'durable input',
    ]
    assert spawns[0][2]['meta'] == {'name': 'Shared start'}
    assert spawns[1][2]['meta'] == {
        'name': 'Shared start', 'run_id': 'run-1',
    }
    assert spawns[1][2]['task_id'] == 'run-1'
    assert spawns[1][2]['durable_runs'] is runs
    assert [call[2]['owner_user_id'] for call in spawns] == [41, 41]
    resolver = spawns[0][2]['subflow_resolver_provider']()
    assert resolver('child-1') == {'id': 'child-1'}


def test_start_rejects_unknown_delivery_kind_before_runtime_work(monkeypatch):
    spawns = []
    monkeypatch.setattr(
        start_module, 'spawn_runtime_flow',
        lambda *args, **kwargs: spawns.append((args, kwargs)),
    )
    service = OrchestrationRuntimeStartService(
        _Runtime(), definition_service=_Definitions)

    with pytest.raises(RuntimeStartError, match='start kind'):
        service.start('future-mode', _definition(), owner_user_id=41)

    assert spawns == []


def test_empty_durable_id_fails_before_runtime_visibility(monkeypatch):
    spawns = []
    monkeypatch.setattr(
        start_module, 'spawn_runtime_flow',
        lambda *args, **kwargs: spawns.append((args, kwargs)),
    )
    service = OrchestrationRuntimeStartService(
        _Runtime(),
        definition_service=_Definitions,
        run_service=lambda: _Runs(run_id=''),
    )

    with pytest.raises(RuntimeStartError, match='create durable'):
        service.start('durable', _definition(), owner_user_id=41)

    assert spawns == []


def test_spawn_failure_closes_runtime_and_durable_projections(monkeypatch):
    runtime = _Runtime()
    runs = _Runs()

    def fail_spawn(*_args, **_kwargs):
        raise OSError('thread unavailable')

    monkeypatch.setattr(start_module, 'spawn_runtime_flow', fail_spawn)
    service = OrchestrationRuntimeStartService(
        runtime,
        definition_service=_Definitions,
        run_service=lambda: runs,
    )

    with pytest.raises(RuntimeStartError) as caught:
        service.start('durable', _definition(), owner_user_id=41)

    assert caught.value.run_id == 'run-1'
    assert isinstance(caught.value.__cause__, OSError)
    assert runtime.finished[0][0] == 'run-1'
    assert runtime.finished[0][1]['error_context'] == 'orchestration:start'
    assert len(runs.transitions) == 1
    run_id, status, values = runs.transitions[0]
    assert (run_id, status, values['final']) == ('run-1', 'error', '')
    error = values['error']
    assert error['kind'] == 'internal'
    assert error['severity'] == 'error'
    assert error['retryable'] is False
    assert error['message'] == 'Runtime worker could not be started'
    assert error['detail'] == 'thread unavailable'
    assert error['context'] == 'durable start failure'
    assert error['source'] == 'orchestration:runtime-start'


def test_cleanup_failure_does_not_mask_primary_spawn_failure(monkeypatch):
    runtime = _Runtime()
    runs = _Runs()
    runs.transition_status = lambda *_args, **_values: (
        (_ for _ in ()).throw(OSError('cleanup database offline')))
    monkeypatch.setattr(
        start_module,
        'spawn_runtime_flow',
        lambda *_args, **_values: (_ for _ in ()).throw(
            RuntimeError('primary spawn failure')),
    )
    service = OrchestrationRuntimeStartService(
        runtime,
        definition_service=_Definitions,
        run_service=lambda: runs,
    )

    with pytest.raises(RuntimeStartError) as caught:
        service.start('durable', _definition(), owner_user_id=41)

    assert caught.value.run_id == 'run-1'
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == 'primary spawn failure'
    assert runtime.finished[0][0] == 'run-1'


def test_transient_spawn_failure_uses_the_same_service_error(monkeypatch):
    monkeypatch.setattr(
        start_module,
        'spawn_runtime_flow',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('runtime unavailable')),
    )
    service = OrchestrationRuntimeStartService(
        _Runtime(), definition_service=_Definitions)

    with pytest.raises(RuntimeStartError) as caught:
        service.start('ephemeral', _definition(), owner_user_id=41)

    assert isinstance(caught.value.__cause__, RuntimeError)


def test_start_and_worker_dependencies_use_shared_capability_ports():
    start_source = open(
        'lib/orchestration/runtime_start_service.py', encoding='utf-8',
    ).read()
    recovery_source = open(
        'lib/orchestration/runtime_start_recovery.py', encoding='utf-8',
    ).read()
    worker_source = open(
        'lib/orchestration/runtime_service.py', encoding='utf-8',
    ).read()
    ports_source = open(
        'lib/orchestration/runtime_ports.py', encoding='utf-8',
    ).read()

    assert 'runtime: OrchestrationTaskRuntimePort' in start_source
    assert 'definition_service: OrchestrationDefinitionProvider' in \
        start_source
    assert 'run_service: OrchestrationDurableRunProvider | None' in \
        start_source
    assert 'runs: OrchestrationDurableRunPort' in recovery_source
    assert 'durable_runs: OrchestrationDurableRunPort | None' in worker_source
    assert 'DurableRunProjection(runs, run_id).record_error(' \
        in recovery_source
    assert 'recover_failed_durable_start(' in start_source
    assert 'DurableRunProjection(durable_runs, durable_run_id)' in worker_source
    assert 'class OrchestrationTaskRuntimePort(Protocol)' in ports_source
    assert 'class OrchestrationDurableRunPort(Protocol)' in ports_source
    assert 'Callable[[], Any]' not in start_source
