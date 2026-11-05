"""Owner Stop stops Artifact source retries through the real tool pipeline."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.storage_authority_sql.operations_pkg._artifacts import (
    _tool_result_artifact_read, _tool_result_artifact_search,
)
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_artifact_decoded_results import artifact_store as artifact_store, save

pytestmark = pytest.mark.unit


def dispatch(task, operation, arguments, *, call_id='artifact-stop', round_num=0):
    encoded = json.dumps(arguments)
    _, row, _ = _build_tool_round_entry(operation, arguments, call_id, encoded, round_num + 1, False)
    row['llmRound'] = round_num
    task.setdefault('toolRounds', []).append(row)
    call = {'id': call_id, 'type': 'function', 'function': {
        'name': operation, 'arguments': encoded}}
    messages, captured = [], []
    execute_tool_pipeline(task, [(call, operation, call_id, arguments, round_num + 1, row, None)],
        cfg=task['config'], project_path=None, project_enabled=False, tool_list=[],
        messages=messages, all_search_results_text=[], round_num=round_num, model='fixture',
        internal_result_sink=SimpleNamespace(capture=lambda _, value: captured.append(value)))
    return row, messages, captured


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('signal', ['flag', 'event'])
@pytest.mark.parametrize('stop_at', [1, 2])
def test_owner_stop_after_source_page_prevents_further_budget_retry(
        artifact_store, observations, monkeypatch, mode, operation, signal, stop_at):
    is_read = operation == 'read_tool_artifact'
    body = ('precise evidence ' * 1000 if is_read else ''.join(
        f'part {i} needle ' + f'distinct evidence {i} ' * 20 + '\n' for i in range(20)))
    reference = save(artifact_store, body, canonical=True)
    arguments = {'artifact_ref': reference, 'limit': 65536 if is_read else 20,
                 **({} if is_read else {'query': 'needle'})}
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task['_userId'] = 17
    task['_toolContractDocumentsByName'] = {operation: handler._CONTRACTS[operation].search_document()}
    queries = []
    source_query = _tool_result_artifact_read if is_read else _tool_result_artifact_search

    def query(**values):
        queries.append(values)
        page = source_query(artifact_store, {**values, 'now_ms': 2})
        if len(queries) == stop_at:
            if signal == 'flag':
                task['aborted'] = True
            else:
                task['abort_event'].set()
            # The stopped fitting loop must not try to reduce metadata that
            # remains too large even after the first narrower query.
            page['requiredEvidence'] = 'source metadata ' * 1000
        return page

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query, search=query))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(handler, '_RESPONSE_TOKEN_BUDGET', 1400)
    row, messages, captured = dispatch(task, operation, arguments)

    assert len(queries) == stop_at, 'A stopped owner must not issue another source query to fit a page'
    assert row['status'] == 'aborted'
    assert captured[0].execution_status == 'aborted'
    assert 'artifact_store_unavailable' not in messages[0]['content']
    assert [event['outcome'] for event in observations[1]] == ['aborted']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_stop_before_batch_refit_issues_no_additional_source_queries(
        artifact_store, observations, monkeypatch, mode, operation):
    is_read = operation == 'read_tool_artifact'
    body = ('precise evidence ' * 240 if is_read else ''.join(
        f'part {i} needle ' + f'distinct evidence {i} ' * 8 + '\n' for i in range(8)))
    reference = save(artifact_store, body, canonical=True)
    item = {'artifact_ref': reference, 'limit': 65536 if is_read else 20,
            **({} if is_read else {'query': 'needle'})}
    arguments = {'reads' if is_read else 'searches': [dict(item) for _ in range(8)]}
    task = task_for(handler._handle_tool_result_artifact, mode, name=operation)
    task['_userId'] = 17
    task['_toolContractDocumentsByName'] = {operation: handler._CONTRACTS[operation].search_document()}
    calls = []
    source_query = _tool_result_artifact_read if is_read else _tool_result_artifact_search

    def query(**values):
        with artifact_store.lock:
            calls.append(values)
            return source_query(artifact_store, {**values, 'now_ms': 2})

    count = handler._model_tokens

    def stop_after_batch_count(operation, value, model):
        result = count(operation, value, model)
        if value.get('items') and all('index' in item for item in value['items']):
            assert result > handler._RESPONSE_TOKEN_BUDGET
            task['aborted'] = True
        return result

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query, search=query))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(handler, '_model_tokens', stop_after_batch_count)
    row, messages, captured = dispatch(task, operation, arguments)

    assert len(calls) == 8 and all(values['limit'] == item['limit'] for values in calls)
    assert row['status'] == captured[0].execution_status == 'aborted'
    assert 'artifact_store_unavailable' not in messages[0]['content']
    assert [event['outcome'] for event in observations[1]] == ['aborted']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_stop_joins_running_batch_queries_and_keeps_completed_sibling_receipts(
        artifact_store, observations, monkeypatch, mode):
    fast = save(artifact_store, 'completed evidence', canonical=True)
    slow = save(artifact_store, 'precise evidence ' * 1000, canonical=True)
    entered, release, exited, completed = Event(), Event(), Event(), Event()
    calls, lock = [], Lock()
    task = task_for(handler._handle_tool_result_artifact, mode, name='read_tool_artifact')
    task['_userId'] = 17
    task['_toolContractDocumentsByName'] = {
        'read_tool_artifact': handler._CONTRACTS['read_tool_artifact'].search_document()}

    def query(**values):
        with lock:
            calls.append(values)
        if values['artifact_ref'] == slow:
            entered.set()
            try:
                assert release.wait(5), 'test did not release its owned query'
            finally:
                exited.set()
        with artifact_store.lock:
            result = _tool_result_artifact_read(artifact_store, {**values, 'now_ms': 2})
        if values['artifact_ref'] == fast:
            completed.set()
        return result

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query))
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    # A first, completed tool receipt is independent from the later stopped
    # batch. Stop must join the running batch without rewriting that receipt.
    first_row, first_messages, first_captured = dispatch(
        task, 'read_tool_artifact', {'artifact_ref': fast}, call_id='artifact-complete')
    assert completed.is_set() and first_row['status'] == 'done'
    first_text = first_captured[0].text()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(dispatch, task, 'read_tool_artifact',
            {'reads': [{'artifact_ref': slow}, {'artifact_ref': fast}]}, round_num=1)
        try:
            assert entered.wait(5)
            task['aborted'] = True
            task['abort_event'].set()
            assert not pending.done() and not exited.is_set()
        finally:
            release.set()
        row, messages, captured = pending.result(timeout=5)

    assert exited.is_set()
    assert sum(call['artifact_ref'] == slow for call in calls) == 1
    assert row['status'] == captured[0].execution_status == 'aborted'
    assert first_row['status'] == 'done' and first_captured[0].text() == first_text
    assert 'completed evidence' in first_messages[0]['content']
    assert [event['outcome'] for event in observations[1]] == ['ok', 'aborted']
    assert 'artifact_store_unavailable' not in messages[0]['content']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_source_control_failure_without_owner_stop_keeps_nonretryable_failure(
        artifact_store, observations, monkeypatch, operation):
    from lib.llm.llm_errors import AbortedError
    is_read = operation == 'read_tool_artifact'
    good, failed = save(artifact_store, 'tiny needle', canonical=True), 'tool-result:' + 'f' * 64
    source_query = _tool_result_artifact_read if is_read else _tool_result_artifact_search
    arguments = {'reads' if is_read else 'searches': [
        {'artifact_ref': reference, **({} if is_read else {'query': 'needle'})}
        for reference in (good, failed)]}

    def query(**values):
        if values['artifact_ref'] == failed:
            raise AbortedError('source operation ended independently')
        return source_query(artifact_store, {**values, 'now_ms': 2})

    task = task_for(handler._handle_tool_result_artifact, name=operation)
    task['_userId'] = 17
    task['_toolContractDocumentsByName'] = {operation: handler._CONTRACTS[operation].search_document()}
    monkeypatch.setattr(handler, 'ToolResultArtifactRepository',
                        lambda: SimpleNamespace(read_range=query, search=query))
    row, _, captured = dispatch(task, operation, arguments)
    result = json.loads(captured[0].text())
    assert result['items'][0]['status'] == 'ok'
    failure = result['items'][1]
    assert failure['artifactRef'] == failed
    assert failure['error']['code'] == 'artifact_store_unavailable'
    assert failure['error']['retryable'] is False
    assert row['status'] == captured[0].execution_status == 'error'
    assert not task['aborted'] and not task.get('_activeToolContexts')
