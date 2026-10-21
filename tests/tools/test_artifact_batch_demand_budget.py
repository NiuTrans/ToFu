"""Artifact batches spend a shared response budget on actual page demand.

Exercise the handler and production SQLite queries. Query counts, exact source
pages, error identity and opaque continuations are observable specifications.
"""

from __future__ import annotations

from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.agent_core.storage_errors import StorageError
from lib.storage_authority_sql.operations_pkg._artifacts import (
    _tool_result_artifact_read, _tool_result_artifact_search,
)
from tests.tools.test_artifact_decoded_results import artifact_store as artifact_store, save
from tests.tools.test_tool_result_artifact_batch import _invoke

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def byte_accounting(monkeypatch):
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))


def counted_repository(store, calls):
    def read(**arguments):
        with store.lock:
            calls.append(('read', dict(arguments)))
            return _tool_result_artifact_read(store, {**arguments, 'now_ms': 2})

    def search(**arguments):
        with store.lock:
            calls.append(('search', dict(arguments)))
            return _tool_result_artifact_search(store, {**arguments, 'now_ms': 2})

    return SimpleNamespace(read_range=read, search=search)


@pytest.mark.parametrize('canonical', [False, True])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_mixed_batch_returns_every_requested_page_when_the_aggregate_fits(
        monkeypatch, artifact_store, canonical, operation):
    is_read = operation == 'read_tool_artifact'
    body = ('exact source ' * 160 if is_read else ''.join(
        f'part {i} needle ' + 'payload data ' * 6 + '\n' for i in range(6)))
    large = save(artifact_store, body, canonical=canonical)
    tiny = save(artifact_store, 'tiny', canonical=canonical)
    first = {'artifact_ref': large, 'limit': 65536 if is_read else 20}
    other = {'artifact_ref': tiny, 'limit': 65536 if is_read else 20}
    if not is_read:
        first['query'] = other['query'] = 'needle'
    arguments = [first] + [dict(other) for _ in range(7)]
    query = _tool_result_artifact_read if is_read else _tool_result_artifact_search
    expected = {'status': 'ok', 'items': [
        {'index': index, 'status': 'ok', **query(artifact_store, {
            **item, 'user_id': 17, 'now_ms': 2, 'offset': 0, 'cursor': 0})}
        for index, item in enumerate(arguments)]}
    assert handler._model_tokens(operation, expected, '') < handler._RESPONSE_TOKEN_BUDGET
    calls = []
    result = _invoke(monkeypatch, operation,
        {'reads' if is_read else 'searches': arguments}, counted_repository(artifact_store, calls))

    assert result == expected
    assert len(calls) == 8
    assert sorted(call['limit'] for _, call in calls) == sorted(item['limit'] for item in arguments)


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_single_fitting_uses_observed_extent_instead_of_repeating_the_same_full_page(
        artifact_store, operation):
    is_read = operation == 'read_tool_artifact'
    body = ('bounded text ' * 160 if is_read else ''.join(
        f'part {i} needle ' + 'payload data ' * 6 + '\n' for i in range(6)))
    reference = save(artifact_store, body, canonical=True)
    arguments = {'artifact_ref': reference, 'limit': 65536 if is_read else 20}
    if not is_read:
        arguments['query'] = 'needle'
    calls = []
    result = handler._execute_one(operation, arguments, user_id=17,
        repository=counted_repository(artifact_store, calls), token_budget=1300)

    assert result['status'] == 'ok'
    assert handler._model_tokens(operation, result, '') <= 1300
    assert len(calls) >= 2
    extent = len(body.encode()) if is_read else 6
    assert calls[1][1]['limit'] <= extent // 2
    assert len(calls) <= 4
    assert result['nextCursor'].startswith('trq3.')


@pytest.mark.parametrize('operation,content', [
    ('read_tool_artifact', ''), ('read_tool_artifact', '🙂'),
    ('search_tool_artifact', ''),
])
def test_irreducible_page_does_not_repeat_an_identical_query(operation, content):
    is_read = operation == 'read_tool_artifact'
    calls = []
    page = {'artifactRef': 'tool-result:' + 'd' * 64, 'coverage': 'complete',
            'truncated': False, 'nextCursor': None, 'resumeCursor': 'trq3.start.signature',
            'requiredMetadata': 'owned metadata ' * 200}
    if is_read:
        page.update(content=content, offset=0)
    else:
        page.update(query='needle', items=[{
            'artifactRef': page['artifactRef'], 'offset': 0, 'text': 'needle'}])

    def query(**arguments):
        calls.append(arguments)
        return page

    arguments = {'artifact_ref': page['artifactRef'], 'limit': 65536 if is_read else 20}
    if not is_read:
        arguments['query'] = 'needle'
    result = handler._execute_one(operation, arguments, user_id=17,
        repository=SimpleNamespace(read_range=query, search=query), token_budget=300)
    assert result['error']['code'] == 'response_metadata_too_large'
    assert not result['error']['retryable']
    assert len(calls) == 1


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('retryable', [False, True])
def test_refits_overlap_and_do_not_retry_or_reclassify_source_failures(
        monkeypatch, artifact_store, operation, retryable):
    is_read = operation == 'read_tool_artifact'
    body = ('precise evidence ' * 260 if is_read else ''.join(
        f'part {i} needle ' + 'distinct payload ' * 20 + '\n' for i in range(6)))
    good = save(artifact_store, body, canonical=True)
    failing = save(artifact_store, 'different ' + body, canonical=True)
    tiny = save(artifact_store, 'tiny', canonical=True)
    missing = 'tool-result:' + 'e' * 64
    source_limit = 65536 if is_read else 20
    arguments = [{'artifact_ref': reference, 'limit': source_limit,
                  **({} if is_read else {'query': 'needle'})}
                 for reference in (good, failing, tiny, missing)]
    rendezvous, lock = Barrier(2), Lock()
    entered, calls = set(), []
    query_source = _tool_result_artifact_read if is_read else _tool_result_artifact_search

    def query(**values):
        reference = values['artifact_ref']
        with lock:
            calls.append(dict(values))
            first_refit = values['limit'] < source_limit and reference not in entered
            if first_refit:
                entered.add(reference)
        if first_refit:
            rendezvous.wait(timeout=5)
        if reference == failing and values['limit'] < source_limit:
            code = ('database_timeout' if retryable else
                    handler.CONTRACT['failures']['invalid_cursor']['storageCode'])
            raise StorageError(code, 'injected source failure', retryable=retryable)
        with artifact_store.lock:
            return query_source(artifact_store, {**values, 'now_ms': 2})

    monkeypatch.setattr(handler, '_RESPONSE_TOKEN_BUDGET', 6000)
    result = _invoke(monkeypatch, operation,
        {'reads' if is_read else 'searches': arguments},
        SimpleNamespace(read_range=query, search=query))

    assert result['status'] == 'partial_failure'
    assert [page['index'] for page in result['items']] == list(range(4))
    assert result['items'][0]['status'] == result['items'][2]['status'] == 'ok'
    assert result['items'][0]['nextCursor'].startswith('trq3.')
    failed = result['items'][1]
    assert failed['artifactRef'] == failing
    assert failed['error']['retryable'] is retryable
    assert failed['error']['code'] == ('artifact_store_unavailable' if retryable else 'invalid_cursor')
    assert 'nextCursor' not in failed
    assert result['items'][3]['error']['code'] == 'artifact_unavailable'
    assert sum(call['artifact_ref'] == failing for call in calls) == 2
    assert sum(call['artifact_ref'] == tiny for call in calls) == 1
    assert sum(call['artifact_ref'] == missing for call in calls) == 1
    assert entered == {good, failing}
    assert handler._model_tokens(operation, result, '') <= 6000
