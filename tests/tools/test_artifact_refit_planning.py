"""Source fitting plans useful pages while storage owns evidence and cursors."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.storage_authority_sql.operations_pkg._artifacts import (
    _tool_result_artifact_put, _tool_result_artifact_read, _tool_result_artifact_search,
)
from lib.tool_results.tool_result_query_contract import MEDIA_TYPE
from tests.tools.test_artifact_decoded_results import artifact_store as artifact_store, save

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def byte_accounting(monkeypatch):
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))


def segmented(store, bodies):
    document = {'resultId': str(uuid4()), 'executionStatus': 'ok',
        'sourceCoverage': 'complete', 'retentionCoverage': 'complete',
        'parts': [{'kind': 'text', 'text': body} for body in bodies], 'children': []}
    return _tool_result_artifact_put(store, {'user_id': 17,
        'content': json.dumps(document, ensure_ascii=False), 'media_type': MEDIA_TYPE,
        'created_at_ms': 1, 'expires_at_ms': 1000})['artifactRef']


@pytest.mark.parametrize('case', ['ascii', 'unicode', 'segments', 'search'])
def test_large_page_fits_with_few_source_queries_and_exact_continuation(artifact_store, case):
    is_read = case != 'search'
    operation = 'read_tool_artifact' if is_read else 'search_tool_artifact'
    if case == 'ascii':
        reference = save(artifact_store, 'precise evidence ' * 3000, canonical=True)
    elif case == 'unicode':
        reference = save(artifact_store, '完整证据中🙂\n' * 1800, canonical=True)
    else:
        reference = segmented(artifact_store, [
            f'part {i} needle ' + 'distinct evidence 中🙂 ' * 50 + '\n' for i in range(20)])
    query_source = _tool_result_artifact_read if is_read else _tool_result_artifact_search
    arguments = {'artifact_ref': reference, 'limit': 65536 if is_read else 20,
                 **({} if is_read else {'query': 'needle'})}
    original = query_source(artifact_store, {**arguments, 'user_id': 17, 'now_ms': 2})
    calls, pages, snapshots = [], [], []

    def query(**values):
        calls.append(dict(values))
        page = query_source(artifact_store, {**values, 'now_ms': 2})
        pages.append(page)
        snapshots.append(deepcopy(page))
        return page

    result = handler._execute_one(operation, arguments, user_id=17,
        repository=SimpleNamespace(read_range=query, search=query), token_budget=1600)

    assert result['status'] == 'ok'
    assert len(calls) <= 3, 'Fitting a known page must not scan the source at every halved size'
    assert 1250 <= handler._model_tokens(operation, result, '') <= 1600
    assert result == {'status': 'ok', **pages[-1]}
    assert pages == snapshots, 'Planning must not mutate authority pages'
    assert result['nextCursor'].startswith('trq3.')
    remainder = query_source(artifact_store, {**arguments,
        'user_id': 17, 'now_ms': 2,
        'offset' if is_read else 'cursor': result['nextCursor']})
    field = 'content' if is_read else 'items'
    assert result[field] + remainder[field] == original[field]
    replay = query_source(artifact_store, {**arguments, 'user_id': 17, 'now_ms': 2,
        'limit': calls[-1]['limit'], 'offset' if is_read else 'cursor': result['resumeCursor']})
    assert replay == pages[-1]


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_variable_required_metadata_cannot_make_fitting_loop_unbounded(operation):
    is_read = operation == 'read_tool_artifact'
    calls = []
    reference = 'tool-result:' + 'f' * 64
    original_limit = 4096 if is_read else 20

    def query(**values):
        limit = values['limit']
        calls.append(limit)
        page = {'artifactRef': reference, 'coverage': 'incomplete', 'truncated': True,
            'nextCursor': f'trq3.next-{limit}', 'resumeCursor': 'trq3.original',
            'requiredEvidence': 'metadata' * (original_limit - limit)}
        if is_read:
            page.update(offset=0, content='x' * limit)
        else:
            page.update(query='needle', items=[{
                'artifactRef': reference, 'offset': i, 'text': 'needle ' * 100}
                for i in range(limit)])
        return page

    arguments = {'artifact_ref': reference, 'limit': original_limit,
                 **({} if is_read else {'query': 'needle'})}
    result = handler._execute_one(operation, arguments, user_id=17,
        repository=SimpleNamespace(read_range=query, search=query), token_budget=700)

    assert result['error']['code'] == 'response_metadata_too_large'
    assert result['error']['retryable'] is False
    assert result['artifactRef'] == reference
    assert 'nextCursor' not in result
    assert all(after < before for before, after in zip(calls, calls[1:]))
    assert len(calls) <= original_limit.bit_length() + 2


@pytest.mark.parametrize('shape', ['dense-first', 'dense-last', 'future-provenance'])
def test_irregular_token_density_and_unknown_metadata_keep_exact_evidence(
        artifact_store, monkeypatch, shape):
    from tiktoken import get_encoding
    encoding = get_encoding('o200k_base')
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(encoding.encode(text)))
    dense, sparse = '精确🙂证据\n' * 1000, 'a' * 16000
    body = dense + sparse if shape == 'dense-first' else sparse + dense
    reference = save(artifact_store, body, canonical=True)
    arguments = {'artifact_ref': reference, 'limit': 65536}
    calls, pages, snapshots = [], [], []

    def query(**values):
        calls.append(dict(values))
        page = _tool_result_artifact_read(artifact_store, {**values, 'now_ms': 2})
        page['requiredEvidence'] = {'version': 2, 'origins': ['retained source'],
                                    'policy': 'Preserve this evidence field.'}
        if shape == 'future-provenance':
            page['provenance'] = {'futureVersion': 2, 'source': reference}
        pages.append(page)
        snapshots.append(deepcopy(page))
        return page

    result = handler._execute_one('read_tool_artifact', arguments, user_id=17,
        repository=SimpleNamespace(read_range=query), token_budget=800)

    assert result['status'] == 'ok'
    assert handler._model_tokens('read_tool_artifact', result, '') <= 800
    assert result == {'status': 'ok', **pages[-1]}
    assert pages == snapshots
    assert result['requiredEvidence'] == pages[0]['requiredEvidence']
    assert len(calls) <= arguments['limit'].bit_length() + 2
    assert all(after['limit'] < before['limit'] for before, after in zip(calls, calls[1:]))
    rest = _tool_result_artifact_read(artifact_store, {**arguments,
        'user_id': 17, 'now_ms': 2, 'offset': result['nextCursor']})
    assert result['content'] + rest['content'] == body
