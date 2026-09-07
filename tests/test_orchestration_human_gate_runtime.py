"""Focused tests for the execution-side human-gate request ports."""

from __future__ import annotations

from pathlib import Path
import inspect
import threading
import time

import pytest

from lib.orchestration.human_gate_runtime import (
    HumanGateRequestIdentity,
    HumanGateRequestPorts,
    OrchestrationHumanGateRuntime,
)
import lib.orchestration.human_gate_request_identity as human_gate_request_identity
import lib.orchestration.human_gate_runtime_ports as human_gate_runtime_ports
import lib.orchestration.human_gate_runtime_result as human_gate_runtime_result
from lib.orchestration_engine import FlowExecutor


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = 82


def _runtime(*, approval=True, guidance='answer', aborted=False):
    events = []
    calls = []

    def request_approval(request_id, timeout, owner_user_id):
        calls.append(('approval', request_id, timeout, owner_user_id))
        return approval

    def request_guidance(request_id, task, owner_user_id):
        calls.append((
            'guidance', request_id, task.get('id'), task.get('aborted'),
            owner_user_id,
        ))
        return guidance

    runtime = OrchestrationHumanGateRuntime(
        emit=events.append,
        abort_check=lambda: aborted,
        ports=HumanGateRequestPorts(
            request_approval=request_approval,
            request_guidance=request_guidance,
        ),
        request_scope='test-run',
        owner_user_id=OWNER,
    )
    return runtime, events, calls


def test_notify_emits_without_calling_a_blocking_port():
    runtime, events, calls = _runtime()
    result = runtime.execute({
        'id': 'h', 'name': 'Review',
        'params': {'mode': 'notify', 'prompt': 'Heads up'},
    }, 'before')

    assert result.context == 'before'
    assert result.aborted is False
    assert calls == []
    assert events == [{
        'type': 'human_notify', 'node_id': 'h', 'name': 'Review',
        'prompt': 'Heads up',
    }]


@pytest.mark.parametrize(('approved', 'aborted'), [(True, False), (False, True)])
def test_approval_uses_canonical_timeout_and_projects_resolution(approved, aborted):
    runtime, events, calls = _runtime(approval=approved)
    result = runtime.execute({
        'id': 'h', 'params': {'mode': 'approve', 'timeout_sec': '45'},
    }, 'before')

    assert result.aborted is aborted
    assert calls == [('approval', 'orch_test-run_h_1', 45, OWNER)]
    assert [event['type'] for event in events] == [
        'human_request', 'human_resolved',
    ]
    assert events[-1]['approved'] is approved
    assert events[-1]['resolution'] == (
        'approved' if approved else 'rejected')


def test_invalid_approval_timeout_uses_backend_default():
    runtime, _events, calls = _runtime()
    runtime.execute({
        'id': 'h', 'params': {'mode': 'approve', 'timeout_sec': 'invalid'},
    }, '')
    assert calls == [('approval', 'orch_test-run_h_1', 300, OWNER)]


def test_missing_mode_uses_the_shared_control_default():
    runtime, events, calls = _runtime()
    result = runtime.execute({'id': 'h', 'params': {}}, 'before')

    assert result.aborted is False
    assert calls == [('approval', 'orch_test-run_h_1', 300, OWNER)]
    assert events[0]['mode'] == 'approve'


def test_input_port_receives_abort_aware_task_and_appends_answer():
    runtime, events, calls = _runtime(guidance='USE PLAN B', aborted=True)
    result = runtime.execute({
        'id': 'h', 'name': 'Owner', 'params': {'mode': 'input'},
    }, 'before')

    assert result.aborted is False
    assert result.context == 'before\n\n[Human input — Owner]\nUSE PLAN B'
    assert calls == [(
        'guidance', 'orch_test-run_h_1', 'orch_test-run_h_1', True, OWNER)]
    assert events[-1]['preview'] == 'USE PLAN B'
    assert events[-1]['resolution'] == 'answered'


def test_cancelled_input_returns_abort_with_explicit_resolution_event():
    runtime, events, _calls = _runtime(guidance=None)
    result = runtime.execute({
        'id': 'h', 'params': {'mode': 'input'},
    }, 'before')

    assert result.aborted is True
    assert result.context == 'before'
    assert [event['type'] for event in events] == [
        'human_request', 'human_resolved',
    ]
    assert events[-1]['resolution'] == 'cancelled'
    assert events[-1]['request_id'] == events[0]['request_id']


def test_engine_consumes_runtime_port_without_task_registry_imports():
    engine = (ROOT / 'lib/orchestration_engine.py').read_text()
    runtime = (ROOT / 'lib/orchestration/human_gate_runtime.py').read_text()
    events = (ROOT / 'lib/orchestration/human_gate_events.py').read_text()
    ports = (ROOT / 'lib/orchestration/human_gate_runtime_ports.py').read_text()

    assert 'OrchestrationHumanGateRuntime' in engine
    assert 'request_write_approval' not in engine
    assert 'request_human_guidance' not in engine
    assert 'class ApprovalRequester(Protocol)' in ports
    assert 'class GuidanceRequester(Protocol)' in ports
    assert 'class HumanGateRequestIdentity' not in runtime
    assert 'class HumanGateRuntimeResult' not in runtime
    assert 'human_gate_request_event(' in runtime
    assert "'type': 'human_request'" not in runtime
    assert "'type': 'human_resolved'" not in runtime
    assert 'def human_gate_resolved_event(' in events
    assert 'human_gate_runtime_ports import HumanGateRequestPorts' in engine


def test_runtime_facade_preserves_split_owner_identities():
    import lib.orchestration.human_gate_runtime as human_gate_runtime

    assert human_gate_runtime.HumanGateRequestPorts is \
        human_gate_runtime_ports.HumanGateRequestPorts
    assert human_gate_runtime.HumanGateRequestIdentity is \
        human_gate_request_identity.HumanGateRequestIdentity
    assert human_gate_runtime.HumanGateRuntimeResult is \
        human_gate_runtime_result.HumanGateRuntimeResult
    assert 'class HumanGateRequestPorts' not in inspect.getsource(
        human_gate_runtime)


def test_request_identity_is_scoped_and_shared_across_execution_tree():
    identity = HumanGateRequestIdentity('run-one')

    assert identity.next('review') == 'orch_run-one_review_1'
    assert identity.next('review') == 'orch_run-one_review_2'
    assert HumanGateRequestIdentity('run-two').next('review') \
        == 'orch_run-two_review_1'
    assert HumanGateRequestIdentity('run/one').scope != identity.scope


def test_concurrent_identical_gates_resolve_only_their_own_registry_entry():
    from lib.tasks_pkg import approval

    events = {'one': [], 'two': []}
    owners = {'one': 91, 'two': 92}
    results = {}
    runtimes = {
        name: OrchestrationHumanGateRuntime(
            emit=events[name].append,
            abort_check=lambda: False,
            request_scope='run-' + name,
            owner_user_id=owners[name],
        )
        for name in events
    }

    def execute(name):
        results[name] = runtimes[name].execute({
            'id': 'review',
            'params': {'mode': 'approve', 'timeout_sec': 2},
        }, '')

    threads = [threading.Thread(target=execute, args=(name,))
               for name in events]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 1
    request_ids = {}
    while time.monotonic() < deadline:
        request_ids = {
            name: next((event['request_id'] for event in rows
                        if event['type'] == 'human_request'), '')
            for name, rows in events.items()
        }
        registered = all(
            request_id and approval.is_write_approval_pending(
                request_id, owner_user_id=owners[name])
            for name, request_id in request_ids.items()
        )
        if all(request_ids.values()) and registered:
            break
        time.sleep(0.01)

    try:
        assert request_ids == {
            'one': 'orch_run-one_review_1',
            'two': 'orch_run-two_review_1',
        }
        assert not approval.resolve_write_approval(
            request_ids['one'], True, owner_user_id=owners['two'])
        assert approval.resolve_write_approval(
            request_ids['one'], True, owner_user_id=owners['one'])
        assert approval.resolve_write_approval(
            request_ids['two'], False, owner_user_id=owners['two'])
        for thread in threads:
            thread.join(timeout=1)
        assert not any(thread.is_alive() for thread in threads)
        assert results['one'].aborted is False
        assert results['two'].aborted is True
    finally:
        for name, request_id in request_ids.items():
            if request_id:
                approval.resolve_write_approval(
                    request_id, False, owner_user_id=owners[name])


def test_injected_gate_ports_cross_an_isolated_subflow_boundary():
    calls = []
    ports = HumanGateRequestPorts(
        request_approval=lambda request_id, timeout, owner_user_id: (
            calls.append((request_id, timeout, owner_user_id)) or True
        ),
        request_guidance=lambda _request_id, _task, _owner_user_id: None,
    )
    child = {
        'schema': 'tofu.orchestration/v1',
        'name': 'Child',
        'nodes': [
            {'id': 'cs', 'type': 'control', 'kind': 'start'},
            {'id': 'h', 'type': 'control', 'kind': 'human',
             'params': {'mode': 'approve', 'timeout_sec': 17}},
            {'id': 'ce', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'cs', 'to': 'h'},
            {'from': 'h', 'to': 'ce'},
        ],
    }
    parent = {
        'schema': 'tofu.orchestration/v1',
        'name': 'Parent',
        'nodes': [
            {'id': 'ps', 'type': 'control', 'kind': 'start'},
            {'id': 'box', 'type': 'subflow', 'role': 'general',
             'params': {'scope': 'isolated', 'definition': child}},
            {'id': 'pe', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'ps', 'to': 'box'},
            {'from': 'box', 'to': 'pe'},
        ],
    }

    result = FlowExecutor(
        parent,
        agent_runner=lambda _node, _context, _iteration: {},
        human_gate_ports=ports,
        parent_task={'id': 'parent-run'},
        human_gate_owner_user_id=OWNER,
    ).run(initial_context='seed')

    assert result['ok'] is True
    assert calls == [('orch_parent-run_h_1', 17, OWNER)]
