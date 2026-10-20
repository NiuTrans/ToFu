"""Real artifact handler/pipeline verdicts agree with retained item outcomes."""
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for

pytestmark = pytest.mark.unit
REFERENCE = 'tool-result:' + 'a' * 64
MISSING = 'tool-result:' + 'b' * 64


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation,arguments,expected', [
    ('read_tool_artifact', {'artifact_ref': MISSING}, 'error'),
    ('search_tool_artifact', {'artifact_ref': MISSING, 'query': 'evidence'}, 'error'),
    ('read_tool_artifact', {'reads': [
        {'artifact_ref': MISSING}, {'artifact_ref': REFERENCE}]}, 'error'),
    ('read_tool_artifact', {'reads': [
        {'artifact_ref': MISSING}, {'artifact_ref': MISSING}]}, 'error'),
    ('read_tool_artifact', {'artifact_ref': REFERENCE, 'limit': 0}, 'error'),
    ('read_tool_artifact', {'artifact_ref': REFERENCE}, 'ok'),
    ('read_tool_artifact', {'reads': [
        {'artifact_ref': REFERENCE}, {'artifact_ref': REFERENCE}]}, 'ok'),
])
def test_captured_evidence_event_and_audit_keep_the_actual_artifact_verdict(
        mode, operation, arguments, expected, observations, monkeypatch):
    # The successful source deliberately contains error-shaped application data.
    body = '{"status":"error","error":{"code":"example-data"}}'
    calls = []

    class Repository:
        def read_range(self, **kwargs):
            calls.append(kwargs['artifact_ref'])
            if kwargs['artifact_ref'] == MISSING:
                return None
            return {'artifactRef': REFERENCE, 'content': body, 'offset': 0,
                    'coverage': 'complete', 'nextCursor': None, 'truncated': False,
                    'media': [{'reference': 'tofu-media:retained-image',
                               'mimeType': 'image/png'}]}

        def search(self, **kwargs):
            calls.append(kwargs['artifact_ref'])
            return None

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', Repository)
    contract = READ_TOOL_ARTIFACT if operation == 'read_tool_artifact' else SEARCH_TOOL_ARTIFACT
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task['_toolContractDocumentsByName'] = {operation: contract.search_document()}
    call_id = 'artifact-occurrence'
    _, row, _ = _build_tool_round_entry(
        operation, arguments, call_id, json.dumps(arguments), 1, False)
    row['llmRound'] = 0
    task['toolRounds'] = [row]
    call = {'id': call_id, 'type': 'function', 'function': {
        'name': operation, 'arguments': json.dumps(arguments)}}
    messages, captured = [], []
    execute_tool_pipeline(
        task, [(call, operation, call_id, arguments, 1, row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[],
        round_num=0, model='test-model', internal_result_sink=SimpleNamespace(
            capture=lambda _call, value: captured.append(value)))

    verdict = 'done' if expected == 'ok' else 'error'
    assert row['status'] == verdict
    assert captured[0].execution_status == expected
    events, audits = observations
    assert all(event['status'] == verdict for event in events
               if event.get('type') == 'tool_result')
    assert [event['outcome'] for event in audits] == [expected]
    assert not task.get('_activeToolContexts')
    assert len(calls) == (0 if arguments.get('limit') == 0 else
                          len(arguments['reads']) if 'reads' in arguments else 1)
    if expected == 'ok' or 'reads' in arguments and any(
            item['artifact_ref'] == REFERENCE for item in arguments['reads']):
        delivered = messages[0]['content']
        text = (delivered if isinstance(delivered, str) else
                '\n'.join(block.get('text', '') for block in delivered))
        assert 'example-data' in text
        assert any(part.kind == 'media' and part.reference == 'tofu-media:retained-image'
                   for part in captured[0].parts)
        assert isinstance(delivered, list)
        assert any(block.get('type') == 'image_url' for block in delivered)
