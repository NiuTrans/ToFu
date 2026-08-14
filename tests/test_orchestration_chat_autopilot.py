"""Autopilot-only completion port for Flow-backed chat runs."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.orchestration_chat_autopilot import (
    OrchestrationAutopilotCompletionPorts,
    complete_orchestration_autopilot_flow,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _ports(calls, *, fail=''):
    def action(name):
        def invoke(*args, **kwargs):
            calls.append((name, args, kwargs))
            if fail == name:
                raise RuntimeError(name + ' failed')
        return invoke

    return OrchestrationAutopilotCompletionPorts(
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
    task = {'id': 'run-one', 'convId': 'conv-one'}

    result = complete_orchestration_autopilot_flow(
        task, terminal, ports=_ports(calls))

    assert result.ok is True
    assert result.reason == reason
    assert [call[0] for call in calls] == ['concluded', 'marker', 'run_id']
    assert calls[0][1] == (task, 'conv-one', 'run-one')
    assert calls[0][2] == {'reason': reason}
    assert calls[1][1] == ('conv-one',)
    assert calls[2][1] == ('conv-one',)


def test_one_cleanup_failure_does_not_skip_the_other_controls():
    calls = []
    terminal = SimpleNamespace(category='failure', stop_reason='node_failed')

    result = complete_orchestration_autopilot_flow(
        {'id': 'run-two', 'convId': 'conv-two'},
        terminal,
        ports=_ports(calls, fail='marker'),
    )

    assert result.ok is False
    assert result.concluded_emitted is True
    assert result.marker_cleared is False
    assert result.run_id_cleared is True
    assert [call[0] for call in calls] == ['concluded', 'marker', 'run_id']


def test_endpoint_runner_only_selects_the_autopilot_completion_port():
    runner = (ROOT / 'lib' / 'orchestration_endpoint_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_chat_flow_runtime.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'complete_autopilot=complete_orchestration_autopilot_flow' in runtime
    assert 'ports.complete_autopilot(task, terminal)' in runtime
    assert 'clear_autopilot_marker' not in runner + runtime
    assert '_emit_run_concluded_event' not in runner + runtime
    assert '_clear_run_id' not in runner + runtime
