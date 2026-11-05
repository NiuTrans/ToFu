"""Cancelled Artifact fits preserve stored evidence on both real authorities."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_artifact_query_cancellation import dispatch
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_tool_result_query_backends import (
    repo as repo, storage as storage, manifest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_stopped_fit_does_not_requery_or_modify_restart_evidence(
        repo, storage, observations, monkeypatch, mode, operation):
    is_read = operation == 'read_tool_artifact'
    if is_read:
        reference = manifest(repo, [{'kind': 'text', 'text': 'precise 中🙂 evidence ' * 1000}], ttl=120000)
    else:
        reference = manifest(repo, [{'kind': 'text', 'text':
            f'part {i} needle ' + f'distinct evidence {i} ' * 35 + '\n'}
            for i in range(20)], ttl=120000)
    arguments = {'artifact_ref': reference, 'limit': 65536 if is_read else 20,
                 **({} if is_read else {'query': 'needle'})}
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task['_toolContractDocumentsByName'] = {operation: handler._CONTRACTS[operation].search_document()}
    source_query = repo.read_range if is_read else repo.search
    calls, pages = [], []

    def query(**values):
        calls.append(values)
        page = source_query(**values)
        pages.append(page)
        task['aborted'] = True
        task['abort_event'].set()
        return page

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query, search=query))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    row, messages, captured = dispatch(task, operation, arguments)

    assert len(calls) == 1
    assert row['status'] == captured[0].execution_status == 'aborted'
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_execution_cancelled' and error.retryable is False
    assert [event['outcome'] for event in observations[1]] == ['aborted']
    assert not task.get('_activeToolContexts')
    storage.stop()
    storage.start()
    assert source_query(**calls[0]) == pages[0]
