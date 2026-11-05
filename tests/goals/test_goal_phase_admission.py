"""Goal phase authority must precede buffer reset and successful projection."""
from copy import deepcopy
import threading

import pytest

from lib.orchestration.chat_flow_adapter import FlowEventAdapter
from lib.orchestration.chat_event_sink import OrchestrationChatTaskEventSink
from lib.orchestration.graph import FlowEventDeliveryError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('role', ['planner', 'worker', 'virtual_user'])
@pytest.mark.parametrize('status,finish', [
    ('cancelled', 'aborted'), ('aborted', 'aborted'),
    ('failed', 'error'), ('stalled', 'incomplete'),
])
def test_execution_failure_overrides_provider_stop_without_rebuilding_history(role, status, finish):
    canonical = {'content': 'partial history', 'thinking': 'reasoning',
                 'finishReason': 'stop', 'segments': [{'type': 'text', 'text': 'history'}],
                 'toolRounds': [{'toolCallId': 'call-one'}]}
    before = deepcopy(canonical)
    adapter = FlowEventAdapter(projection='autopilot')
    adapter.on_event({'type': 'step_complete', 'role': role, 'status': status,
                      'outputProjection': canonical})
    message = adapter.messages[0]
    assert message['finishReason'] == finish
    assert message['segments'] == canonical['segments']
    assert message['toolRounds'] == canonical['toolRounds']
    assert canonical == before


@pytest.mark.parametrize('admitted', [False, True])
def test_writable_turn_admission_precedes_clearing_previous_live_output(monkeypatch, admitted):
    task = {'content': 'previous text', 'thinking': 'previous reasoning',
            'segments': [{'type': 'text', 'text': 'previous text'}],
            'toolRounds': [{'toolCallId': 'previous-call'}],
            '_flowLiveTurn': {'turnId': 'previous', 'attemptId': 'previous-attempt'},
            'abort_event': threading.Event()}
    keys = ('content', 'thinking', 'segments', 'toolRounds', '_flowLiveTurn')
    previous = {key: deepcopy(task[key]) for key in keys}
    events = []
    def admit(owner, event):
        assert {key: owner[key] for key in keys} == previous
        if not admitted:
            raise RuntimeError('injected storage rejection')
        owner['_flowLiveTurn'] = {'turnId': 'next', 'attemptId': 'next-attempt'}
    monkeypatch.setattr('lib.orchestration.chat_live_turn.begin_live_flow_turn', admit)
    sink = OrchestrationChatTaskEventSink(task, lambda owner, event: events.append(event))
    adapter = FlowEventAdapter(on_stream=sink, projection='autopilot')
    start = {'type': 'step_start', 'role': 'worker'}
    if not admitted:
        with pytest.raises(FlowEventDeliveryError):
            adapter.on_event(start)
        with pytest.raises(FlowEventDeliveryError):
            adapter.on_event({'type': 'step_delta', 'chunk': 'must not write'})
        assert {key: task[key] for key in keys} == previous
        assert task['abort_event'].is_set()
        assert events == []
    else:
        adapter.on_event(start)
        assert task['content'] == task['thinking'] == ''
        assert task['toolRounds'] == []
        assert 'segments' not in task
        assert task['_flowLiveTurn']['turnId'] == 'next'
        assert len(events) == 1
