"""Demand fitting preserves source pages and continuations through real stores.

Both dispatch formats retain native values for programs and restart recovery.
Repeated occurrences, selectors and unavailable sources keep their bindings.
"""

from __future__ import annotations

import json
from threading import Lock
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.result_surfaces import project_model_source
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_tool_result_query_backends import (
    repo as repo, storage as storage, manifest, put,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_oversubscribed_batch_preserves_occurrences_scope_and_signed_continuation(
        repo, storage, observations, monkeypatch, mode, operation):
    is_read = operation == 'read_tool_artifact'
    if is_read:
        reference = manifest(repo, [{'kind': 'json', 'text': json.dumps({
            'selected': 'precise 中🙂 evidence ' * 420,
            'excluded': 'must stay outside the selector'}, ensure_ascii=False)}], ttl=120000)
        initial = repo.read_range(user_id=1, artifact_ref=reference,
                                  selector='/selected', limit=17)
    else:
        reference = manifest(repo, [{'kind': 'text', 'text':
            f'part {i} needle ' + 'distinct evidence 中🙂 ' * 35 + '\n'}
            for i in range(20)], ttl=120000)
        initial = repo.search(user_id=1, artifact_ref=reference, query='needle', limit=1)
    tiny = put(repo, 'tiny', ttl=120000)
    denied = 'tool-result:' + 'e' * 64
    first = {'artifact_ref': reference, 'cursor': initial['nextCursor'],
             'limit': 65536 if is_read else 20}
    other = {'artifact_ref': tiny, 'limit': 65536 if is_read else 20}
    failed = {'artifact_ref': denied, 'limit': 65536 if is_read else 20}
    if is_read:
        first['selector'] = '/selected'
    else:
        first['query'] = other['query'] = failed['query'] = 'needle'
    arguments = {'reads' if is_read else 'searches':
                 [dict(first), dict(other), dict(first), dict(failed)] +
                 [dict(other) for _ in range(3)]}
    calls, lock = [], Lock()

    def query(**values):
        result = (repo.read_range if is_read else repo.search)(**values)
        with lock:
            calls.append((values, result))
        return result

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', lambda: SimpleNamespace(
        read_range=query, search=query))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(handler, '_RESPONSE_TOKEN_BUDGET', 4200)
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    contract = handler._CONTRACTS[operation]
    task['_toolContractDocumentsByName'] = {operation: contract.search_document()}
    _, row, _ = _build_tool_round_entry(operation, arguments, 'demand-page',
                                       json.dumps(arguments), 1, False)
    row['llmRound'] = 0
    task['toolRounds'] = [row]
    call = {'id': 'demand-page', 'type': 'function', 'function': {
        'name': operation, 'arguments': json.dumps(arguments)}}
    messages, captured = [], []
    execute_tool_pipeline(task, [(call, operation, 'demand-page', arguments, 1, row, None)],
        cfg=task['config'], project_path=None, project_enabled=False, tool_list=[],
        messages=messages, all_search_results_text=[], round_num=0, model='fixture',
        internal_result_sink=SimpleNamespace(capture=lambda _id, value: captured.append(value)))

    canonical = json.loads(captured[0].text())
    Draft202012Validator(contract.decoded_result_schema).validate(canonical)
    assert canonical['status'] == 'partial_failure'
    assert [item['index'] for item in canonical['items']] == list(range(7))
    # The common failure delivery owns its outer envelope; the complete query
    # projection remains its structured payload, including successful siblings.
    delivered = json.loads(messages[-1]['content'])
    assert delivered['status'] == 'error'
    assert delivered['items'] == [json.loads(project_model_source(operation, captured[0].text()))]
    assert handler._model_tokens(operation, canonical, '') <= 4200
    failure = canonical['items'][3]
    assert failure['artifactRef'] == denied
    assert failure['error']['code'] == 'artifact_unavailable'
    assert failure['error']['retryable'] is False
    assert sum(values['artifact_ref'] == denied for values, _ in calls) == 1
    assert sum(values['artifact_ref'] == tiny for values, _ in calls) == 4
    large_calls = [(values, page) for values, page in calls if values['artifact_ref'] == reference]
    assert len(large_calls) > 2
    assert all(values['user_id'] == 1 for values, _ in calls)
    assert all(values.get('offset' if is_read else 'cursor') == first['cursor']
               for values, _ in large_calls)
    assert all(values.get('selector' if is_read else 'query') ==
               ('/selected' if is_read else 'needle') for values, _ in large_calls)
    for index in (0, 2):
        page = canonical['items'][index]
        assert page['status'] == 'ok'
        assert page['nextCursor'].startswith('trq3.')
        assert not page.get('outputTruncated')
        assert {key: value for key, value in page.items() if key not in ('index', 'status')} in [
            source for _, source in large_calls]
    assert not task.get('_activeToolContexts')

    storage.stop()
    storage.start()
    retained = repo.read_range(user_id=1,
        artifact_ref=row['toolResultEvidence']['artifactRef'], limit=65536)
    assert json.loads(retained['content']) == canonical
    for index in (0, 2):
        page = canonical['items'][index]
        if is_read:
            rest = repo.read_range(user_id=1, artifact_ref=reference,
                selector='/selected', offset=page['nextCursor'], limit=65536)
            expected = repo.read_range(user_id=1, artifact_ref=reference,
                selector='/selected', offset=first['cursor'], limit=65536)
            assert page['content'] + rest['content'] == expected['content']
        else:
            rest = repo.search(user_id=1, artifact_ref=reference,
                query='needle', cursor=page['nextCursor'], limit=20)
            expected = repo.search(user_id=1, artifact_ref=reference,
                query='needle', cursor=first['cursor'], limit=20)
            assert page['items'] + rest['items'] == expected['items']
