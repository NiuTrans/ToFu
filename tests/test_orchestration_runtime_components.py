"""Focused ownership for runtime outcomes and event fan-out."""

from __future__ import annotations

import pytest

from lib.orchestration.durable_projection import DurableProjectionError
from lib.orchestration.human_gate_runtime import (
    HumanGateRequestPorts,
    OrchestrationHumanGateRuntime,
)
from lib.orchestration.runtime_event_sink import FlowEventSink
from lib.orchestration.runtime_outcome import (
    FlowRunOutcome,
    aborted_race_outcome,
    failure_outcome,
)
pytestmark = pytest.mark.unit


def test_outcome_helpers_preserve_executor_and_terminal_semantics():
    executor = object()
    failed = failure_outcome(
        RuntimeError('worker failed'), 'exception', executor=executor)
    assert failed.executor is executor
    assert failed.lifecycle_status == 'error'
    assert failed.failure_kind == 'exception'
    assert failed.error_envelope is not None
    assert failed.error_envelope['kind'] == 'generic'
    assert failed.error_envelope['detail'] == 'RuntimeError: worker failed'
    assert failed.error_envelope['outcome']['stop_reason'] == 'exception'

    aborted = aborted_race_outcome(FlowRunOutcome(
        {'ok': True, 'status': 'completed', 'final': 'late'},
        executor=executor,
    ))
    assert aborted.executor is executor
    assert aborted.lifecycle_status == 'aborted'
    assert aborted.failure_kind == 'aborted'


def test_event_sink_can_opt_into_delta_persistence_without_two_paths():
    live: list[dict] = []
    durable: list[tuple[int, dict, str]] = []
    sink = FlowEventSink(
        lambda event: live.append(event) or len(live),
        durable_project=lambda seq, event, status: durable.append(
            (seq, event, status)),
        persist_deltas=True,
    )
    delta = {'type': 'step_delta', 'chunk': 'x'}
    sink(delta)
    assert live == [delta]
    assert durable == [(1, delta, '')]


def test_event_sink_rejects_unsequenced_durable_fact():
    sink = FlowEventSink(
        lambda _event: None,
        durable_project=lambda _seq, _event, _status: None,
    )
    with pytest.raises(DurableProjectionError, match='assign a sequence'):
        sink({'type': 'flow_start'})


def test_cancelled_human_input_closes_live_and_durable_gate_lifecycle():
    live: list[dict] = []
    durable: list[tuple[int, dict, str]] = []
    sink = FlowEventSink(
        lambda event: live.append(event) or len(live),
        durable_project=lambda seq, event, status: durable.append(
            (seq, event, status)),
    )
    runtime = OrchestrationHumanGateRuntime(
        emit=sink,
        abort_check=lambda: True,
        ports=HumanGateRequestPorts(
            request_guidance=lambda _request_id, _task, _owner: None,
        ),
        request_scope='durable-cancel',
        owner_user_id=1,
    )

    result = runtime.execute({
        'id': 'human', 'params': {'mode': 'input'},
    }, 'before')

    assert result.aborted is True
    assert [event['type'] for event in live] == [
        'human_request', 'human_resolved',
    ]
    assert [event['type'] for _, event, _ in durable] == [
        'human_request', 'human_resolved',
    ]
    assert [seq for seq, _, _ in durable] == [1, 2]
    assert durable[-1][1]['resolution'] == 'cancelled'
    assert [status for _, _, status in durable] == ['paused', 'running']


def test_event_sink_resumes_only_after_the_last_parallel_gate_closes():
    statuses: list[str] = []
    sink = FlowEventSink(
        lambda _event: 1,
        durable_project=lambda _seq, _event, status: statuses.append(status),
    )

    sink({'type': 'flow_start'})
    sink({'type': 'human_request', 'request_id': 'gate-a'})
    sink({'type': 'human_request', 'request_id': 'gate-b'})
    sink({'type': 'human_resolved', 'request_id': 'gate-a'})
    assert [status for status in statuses if status] == ['running', 'paused']
    sink({'type': 'human_resolved', 'request_id': 'gate-b'})
    sink({'type': 'human_resolved', 'request_id': 'gate-b'})

    assert [status for status in statuses if status] == [
        'running', 'paused', 'running',
    ]
