"""Execution failures preserve their cause without inventing schema advice."""
import pytest

from lib.tasks_pkg.executor._execute import _dispatch_admitted_tool_one
from tests.tasks_pkg.test_tool_outcome_finality import observations

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', [
    ValueError('Project .venv is incomplete; repair it before running commands'),
    PermissionError('Interpreter is not executable'),
    RuntimeError('Worker is unavailable'),
])
def test_handler_failure_preserves_cause_and_settles_error(failure, monkeypatch):
    finalized = []
    monkeypatch.setattr(
        'lib.tasks_pkg.executor._execute._finalize_tool_round',
        lambda *args, **kwargs: finalized.append(kwargs['status']))

    class ToolEnvironment:
        def resolve(self, name):
            def handler(*args):
                raise failure
            return handler

    task = {'id': 'runtime-failure', '_userId': 1, '_tool_env': ToolEnvironment()}
    round_entry = {}
    call_id, result, succeeded = _dispatch_admitted_tool_one(
        task, {}, 'run_command', 'runtime-call', {'command': 'stat .venv'},
        0, round_entry, {}, None, False)
    assert call_id == 'runtime-call'
    assert not succeeded
    assert str(failure) in result
    assert type(failure).__name__ in result
    assert 'parameter schema' not in result
    assert 'retry with corrected arguments' not in result
    assert round_entry['_runtimeState'] == 'error'
    assert finalized == ['error']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_actual_unavailable_tool_remains_a_failed_resolution(mode, observations):
    from lib.tools.result_envelope import tool_result_error
    from tests.tasks_pkg.test_tool_outcome_finality import dispatch, task_for

    name = 'unregistered_collection_probe'
    task = task_for(None, mode, name=name)
    row, messages, captured = dispatch(task, name=name)

    assert row['status'] == 'error'
    assert captured[0].execution_status == 'error'
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_unavailable' and error.retryable is False
    assert 'not registered' in error.message
    assert 'executionEffect' not in row
    assert not task.get('_activeToolContexts')
    assert [event['outcome'] for event in observations[1]] == ['error']


def test_direct_missing_handler_preserves_failure_when_presentation_fails(
        observations, monkeypatch):
    from lib.tasks_pkg.executor import _execute_tool_one
    from lib.tools.result_value import capture_result
    from tests.tasks_pkg.test_tool_outcome_finality import task_for

    def fail_presentation(*args, **kwargs):
        raise RuntimeError('observer unavailable')

    monkeypatch.setattr(
        'lib.tasks_pkg.executor._execute._finalize_tool_round', fail_presentation)
    name = 'unregistered_collection_probe'
    task, row = task_for(None, name=name), {}
    _, content, _ = _execute_tool_one(
        task, {}, name, 'missing-handler', {}, 1, row, {}, None, False)

    assert row['status'] == row['_runtimeState'] == 'error'
    assert capture_result(content).execution_status == 'error'
    assert 'executionEffect' not in row
    assert not task.get('_activeToolContexts')
    assert [event['outcome'] for event in observations[1]] == ['error']


def test_missing_handler_keeps_parent_publication_revocation(observations):
    from lib.agent_core.publication import ChildPublicationFence, ChildPublicationRevoked
    from lib.tasks_pkg.executor import _execute_tool_one
    from tests.tasks_pkg.test_tool_outcome_finality import task_for

    parent = ChildPublicationFence()
    name = 'unregistered_collection_probe'
    task, row = task_for(None, name=name), {}

    def execute(_event):
        parent.close()
        _execute_tool_one(task, {}, name, 'missing-handler', {}, 1, row, {}, None, False)

    with pytest.raises(ChildPublicationRevoked):
        parent.deliver(execute, None)
    assert not task.get('_activeToolContexts')
    assert 'executionEffect' not in row
