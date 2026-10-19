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
    assert cursor.startswith('trq1.')
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
    children = [{'name': 'large', 'callId': str(i), 'artifactRef': put(repo, 'x' * (1024 * 1024) + f' needle-{i}')} for i in range(18)]
    root = manifest(repo, children=children)
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
