"""Diagnostic rendering failures preserve the root loop's recovery decisions."""
import json
from types import SimpleNamespace

import pytest

from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.tasks_pkg.orchestrator import _root_agent_loop as root
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_diagnostic_recovery import UnprintableConnection, UnprintableValue
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('failure_type', [UnprintableConnection, UnprintableValue])
def test_root_continuation_uses_original_failure_when_diagnostics_cannot_render(
        mode, failure_type, observations):
    calls = []
    retryable = failure_type is UnprintableConnection

    def handler(task, tc, name, call_id, arguments, *args):
        calls.append(call_id)
        if len(calls) <= 3:
            raise failure_type('original failure')
        return call_id, 'complete source evidence after recovery', False

    task = recovery_task(handler, 'read_only', mode)
    task.update(convId='', _transientRuntime=True, _suppressEvents=True,
        toolRounds=[], _executable_tool_catalog=[{'type': 'function', 'function': {
            'name': 'fetch_url', 'parameters': {'type': 'object'}}}])
    state = SimpleNamespace(tool_round_num=0, model='test-model')
    request = SimpleNamespace(task=task, state=state, messages=[], all_search_results_text=[],
        tid=task['id'], cfg=task['config'], project_path=None, project_enabled=False,
        tool_list=task['_executable_tool_catalog'])
    hooks = root._RootLoopHooks(request)
    hooks.round_context['stream_accumulator'] = SimpleNamespace(announced_tc_map={})

    def provider(round_num, tools):
        if round_num >= 4:
            return {'role': 'assistant', 'content': 'Finished with recovered evidence.'}, 'stop', {}
        message = {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'diagnostic-read-{round_num}', 'type': 'function', 'function': {
                'name': 'fetch_url', 'arguments': json.dumps({'url': 'https://example.test/source'})}}]}
        state.assistant_msg = message
        request.messages.append(message)
        return message, 'tool_calls', {}

    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=None, dispatch=provider,
        execute_tools=hooks.execute_tools, max_consecutive_no_progress_rounds=0,
        max_consecutive_nonretryable_failure_rounds=3,
        on_nonretryable_failure_state=hooks.observe_nonretryable_failure_state)
    feedback = [message['content'] for message in request.messages if message.get('role') == 'tool']
    for content in feedback[:3]:
        error = tool_result_error(content)
        assert error.code == ('tool_connection_failed' if retryable else 'tool_execution_failed')
        assert error.retryable is retryable and failure_type.__name__ in error.message
    if retryable:
        assert outcome.completed and outcome.exit_reason == 'completed'
        assert len(calls) == 4 and 'complete source evidence' in feedback[-1]
        assert [row['status'] for row in task['toolRounds']] == ['error'] * 3 + ['done']
    else:
        assert outcome.halted and outcome.exit_reason == 'nonretryable_tool_failure'
        assert len(calls) == 3 and all(row['status'] == 'error' for row in task['toolRounds'])
    assert not task.get('_activeToolContexts')
