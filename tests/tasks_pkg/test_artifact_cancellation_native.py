"""Native parent Stop joins an actual Artifact handler without source retries."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import ToolRequest
from lib.identity import PrincipalContext
from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.native_gateway import NativeGateway
from lib.storage_authority_sql.operations_pkg._artifacts import (
    _tool_result_artifact_read, _tool_result_artifact_search,
)
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_artifact_decoded_results import artifact_store as artifact_store, save

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_native_parent_stop_reaches_query_fit_before_another_source_read(
        artifact_store, observations, monkeypatch, mode, operation):
    is_read = operation == 'read_tool_artifact'
    body = ('precise evidence ' * 1000 if is_read else ''.join(
        f'part {i} needle ' + f'distinct evidence {i} ' * 20 + '\n' for i in range(20)))
    reference = save(artifact_store, body, canonical=True)
    arguments = {'artifact_ref': reference, 'limit': 65536 if is_read else 20,
                 **({} if is_read else {'query': 'needle'})}
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task['_userId'] = 17
    task['abort_event'] = Event()
    task['toolRounds'] = []
    contract = handler._CONTRACTS[operation]
    task['_toolContractDocumentsByName'] = {operation: contract.search_document()}
    task['_executable_tool_catalog'] = [contract.provider_schema()]
    principal = PrincipalContext.user(subject_id='artifact-native-stop', owner_user_id=17)
    entered, release, exited = Event(), Event(), Event()
    queries = []
    source_query = _tool_result_artifact_read if is_read else _tool_result_artifact_search

    def query(**values):
        queries.append(values)
        page = source_query(artifact_store, {**values, 'now_ms': 2})
        entered.set()
        try:
            assert release.wait(5), 'test did not release its owned query'
        finally:
            exited.set()
        return page

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query, search=query))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))

    async def run(executor):
        gateway = NativeGateway(principal=principal, task=task, executor=executor,
            configuration=task['config'], tool_list=task['_executable_tool_catalog'],
            permissions={operation: 'read'}, project_path=None, project_enabled=False,
            model='fixture', round_source=lambda: 0, decision_policy=None,
            pending_calls=2, pending_bytes=65536)
        stop = asyncio.Event()
        request = ToolRequest(principal, task['id'], 'artifact-native-stop', operation,
                              arguments, cancelled=stop, model_round=0)
        work = asyncio.create_task(gateway.execute(request))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            stop.set()
            assert await asyncio.to_thread(task['abort_event'].wait, 5)
            assert task['aborted'] and not work.done() and not exited.is_set()
        finally:
            release.set()
            try:
                feedback = await asyncio.wait_for(work, 5)
            finally:
                gateway.close()
        return feedback

    with ThreadPoolExecutor(max_workers=1) as executor:
        feedback = asyncio.run(run(executor))

    assert exited.is_set() and len(queries) == 1
    assert len(task['toolRounds']) == 1 and task['toolRounds'][0]['status'] == 'aborted'
    assert 'artifact_store_unavailable' not in feedback
    assert [event['outcome'] for event in observations[1]] == ['aborted']
    assert not task.get('_activeToolContexts')
