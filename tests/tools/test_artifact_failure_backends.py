"""Classified query failure and corrected retained reads survive storage restart."""
import json
from types import SimpleNamespace

import pytest

from lib.agent_core.progress_evidence import nonretryable_failure_signature
from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.result_envelope import tool_result_error
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_tool_result_query_backends import repo as repo, storage as storage, put

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_query_policy_and_original_evidence_survive_restart_and_correction(
        mode, operation, repo, storage, observations, monkeypatch):
    original = 'retained evidence 中🙂'
    reference = put(repo, original, ttl=120000)
    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', lambda: repo)
    contract = READ_TOOL_ARTIFACT if operation == 'read_tool_artifact' else SEARCH_TOOL_ARTIFACT
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task.update(convId='', _transientRuntime=True, _suppressEvents=True, toolRounds=[])
    task['_toolContractDocumentsByName'] = {operation: contract.search_document()}

    def dispatch(cursor, round_num):
        arguments = {'artifact_ref': reference, 'cursor': cursor,
                     **({'query': 'retained'} if operation == 'search_tool_artifact' else {})}
        call_id = f'query-{round_num}'
        call = {'id': call_id, 'type': 'function', 'function': {
            'name': operation, 'arguments': json.dumps(arguments)}}
        _, row, _ = _build_tool_round_entry(operation, arguments, call_id,
            call['function']['arguments'], round_num + 1, False)
        row['llmRound'] = round_num
        task['toolRounds'].append(row)
        messages, captured = [], []
        execute_tool_pipeline(task, [(call, operation, call_id, arguments, round_num + 1, row, None)],
            task['config'], None, False, [], messages, [], round_num, 'test-model',
            internal_result_sink=SimpleNamespace(capture=lambda _, value: captured.append(value)))
        return call, messages[0]['content'], captured[0], row

    bad_cursor = 'trq2.invalid.signature'
    first = dispatch(bad_cursor, 0)
    error = tool_result_error(first[1])
    assert error and error.code == 'invalid_cursor' and error.retryable is False
    assert 'do not repeat the source tool' in error.next_action
    native_error = json.loads(first[2].text())
    assert native_error['artifactRef'] == reference and native_error['error']['code'] == 'invalid_cursor'
    storage.stop()
    storage.start()
    repeated = dispatch(bad_cursor, 1)
    signatures = [nonretryable_failure_signature(call, text, execution_status=canonical.execution_status)
                  for call, text, canonical, _row in (first, repeated)]
    assert signatures[0] and signatures[0] == signatures[1]
    corrected = dispatch('0', 2)
    assert corrected[2].execution_status == 'ok' and corrected[3]['status'] == 'done'
    assert 'retained' in corrected[1]
    assert tool_result_error(corrected[1]) is None
    assert not nonretryable_failure_signature(corrected[0], corrected[1], execution_status='ok')
    assert repo.read_range(user_id=1, artifact_ref=reference, limit=65536)['content'] == original
    assert [entry['outcome'] for entry in observations[1]] == ['error', 'error', 'ok']
