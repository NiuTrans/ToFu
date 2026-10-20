"""Explicit gateway child occurrences survive provider repetition protection."""
import json
import threading

import pytest

from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
from lib.tools.programmatic import ProgrammaticResultBudget

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('programmatic', [False, True])
@pytest.mark.parametrize('name', ['synthetic_read', 'write_file'])
def test_identical_children_each_have_execution_evidence(monkeypatch, programmatic, name):
    executed = []

    def execute(task, tc, tool, call_id, args, rn, row, *rest, **kwargs):
        executed.append(call_id)
        return call_id, json.dumps({'occurrence': call_id}), False

    for module in ('_pipeline', '_heartbeat'):
        monkeypatch.setattr(
            f'lib.tasks_pkg.tool_dispatch.{module}._execute_tool_one', execute)
    arguments = ({'path': 'requested.txt', 'content': 'requested'}
                 if name == 'write_file' else {})
    tools = [{'type': 'function', 'function': {
        'name': name, 'parameters': {'type': 'object', 'properties': {
            key: {'type': 'string'} for key in arguments}}}}]
    task = {
        'id': 'gateway-occurrences', 'convId': '', '_userId': 17,
        '_transientRuntime': True, '_suppressEvents': True,
        'status': 'running', 'aborted': False, '_attended': False,
        'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
        'model': 'test-model', '_executable_tool_catalog': tools,
    }
    calls = [{'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}
        for call_id in ('first', 'second')]
    budget = ProgrammaticResultBudget() if programmatic else None
    receipts = _execute_call_batch(
        task, calls, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, model='test-model', llm_round=1,
        program_result_budget=budget)

    assert sorted(executed) == ['first', 'second']
    assert [row['call_id'] for row in receipts] == ['first', 'second']
    assert [row['status'] for row in receipts] == ['done', 'done']
    assert all(row.canonical is not None for row in receipts)
    assert [json.loads(row.canonical.text()) for row in receipts] == [
        {'occurrence': 'first'}, {'occurrence': 'second'}]
    if programmatic:
        assert [json.loads(row['output']) for row in receipts] == [
            {'occurrence': 'first'}, {'occurrence': 'second'}]
