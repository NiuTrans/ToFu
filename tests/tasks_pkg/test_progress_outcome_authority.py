"""Root/native progress uses real settled outcomes before interpreting source text."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import ProgressLimits, ProgressRequest, ToolRequest
from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_gateway import NativeGateway
from lib.tasks_pkg.orchestrator import _root_agent_loop as root
from lib.tools.result_envelope import typed_tool_error
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for

pytestmark = pytest.mark.unit


def source_task(mode, projection, actual_failure):
    error = replace(typed_tool_error('source_unavailable', retryable=False,
        next_action='Inspect another source.'), summary='Example "failure".\n' * 40)
    text = error.to_model_text() if projection == 'framed' else error.to_envelope_text()
    if projection == 'framed':
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
    entered = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        entered.append(call_id)
        return call_id, text if actual_failure else str(text), False

    task = task_for(handler, mode)
    definitions = [{'type': 'function', 'function': {'name': 'fetch_url',
        'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}},
                       'required': ['url']}}}]
    task.update(convId='', _transientRuntime=True, _suppressEvents=True,
        toolRounds=[], abort_event=threading.Event(), _executable_tool_catalog=definitions)
    return task, definitions, entered


def call(round_num):
    return {'id': f'source-{round_num}', 'type': 'function', 'function': {
        'name': 'fetch_url', 'arguments': json.dumps({'url': 'https://example.test/error-doc'})}}


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('projection', ['envelope', 'framed'])
@pytest.mark.parametrize('actual_failure', [False, True])
def test_root_actual_dispatch_documents_continue_and_real_failures_stop(
        mode, projection, actual_failure, observations):
    task, definitions, entered = source_task(mode, projection, actual_failure)
    state = SimpleNamespace(tool_round_num=0, model='test-model')
    request = SimpleNamespace(task=task, state=state, messages=[], all_search_results_text=[],
        tid=task['id'], cfg=task['config'], project_path=None, project_enabled=False, tool_list=definitions)
    hooks = root._RootLoopHooks(request)
    hooks.round_context['stream_accumulator'] = SimpleNamespace(announced_tc_map={})

    def dispatch(round_num, tools):
        message = ({'role': 'assistant', 'content': 'Finished reading the examples.'}
                   if round_num == 4 else {'role': 'assistant', 'content': '', 'tool_calls': [call(round_num)]})
        state.assistant_msg = message
        request.messages.append(message)
        return message, 'stop' if round_num == 4 else 'tool_calls', {}

    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=None, dispatch=dispatch,
        execute_tools=hooks.execute_tools, max_consecutive_no_progress_rounds=0,
        max_consecutive_nonretryable_failure_rounds=3)
    if actual_failure:
        assert outcome.halted and outcome.exit_reason == 'nonretryable_tool_failure'
        assert outcome.consecutive_nonretryable_failure_rounds == 3
        assert len(entered) == len(task['toolRounds']) == 3
        assert all(row['status'] == 'error' for row in task['toolRounds'])
    else:
        assert outcome.completed and outcome.exit_reason == 'completed'
        assert outcome.consecutive_nonretryable_failure_rounds == 0
        assert len(task['toolRounds']) == 4
        assert all(row['status'] == 'done' for row in task['toolRounds'])
        assert len(entered) == 1, 'successful example reads should still reuse their cache'


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('projection', ['envelope', 'framed'])
@pytest.mark.parametrize('actual_failure', [False, True])
def test_native_actual_dispatch_keeps_source_status_through_delivery_and_cache(
        mode, projection, actual_failure, observations):
    task, definitions, entered = source_task(mode, projection, actual_failure)
    principal = PrincipalContext.user(subject_id='progress-outcome', owner_user_id=1)

    async def run(executor):
        gateway = NativeGateway(principal=principal, task=task, executor=executor,
            configuration=task['config'], tool_list=definitions, permissions={'fetch_url': 'read'},
            project_path=None, project_enabled=False, model='test-model', round_source=None,
            decision_policy=None, pending_calls=8, pending_bytes=8192)
        progress = gateway.enable_progress(ProgressLimits(2, 2), 4096)
        signatures = []
        try:
            for round_num in range(4):
                content = await gateway.execute(ToolRequest(principal, task['id'], f'request-{round_num}',
                    'fetch_url', {'url': 'https://example.test/error-doc'}, model_round=round_num))
                assert content
                observed = await progress.observe(ProgressRequest(
                    principal, task['id'], round_num, 1, threading.Event()))
                assert observed['resultEvidenceComplete'] and observed['evidenceIds']
                signatures.append(observed['nonretryableFailureSignatures'])
            if actual_failure:
                assert signatures[0] and all(value == signatures[0] for value in signatures)
            else:
                assert signatures == [[], [], [], []]
        finally:
            gateway.close()
        assert progress.gateway is None and progress.batch is None

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))
    assert len(entered) == (4 if actual_failure else 1)
    assert all(row['status'] == ('error' if actual_failure else 'done') for row in task['toolRounds'])


@pytest.mark.parametrize('missing', ['status', 'matching_status', 'current_result'])
def test_root_cannot_pair_current_call_with_foreign_or_historical_failure(missing, monkeypatch):
    task, definitions, _ = source_task('v2', 'envelope', True)
    failure = typed_tool_error('source_unavailable', retryable=False, next_action='Use another source.').to_model_text()
    current = call(0)
    # Providers can reuse correlation IDs. An old result never settles the
    # current occurrence, even when its call and visible payload match exactly.
    messages = [{'role': 'tool', 'tool_call_id': current['id'], 'content': str(failure)},
                {'role': 'assistant', 'tool_calls': [current]}]
    request = SimpleNamespace(task=task, state=SimpleNamespace(), messages=messages,
        all_search_results_text=[], tid=task['id'], cfg={}, project_path=None,
        project_enabled=False, tool_list=definitions)
    hooks = root._RootLoopHooks(request)
    hooks.round_context['stream_accumulator'] = SimpleNamespace(announced_tc_map={})

    def publish(*args, internal_result_sink, **kwargs):
        if missing != 'status':
            identity = 'foreign-call' if missing == 'matching_status' else current['id']
            internal_result_sink.capture(identity, failure.canonical)
        if missing != 'current_result':
            messages.append({'role': 'tool', 'tool_call_id': current['id'], 'content': str(failure)})
        return False

    monkeypatch.setattr(root, 'run_tool_dispatch', publish)
    result = hooks.execute_tools(1, [current])
    assert not result['nonretryable_failure_signatures']
    assert '_lastNonretryableToolCall' not in task


@pytest.mark.parametrize('missing', ['status', 'matching_status'])
def test_native_missing_or_foreign_capture_cannot_authorize_terminal_failure(missing, monkeypatch):
    task, definitions, _ = source_task('v2', 'envelope', True)
    principal = PrincipalContext.user(subject_id='progress-outcome', owner_user_id=1)
    failure = typed_tool_error('source_unavailable', retryable=False, next_action='Use another source.').to_model_text()

    def pipeline(*args, internal_result_sink, **kwargs):
        messages = args[6]
        current = messages[0]['tool_calls'][0]
        if missing == 'matching_status':
            internal_result_sink.capture('foreign-call', failure.canonical)
        messages.append({'role': 'tool', 'tool_call_id': current['id'], 'content': str(failure)})

    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch.api.execute_tool_pipeline', pipeline)
    with ThreadPoolExecutor(max_workers=1) as executor:
        gateway = NativeGateway(principal=principal, task=task, executor=executor,
            configuration=task['config'], tool_list=definitions, permissions={'fetch_url': 'read'},
            project_path=None, project_enabled=False, model='test-model', round_source=None,
            decision_policy=None, pending_calls=8, pending_bytes=8192)
        progress = gateway.enable_progress(ProgressLimits(2, 2), 4096)
        try:
            gateway._execute(ToolRequest(principal, task['id'], 'request', 'fetch_url',
                {'url': 'https://example.test/error-doc'}, model_round=0), None)
            observed = asyncio.run(progress.observe(ProgressRequest(
                principal, task['id'], 0, 1, threading.Event())))
            assert observed['resultEvidenceComplete'] and observed['evidenceIds']
            assert observed['nonretryableFailureSignatures'] == []
        finally:
            gateway.close()
