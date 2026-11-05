"""The real root loop continues through storage pressure, without tool replay authority drift."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.storage_errors import StorageError
from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.tasks_pkg.handlers import tool_result_artifacts as artifact
from lib.tasks_pkg.orchestrator import _root_agent_loop as root
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_artifact_failure_recovery import owner
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit
REFERENCE = 'tool-result:' + 'a' * 64


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['fetch_url', 'read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('storage_code', [
    'database_busy', 'database_timeout', 'database_unavailable', 'database_integrity',
])
def test_root_continues_transient_storage_failures_and_stops_unchanged_terminal_faults(
        mode, operation, storage_code, observations, monkeypatch):
    transient = storage_code != 'database_integrity'
    calls = []

    def query(**arguments):
        calls.append(arguments)
        if len(calls) <= 3:
            # The storage wire flag is not whole-tool replay authority. Use
            # opposite flags to verify the typed source code and tool policy.
            raise StorageError.from_payload(StorageError(storage_code,
                'controlled storage condition', retryable=not transient).to_payload())
        page = {'artifactRef': REFERENCE, 'coverage': 'complete',
                'truncated': False, 'nextCursor': None}
        if operation == 'search_tool_artifact':
            return {**page, 'query': 'evidence', 'items': [{
                'offset': 0, 'text': 'complete retained evidence', 'artifactRef': REFERENCE}]}
        return {**page, 'content': 'complete retained evidence', 'offset': 0}

    if operation == 'fetch_url':
        def handler(task, tc, name, call_id, arguments, *args):
            result = query(**arguments)
            return call_id, result['content'], False

        task = recovery_task(handler, 'read_only', mode)
        task.update(convId='', _transientRuntime=True, _suppressEvents=True,
            toolRounds=[], abort_event=threading.Event(), _executable_tool_catalog=[{
                'type': 'function', 'function': {'name': operation, 'parameters': {'type': 'object'}}}])
        arguments = {'url': 'https://example.test/retained-source'}
    else:
        contract = artifact._CONTRACTS[operation]
        task = owner(mode, contract)
        monkeypatch.setattr(artifact, 'ToolResultArtifactRepository',
                            lambda: SimpleNamespace(read_range=query, search=query))
        arguments = {'artifact_ref': REFERENCE,
                     **({'query': 'evidence'} if operation == 'search_tool_artifact' else {})}
    state = SimpleNamespace(tool_round_num=0, model='test-model')
    request = SimpleNamespace(task=task, state=state, messages=[], all_search_results_text=[],
        tid=task['id'], cfg=task['config'], project_path=None, project_enabled=False,
        tool_list=task['_executable_tool_catalog'])
    hooks = root._RootLoopHooks(request)
    hooks.round_context['stream_accumulator'] = SimpleNamespace(announced_tc_map={})

    def provider(round_num, tools):
        if round_num >= 4:
            return {'role': 'assistant', 'content': 'Finished with retained evidence.'}, 'stop', {}
        message = {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'storage-read-{round_num}', 'type': 'function', 'function': {
                'name': operation, 'arguments': json.dumps(arguments)}}]}
        state.assistant_msg = message
        request.messages.append(message)
        return message, 'tool_calls', {}

    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=None, dispatch=provider,
        execute_tools=hooks.execute_tools, max_consecutive_no_progress_rounds=0,
        max_consecutive_nonretryable_failure_rounds=3,
        on_nonretryable_failure_state=hooks.observe_nonretryable_failure_state)
    feedback = [message['content'] for message in request.messages if message.get('role') == 'tool']
    expected_code = storage_code if operation == 'fetch_url' else 'artifact_store_unavailable'
    for content in feedback[:3]:
        error = tool_result_error(content)
        assert error.code == expected_code and error.retryable is transient
    if transient:
        assert outcome.completed and outcome.exit_reason == 'completed'
        assert len(calls) == 4 and all(value == calls[0] for value in calls)
        assert [row['status'] for row in task['toolRounds']] == ['error'] * 3 + ['done']
        assert 'complete retained evidence' in feedback[-1]
    else:
        assert outcome.halted and outcome.exit_reason == 'nonretryable_tool_failure'
        assert len(calls) == 3 and all(row['status'] == 'error' for row in task['toolRounds'])
    assert not task.get('_activeToolContexts')
