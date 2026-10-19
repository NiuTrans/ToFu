"""Oversized bound evidence round-trips through existing owner-scoped tool reads."""
import hashlib
import json
import pytest
from lib.research import context
from tests.research.test_research_workspace_evidence_guard import storage as storage
pytestmark = pytest.mark.unit


def test_source_descriptor_reads_complete_original_on_selected_store(storage, monkeypatch):
    import lib.tool_results.tool_result_artifacts as artifacts
    from lib.tasks_pkg.handlers.tool_result_artifacts import _execute_one
    supervisor, _ = storage
    monkeypatch.setattr(artifacts, 'get_storage_client', lambda **kwargs: supervisor.client)
    monkeypatch.setattr(context, 'action_window', lambda model: 32000)
    monkeypatch.setattr('lib.token_counter.count_text', lambda text, **kwargs: len(text) // 4)
    source = {'source_task_id': 'completed-discovery', 'survey_markdown': '012345证据😀' * 30000,
              'gap_map': [{'id': 'final-gap', 'evidence': 'original tail'}]}
    messages = [{'role': 'system', 'content': 'Read source evidence.'},
                {'role': 'user', 'content': json.dumps({'research_artifacts': source})}]
    tools = [{'function': {'name': 'read_tool_artifact'}}]
    prepared = context.prepare_action_messages(messages, model='test', tools=tools, user_id=1)
    descriptor = json.loads(prepared[1]['content'])['research_artifacts']
    repeated = context.prepare_action_messages(messages, model='test', tools=tools, user_id=1)
    assert json.loads(repeated[1]['content'])['research_artifacts']['artifact_ref'] == descriptor['artifact_ref']
    assert len(prepared[1]['content'].encode()) < 1024
    repository = artifacts.ToolResultArtifactRepository()
    cursor = '0'
    pages = []
    for _ in range(100):
        page = _execute_one('read_tool_artifact', {'artifact_ref': descriptor['artifact_ref'],
                            'cursor': cursor, 'limit': 16384}, user_id=1,
                            repository=repository, model='test', token_budget=8000)
        assert page['status'] == 'ok'
        pages.append(page['content'])
        cursor = page.get('nextCursor')
        if not cursor:
            break
    else:
        pytest.fail('source cursor failed to reach original end')
    original = ''.join(pages)
    assert len(pages) > 1
    assert json.loads(original) == source
    assert hashlib.sha256(original.encode()).hexdigest() == descriptor['source_digest']
    assert len(original) == descriptor['total_chars']
    foreign = _execute_one('read_tool_artifact', {'artifact_ref': descriptor['artifact_ref'], 'limit': 100},
                           user_id=2, repository=repository, model='test', token_budget=8000)
    assert foreign['status'] == 'error'
    assert not foreign.get('content')


@pytest.mark.parametrize('changed', [False, True])
def test_expired_source_restores_exact_original_or_refuses_drift(storage, monkeypatch, changed):
    import time
    import lib.tool_results.tool_result_artifacts as artifacts
    supervisor, _ = storage
    monkeypatch.setattr(artifacts, 'get_storage_client', lambda **kwargs: supervisor.client)
    monkeypatch.setattr(context, 'action_window', lambda model: 32000)
    monkeypatch.setattr('lib.token_counter.count_text', lambda text, **kwargs: len(text) // 4)
    source = {'source_task_id': 'original', 'survey_markdown': '012345证据😀' * 30000}
    messages = [{'role':'system', 'content':'system'}, {'role':'user', 'content':json.dumps({'research_artifacts':source})}]
    ordinary_class = artifacts.ToolResultArtifactRepository
    past = int(time.time() * 1000) - artifacts.DEFAULT_TTL_MS - 10000
    class PastWrites(ordinary_class):
        def put(self, **kwargs):
            return super().put(**kwargs, now_ms=past)
    monkeypatch.setattr(artifacts, 'ToolResultArtifactRepository', PastWrites)
    prepared = context.prepare_action_messages(messages, model='test',
        tools=[{'function':{'name':'read_tool_artifact'}}], user_id=1)
    descriptor = json.loads(prepared[1]['content'])['research_artifacts']
    reference = descriptor['artifact_ref']
    monkeypatch.setattr(artifacts, 'ToolResultArtifactRepository', ordinary_class)
    repository = ordinary_class()
    first = repository.read_range(user_id=1, artifact_ref=reference, limit=100, now_ms=past)
    cursor = first['nextCursor']
    assert repository.read_range(user_id=1, artifact_ref=reference) is None
    if changed:
        with pytest.raises(ValueError, match='changed'):
            context.restore_source_reference(prepared, user_id=1, artifact_ref=reference,
                load_source=lambda: {**source, 'survey_markdown':'different evidence'})
        assert repository.read_range(user_id=1, artifact_ref=reference) is None
    else:
        assert context.restore_source_reference(prepared, user_id=1, artifact_ref=reference, load_source=lambda:source)
        from lib.tasks_pkg.handlers.tool_result_artifacts import _execute_one
        def read(cursor):
            return _execute_one('read_tool_artifact', {'artifact_ref':reference, 'cursor':cursor, 'limit':100},
                                user_id=1, repository=repository, model='test', token_budget=8000)
        old = read(cursor)
        if str(cursor).startswith('trq1.'):
            assert old['status'] == 'error'  # expired signed authorization remains expired
        else:
            assert old['status'] == 'ok'  # an owned numeric range is still an allowed read
        restarted = read('0')
        assert restarted['status'] == 'ok'
        continued = read(restarted['nextCursor'])
        assert continued['status'] == 'ok'
        original = json.dumps(source, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        assert restarted['content'] == first['content']
        assert original.startswith(restarted['content'] + continued['content'])
        assert not context.restore_source_reference(prepared, user_id=1, artifact_ref=reference,
            load_source=lambda: pytest.fail('available source unnecessarily reloaded'))
    assert json.loads(prepared[1]['content'])['research_artifacts'] == descriptor


def test_unrelated_reference_cannot_trigger_origin_reads_or_cache_writes(monkeypatch):
    import lib.tool_results.tool_result_artifacts as artifacts
    monkeypatch.setattr(artifacts.ToolResultArtifactRepository, 'read_range', lambda *a, **k:
        pytest.fail('unrelated reference probed'))
    messages = [{'role':'system','content':'system'}, {'role':'user','content':json.dumps(
        {'research_artifacts':{'delivery':'read_tool_artifact', 'artifact_ref':'owned-ref'}})}]
    assert not context.restore_source_reference(messages, user_id=1, artifact_ref='different-ref',
        load_source=lambda:pytest.fail('unrelated original loaded'))
