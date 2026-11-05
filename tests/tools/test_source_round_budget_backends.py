"""Real dispatch uses declared source limits while preserving original program input."""
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2
from lib.tools.result_projection_text import load_text_field
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_tool_result_query_backends import repo as repo, storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('program', [False, True])
def test_parallel_source_round_keeps_its_allowance_and_original_inputs(
        repo, storage, observations, monkeypatch, program):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 800)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 2400)
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', 2400)
    monkeypatch.setattr(_budget, '_store_tool_result_artifact',
                        lambda *a, **kw: pytest.fail('Source overflow cannot create an artifact'))
    body = 'Source code: {"path": "源码/🙂", "version": 9007199254740993}\n' * 200
    received = {'source': body, 'version': 'Observed source version: 17.'}
    executed, captured, rows, parsed, messages = [], {}, {}, [], []

    def handler(task, tc, name, call_id, *args):
        executed.append(call_id)
        return call_id, received[call_id], False

    task = task_for(handler)
    task['_toolContractDocumentsByName']['fetch_url'] = ToolContractV2(
        name='fetch_url', parameters={'type': 'object'}, permission='read',
        model_description='Read one fixture source.', result_recovery='source').search_document()
    if program:
        task['_ptc_local'] = {'tier': 'program'}
        task['_executable_tool_catalog'] = [{'type': 'function', 'function': {
            'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    for number, call_id in enumerate(received, 1):
        arguments = {'url': f'https://example.test/{call_id}'}
        encoded = json.dumps(arguments)
        _, row, _ = _build_tool_round_entry('fetch_url', arguments, call_id, encoded, number, False)
        row['llmRound'] = 0
        rows[call_id] = row
        call = {'id': call_id, 'type': 'function',
                'function': {'name': 'fetch_url', 'arguments': encoded}}
        parsed.append((call, 'fetch_url', call_id, arguments, number, row, None))
    task['toolRounds'] = list(rows.values())
    try:
        execute_tool_pipeline(task, parsed, cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=messages, all_search_results_text=[],
            round_num=0, model='test-model',
            internal_result_sink=SimpleNamespace(capture=captured.__setitem__))
        assert sorted(executed) == ['source', 'version']
        assert len(messages) == 2
        assert 2280 < sum(len(message['content'].encode()) for message in messages) <= 2400
        assert all(row['status'] == 'done' for row in rows.values())
        assert {key: value.text() for key, value in captured.items()} == received
        assert all(value.execution_status == 'ok' for value in captured.values())
        source_message = next(message for message in messages if message['tool_call_id'] == 'source')
        view = load_text_field(source_message['content'], 'summary')
        assert view is not None and 'Source code:' in view['summary']
        assert view['recovery']['tool'] == 'fetch_url'
        assert not rows['source']['toolResultEvidence'].get('artifactRef')
        assert not task.get('_activeToolContexts')
        if program:
            from lib.tools.program_results import read_program_result
            from lib.tools.programmatic import ProgrammaticResultBudget
            metadata = view['programInput']
            assert metadata['bytes'] == len(body.encode())
            storage.stop()
            storage.start()
            original = read_program_result(task, {'ref': metadata['ref']}, {}, user_id=1,
                budget=ProgrammaticResultBudget(), catalog_names={'fetch_url'})
            assert original['output'] == body and not original['output_truncated']
    finally:
        from lib.agent_core.admission import fire_terminal_callbacks
        fire_terminal_callbacks(task['id'])
