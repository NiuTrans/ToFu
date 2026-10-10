"""Execution failures preserve their cause without inventing schema advice."""
import pytest

from lib.tasks_pkg.executor._execute import _dispatch_admitted_tool_one

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
