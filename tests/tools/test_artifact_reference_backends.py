"""Reference inheritance through real query authorities and model dispatch.

Canonical JSON, decoded program schemas and durable replay keep explicit source
references; model-only inheritance is validated on both storage backends.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.result_surfaces import project_model_source
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_artifact_read_text_projection import decode_read_view
from tests.tools.test_artifact_reference_projection import restore_references
from tests.tools.test_tool_result_query_backends import (
    repo as repo, storage as storage, manifest, put,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('alias', [False, True])
def test_dispatch_preserves_sources_program_values_and_restart_recovery(
        repo, storage, observations, monkeypatch, mode, operation, alias):
    child = put(repo, 'child needle 中🙂', ttl=120000)
    reference = manifest(repo, [
        {'kind': 'text', 'text': 'first needle 精确。\n'},
        {'kind': 'text', 'text': 'last needle 证据。\n'},
    ], [{'name': 'fetch_url', 'callId': 'same', 'artifactRef': child}], ttl=120000)
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    requested_ref = f'tr:read:{reference[-8:]}' if alias else reference
    if alias:
        task['_artifactAliases'] = {requested_ref: reference}
    is_read = operation == 'read_tool_artifact'
    contract = READ_TOOL_ARTIFACT if is_read else SEARCH_TOOL_ARTIFACT
    arguments = {'artifact_ref': requested_ref, 'limit': 65536 if is_read else 20}
    if not is_read:
        arguments['query'] = 'needle'
    task['_toolContractDocumentsByName'] = {operation: contract.search_document()}
    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', lambda: repo)
    _, row, _ = _build_tool_round_entry(operation, arguments, 'reference-page',
                                       json.dumps(arguments), 1, False)
    row['llmRound'] = 0
    task['toolRounds'] = [row]
    call = {'id': 'reference-page', 'type': 'function', 'function': {
        'name': operation, 'arguments': json.dumps(arguments)}}
    messages, captured = [], []
    execute_tool_pipeline(
        task, [(call, operation, 'reference-page', arguments, 1, row, None)],
        cfg=task['config'], project_path=None, project_enabled=False, tool_list=[],
        messages=messages, all_search_results_text=[], round_num=0, model='fixture',
        internal_result_sink=SimpleNamespace(capture=lambda _id, value: captured.append(value)))

    source = captured[0].text()
    canonical = json.loads(source)
    Draft202012Validator(contract.decoded_result_schema).validate(canonical)
    delivered = messages[-1]['content']
    assert delivered == project_model_source(operation, source)
    model = decode_read_view(delivered)
    key = 'provenance' if is_read else 'items'
    assert restore_references(model, operation)[key] == canonical[key]
    assert len(canonical[key]) == 3
    assert [item['artifactRef'] for item in canonical[key]] == [reference, reference, child]
    assert model[key][2]['artifactRef'] == child
    assert ('artifactRef' in model[key][0]) is alias
    assert ('artifactRef' in model[key][1]) is alias
    assert model['artifactRef'] == requested_ref
    assert model['coverage'] == canonical['coverage'] == 'complete'
    assert model['sourceCoverage'] == canonical['sourceCoverage'] == 'complete'
    assert model['retentionCoverage'] == canonical['retentionCoverage'] == 'complete'
    assert captured[0].execution_status == 'ok' and row['status'] == 'done'
    assert not task.get('_activeToolContexts')

    storage.stop()
    storage.start()
    recovered = repo.read_range(user_id=1, artifact_ref=row['toolResultEvidence']['artifactRef'],
                                limit=65536)
    assert json.loads(recovered['content']) == canonical
    if is_read:
        replay = repo.read_range(user_id=1, artifact_ref=reference,
                                 offset=canonical['resumeCursor'], limit=65536)
        assert replay['content'] == canonical['content']
    else:
        replay = repo.search(user_id=1, artifact_ref=reference, query='needle',
                             cursor=canonical['resumeCursor'], limit=20)
    assert replay[key] == canonical[key]
