"""The real root loop receives each observation and keeps exact restart recovery."""
import json
from types import SimpleNamespace

import pytest

from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.tasks_pkg.compaction import _budget
from lib.tasks_pkg.orchestrator import _root_agent_loop as root
from lib.tools.contracts import ToolContractV2
from lib.tools.result_projection_text import load_text_field
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_tool_result_query_backends import repo as repo, storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('program', [False, True])
@pytest.mark.parametrize('policy', ['source', 'artifact'])
def test_next_root_round_receives_every_observation_with_exact_restart_recovery(
        repo, storage, observations, monkeypatch, program, policy):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 2000)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 6000)
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', 6000)
    publications = []
    store = _budget._store_tool_result_artifact
    def retain(content, **kwargs):
        publications.append(content)
        return store(content, **kwargs)
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', retain)
    for owner in ('_parse', '_labels'):
        monkeypatch.setattr(f'lib.tasks_pkg.tool_dispatch.{owner}.append_event',
                            lambda task, event: observations[0].append(event))
    received = {str(i): f'EVIDENCE-{i}: {{"path": "源码/🙂", "version": 9007199254740993}}\n' * 200
                for i in range(4)}
    executed, captured = [], {}

    def handler(task, tc, name, call_id, *args):
        executed.append(call_id)
        return call_id, received[call_id], False

    task = task_for(handler)
    task['toolRounds'] = []
    task['_toolContractDocumentsByName']['fetch_url'] = ToolContractV2(
        name='fetch_url', parameters={'type': 'object'}, permission='read',
        model_description='Read one fixture source.', result_recovery=policy).search_document()
    catalog = [{'type': 'function', 'function': {
        'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    if program:
        task['_ptc_local'] = {'tier': 'program'}
        task['_executable_tool_catalog'] = catalog
    calls = [{'id': key, 'type': 'function', 'function': {'name': 'fetch_url',
              'arguments': json.dumps({'url': f'https://example.test/{key}'})}}
             for key in received]
    state = SimpleNamespace(model='test-model', tool_round_num=0)
    request = SimpleNamespace(task=task, state=state, messages=[], all_search_results_text=[],
        tid=task['id'], cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=catalog)
    hooks = root._RootLoopHooks(request)
    hooks.round_context['stream_accumulator'] = SimpleNamespace(announced_tc_map={})
    dispatch_tools = root.run_tool_dispatch
    def observe_dispatch(*args, internal_result_sink, **kwargs):
        def capture(call_id, value):
            captured[call_id] = value
            internal_result_sink.capture(call_id, value)
        return dispatch_tools(*args, **kwargs,
            internal_result_sink=SimpleNamespace(capture=capture))
    monkeypatch.setattr(root, 'run_tool_dispatch', observe_dispatch)
    model_rounds, views = [], {}

    def provider(round_num, tools):
        model_rounds.append(round_num)
        assert round_num <= 1, 'The scripted provider cannot request a corrective source read'
        if round_num == 0:
            message = {'role': 'assistant', 'content': '', 'tool_calls': calls}
            state.assistant_msg = message
            request.messages.append(message)
            return message, 'tool_calls', {}
        messages = [message for message in request.messages if message['role'] == 'tool']
        assert len(messages) == 4
        assert 5700 < sum(len(message['content'].encode()) for message in messages) <= 6000
        for message in messages:
            key, content = message['tool_call_id'], message['content']
            view = load_text_field(content, 'summary') or json.loads(content)
            assert f'EVIDENCE-{key}' in view['summary']
            views[key] = view
        return {'role': 'assistant', 'content': 'All four observations received.'}, 'stop', {}

    try:
        outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=catalog,
            dispatch=provider, execute_tools=hooks.execute_tools,
            max_consecutive_no_progress_rounds=0)
        assert outcome.completed and outcome.exit_reason == 'completed'
        assert model_rounds == [0, 1] and sorted(executed) == sorted(received)
        assert {key: value.text() for key, value in captured.items()} == received
        assert all(value.execution_status == 'ok' for value in captured.values())
        rows = task['toolRounds']
        assert len(rows) == 4 and all(row['status'] == 'done' for row in rows)
        assert not task.get('_activeToolContexts')
        # Artifact policy publishes once per first-stage result; aggregate fitting
        # must not repeat persistence. Source policy never publishes overflow.
        assert len(publications) == (4 if policy == 'artifact' else 0)
        storage.stop()
        storage.start()
        for key, row in zip(received, rows, strict=True):
            reference = row['toolResultEvidence'].get('artifactRef')
            if policy == 'artifact':
                page = repo.read_range(user_id=1, artifact_ref=reference, offset=0, limit=100000)
                assert page['content'] == received[key] and page['nextCursor'] is None
            else:
                assert not reference and views[key]['recovery']['tool'] == 'fetch_url'
            if program:
                from lib.tools.program_results import read_program_result
                from lib.tools.programmatic import ProgrammaticResultBudget
                metadata = views[key]['programInput']
                assert metadata['bytes'] == len(received[key].encode())
                original = read_program_result(task, {'ref': metadata['ref']}, {}, user_id=1,
                    budget=ProgrammaticResultBudget(), catalog_names={'fetch_url'})
                assert original['output'] == received[key] and not original['output_truncated']
    finally:
        from lib.agent_core.admission import fire_terminal_callbacks
        fire_terminal_callbacks(task['id'])
