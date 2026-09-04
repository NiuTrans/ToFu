"""Live task-state projection for Flow-backed chat events."""

from pathlib import Path

import pytest

from lib.orchestration_chat_event_sink import OrchestrationChatTaskEventSink


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


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
    runner = (ROOT / 'lib' / 'orchestration_chat_flow_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_chat_flow_runtime.py').read_text()
    completion = (
        ROOT / 'lib' / 'orchestration_chat_completion.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'OrchestrationChatTaskEventSink(' in runtime
    assert 'task, ports.append_event' in runtime
    assert 'on_stream=task_event_sink' in runtime
    assert 'task_event_sink=task_event_sink' in runtime
    assert 'append_event=ports.append_event' in runtime
    assert 'self._task_event_sink.replace_content(' in completion
    assert "ev.get('type') == 'flow_iteration'" not in runner + runtime
    assert 'class _NullLock' not in runner + runtime
