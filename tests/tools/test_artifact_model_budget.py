"""Artifact continuation budgets count the delivered view, preserving owned pages."""
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tools.result_surfaces import project_model_source
from tests.tools.test_tool_result_artifact_batch import _invoke

pytestmark = pytest.mark.unit

REFERENCE = 'tool-result:' + 'a' * 64


def page(operation, *, more=True, **changes):
    value = {
        'artifactRef': REFERENCE, 'coverage': 'incomplete' if more else 'complete',
        'truncated': more, 'nextCursor': 'trq1.next-page' if more else None,
        'resumeCursor': 'trq1.' + 'signed-original-page.' * 80,
        'sourceCoverage': 'complete', 'retentionCoverage': 'complete',
    }
    if operation == 'read_tool_artifact':
        value.update(content='exact 中🙂\n', offset=0)
    else:
        value.update(query='exact', items=[{
            'offset': 0, 'text': 'exact 中🙂\n', 'artifactRef': REFERENCE}])
    return {**value, **changes}


@pytest.fixture(autouse=True)
def conservative_token_counter(monkeypatch):
    monkeypatch.setattr(
        handler, '_count_tokens', lambda text, model: len(text.encode('utf-8')))


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('more', [False, True])
def test_fitting_model_page_is_not_requeried_or_refused(operation, more):
    source = page(operation, more=more)
    queries = []

    def query(**arguments):
        queries.append(arguments)
        return source

    repository = SimpleNamespace(read_range=query, search=query)
    arguments = {'artifact_ref': REFERENCE, 'limit': 8}
    if operation == 'search_tool_artifact':
        arguments['query'] = 'exact'
    result = handler._execute_one(
        operation, arguments, user_id=17, repository=repository, token_budget=700)

    assert result == {'status': 'ok', **source}
    assert len(queries) == 1
    assert len(handler._serialize(result).encode()) > 700
    assert len(project_model_source(operation, handler._serialize(result)).encode()) <= 700


def test_repeated_search_excerpts_fit_without_requerying_or_dropping_hits():
    matches = [{'offset': offset, 'text': 'Exact evidence 中🙂\n' * 8,
                'artifactRef': REFERENCE, 'path': ['/parts/0']} for offset in range(20)]
    source = page('search_tool_artifact', items=matches)
    queries = []

    def search(**arguments):
        queries.append(arguments)
        return {**source, 'items': matches[:arguments['limit']]}

    result = handler._execute_one('search_tool_artifact', {
        'artifact_ref': REFERENCE, 'query': 'Exact', 'limit': 20}, user_id=17,
        repository=SimpleNamespace(search=search), token_budget=1100)

    assert result == {'status': 'ok', **source}
    assert len(queries) == 1 and queries[0]['limit'] == 20
    assert len(project_model_source('search_tool_artifact', handler._serialize(result)).encode()) <= 1100


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_batch_view_fits_without_discarding_canonical_page_data(operation, monkeypatch):
    # Default metadata is intentionally larger than the model budget; the
    # actual sixteen requested pages fit. Each must remain a usable result.
    source = page(operation, more=False)
    queries = []

    def query(**arguments):
        queries.append(arguments)
        return source

    item = {'artifact_ref': REFERENCE, 'limit': 8}
    if operation == 'search_tool_artifact':
        item['query'] = 'exact'
    batch_key = 'reads' if operation == 'read_tool_artifact' else 'searches'
    result = _invoke(monkeypatch, operation, {batch_key: [dict(item) for _ in range(16)]},
                     SimpleNamespace(read_range=query, search=query))

    assert result['status'] == 'ok'
    assert len(queries) == 16
    assert result['items'] == [
        {'index': index, 'status': 'ok', **source} for index in range(16)]
    raw = handler._serialize(result)
    assert len(raw.encode()) > handler._RESPONSE_TOKEN_BUDGET
    assert len(project_model_source(operation, raw).encode()) <= handler._RESPONSE_TOKEN_BUDGET
    retained = []

    def persist(canonical, *, user_id):
        retained.append((user_id, canonical.text()))
        return 'tool-result:' + 'b' * 64

    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2(operation, raw, user_id=17)
    delivery = split_tool_result_delivery(envelope)
    assert retained == [(17, raw)]
    assert json.loads(envelope.canonical.text()) == result
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']
    for item in json.loads(delivery.model_text)['items']:
        assert 'resumeCursor' not in item
        assert item['artifactRef'] == REFERENCE


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('changes', [
    {'nextCursor': 'trq1.' + 'next-page.' * 150},
    {'unknownEvidence': 'must remain visible.' * 150},
    {'outputTruncated': True},
    {'coverage': 'future-unknown-coverage'},
])
def test_required_or_unrecognized_metadata_still_consumes_budget(operation, changes):
    source = {'status': 'ok', **page(operation, **changes)}
    result = handler._fit_one_result(operation, source, '', 700)
    assert result['status'] == 'error'
    assert result['error']['code'] == 'response_metadata_too_large'
    assert result['artifactRef'] == REFERENCE
    assert 'nextCursor' not in result


@pytest.mark.parametrize('prefix', ['trq1.', 'trq2.', 'trq3.'])
def test_opaque_next_cursor_never_survives_local_body_clipping(prefix):
    source = {'status': 'ok', **page(
        'read_tool_artifact', nextCursor=prefix + 'next-page', content='evidence ' * 500)}
    # A decoded/programmatic page may omit the optional replay cursor. Its next
    # cursor still authenticates the whole source page, including any unseen tail.
    source.pop('resumeCursor')
    before = handler._serialize(source)

    result = handler._fit_read_result(source, '', 700)

    assert result['error']['code'] == 'response_metadata_too_large'
    assert result['artifactRef'] == REFERENCE
    assert 'nextCursor' not in result
    assert handler._serialize(source) == before
