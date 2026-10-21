"""Final host outcomes replace stale envelope receipts through real dispatch/storage."""
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tools import result_value
from lib.tools.result_envelope import split_tool_result_delivery
from tests.tasks_pkg.test_tool_outcome_finality import (
    dispatch, observations as observations, task_for,
)
from tests.tools.test_tool_result_query_backends import repo as repo, storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('outcome', ['error', 'rejected'])
@pytest.mark.parametrize('storage_available', [True, False])
def test_pipeline_rebinds_prebudgeted_evidence_to_final_outcome_without_replaying(
        repo, storage, observations, monkeypatch, record_property, outcome, storage_available):
    body = 'original observation ' * 800
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 1000)
    first = _budget.budget_tool_result_v2('fixture_read', body, user_id=1)
    initial = split_tool_result_delivery(first).evidence
    original_ref = initial['artifactRef']
    assert repo.read_range(user_id=1, artifact_ref=original_ref)['executionStatus'] == 'ok'
    persisted = []
    published = []
    original_persist = result_value.persist_result
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    original_put = ToolResultArtifactRepository.put

    def put(repository, **kwargs):
        published.append(kwargs['content'])
        return original_put(repository, **kwargs)

    monkeypatch.setattr(ToolResultArtifactRepository, 'put', put)

    def persist(value, **kwargs):
        persisted.append(value.result_id)
        if not storage_available:
            raise OSError('private retention diagnostic')
        return original_persist(value, **kwargs)

    monkeypatch.setattr(result_value, 'persist_result', persist)
    executed = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        executed.append(call_id)
        if len(executed) == 1:
            _finalize_tool_round(task, number, row, [{'content': 'Final occurrence failed.'}], status=outcome)
            return call_id, first, False
        return call_id, 'recovered observation', False

    task = task_for(handler)
    row, messages, captured = dispatch(task)
    evidence = row['toolResultEvidence']
    assert row['status'] == captured[0].execution_status == outcome
    assert evidence['status'] == 'error' and evidence['executionStatus'] == outcome
    assert evidence['resultId'] == initial['resultId']
    assert json.loads(messages[0]['content'])['status'] == 'error'
    assert len(messages[0]['content'].encode()) <= 1000
    assert len(persisted) == 1, 'Final receipt publication must not repeat inside fitting'
    assert 'private retention diagnostic' not in messages[0]['content']
    second_row, second_messages, _ = dispatch(task, call_id='corrected-occurrence', round_num=1)
    assert executed == ['occurrence-1', 'corrected-occurrence']
    assert second_row['status'] == 'done' and second_messages[0]['content'] == 'recovered observation'
    storage.stop()
    storage.start()
    previous = repo.read_range(user_id=1, artifact_ref=original_ref, limit=65536)
    assert previous['content'] == body and previous['executionStatus'] == 'ok'
    if storage_available:
        assert len(published) == 1
        manifest = json.loads(published[0])
        assert manifest['parts'] == [{'kind': 'artifact', 'artifactRef': original_ref}]
        record_property('replacement_manifest_bytes', len(published[0].encode()))
        record_property('original_source_bytes', len(body.encode()))
        assert evidence['artifactRef'] != original_ref
        assert evidence['retentionCoverage'] == 'complete'
        restored = repo.read_range(user_id=1, artifact_ref=evidence['artifactRef'], limit=65536)
        assert restored['content'] == body and restored['executionStatus'] == outcome
    else:
        assert not published
        assert not evidence.get('artifactRef')
        assert evidence['retentionCoverage'] == 'unavailable'
