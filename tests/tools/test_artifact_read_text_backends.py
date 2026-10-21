"""Read-text delivery through real query authorities, dispatch and durable recovery."""
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT
from lib.tools.result_envelope import tool_result_observation
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_artifact_read_text_projection import BODY, decode_read_view
from tests.tools.test_tool_result_query_backends import (
    repo as repo, storage as storage, manifest, put,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('canonical', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_owned_read_body_reaches_dispatch_and_remains_recoverable(
        repo, storage, observations, monkeypatch, mode, canonical, batch):
    reference = (manifest(repo, [{'kind': 'text', 'text': BODY}], ttl=120000)
                 if canonical else put(repo, BODY, ttl=120000))
    missing = 'tool-result:' + '0' * 64
    arguments = {'artifact_ref': reference, 'limit': 65536}
    if batch:
        arguments = {'reads': [arguments, {'artifact_ref': missing}]}
    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', lambda: repo)
    task = task_for(handler._handle_tool_result_artifact, mode, name='read_tool_artifact')
    task['_toolContractDocumentsByName'] = {'read_tool_artifact': READ_TOOL_ARTIFACT.search_document()}
    _, row, _ = _build_tool_round_entry('read_tool_artifact', arguments, 'read-page',
                                       json.dumps(arguments), 1, False)
    row['llmRound'] = 0
    task['toolRounds'] = [row]
    call = {'id': 'read-page', 'type': 'function', 'function': {
        'name': 'read_tool_artifact', 'arguments': json.dumps(arguments)}}
    messages, captured = [], []
    execute_tool_pipeline(task, [(call, 'read_tool_artifact', 'read-page', arguments, 1, row, None)],
        cfg=task['config'], project_path=None, project_enabled=False, tool_list=[],
        messages=messages, all_search_results_text=[], round_num=0, model='fixture',
        internal_result_sink=SimpleNamespace(capture=lambda _id, value: captured.append(value)))

    text = messages[0]['content']
    if mode == 'v2':
        assert BODY in text
        view = tool_result_observation(text, row['toolResultEvidence'])['summary'] if batch else text
        result = decode_read_view(view)
    else:
        # Legacy mode explicitly retains its original producer JSON contract.
        result = json.loads(text)
        assert json.loads(captured[0].text()) == result
    page = result['items'][0] if batch else result
    assert page['content'] == BODY and page['artifactRef'] == reference
    assert row['status'] == ('error' if batch else 'done')
    assert [entry['outcome'] for entry in observations[1]] == ['error' if batch else 'ok']
    assert captured[0].execution_status == ('error' if batch else 'ok')
    assert not task.get('_activeToolContexts')
    if batch:
        assert result['items'][1]['error']['code'] == 'artifact_unavailable'

    evidence = row.get('toolResultEvidence')
    if mode == 'v2':
        assert evidence['viewCoverage'] == 'complete' and evidence['retentionCoverage'] == 'complete'
    storage.stop()
    storage.start()
    recovery_ref = evidence['artifactRef'] if mode == 'v2' else reference
    if mode == 'v2':
        retained = repo.read_range(user_id=1, artifact_ref=recovery_ref, limit=65536)
        original = json.loads(retained['content'])
        retained_page = original['items'][0] if batch else original
    else:
        retained_page = page
    assert retained_page['content'] == BODY and retained_page['resumeCursor'].startswith('trq3.')
    replay = repo.read_range(user_id=1, artifact_ref=reference, offset=retained_page['resumeCursor'], limit=65536)
    assert replay['content'] == BODY
    from lib.agent_core.storage_errors import StorageError
    try:
        denied = repo.read_range(user_id=2, artifact_ref=recovery_ref)
    except StorageError as error:
        assert error.code == 'database_forbidden' and not error.retryable
    else:
        assert denied is None
