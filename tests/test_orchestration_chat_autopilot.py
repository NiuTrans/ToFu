"""Autopilot-only completion port for Flow-backed chat runs."""

from types import SimpleNamespace

import pytest

import lib.orchestration_chat_flow_runner as flow_runner
import lib.orchestration_chat_flow_runtime as flow_runtime
from lib.orchestration_chat_autopilot import (
    OrchestrationAutopilotCompletionPorts,
    complete_orchestration_autopilot_flow,
)


pytestmark = pytest.mark.unit


def _ports(calls, *, fail=''):
    def action(name):
        def invoke(*args, **kwargs):
            calls.append((name, args, kwargs))
            if fail == name:
                raise RuntimeError(name + ' failed')
            if name == 'goal':
                return {'runId': args[0].get('_goalRunId')}
        return invoke

    return OrchestrationAutopilotCompletionPorts(
        complete_goal_run=action('goal'),
        emit_concluded=action('concluded'),
        clear_marker=action('marker'),
        clear_run_id=action('run_id'),
    )


@pytest.mark.parametrize(
    ('terminal', 'reason'),
    [
        (SimpleNamespace(category='success', stop_reason='verified_complete'),
         'task_done'),
        (SimpleNamespace(category='incomplete', stop_reason='max_iterations'),
         'max_iterations'),
    ],
)
def test_completion_reason_and_all_autopilot_controls_share_one_port(
    terminal, reason,
):
    calls = []
    task = {
        'id': 'task-one', 'convId': 'conv-one',
        '_goalRunId': 'goal-run-one',
    }

    result = complete_orchestration_autopilot_flow(
        task, terminal, ports=_ports(calls))

    assert result.ok is True
    assert result.reason == reason
    assert [call[0] for call in calls] == [
        'goal', 'concluded', 'marker', 'run_id']
    assert calls[0][1] == (task, terminal)
    assert calls[1][1] == (task, 'conv-one', 'goal-run-one')
    assert calls[1][2] == {'reason': reason, 'report': ''}
    assert calls[2][1] == (task, 'conv-one')
    assert calls[3][1] == (task, 'conv-one')


def test_goal_stop_report_is_forwarded_to_the_concluded_record():
    """The runtime-stashed terminal VU verdict reaches the fold record."""
    calls = []
    terminal = SimpleNamespace(category='success', stop_reason='verified')
    task = {
        'id': 'task-report', 'convId': 'conv-report',
        '_goalRunId': 'goal-run-report',
        '_goalStopReport': 'Verified: tests pass, build green.',
    }

    result = complete_orchestration_autopilot_flow(
        task, terminal, ports=_ports(calls))

    assert result.ok is True
    assert calls[1][2] == {
        'reason': 'task_done',
        'report': 'Verified: tests pass, build green.',
    }


def test_one_cleanup_failure_does_not_skip_the_other_controls():
    calls = []
    terminal = SimpleNamespace(category='failure', stop_reason='node_failed')

    result = complete_orchestration_autopilot_flow(
        {
            'id': 'task-two', 'convId': 'conv-two',
            '_goalRunId': 'goal-run-two',
        },
        terminal,
        ports=_ports(calls, fail='marker'),
    )

    assert result.ok is False
    assert result.concluded_emitted is True
    assert result.marker_cleared is False
    assert result.run_id_cleared is True
    assert [call[0] for call in calls] == [
        'goal', 'concluded', 'marker', 'run_id']


def test_required_goal_transition_failure_stops_compatibility_cleanup():
    calls = []
    terminal = SimpleNamespace(category='success', stop_reason='completed')

    with pytest.raises(RuntimeError, match='goal failed'):
        complete_orchestration_autopilot_flow(
            {
                'id': 'task-three', 'convId': 'conv-three',
                '_goalRunId': 'goal-run-three',
            },
            terminal,
            ports=_ports(calls, fail='goal'),
        )

    assert [call[0] for call in calls] == ['goal']


def test_flow_runner_only_selects_the_autopilot_completion_port(monkeypatch):
    """Exercise the assembly seam instead of pinning implementation text."""
    runner_calls = []
    delegated_result = object()

    def execute_from_runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        return delegated_result

    monkeypatch.setattr(
        flow_runner,
        'execute_orchestration_chat_flow_task',
        execute_from_runner,
    )
    task = {'id': 'run-three'}
    definition = {'nodes': []}
    definition_service = SimpleNamespace(get_definition=lambda _ref: None)

    assert flow_runner._execute_flow_as_chat_task(
        task,
        definition,
        label='autopilot',
        max_iter=4,
        definition_service=definition_service,
    ) is delegated_result
    assert len(runner_calls) == 1
    runner_args, runner_kwargs = runner_calls[0]
    assert runner_args == (task, definition)
    assert runner_kwargs['label'] == 'autopilot'
    assert runner_kwargs['max_iterations'] == 4
    assert runner_kwargs['definition_service'] is definition_service
    assert task == {'id': 'run-three'}

    completion_calls = []
    autopilot_calls = []

    class FakeLaunch:
        projection = 'autopilot'

        def apply_task_projection(self, owner, *, label):
            completion_calls.append(('project', owner, label))

        def execution_kwargs(self, owner, *, subflow_resolver):
            assert owner is task
            assert subflow_resolver is definition_service.get_definition
            return {'runtime_probe': True}

    class FakeEventSink:
        def __init__(self, owner, append_event):
            self.owner = owner
            self.append_event = append_event

    class FakeTurnPersistence:
        def __init__(self, owner, *, store_turns, sync_turns):
            self.owner = owner
            self.messages = None

        def bind(self, messages):
            self.messages = messages

        def __call__(self, _message):
            return True

    class FakeAdapter:
        def __init__(self, *, emit, on_stream, **_kwargs):
            self.messages = [{
                'role': 'user',
                'content': ('All acceptance criteria verified by reading the '
                            'diff.\n[VU: TASK_DONE]\n'
                            '[PROGRESS: resolved=2 remaining=0]'),
            }, {'role': 'assistant', 'content': 'final answer'}]
            self.emit = emit
            self.on_stream = on_stream

        def on_event(self, _event):
            return None

    terminal = SimpleNamespace(
        chat_status='completed', stop_reason='verified_complete')

    class FakeCompletion:
        iterations = 1

        def __init__(self, owner, **kwargs):
            assert owner is task
            completion_calls.append(('completion', kwargs))

        def prepare(self):
            completion_calls.append(('prepare',))
            return terminal

        def finish(self):
            completion_calls.append(('finish',))

    import lib.orchestration.runtime_service as runtime_service
    import lib.orchestration_chat_completion as completion_module
    import lib.orchestration_chat_event_sink as event_sink_module
    import lib.orchestration_chat_flow_adapter as adapter_module
    import lib.orchestration_chat_turn_persistence as persistence_module

    monkeypatch.setattr(
        flow_runtime,
        'build_orchestration_chat_flow_launch',
        lambda *_args, **_kwargs: FakeLaunch(),
    )
    monkeypatch.setattr(runtime_service, 'execute_flow',
                        lambda *_args, **kwargs: kwargs['runtime_probe'])
    monkeypatch.setattr(completion_module, 'OrchestrationChatFlowCompletion',
                        FakeCompletion)
    monkeypatch.setattr(event_sink_module, 'OrchestrationChatTaskEventSink',
                        FakeEventSink)
    monkeypatch.setattr(adapter_module, 'FlowEventAdapter', FakeAdapter)
    monkeypatch.setattr(
        persistence_module,
        'OrchestrationChatTurnPersistence',
        FakeTurnPersistence,
    )
    monkeypatch.setattr(flow_runtime, 'audit_log', lambda *_args, **_kwargs: None)

    noop = lambda *_args, **_kwargs: None
    ports = flow_runtime.OrchestrationChatFlowRuntimePorts(
        append_event=noop,
        persist_task_result=noop,
        notify_terminal=noop,
        stamp_terminal=noop,
        store_turns=noop,
        sync_turns=noop,
        complete_autopilot=lambda owner, result: autopilot_calls.append(
            (owner, result)),
    )
    flow_runtime.execute_orchestration_chat_flow_task(
        task,
        definition,
        label='autopilot',
        max_iterations=4,
        definition_service=definition_service,
        ports=ports,
    )

    assert autopilot_calls == [(task, terminal)]
    assert ('prepare',) in completion_calls
    assert completion_calls[-1] == ('finish',)

    # The runtime stashes the sanitized terminal VU verdict before the
    # completion boundary forwards it into the concluded run record.
    assert task['_goalStopReport'] == (
        'All acceptance criteria verified by reading the diff.')


def test_vu_stop_report_sources_and_sanitizes_the_terminal_verdict():
    from lib.orchestration_chat_flow_runtime import (
        GOAL_STOP_REPORT_MAX_CHARS,
        _vu_stop_report,
    )

    messages = [
        {'role': 'user', 'content': 'first gap: tests missing'},
        {'role': 'assistant', 'content': 'work'},
        {'role': 'user',
         'content': ('Sign-off: verified the build.\n[VU: TASK_DONE]\n'
                     '[PROGRESS: resolved=3 remaining=0]')},
    ]
    assert _vu_stop_report(messages) == 'Sign-off: verified the build.'
    # A sentinel-only verdict leaves no prose — no report is stored.
    assert _vu_stop_report([
        {'role': 'user',
         'content': '[VU: TASK_DONE]\n[PROGRESS: resolved=1 remaining=0]'},
    ]) == ''
    # No VU utterance at all → no report.
    assert _vu_stop_report([{'role': 'assistant', 'content': 'x'}]) == ''
    assert _vu_stop_report([]) == ''
    # The settings blob stays bounded regardless of model verbosity.
    assert len(_vu_stop_report([
        {'role': 'user', 'content': 'x' * (GOAL_STOP_REPORT_MAX_CHARS * 3)},
    ])) == GOAL_STOP_REPORT_MAX_CHARS
