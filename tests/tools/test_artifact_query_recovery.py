"""Artifact query refusals preserve the cause and a usable read-only recovery."""
import json
import pytest

from lib.agent_core.storage_errors import StorageError
from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tool_results.tool_result_query_contract import CONTRACT
from tests.tools.test_tool_result_query_backends import (
    manifest, put, repo as repo, storage as storage,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('arguments,code,correction', [
    ({'selector': '/field~2'}, 'invalid_selector', {'selector': '/field'}),
    ({'selector': 'field'}, 'invalid_selector', {'selector': '/field'}),
    ({'cursor': 'trq2.invalid.signature'}, 'invalid_cursor', {'cursor': '0'}),
    ({'cursor': 'not-a-cursor'}, 'invalid_cursor', {'cursor': '0'}),
    ({'cursor': '-1'}, 'invalid_cursor', {'cursor': '0'}),
])
def test_rejected_query_guides_a_corrected_read_of_the_same_evidence(
        repo, arguments, code, correction):
    root = manifest(repo, [{'kind': 'json', 'text': '{"field":"retained evidence"}'}])
    call = {'artifact_ref': root, **arguments}
    failed = handler._execute_one('read_tool_artifact', call, user_id=1, repository=repo)
    assert failed['status'] == 'error'
    assert failed['error']['code'] == code
    assert failed['error']['nextAction']
    assert failed['error']['retryable'] is False
    assert failed['artifactRef'] == root
    corrected = handler._execute_one(
        'read_tool_artifact', {**call, **correction}, user_id=1, repository=repo)
    assert corrected['status'] == 'ok'
    assert 'retained evidence' in corrected['content']


def test_expired_continuation_is_not_reported_as_malformed_input(repo):
    root = put(repo, 'retained evidence', ttl=1000)
    first = repo.read_range(user_id=1, artifact_ref=root, limit=1,
                            now_ms=repo.query_test_now_ms + 1)
    with pytest.raises(StorageError) as raised:
        repo.read_range(user_id=1, artifact_ref=root, offset=first['nextCursor'],
                        now_ms=repo.query_test_now_ms + 1001)
    assert raised.value.code == 'tool_result_continuation_unavailable'


@pytest.mark.parametrize('storage_code,retryable', [
    ('database_integrity', False), ('database_forbidden', False),
    ('database_protocol_error', False), ('database_busy', True),
])
def test_storage_failure_keeps_its_retryability_without_guessing_cursor_fault(
        storage_code, retryable):
    class Repository:
        def read_range(self, **kwargs):
            raise StorageError(storage_code, 'private diagnostic must not reach the model',
                               retryable=retryable)

    result = handler._execute_one('read_tool_artifact',
        {'artifact_ref': 'tool-result:' + 'a' * 64}, user_id=1, repository=Repository())
    assert result['error']['code'] == 'artifact_store_unavailable'
    assert result['error']['retryable'] is retryable
    assert 'private diagnostic' not in str(result)
    assert result['error']['nextAction']


@pytest.mark.parametrize('code,failure', list(CONTRACT['failures'].items()))
def test_query_failure_survives_the_storage_wire_with_declared_guidance(code, failure):
    source = StorageError(failure['storageCode'], 'host diagnostic',
                          retryable=failure['retryable'])
    restored = StorageError.from_payload(source.to_payload())
    assert restored.code == source.code == failure['storageCode']
    result = handler._error_result(restored)
    assert result['error'] == {'code': code, 'retryable': failure['retryable'],
                               'nextAction': failure['nextAction']}


def test_failed_read_keeps_the_next_batch_item_and_its_evidence(repo, monkeypatch):
    root = manifest(repo, [{'kind': 'json', 'text': '{"field":"retained evidence"}'}])
    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', lambda: repo)
    monkeypatch.setattr(handler, 'task_user_id', lambda _task: 1)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *_args, **_kwargs: None)
    arguments = {'reads': [
        {'artifact_ref': root, 'selector': '/field~2'},
        {'artifact_ref': root, 'selector': '/field'},
    ]}
    _, content, _ = handler._handle_tool_result_artifact(
        {'model': ''}, {}, 'read_tool_artifact', 'query-batch', arguments,
        1, {}, {}, '', False)
    result = json.loads(content)
    assert result['status'] == 'partial_failure'
    failed, successful = result['items']
    assert failed['error']['code'] == 'invalid_selector'
    assert failed['error']['nextAction']
    assert failed['index'] == 0 and successful['index'] == 1
    assert successful['status'] == 'ok'
    assert successful['content'] == '"retained evidence"'
