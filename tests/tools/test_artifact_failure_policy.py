"""Artifact recovery policy reaches real dispatch without changing decoded evidence."""
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from lib.agent_core.progress_evidence import nonretryable_failure_signature
from lib.agent_core.storage_errors import StorageError
from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.result_envelope import tool_result_error, typed_tool_error
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for

pytestmark = pytest.mark.unit


def dispatch(monkeypatch, operation, outcomes, *, mode='v2', batch=False, cursor='0'):
    reference = lambda index: 'tool-result:' + chr(ord('a') + index) * 64
    example = str(typed_tool_error('documented_error', retryable=False,
        next_action='Example only.').to_model_text())
    queried = []

    def query(**values):
        index = ord(values['artifact_ref'][-1]) - ord('a')
        queried.append(index)
        outcome = outcomes[index]
        if outcome == 'missing':
            return None
        if outcome == 'busy':
            raise StorageError('database_busy', 'private store diagnostic', retryable=True)
        if outcome == 'invalid':
            raise StorageError('tool_result_invalid_cursor', 'private cursor diagnostic', retryable=False)
        page = {'artifactRef': values['artifact_ref'], 'coverage': 'complete',
                'truncated': False, 'nextCursor': None}
        if operation == 'read_tool_artifact':
            return {**page, 'content': example, 'offset': 0}
        return {**page, 'query': values['query'], 'items': [
            {'artifactRef': values['artifact_ref'], 'offset': 0, 'text': example}]}

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
        lambda: SimpleNamespace(read_range=query, search=query))
    contract = READ_TOOL_ARTIFACT if operation == 'read_tool_artifact' else SEARCH_TOOL_ARTIFACT
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task.update(convId='', _transientRuntime=True, _suppressEvents=True)
    task['_toolContractDocumentsByName'] = {operation: contract.search_document()}
    calls = [{'artifact_ref': reference(index), 'cursor': cursor,
              **({'query': 'needle'} if operation == 'search_tool_artifact' else {})}
             for index in range(len(outcomes))]
    arguments = {('reads' if operation == 'read_tool_artifact' else 'searches'): calls} if batch else calls[0]
    call = {'id': 'artifact-occurrence', 'type': 'function', 'function': {
        'name': operation, 'arguments': json.dumps(arguments)}}
    _, row, _ = _build_tool_round_entry(operation, arguments, call['id'],
                                       call['function']['arguments'], 1, False)
    row['llmRound'] = 0
    task['toolRounds'] = [row]
    messages, captured = [], []
    execute_tool_pipeline(task, [(call, operation, call['id'], arguments, 1, row, None)],
        {'autoApply': True}, None, False, [], messages, [], 0, 'test-model',
        internal_result_sink=SimpleNamespace(capture=lambda _, value: captured.append(value)))
    canonical = captured[0]
    decoded = json.loads(canonical.text())
    Draft202012Validator(contract.decoded_result_schema).validate(decoded)
    return call, messages[0]['content'], canonical, decoded, row, queried


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('outcome,code,retryable', [
    ('missing', 'artifact_unavailable', False),
    ('invalid', 'invalid_cursor', False),
    ('busy', 'artifact_store_unavailable', True),
])
def test_single_artifact_failure_preserves_typed_policy_and_exact_decoded_json(
        mode, operation, outcome, code, retryable, observations, monkeypatch):
    call, text, canonical, decoded, row, queried = dispatch(
        monkeypatch, operation, [outcome], mode=mode)
    assert canonical.execution_status == row['status'] == 'error'
    assert decoded['error']['code'] == code and decoded['error']['retryable'] is retryable
    error = tool_result_error(text)
    assert error is not None, 'a declared artifact failure lost its machine-readable recovery policy'
    assert error.code == code and error.retryable is retryable
    assert error.next_action == decoded['error']['nextAction']
    assert bool(nonretryable_failure_signature(
        call, text, execution_status=canonical.execution_status)) is not retryable
    assert 'private' not in text and 'private' not in canonical.text()
    assert queried == [0]


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('outcomes,terminal', [
    (['missing', 'invalid'], True), (['missing', 'busy'], False),
    (['missing', 'ok'], False), (['ok', 'ok'], False),
])
def test_artifact_batch_classification_requires_every_item_to_fail_nonretryably(
        mode, operation, outcomes, terminal, observations, monkeypatch):
    call, text, canonical, decoded, row, queried = dispatch(
        monkeypatch, operation, outcomes, mode=mode, batch=True)
    assert decoded['status'] == ('ok' if outcomes == ['ok', 'ok'] else 'partial_failure')
    assert [item['index'] for item in decoded['items']] == [0, 1]
    assert sorted(queried) == [0, 1]
    signature = nonretryable_failure_signature(call, text, execution_status=canonical.execution_status)
    assert bool(signature) is terminal
    if terminal:
        error = tool_result_error(text)
        assert error.code == 'tool_batch_failed' and error.retryable is False
        assert 'artifact_unavailable' in text and 'invalid_cursor' in text
    if 'ok' in outcomes:
        assert 'documented_error' in text and 'documented_error' in canonical.text()


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_changed_artifact_failure_cause_rearms_recovery_without_new_arguments(
        operation, observations, monkeypatch):
    first = dispatch(monkeypatch, operation, ['missing', 'missing'], batch=True)
    second = dispatch(monkeypatch, operation, ['missing', 'invalid'], batch=True)
    assert first[0] == second[0]
    signatures = [nonretryable_failure_signature(call, text, execution_status=canonical.execution_status)
                  for call, text, canonical, *_rest in (first, second)]
    assert all(signatures) and signatures[0] != signatures[1]
