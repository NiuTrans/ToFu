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


def test_relayed_tool_start_without_status_is_a_running_round():
    """A foreign tool_start admitted into the parent's durable round list must
    carry the canonical pending verdict. Without it, every Turn projection
    fold mirrored the round as ``result: {content: None, status: None}`` — a
    shape the generated snapshot contract rejects (conv mu5p3vn9x32wcd)."""
    task = {'content': '', 'thinking': '', 'toolRounds': []}
    events = []
    sink = OrchestrationChatTaskEventSink(task, lambda owner, event: events.append(event))
    sink({'type': 'tool_start', 'roundNum': 1, 'toolCallId': 'call-1',
          'toolName': 'load_skill', 'toolArgs': '{}', 'query': 'load_skill'})
    assert task['toolRounds'][0]['status'] == 'searching'
    sink({'type': 'tool_complete', 'roundNum': 1, 'toolCallId': 'call-1',
          'toolName': 'load_skill', 'toolContent': 'FULL'})
    assert task['toolRounds'][0]['status'] == 'done'
    assert task['toolRounds'][0]['toolContent'] == 'FULL'
    # An explicit producer verdict always wins over the pending default.
    sink({'type': 'tool_start', 'roundNum': 2, 'toolCallId': 'call-2',
          'toolName': 'run_command', 'toolArgs': '{}', 'status': 'executing'})
    assert task['toolRounds'][1]['status'] == 'executing'
