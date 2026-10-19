"""Startup evidence is content-free and does not alter task authority."""
import json
import threading
from unittest.mock import Mock

import pytest

from lib import observability
from lib.tasks_pkg import spawn

pytestmark = pytest.mark.unit


def test_startup_anchors_pair_identity_and_use_monotonic_duration(monkeypatch):
    records = []
    monkeypatch.setattr(observability.logger, 'info', lambda fmt, value: records.append(json.loads(value)))
    clock = iter([10.0, 10.125])
    wall = iter([100.0, 90.0])  # Wall-clock adjustment cannot change duration.
    monkeypatch.setattr(observability.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(observability.time, 'time', lambda: next(wall))
    observe = Mock()
    monkeypatch.setattr(observability._STORE, 'observe', observe)
    with observability.measure_task_startup('workspace_binding', identity={
            'task_id': 'task', 'conversation_id': 'conv', 'attempt_id': 'attempt',
            'turn_id': 'turn', 'user_id': 7, 'config': 'private-body',
            'trace_id': 'not-admitted'}):
        assert len(records) == 1
        assert records[0]['boundary'] == 'start'
    assert records[1]['durationMs'] == 125.0
    assert records[1]['outcome'] == 'success'
    assert records[1]['observedAtMs'] == 90000
    assert records[0]['attempt_id'] == records[1]['attempt_id'] == 'attempt'
    assert 'private-body' not in json.dumps(records)
    assert 'trace_id' not in records[0]
    assert observe.call_args.kwargs == {'stage': 'workspace_binding', 'outcome': 'success'}


def test_timing_sink_failure_preserves_base_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('sink unavailable')
    monkeypatch.setattr(observability.logger, 'info', fail)
    monkeypatch.setattr(observability._STORE, 'observe', fail)
    failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with observability.measure_task_startup('context', identity={'task_id': 'task'}):
            raise failure
    assert caught.value is failure


def test_slow_workspace_admission_has_phase_before_binding_and_still_fails_closed(monkeypatch):
    order = []
    task = {'id': 'startup-task', 'convId': 'startup-conv', 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: order.append(('phase', fields['detailKey'])))
    failure = RuntimeError('workspace admission refused')
    def bind(task):
        assert task['status'] == 'running'
        assert order == [('phase', 'stream.phase.workerStarting')]
        order.append(('binding', 'entered'))
        raise failure
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace', bind)
    started = threading.Event()
    with pytest.raises(RuntimeError) as caught:
        spawn._mark_worker_started(task, started)
    assert caught.value is failure
    assert not started.is_set()  # Never grant the runner access before binding.
    assert order[-1] == ('binding', 'entered')


def test_workspace_phase_retires_only_after_binding_succeeds(monkeypatch):
    phases = []
    task = {'id': 'workspace-ready', 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append(fields['detailKey']))
    started = threading.Event()

    def bind(task):
        assert phases == ['stream.phase.workerStarting']

        from lib.tasks_pkg.workspace_delivery import _emit_workspace_preparing
        _emit_workspace_preparing(task)
        _emit_workspace_preparing(task)  # One notice even for multiple roots.
        assert not started.is_set()

    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace', bind)
    monkeypatch.setattr('lib.conversations.project_brain.note_isolated_workspace_signal',
                        lambda *args: None)
    assert spawn._mark_worker_started(task, started)
    assert started.is_set()
    assert phases == ['stream.phase.workerStarting', 'stream.phase.workspacePreparing',
                      'stream.phase.workerStarting']


def test_no_workspace_keeps_ordinary_worker_start_phase(monkeypatch):
    phases = []
    task = {'id': 'no-workspace', 'status': 'pending', 'config': {}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append(fields['detailKey']))
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace',
                        Mock(side_effect=AssertionError('No workspace should be bound')))
    assert spawn._mark_worker_started(task)
    assert phases == ['stream.phase.workerStarting']
