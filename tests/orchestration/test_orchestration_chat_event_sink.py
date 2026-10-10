"""Live task-state projection for Flow-backed chat events."""

from pathlib import Path

import pytest

from lib.orchestration.chat_event_sink import OrchestrationChatTaskEventSink


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_failed_phase_admission_stops_dispatch_and_never_falls_back_to_root(monkeypatch):
    import threading
    from lib.orchestration import chat_live_turn as orchestration_chat_live_turn
    from lib.orchestration.graph import FlowEventDeliveryError
    def fail(*args):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(orchestration_chat_live_turn, 'begin_live_flow_turn', fail)
    task = {'abort_event': threading.Event(), '_flow_phase': 'working',
            '_flow_iteration': 1, '_flow_current_turn': {'turnRole': 'worker'},
            'content': 'Previous phase', 'thinking': 'Previous reasoning',
            'toolRounds': [{'toolCallId': 'previous-call'}],
            'segments': [{'type': 'text', 'text': 'Previous phase'}]}
    from copy import deepcopy
    before = deepcopy({key: value for key, value in task.items() if key != 'abort_event'})
    forwarded = []
    sink = OrchestrationChatTaskEventSink(task, lambda *args: forwarded.append(args))
    with pytest.raises(FlowEventDeliveryError, match='storage unavailable'):
        sink({'type': 'flow_iteration', 'flowProjection': 'autopilot',
              'phase': 'reviewing', 'iteration': 2, 'turnRole': 'virtual_user'})
    assert task['abort_event'].is_set()
    assert {key: task[key] for key in before} == before
    with pytest.raises(RuntimeError, match='no writable Turn'):
        sink({'type': 'delta', 'content': 'late'})
    assert forwarded == []
    from lib.orchestration._builtin_definitions import build_autopilot_definition
    from lib.orchestration.chat_flow_adapter import FlowEventAdapter
    from lib.orchestration.engine import FlowExecutor
    task['abort_event'].clear()
    task.pop('_flowTurnStartFailed')
    adapter = FlowEventAdapter(on_stream=sink, projection='autopilot')
    calls = []
    outcome = FlowExecutor(
        build_autopilot_definition(), on_event=adapter.on_event,
        agent_runner=lambda *args: calls.append(args),
    ).run(initial_context='Keep the original goal')
    assert outcome['status'] == 'failed'
    assert 'storage unavailable' in outcome['error']
    assert calls == []


class _TrackingLock:
    def __init__(self):
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        return self

    def __exit__(self, *_args):
        return False


def test_turn_start_delta_and_finalize_share_one_task_projection():
    lock = _TrackingLock()
    task = {
        'content': 'old content',
        'thinking': 'old thinking',
        'content_lock': lock,
        '_flow_phase': 'planning',
    }
    forwarded = []
    sink = OrchestrationChatTaskEventSink(
        task, lambda owner, event: forwarded.append((owner, event)))

    start = {
        'type': 'flow_iteration',
        'phase': 'working',
        'iteration': 2,
        'flowProjection': 'flow',
        'turnRole': 'worker',
        'emits': 'assistant',
        'vuMsgId': None,
    }
    sink(start)
    sink({'type': 'delta', 'content': 'answer ', 'thinking': 'reason '})
    sink({'type': 'delta', 'content': 42})
    sink({
        'type': 'flow_planner_done',
        'content': 'final answer',
        'thinking': 'final reason',
    })

    assert task['_flow_phase'] == 'working'
    assert task['_flow_iteration'] == 2
    assert task['_flow_current_turn'] == {
        'flowProjection': 'flow',
        'turnRole': 'worker',
        'emits': 'assistant',
    }
    assert task['content'] == 'final answer'
    assert task['thinking'] == 'final reason'
    assert lock.entries == 4
    assert [event for owner, event in forwarded] == [
        start,
        {'type': 'delta', 'content': 'answer ', 'thinking': 'reason '},
        {'type': 'delta', 'content': 42},
        {
            'type': 'flow_planner_done',
            'content': 'final answer',
            'thinking': 'final reason',
        },
    ]
    assert all(owner is task for owner, _event in forwarded)


def test_tool_lifecycle_updates_reconnect_snapshot_without_duplicate_rows():
    task = {'content': '', 'thinking': '', 'toolRounds': []}
    forwarded = []
    sink = OrchestrationChatTaskEventSink(
        task, lambda _owner, event: forwarded.append(event))
    sink({
        'type': 'flow_iteration', 'phase': 'working', 'iteration': 1,
        'flowProjection': 'autopilot', 'turnRole': 'worker',
        'emits': 'assistant',
    })
    start = {
        'type': 'tool_start', 'roundNum': 3, 'llmRound': 3,
        'toolCallId': 'flow-tool-occurrence', 'toolName': 'read_files',
        'query': 'Read a.py', 'toolArgs': {'path': 'a.py'},
        'status': 'searching', 'tStart': 100,
    }
    sink(start)
    sink(start)  # reconnect replay / duplicate start is an upsert
    sink({
        'type': 'tool_result', 'roundNum': 3,
        'toolCallId': 'flow-tool-occurrence', 'toolName': 'read_files',
        'results': [{'title': 'read_files'}], 'status': 'done', 'tEnd': 150,
    })
    sink({
        'type': 'tool_complete', 'roundNum': 3,
        'toolCallId': 'flow-tool-occurrence', 'toolName': 'read_files',
        'toolContent': 'ok', 'isError': False, 'tEnd': 151,
    })

    assert task['toolRounds'] == [{
        'roundNum': 3, 'llmRound': 3,
        'toolCallId': 'flow-tool-occurrence', 'toolName': 'read_files',
        'toolArgs': {'path': 'a.py'}, 'query': 'Read a.py',
        'status': 'done', 'tStart': 100,
        'results': [{'title': 'read_files'}], 'tEnd': 151,
        'toolContent': 'ok',
    }]
    assert [event['type'] for event in forwarded] == [
        'flow_iteration', 'tool_start', 'tool_start',
        'tool_result', 'tool_complete']


def test_tool_start_carries_swarm_agent_identity_into_snapshot_row():
    """The reconnect/poll snapshot row must keep ``agentId`` — the frontend
    debug entry re-derives the ``{parent}#agent:{agentId}`` Request
    Inspector stream from the projected round."""
    task = {'content': '', 'thinking': '', 'toolRounds': []}
    sink = OrchestrationChatTaskEventSink(task, lambda _owner, _event: None)
    sink({
        'type': 'tool_start', 'roundNum': 2, 'llmRound': 2,
        'agentId': 'agent-9', 'toolCallId': 'flow-tool-x',
        'toolName': 'run_command', 'query': 'pytest -x', 'tStart': 100,
    })
    assert task['toolRounds'][0]['agentId'] == 'agent-9'


@pytest.mark.parametrize('late_ref', [False, True])
def test_exact_inspector_reference_survives_live_tool_and_state_handoff(late_ref):
    from copy import deepcopy
    ref = {'taskId': 'root#agent:child', 'roundNum': 1, 'turn': 'request:exact'}
    task, events = {}, []
    sink = OrchestrationChatTaskEventSink(task, lambda owner, event: events.append(deepcopy(event)))
    start = {'type': 'tool_start', 'roundNum': 1, 'llmRound': 0,
             'toolCallId': 'same-provider-id', 'taskId': 'child',
             'attemptId': 'child-attempt', 'toolName': 'read_files', 'query': 'Read'}
    if not late_ref:
        start['inspectorRef'] = ref
    sink(start)
    sink({'type': 'tool_complete', 'roundNum': 1, 'toolCallId': 'same-provider-id',
          'toolContent': 'Full evidence', 'inspectorRef': ref})
    assert task['toolRounds'][0]['inspectorRef'] == ref
    assert task['toolRounds'][0]['taskId'] == 'child'
    assert task['toolRounds'][0]['attemptId'] == 'child-attempt'
    assert events[-1]['inspectorRef'] == ref
    rounds = deepcopy(task['toolRounds'])
    sink({'type': 'state', 'toolRounds': rounds, 'content': 'Final output',
          'thinking': '', 'segments': [{'type': 'tool', 'roundNum': 1, 'inspectorRef': ref}]})
    assert task['toolRounds'][0]['inspectorRef'] == ref
    assert task['segments'][0]['inspectorRef'] == ref
    assert [event['type'] for event in events] == ['tool_start', 'tool_complete']


def test_discard_unknown_events_and_lockless_final_content_are_safe():
    task = {'content': 'partial', 'thinking': 'private'}
    forwarded = []
    sink = OrchestrationChatTaskEventSink(
        task, lambda _owner, event: forwarded.append(event))

    sink({'type': 'flow_critic_msg', 'discard': True})
    sink({'type': 'future_event', 'payload': 'kept'})
    sink.replace_content('deliverable')

    assert task['content'] == 'deliverable'
    assert task['thinking'] == ''
    assert forwarded[-1] == {'type': 'future_event', 'payload': 'kept'}


def test_flow_runner_only_assembles_the_chat_event_sink_port():
    runner = (ROOT / 'lib' / 'orchestration' / 'chat_flow_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration' / 'chat_flow_runtime.py').read_text()
    completion = (
        ROOT / 'lib' / 'orchestration' / 'chat_completion.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'OrchestrationChatTaskEventSink(' in runtime
    assert 'task, ports.append_event' in runtime
    assert 'on_stream=task_event_sink' in runtime
    assert 'task_event_sink=task_event_sink' in runtime
    assert 'append_event=ports.append_event' in runtime
    assert 'self._task_event_sink.replace_content(' in completion
    assert "ev.get('type') == 'flow_iteration'" not in runner + runtime
    assert 'class _NullLock' not in runner + runtime
