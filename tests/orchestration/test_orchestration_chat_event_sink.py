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


def test_turn_start_and_finalize_share_one_task_projection():
    lock = _TrackingLock()
    task = {'content': 'old content', 'thinking': 'old thinking',
            'content_lock': lock, '_flow_phase': 'planning'}
    forwarded = []
    sink = OrchestrationChatTaskEventSink(
        task, lambda owner, event: forwarded.append((owner, event)))
    start = {'type': 'flow_iteration', 'phase': 'working', 'iteration': 2,
             'flowProjection': 'flow', 'turnRole': 'worker',
             'emits': 'assistant', 'vuMsgId': None}
    sink(start)
    final = {'type': 'flow_planner_done', 'content': 'final answer',
             'thinking': 'final reason'}
    sink(final)
    assert task['_flow_phase'] == 'working'
    assert task['_flow_iteration'] == 2
    assert task['_flow_current_turn'] == {
        'flowProjection': 'flow', 'turnRole': 'worker', 'emits': 'assistant'}
    assert task['content'] == 'final answer'
    assert task['thinking'] == 'final reason'
    assert lock.entries == 2
    assert [event for _owner, event in forwarded] == [start, final]
    assert all(owner is task for owner, _event in forwarded)


def test_relayed_events_never_rebuild_output_without_a_bound_child():
    """Live output has one source: a bound child. Relayed frames only publish."""
    task = {'content': 'kept', 'thinking': '', 'toolRounds': [{'toolCallId': 'kept'}]}
    forwarded = []
    sink = OrchestrationChatTaskEventSink(task, lambda _owner, event: forwarded.append(event))
    for event in ({'type': 'delta', 'content': 'relayed'},
                  {'type': 'tool_start', 'roundNum': 1, 'toolCallId': 'relayed'},
                  {'type': 'delta_reset', 'roundNum': 0}):
        sink(event)
    assert task['content'] == 'kept'
    assert task['toolRounds'] == [{'toolCallId': 'kept'}]
    assert [event['type'] for event in forwarded] == ['delta', 'tool_start', 'delta_reset']


def test_bound_child_output_is_what_every_published_frame_carries():
    """The parent renders the running child's own state, as a normal Turn does."""
    ref = {'taskId': 'root#agent:child', 'roundNum': 1, 'turn': 'request:exact'}
    task = {'content': '', 'thinking': '', 'toolRounds': []}
    seen = []
    sink = OrchestrationChatTaskEventSink(task, lambda owner, event: seen.append(
        (event['type'], owner['content'], [dict(row) for row in owner['toolRounds']],
         owner.get('segments'))))
    row = {'roundNum': 1, 'toolCallId': 'call', 'taskId': 'child', 'status': 'searching',
           'assistantContent': 'Narration', 'inspectorRef': ref}
    child = {'content': 'Narration', 'thinking': 'why', 'toolRounds': []}
    with task['_flowLiveOutputPort'](child) as bound:
        assert bound
        sink({'type': 'delta', 'content': 'Narration'})
        child.update(content='', toolRounds=[row],
                     segments=[{'type': 'text', 'text': 'Narration'}])
        sink({'type': 'phase', 'phase': 'tool_exec'})
        row['status'] = 'done'
        child.update(content='Final', segments=[{'type': 'text', 'text': 'Narration'},
                                                {'type': 'text', 'text': 'Final'}])
    assert [entry[:2] for entry in seen] == [('delta', 'Narration'), ('phase', '')]
    assert seen[1][2] == [{**row, 'status': 'searching'}]
    assert seen[1][3] == [{'type': 'text', 'text': 'Narration'}]
    # Settlement after the last frame crosses once when the child finishes.
    assert task['content'] == 'Final'
    assert task['toolRounds'][0]['status'] == 'done'
    assert task['toolRounds'][0]['inspectorRef'] == ref
    assert task['toolRounds'][0] is not row  # never aliases the child's live rows
    sink({'type': 'delta', 'content': 'after'})
    assert task['content'] == 'Final'


def test_concurrent_sibling_is_refused_the_visible_output():
    task = {'content': '', 'thinking': '', 'toolRounds': []}
    sink = OrchestrationChatTaskEventSink(task, lambda *_args: None)
    first, sibling = {'content': 'first'}, {'content': 'sibling'}
    with task['_flowLiveOutputPort'](first) as bound:
        with task['_flowLiveOutputPort'](sibling) as sibling_bound:
            assert bound and not sibling_bound
            sink({'type': 'delta', 'content': 'x'})
        assert task['content'] == 'first'
    with task['_flowLiveOutputPort'](sibling) as bound:
        assert bound


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
