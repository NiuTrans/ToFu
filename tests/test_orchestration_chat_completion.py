"""Canonical completion projection for Flow-backed chat tasks."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.orchestration_chat_completion import OrchestrationChatFlowCompletion
from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Terminal:
    def __init__(
        self,
        *,
        category='success',
        chat_status='done',
        stop_reason='verified_complete',
        finish_reason='stop',
        runtime_error='',
    ):
        self.category = category
        self.chat_status = chat_status
        self.stop_reason = stop_reason
        self.finish_reason = finish_reason
        self.runtime_error = runtime_error

    @property
    def error_envelope(self):
        if not self.runtime_error:
            return None
        return {
            'kind': 'generic',
            'message': 'Orchestration flow execution failed',
            'detail': self.runtime_error,
            'outcome': self.as_dict(),
        }

    def as_dict(self):
        return {
            'format': 'tofu.orchestration.outcome/v1',
            'category': self.category,
            'chat_status': self.chat_status,
            'stop_reason': self.stop_reason,
            'finish_reason': self.finish_reason,
            'error': self.runtime_error,
        }


class _ContentSink:
    def __init__(self):
        self.values = []

    def replace_content(self, value):
        self.values.append(value)


class _TurnPersistence:
    def __init__(self):
        self.calls = 0

    def finalize(self):
        self.calls += 1


def _completion(terminal, *, messages=None, executor=None, task=None):
    task = task or {'id': 'task-one', 'usage': {'total': 3}}
    events = []
    persisted = []
    notified = []
    content = _ContentSink()
    turns = _TurnPersistence()
    outcome = SimpleNamespace(
        result={'agents_run': 2, 'final': 'scratchpad fallback'},
        terminal_outcome=terminal,
        executor=executor,
    )
    completion = OrchestrationChatFlowCompletion(
        task,
        projection='flow',
        outcome=outcome,
        messages=messages or [],
        task_event_sink=content,
        turn_persistence=turns,
        append_event=lambda owner, event: events.append((owner, event)),
        persist_task_result=lambda owner: persisted.append(owner),
        notify_terminal=lambda owner: notified.append(owner),
        stamp_terminal=stamp_chat_task_terminal,
    )
    return completion, task, content, turns, events, persisted, notified


def test_prepare_selects_last_assistant_and_captures_partial_trace_once():
    terminal = _Terminal()
    executor = SimpleNamespace(trace=[{'node_id': 'worker'}])
    messages = [
        {'role': 'assistant', 'content': 'deliverable'},
        {'role': 'user', 'content': 'review verdict'},
    ]
    completion, task, content, turns, _events, _persisted, _notified = _completion(
        terminal, messages=messages, executor=executor)

    assert completion.prepare() is terminal
    assert completion.prepare() is terminal
    assert content.values == ['deliverable']
    assert task['_flow_trace'] == [{'node_id': 'worker'}]
    assert task['_endpoint_turns'] is messages
    assert task['_flow_turns'] is messages
    assert turns.calls == 1


@pytest.mark.parametrize(
    ('terminal', 'expected_extra'),
    [
        (_Terminal(
            category='incomplete',
            stop_reason='max_iterations',
            finish_reason='incomplete',
        ), {'incomplete': True}),
        (_Terminal(
            category='failure',
            chat_status='error',
            stop_reason='node_failed',
            finish_reason='error',
            runtime_error='worker exploded',
        ), {'error_detail': 'worker exploded'}),
    ],
)
def test_finish_projects_honest_terminal_event_and_is_idempotent(
    terminal, expected_extra,
):
    task = {'id': 'task-two', 'model': 'model-x'}
    completion, task, content, turns, events, persisted, notified = _completion(
        terminal, task=task)

    assert completion.finish() is terminal
    assert completion.finish() is terminal

    assert content.values == ['scratchpad fallback']
    assert turns.calls == 1
    assert len(events) == 2
    assert all(owner is task for owner, _event in events)
    assert [event['type'] for _owner, event in events] == [
        'endpoint_complete', 'done']
    done = events[-1][1]
    assert done['finishReason'] == terminal.finish_reason
    assert done['endpointReason'] == terminal.stop_reason
    assert done['model'] == 'model-x'
    assert done['orchestrationOutcome']['category'] == terminal.category
    if expected_extra.get('incomplete'):
        assert done['incomplete'] is True
    if expected_extra.get('error_detail'):
        assert done['error']['kind'] == 'generic'
        assert done['error']['detail'] == expected_extra['error_detail']
        assert task['error'] == done['error']
    assert task['status'] == terminal.chat_status
    assert task['finishReason'] == terminal.finish_reason
    assert task['finished_at'] > 0
    assert persisted == [task]
    assert notified == [task]


def test_endpoint_runner_keeps_only_projection_specific_completion_logic():
    runner = (ROOT / 'lib' / 'orchestration_endpoint_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_chat_flow_runtime.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'OrchestrationChatFlowCompletion(' in runtime
    assert 'terminal = completion.prepare()' in runtime
    assert 'completion.finish()' in runtime
    assert "EventType.ENDPOINT_COMPLETE" not in runner + runtime
    assert "result.get('final'" not in runner + runtime
