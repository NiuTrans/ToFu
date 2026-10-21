"""Artifact argument correction and nested failures use the actual shared loop."""
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.agent_core.progress_evidence import nonretryable_failure_signature
from lib.tasks_pkg.handlers import tool_result_artifacts as artifact
from lib.tasks_pkg.handlers.tool_gateway import handle_execute_tools
from lib.tasks_pkg.orchestrator import _root_agent_loop as root
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2
from lib.tools.gateway import execute_tools_schema
from lib.tools.result_envelope import tool_result_error
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for

pytestmark = pytest.mark.unit
REFERENCE = 'tool-result:' + 'a' * 64


def owner(mode, contract):
    task = task_for(artifact._handle_tool_result_artifact, mode, name=contract.name)
    task.update(convId='', _transientRuntime=True, _suppressEvents=True,
        toolRounds=[], abort_event=threading.Event(), _executable_tool_catalog=[contract.provider_schema()])
    task['_toolContractDocumentsByName'] = {contract.name: contract.search_document()}
    return task


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('corrected', [False, True])
def test_real_root_loop_corrects_artifact_arguments_or_stops_unchanged_failures(
        mode, operation, corrected, observations, monkeypatch):
    contract = READ_TOOL_ARTIFACT if operation == 'read_tool_artifact' else SEARCH_TOOL_ARTIFACT
    task = owner(mode, contract)
    queries = []

    def query(**arguments):
        queries.append(arguments)
        page = {'artifactRef': REFERENCE, 'coverage': 'complete', 'truncated': False, 'nextCursor': None}
        return ({**page, 'content': 'retained evidence', 'offset': 0} if operation == 'read_tool_artifact'
                else {**page, 'query': 'evidence', 'items': []})

    monkeypatch.setattr(artifact, 'ToolResultArtifactRepository',
        lambda: SimpleNamespace(read_range=query, search=query))
    state = SimpleNamespace(tool_round_num=0, model='test-model')
    request = SimpleNamespace(task=task, state=state, messages=[], all_search_results_text=[],
        tid=task['id'], cfg=task['config'], project_path=None, project_enabled=False,
        tool_list=task['_executable_tool_catalog'])
    hooks = root._RootLoopHooks(request)
    hooks.round_context['stream_accumulator'] = SimpleNamespace(announced_tc_map={})

    def dispatch(round_num, tools):
        if round_num == 3:
            return {'role': 'assistant', 'content': 'Finished.'}, 'stop', {}
        cursor = ('0' if round_num == 2 else f'changed-invalid-{round_num}') if corrected else 'invalid'
        arguments = {'artifact_ref': REFERENCE, 'cursor': cursor,
                     **({'query': 'evidence'} if operation == 'search_tool_artifact' else {})}
        message = {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'query-{round_num}', 'type': 'function', 'function': {
                'name': operation, 'arguments': json.dumps(arguments)}}]}
        state.assistant_msg = message
        request.messages.append(message)
        return message, 'tool_calls', {}

    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=None, dispatch=dispatch,
        execute_tools=hooks.execute_tools, max_consecutive_no_progress_rounds=0,
        max_consecutive_nonretryable_failure_rounds=3,
        on_nonretryable_failure_state=hooks.observe_nonretryable_failure_state)
    assert len(task['toolRounds']) == 3
    if corrected:
        assert outcome.completed and outcome.exit_reason == 'completed'
        assert [row['status'] for row in task['toolRounds']] == ['error', 'error', 'done']
        assert len(queries) == 1
    else:
        assert outcome.halted and outcome.exit_reason == 'nonretryable_tool_failure'
        assert all(row['status'] == 'error' for row in task['toolRounds'])
        assert not queries
    failures = [message['content'] for message in request.messages if message.get('role') == 'tool'][:2]
    assert all(tool_result_error(content).code == 'invalid_cursor' for content in failures)
    if not corrected:
        assert any('correct the arguments' in str(message.get('content')) for message in request.messages)


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_native_artifact_failures_keep_policy_when_nested_in_execute_tools(
        mode, observations, monkeypatch):
    monkeypatch.setattr(artifact, 'ToolResultArtifactRepository',
        lambda: SimpleNamespace(read_range=lambda **kwargs: None))
    task = owner(mode, READ_TOOL_ARTIFACT)
    schema = execute_tools_schema(include_program=False)
    task['_toolContractDocumentsByName']['execute_tools'] = ToolContractV2(
        name='execute_tools', parameters=schema['function']['parameters'],
        permission='read', model_description='Run owned tools.').search_document()
    task['_tool_env'] = SimpleNamespace(resolve=lambda name:
        handle_execute_tools if name == 'execute_tools' else artifact._handle_tool_result_artifact)
    arguments = {'calls': [{'name': 'read_tool_artifact', 'arguments': {'artifact_ref': REFERENCE}},
                           {'name': 'read_tool_artifact', 'arguments': {'artifact_ref': REFERENCE, 'cursor': 'bad'}}]}
    call = {'id': 'outer', 'type': 'function', 'function': {
        'name': 'execute_tools', 'arguments': json.dumps(arguments)}}
    _, row, _ = _build_tool_round_entry('execute_tools', arguments, 'outer',
                                       call['function']['arguments'], 1, False)
    row['llmRound'] = 0
    task['toolRounds'].append(row)
    messages, captured = [], []
    execute_tool_pipeline(task, [(call, 'execute_tools', 'outer', arguments, 1, row, None)],
        task['config'], None, False, [schema], messages, [], 0, 'test-model',
        internal_result_sink=SimpleNamespace(capture=lambda _, value: captured.append(value)))
    text = messages[0]['content']
    error = tool_result_error(text)
    assert error and error.code == 'tool_batch_failed' and error.retryable is False
    assert nonretryable_failure_signature(call, text, execution_status=captured[0].execution_status)
    assert 'artifact_unavailable' in text and 'invalid_cursor' in text
    assert [json.loads(child.canonical.text())['error']['code'] for child in captured[0].children] == [
        'artifact_unavailable', 'invalid_cursor']
    assert all(item['status'] == 'error' for item in task['toolRounds'])
