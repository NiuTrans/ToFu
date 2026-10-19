"""Canonical operation integration: disposable authority, real transactions."""
import json
from uuid import uuid4
import pytest

pytest_plugins = ['tests._artifact_sidecar']
pytestmark = pytest.mark.unit


def manifest(repo, owner, parts=(), children=(), now=100, ttl=100000):
    from lib.tool_results.tool_result_query_contract import MEDIA_TYPE
    return repo.put(user_id=owner, now_ms=now, ttl_ms=ttl, media_type=MEDIA_TYPE,
        content=json.dumps(dict(resultId=str(uuid4()), executionStatus='ok', sourceCoverage='complete', retentionCoverage='complete', parts=list(parts), children=list(children))))['artifactRef']


def test_rejected_manifest_rollback_and_reserved_key():
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.storage import get_storage_client
    from lib.tool_results.tool_result_query_contract import MEDIA_TYPE
    repo = ToolResultArtifactRepository()
    # Failed admission must not prune already-expired evidence in its transaction.
    kept = repo.put(user_id=501, content='expired rollback sentinel', now_ms=100, ttl_ms=100)['artifactRef']
    bad = dict(resultId=str(uuid4()), executionStatus='ok', sourceCoverage='complete', retentionCoverage='complete', parts=[{'kind':'text','text':'valid'}, {'kind':'artifact','artifactRef':'bad'}], children=[])
    with pytest.raises(Exception):
        repo.put(user_id=501, content=json.dumps(bad), media_type=MEDIA_TYPE, now_ms=1000)
    assert repo.read_range(user_id=501, artifact_ref=kept, now_ms=150)['content'] == 'expired rollback sentinel'
    client = get_storage_client()
    for op, payload in [('record.get', {'namespace':'tool_result_private','key':'cursor-key-v1'}), ('record.list', {'namespace':'tool_result_private'})]:
        with pytest.raises(Exception):
            client.query(op, payload, deadline=5)


def test_stable_restart_owner_expiry_and_late_child(artifact_storage):
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    ref = repo.put(user_id=502, content='abcdefghij', now_ms=100, ttl_ms=1000)['artifactRef']
    first = repo.read_range(user_id=502, artifact_ref=ref, now_ms=101, limit=3)
    artifact_storage.stop(); artifact_storage.start()
    rest = repo.read_range(user_id=502, artifact_ref=ref, now_ms=102, offset=first['nextCursor'])
    assert rest['content'] == 'defghij'
    with pytest.raises(Exception):
        repo.read_range(user_id=503, artifact_ref=ref, now_ms=102, offset=first['nextCursor'])
    with pytest.raises(Exception):
        repo.read_range(user_id=502, artifact_ref=ref, now_ms=1100, offset=first['nextCursor'])
    child = repo.put(user_id=502, content='child late expiry', now_ms=100, ttl_ms=50)['artifactRef']
    root = manifest(repo, 502, [{'kind':'text','text':'prefix'}], [{'name':'tool','callId':'late','artifactRef':child}])
    page = repo.read_range(user_id=502, artifact_ref=root, now_ms=101, limit=6)
    with pytest.raises(Exception):
        repo.read_range(user_id=502, artifact_ref=root, now_ms=160, offset=page['nextCursor'])


def test_large_graph_over_16mib_search_continuation_without_loops():
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository(deadline_seconds=15)
    children = []
    for i in range(18):
        ref = repo.put(user_id=504, content=('x' * (1024 * 1024)) + f' needle-{i}', now_ms=100)['artifactRef']
        children.append({'name':'large','callId':str(i),'artifactRef':ref})
    root = manifest(repo, 504, children=children)
    cursor, seen, found = 0, set(), []
    for _ in range(100):
        page = repo.search(user_id=504, artifact_ref=root, query='needle-', cursor=cursor, limit=20, now_ms=101)
        found.extend(page['items'])
        cursor = page['nextCursor']
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)
    else:
        pytest.fail('query never terminates')
    assert len(found) == 18
    assert len({x['path'][0]['callId'] for x in found}) == 18


def test_read_media_descriptor_and_plain_json_never_promoted():
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    root = manifest(repo, 505, [{'kind':'media','reference':'tofu-media:original','mimeType':'image/png'}])
    page = repo.read_range(user_id=505, artifact_ref=root, now_ms=101)
    assert page['media'][0]['reference'] == 'tofu-media:original'
    assert page['scope'] == 'text_and_media_descriptors'
    page = repo.search(user_id=505, artifact_ref=root, query='original', now_ms=101)
    assert page['scope'] == 'text_only' and not page['items'] and page['unavailable']
    text = json.dumps({'parts':[{'kind':'artifact','artifactRef':root}]})
    plain = repo.put(user_id=505, content=text, media_type='application/json', now_ms=100)['artifactRef']
    assert repo.read_range(user_id=505, artifact_ref=plain, now_ms=101)['content'] == text


def test_handler_requested_limit_and_maximal_identity_refusal(monkeypatch):
    from lib.tasks_pkg.handlers import tool_result_artifacts as h
    monkeypatch.setattr(h, '_count_tokens', lambda text, model: len(text))
    class Repo:
        limits = []
        def read_range(self, **kw):
            self.limits.append(kw['limit'])
            return {'content':'x'*kw['limit'], 'resumeCursor':'trq1.resume', 'nextCursor':'trq1.next', 'provenance':[]}
    repo = Repo()
    result = h._execute_one('read_tool_artifact', {'artifact_ref':'tool-result:'+'a'*64,'limit':8192}, user_id=1, repository=repo, token_budget=1500)
    assert repo.limits[0] == 8192 and len(repo.limits) > 1
    assert result['content'] and result['nextCursor'] != result['resumeCursor']
    results = [{'status':'ok','artifactRef':'tool-result:'+'a'*64,'resumeCursor':'trq1.'+'x'*2040,'nextCursor':'trq1.'+'y'*2040,'provenance':[{'path':[{'name':'n'*200,'callId':'c'*200}]*8}],'content':'x'} for _ in range(16)]
    value = h._fit_batch_to_budget('read_tool_artifact', results, '')
    assert len(value) <= 7000 and 'response_metadata_too_large' in value
    search = {**results[0], 'items':[{'text':'needle','path':[{'name':'n'*200,'callId':'c'*200}]*8}]*20}
    refused = h._fit_search_result(search, '', 7000)
    assert refused['error']['code'] == 'response_metadata_too_large'
