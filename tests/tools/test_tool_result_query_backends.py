"""Canonical query parity through both real selected storage authorities."""
import json
import time
from dataclasses import dataclass
from uuid import uuid4

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage
from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
from lib.tool_results.tool_result_query_contract import MEDIA_TYPE

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _QueryRepository(ToolResultArtifactRepository):
    query_test_now_ms: int = 0


@pytest.fixture
def repo(storage, monkeypatch):
    monkeypatch.setattr('lib.tool_results.tool_result_artifacts.get_storage_client', lambda write=False: storage.client)
    return _QueryRepository(deadline_seconds=15, query_test_now_ms=int(time.time() * 1000))


def put(repo, text, *, owner=1, ttl=10000, media='text/plain'):
    return repo.put(user_id=owner, content=text, now_ms=repo.query_test_now_ms, ttl_ms=ttl, media_type=media)['artifactRef']


def manifest(repo, parts=(), children=(), **kw):
    value = dict(resultId=str(uuid4()), executionStatus='ok', sourceCoverage='complete',
                 retentionCoverage='complete', parts=list(parts), children=list(children))
    return put(repo, json.dumps(value), media=MEDIA_TYPE, **kw)


def pages(repo, reference, **kw):
    cursor, seen = 0, set()
    for _ in range(1000):
        page = repo.read_range(user_id=1, artifact_ref=reference, offset=cursor, now_ms=repo.query_test_now_ms + 1, **kw)
        yield page
        cursor = page['nextCursor']
        if cursor is None:
            return
        assert cursor not in seen
        seen.add(cursor)
    pytest.fail('read cursor never terminates')


def test_large_valid_selector_can_continue_and_replay(repo):
    # The pointer fits the declared 1024-character source-query bound. Its
    # repeated encoding must not make an otherwise valid read unpageable.
    key = '段' * 700
    content = 'selected 中🙂\n' * 5
    root = manifest(repo, [{'kind': 'json', 'text': json.dumps(
        {key: content}, ensure_ascii=False)}])
    arguments = dict(user_id=1, artifact_ref=root,
                     selector='/' + key, limit=32,
                     now_ms=repo.query_test_now_ms + 1)
    first = repo.read_range(**arguments)
    assert first['nextCursor']
    read = list(pages(repo, root, selector='/' + key, limit=32))
    assert ''.join(page['content'] for page in read) == json.dumps(
        content, ensure_ascii=False, separators=(',', ':'))
    assert repo.read_range(**arguments, offset=first['resumeCursor']) == first


def test_maximum_unicode_search_query_can_continue_and_replay(repo):
    query = '🙂' * 200
    root = put(repo, query + ' gap ' + query)
    arguments = dict(user_id=1, artifact_ref=root, query=query, limit=1,
                     now_ms=repo.query_test_now_ms + 1)
    first = repo.search(**arguments)
    assert len(first['items']) == 1
    assert query in first['items'][0]['text']
    assert first['nextCursor']
    second = repo.search(**arguments, cursor=first['nextCursor'])
    assert len(second['items']) == 1
    assert query in second['items'][0]['text']
    assert [first['items'][0]['offset'], second['items'][0]['offset']] == [0, 205]
    assert repo.search(**arguments, cursor=first['resumeCursor']) == first


def test_nested_unicode_and_repeated_call_ids_keep_exact_segment_identity(repo):
    text = 'ß中🙂é\n' * 100
    leaf = put(repo, text)
    child = manifest(repo, [{'kind': 'artifact', 'artifactRef': leaf}])
    root = manifest(repo, [{'kind': 'text', 'text': 'header'}], [
        {'name': 'same', 'callId': 'same', 'artifactRef': child},
        {'name': 'same', 'callId': 'same', 'artifactRef': child}])
    all_pages = list(pages(repo, root, limit=37))
    assert ''.join(page['content'] for page in all_pages) == 'header' + text * 2
    assert all_pages[-1]['sizeBytes'] == len(('header' + text * 2).encode())
    assert all_pages[-1]['coverage'] == 'complete'
    segments = {tuple(span['segment']) for page in all_pages for span in page['provenance']}
    assert segments == {(0,), (1, 0), (2, 0)}
    for page in all_pages:
        assert sum(span['end'] - span['start'] for span in page['provenance']) == len(page['content'].encode())


def test_signed_route_cursor_survives_restart_and_rejects_changed_bindings(repo, storage):
    child = put(repo, '中abcdefß' * 20)
    root = manifest(repo, children=[{'name': 'tool', 'callId': 'child', 'artifactRef': child}])
    first = repo.read_range(user_id=1, artifact_ref=root, now_ms=repo.query_test_now_ms + 1, limit=5)
    cursor = first['nextCursor']
    assert cursor.startswith('trq2.')
    storage.stop(); storage.start()
    rest = repo.read_range(user_id=1, artifact_ref=root, now_ms=repo.query_test_now_ms + 2, offset=cursor)
    assert first['content'] + rest['content'] == '中abcdefß' * 20
    baseline = dict(user_id=1, artifact_ref=root, now_ms=repo.query_test_now_ms + 2, offset=cursor)
    for changes in [dict(user_id=2), dict(artifact_ref=child), dict(selector='/changed'),
                    dict(offset=cursor[:-1] + ('0' if cursor[-1] != '0' else '1')), dict(now_ms=repo.query_test_now_ms + 10000)]:
        with pytest.raises(Exception):
            repo.read_range(**(baseline | changes))
    with pytest.raises(Exception):
        repo.search(user_id=1, artifact_ref=root, query='a', cursor=cursor, now_ms=repo.query_test_now_ms + 2)


def test_search_adjacent_unicode_and_window_boundary_offsets(repo):
    text = 'ßSSsss ' + 'x' * 65520 + 'Straße 中🙂 needle needle'
    ref = put(repo, text)
    for query, expected in [('ss', [0, 1, 2, 3, 4, text.index('Straße') + 4]),
                            ('needle', [text.index('needle'), text.rindex('needle')])]:
        cursor, found, seen = 0, [], set()
        while True:
            page = repo.search(user_id=1, artifact_ref=ref, query=query, cursor=cursor, limit=1, now_ms=repo.query_test_now_ms + 1)
            found.extend(item['offset'] for item in page['items'])
            cursor = page['nextCursor']
            if cursor is None:
                break
            assert cursor not in seen
            seen.add(cursor)
        assert found == expected


@pytest.mark.parametrize('canonical', [False, True])
def test_handler_search_budget_retains_authority_quotes_and_cursors(
        repo, monkeypatch, canonical):
    from lib.tasks_pkg.handlers import tool_result_artifacts as handler
    from lib.tools.result_surfaces import project_model_source

    monkeypatch.setattr(
        handler, '_count_tokens', lambda text, _model: len(text.encode('utf-8')))
    text = ('中🙂' * 80 + 'needle' + '背景' * 160) * 24
    ref = (manifest(repo, [{'kind': 'text', 'text': text}], ttl=120000)
           if canonical else put(repo, text, ttl=120000))
    arguments = {'artifact_ref': ref, 'query': 'needle', 'limit': 20}
    original = repo.search(user_id=1, **arguments)
    assert len(handler._serialize(original).encode('utf-8')) > 7000

    page = handler._execute_one(
        'search_tool_artifact', arguments, user_id=1, repository=repo)
    assert page['status'] == 'ok' and 0 < len(page['items']) < len(original['items'])
    expected = repo.search(user_id=1, **{**arguments, 'limit': len(page['items'])})
    assert page == {'status': 'ok', **expected}
    assert all('needle' in item['text'] for item in page['items'])
    assert len(project_model_source(
        'search_tool_artifact', handler._serialize(page)).encode('utf-8')) <= 7000
    resumed = handler._execute_one('search_tool_artifact', {
        **arguments, 'cursor': page['nextCursor']}, user_id=1, repository=repo)
    assert resumed['status'] == 'ok' and resumed['items']
    assert resumed['items'][0]['offset'] > page['items'][-1]['offset']


@pytest.mark.parametrize('canonical', [False, True])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_handler_admits_exact_page_using_its_declared_model_budget(
        repo, monkeypatch, canonical, operation):
    from types import SimpleNamespace
    from lib.tasks_pkg.handlers import tool_result_artifacts as handler
    from lib.tools.result_surfaces import project_model_source

    body = 'needle 中🙂\n' * 48
    ref = (manifest(repo, [{'kind': 'text', 'text': body}], ttl=120000)
           if canonical else put(repo, body, ttl=120000))
    arguments = {'artifact_ref': ref, 'limit': 8 if operation == 'read_tool_artifact' else 1}
    if operation == 'search_tool_artifact':
        arguments['query'] = 'needle'
    queries = []

    def query(**values):
        queries.append(values)
        return (repo.read_range(**values) if operation == 'read_tool_artifact'
                else repo.search(**values))

    raw_page = query(user_id=1, **arguments)
    queries.clear()
    expected = {'status': 'ok', **raw_page}
    source = handler._serialize(expected)
    budget = len(project_model_source(operation, source).encode('utf-8'))
    assert budget < len(source.encode('utf-8'))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode('utf-8')))

    result = handler._execute_one(
        operation, arguments, user_id=1,
        repository=SimpleNamespace(read_range=query, search=query),
        token_budget=budget)

    assert result == expected
    assert len(queries) == 1
    assert result['resumeCursor'] and result['nextCursor']


@pytest.mark.parametrize('canonical', [False, True])
def test_default_sixteen_read_batch_returns_each_requested_source_page(
        repo, monkeypatch, canonical):
    from types import SimpleNamespace
    from lib.tasks_pkg.handlers import tool_result_artifacts as handler
    from lib.token_counter.tiktoken_counter import count_text
    from lib.tools.result_surfaces import project_model_source

    body = 'needle\n' * 48
    ref = (manifest(repo, [{'kind': 'text', 'text': body}], ttl=120000)
           if canonical else put(repo, body, ttl=120000))
    expected = {'status': 'ok', **repo.read_range(user_id=1, artifact_ref=ref, limit=8)}
    model_page = project_model_source('read_tool_artifact', handler._serialize(expected))
    assert count_text(model_page, 'gpt-4o') < (handler._RESPONSE_TOKEN_BUDGET - 256) // 16
    queries = []

    def query(**values):
        queries.append(values)
        return repo.read_range(**values)

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query))
    monkeypatch.setattr(handler, 'task_user_id', lambda task: 1)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *args: None)
    _, content, _ = handler._handle_tool_result_artifact(
        {'model': 'gpt-4o'}, {}, 'read_tool_artifact', 'small-page-batch',
        {'reads': [{'artifact_ref': ref, 'limit': 8} for _ in range(16)]},
        1, {}, {}, '', False)

    result = json.loads(content)
    assert result['status'] == 'ok'
    assert len(queries) == 16
    assert result['items'] == [
        {'index': index, **expected} for index in range(16)]
    assert count_text(project_model_source('read_tool_artifact', content), 'gpt-4o') <= 7000


@pytest.mark.parametrize('name', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('more_pages', [False, True])
def test_model_cursor_projection_preserves_owned_source_and_continuation(
        repo, storage, name, more_pages):
    from lib.agent_core.storage_errors import StorageError
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    from lib.tools.result_envelope import split_tool_result_delivery

    reference = manifest(repo, [{'kind': 'text', 'text': 'needle 中文🙂\n' * 8}],
                         ttl=120000)
    if name == 'read_tool_artifact':
        def query(cursor):
            return repo.read_range(user_id=1, artifact_ref=reference, offset=cursor,
                                   limit=17 if more_pages else 65536)
    else:
        def query(cursor):
            return repo.search(user_id=1, artifact_ref=reference, cursor=cursor,
                               query='needle', limit=1 if more_pages else 20)
    page = {'status': 'ok', **query(0)}
    assert page['truncated'] is more_pages
    source = json.dumps(page, ensure_ascii=False, separators=(',', ':'))
    envelope = budget_tool_result_v2(name, source, user_id=1)
    delivery = split_tool_result_delivery(envelope)
    model = json.loads(delivery.model_text)

    assert 'resumeCursor' not in model
    assert model.get('nextCursor') == page['nextCursor']
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert delivery.evidence['retentionCoverage'] == 'complete'
    retained_ref = delivery.evidence['artifactRef']
    if more_pages and name == 'read_tool_artifact':
        storage.stop()
        storage.start()
    retained = repo.read_range(user_id=1, artifact_ref=retained_ref, limit=65536)
    assert json.loads(retained['content']) == page
    try:
        denied = repo.read_range(user_id=2, artifact_ref=retained_ref)
    except StorageError as error:
        assert error.code == 'database_forbidden' and not error.retryable
    else:
        assert denied is None
    assert query(page['resumeCursor']) == {key: value for key, value in page.items()
                                          if key != 'status'}
    if more_pages:
        resumed = query(model['nextCursor'])
        if name == 'read_tool_artifact':
            assert resumed['offset'] > page['offset']
        else:
            assert resumed['items'][0]['offset'] > page['items'][-1]['offset']


def test_selector_media_and_missing_children_are_explicit(repo):
    root = manifest(repo, [{'kind': 'text', 'text': 'not JSON'},
        {'kind': 'json', 'text': '{"a/b":{"~":[1,"中"]}}'},
        {'kind': 'media', 'reference': 'tofu-media:original', 'mimeType': 'image/png'}])
    page = repo.read_range(user_id=1, artifact_ref=root, selector='/a~1b/~0/1', now_ms=repo.query_test_now_ms + 1)
    assert page['content'] == '"中"'
    assert page['coverage'] == 'incomplete' and page['media'][0]['reference'] == 'tofu-media:original'
    for selector in ['/bad~escape', '/' * 34, 'bad']:
        with pytest.raises(Exception):
            repo.read_range(user_id=1, artifact_ref=root, selector=selector, now_ms=repo.query_test_now_ms + 1)
    child = put(repo, 'expired', ttl=50)
    root = manifest(repo, [{'kind': 'text', 'text': 'prefix'}], [{'name': 'late', 'callId': 'x', 'artifactRef': child}])
    first = repo.read_range(user_id=1, artifact_ref=root, limit=6, now_ms=repo.query_test_now_ms + 1)
    with pytest.raises(Exception):
        repo.read_range(user_id=1, artifact_ref=root, offset=first['nextCursor'], now_ms=repo.query_test_now_ms + 60)
    page = repo.search(user_id=1, artifact_ref=root, query='expired', now_ms=repo.query_test_now_ms + 60)
    assert not page['items'] and page['coverage'] == 'incomplete'


def test_private_key_operations_and_invalid_manifest_are_denied(repo, storage):
    kept = put(repo, 'kept')
    for operation, payload in [('record.get', {'key': 'cursor-key-v1'}), ('record.list', {}),
                               ('record.put', {'key': 'cursor-key-v1', 'value': 'forged'}),
                               ('record.delete', {'key': 'cursor-key-v1'})]:
        with pytest.raises(Exception):
            if operation in {'record.get', 'record.list'}:
                storage.client.query(operation, {'namespace': 'tool_result_private', **payload}, deadline=5)
            else:
                storage.client.command(operation, {'namespace': 'tool_result_private', **payload}, 'deny-' + operation, deadline=5)
    for parts in [[{'kind': 'artifact', 'artifactRef': 'bad'}], [{'kind': 'json', 'text': '{'}],
                  [{'kind': 'text', 'text': 'ok', 'sha256': 'tool-result:' + 'a' * 64}]]:
        with pytest.raises(Exception):
            manifest(repo, parts)
    assert repo.read_range(user_id=1, artifact_ref=kept, now_ms=repo.query_test_now_ms + 1)['content'] == 'kept'


def test_search_graph_larger_than_scan_budget_reaches_each_child(repo):
    # Real expiry maintenance continues between pages. Keep this positive
    # traversal fixture live beyond the test timeout, including debug engines;
    # signed-cursor expiry is exercised separately with explicit timestamps.
    lifetime_ms = 600_000
    children = [{'name': 'large', 'callId': str(i), 'artifactRef': put(
        repo, 'x' * (1024 * 1024) + f' needle-{i}', ttl=lifetime_ms)}
        for i in range(18)]
    root = manifest(repo, children=children, ttl=lifetime_ms)
    cursor, seen, found = 0, set(), []
    for _ in range(100):
        page = repo.search(user_id=1, artifact_ref=root, query='needle-', cursor=cursor, limit=20, now_ms=repo.query_test_now_ms + 1)
        found.extend(page['items']); cursor = page['nextCursor']
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)
    else:
        pytest.fail('bounded search did not terminate')
    assert len(found) == 18
    assert {item['path'][0]['callId'] for item in found} == {str(i) for i in range(18)}


def test_invalid_continuation_refuses_even_when_root_is_missing(repo):
    reference = 'tool-result:' + 'f' * 64
    assert repo.read_range(user_id=1, artifact_ref=reference, now_ms=repo.query_test_now_ms + 1) is None
    for cursor in ['invalid', -1, True, {}, 'trq1.invalid.signature']:
        with pytest.raises(Exception):
            repo.read_range(user_id=1, artifact_ref=reference, offset=cursor, now_ms=repo.query_test_now_ms + 1)
    with pytest.raises(Exception):
        repo.search(user_id=1, artifact_ref=reference, query='needle', cursor=1, now_ms=repo.query_test_now_ms + 1)
