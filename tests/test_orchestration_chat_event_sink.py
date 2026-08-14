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
        '_endpoint_phase': 'planning',
    }
    forwarded = []
    sink = OrchestrationChatTaskEventSink(
        task, lambda owner, event: forwarded.append((owner, event)))

    start = {
        'type': 'endpoint_iteration',
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
        'type': 'endpoint_planner_done',
        'content': 'final answer',
        'thinking': 'final reason',
    })

    assert task['_endpoint_phase'] == 'working'
    assert task['_endpoint_iteration'] == 2
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
            'type': 'endpoint_planner_done',
            'content': 'final answer',
            'thinking': 'final reason',
        },
    ]
    assert all(owner is task for owner, _event in forwarded)


def test_discard_unknown_events_and_lockless_final_content_are_safe():
    task = {'content': 'partial', 'thinking': 'private'}
    forwarded = []
    sink = OrchestrationChatTaskEventSink(
        task, lambda _owner, event: forwarded.append(event))

    sink({'type': 'endpoint_critic_msg', 'discard': True})
    sink({'type': 'future_event', 'payload': 'kept'})
    sink.replace_content('deliverable')

    assert task['content'] == 'deliverable'
    assert task['thinking'] == ''
    assert forwarded[-1] == {'type': 'future_event', 'payload': 'kept'}


def test_endpoint_runner_only_assembles_the_chat_event_sink_port():
    runner = (ROOT / 'lib' / 'orchestration_endpoint_runner.py').read_text()
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
    assert "ev.get('type') == 'endpoint_iteration'" not in runner + runtime
    assert 'class _NullLock' not in runner + runtime
