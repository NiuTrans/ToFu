"""Typed storage conditions retain facts without granting whole-tool replay."""
from __future__ import annotations

import pytest
from copy import deepcopy
import json

from lib.agent_core.storage_errors import StorageError
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch, observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('policy', ['read_only', 'idempotent'])
@pytest.mark.parametrize('storage_code', ['database_busy', 'database_timeout', 'database_unavailable'])
@pytest.mark.parametrize('wire_retryable', [False, True])
def test_transient_storage_classification_preserves_admitted_tool_recovery(
        mode, policy, storage_code, wire_retryable, observations):
    calls = []
    failure = StorageError.from_payload(StorageError(storage_code, 'authority pressure',
        retryable=wire_retryable, retry_after_ms=1250).to_payload())

    def handler(task, tc, name, call_id, *args):
        calls.append(call_id)
        raise failure

    task = recovery_task(handler, policy, mode)
    row, messages, captured = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error.code == storage_code and error.retryable is True
    assert '1250 milliseconds' in error.next_action
    assert 'gateway' in error.next_action and 'authorization' in error.next_action
    assert 'authority pressure' in error.message
    assert calls == ['occurrence-1'], 'Projection must not retry the handler itself'
    assert row['status'] == captured[0].execution_status == 'error'
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('policy', ['reconcile', 'missing', 'contradictory', 'changed'])
def test_child_storage_receipt_cannot_grant_whole_tool_replay(mode, policy, observations):
    def handler(task, *args):
        if policy == 'changed':
            task['_toolContractDocumentsByName']['fetch_url'].update(
                effectRecovery='idempotent', idempotency='idempotent')
        raise StorageError('database_busy', 'one storage request was not sent',
            retryable=True, retry_after_ms=1250, operation_id='child-storage-command',
            request_not_dispatched=True)

    task = recovery_task(handler, 'read_only' if policy == 'changed' else policy, mode)
    row, messages, _ = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_effect_unknown' and error.retryable is False
    assert 'database_busy' in error.message
    assert row['executionEffect']['operationId'] in error.message
    assert 'child-storage-command' not in error.message
    assert 'Do not repeat' in error.next_action and '1250' not in error.next_action
    assert row['executionEffect']['state'] == 'unknown'


@pytest.mark.parametrize('storage_code', [
    'database_integrity', 'database_forbidden', 'database_protocol_error',
    'database_conflict', 'database_invalid_payload', 'database_not_found',
])
def test_nontransient_storage_code_is_not_overridden_by_retry_flag_or_nested_busy(
        storage_code, observations):
    def handler(*args):
        raise StorageError(storage_code, 'final storage verdict', retryable=True,
            retry_after_ms=1250) from StorageError('database_busy', 'earlier pressure')

    row, messages, _ = dispatch(recovery_task(handler, 'read_only'))
    error = tool_result_error(messages[0]['content'])
    assert error.code == storage_code and error.retryable is False
    assert '1250' not in error.next_action
    assert row['status'] == 'error'


@pytest.mark.parametrize('wrapped', [False, True])
def test_storage_shaped_application_error_does_not_inherit_recovery(wrapped, observations):
    failure = RuntimeError('database_busy: try again')
    failure.code, failure.retryable, failure.retry_after_ms = 'database_busy', True, 1250
    if wrapped:
        failure.__cause__ = StorageError('database_busy', 'nested pressure')

    def handler(*args):
        raise failure

    _, messages, _ = dispatch(recovery_task(handler, 'read_only'))
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_execution_failed' and error.retryable is False
    assert '1250' not in error.next_action


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_nested_gateway_keeps_successful_sibling_and_storage_recovery(mode, observations):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    entered = []

    def handler(task, tc, name, call_id, *args):
        entered.append(call_id)
        if call_id == 'busy':
            raise StorageError('database_busy', 'authority pressure', retryable=False)
        return call_id, 'complete sibling evidence', False

    task = recovery_task(handler, 'read_only', mode)
    task['toolRounds'] = []
    task['_executable_tool_catalog'] = [{'type': 'function', 'function': {
        'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    calls = [{'id': identity, 'type': 'function', 'function': {'name': 'fetch_url',
        'arguments': json.dumps({'url': 'https://example.test/' + identity})}}
        for identity in ('busy', 'healthy')]
    results = _execute_call_batch(task, calls, cfg={'autoApply': True},
        project_path=None, project_enabled=False, model='test-model', llm_round=1)
    assert sorted(entered) == ['busy', 'healthy']
    assert [result['status'] for result in results] == ['error', 'done']
    error = tool_result_error(results[0]['error'])
    assert error.code == 'database_busy' and error.retryable is True
    assert results[1]['output'] == 'complete sibling evidence'
    assert [result.canonical.execution_status for result in results] == ['error', 'ok']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('collector_gap', [False, True])
def test_collection_failure_preserves_the_storage_code_and_owned_recovery(
        collector_gap, observations, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline
    effect = {'operationId': 'owned-operation', 'scope': 'process',
              'recovery': 'read_only', 'state': 'unknown'}

    def failed_worker(task, tc, name, call_id, arguments, number, row, *args, **kwargs):
        row['executionEffect'] = deepcopy(effect)
        raise StorageError('database_timeout', 'authority deadline elapsed', retryable=False)

    def omit_completed(futures, *args):
        for future in futures:
            assert future.exception(timeout=5) is not None
        return iter(())

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', failed_worker)
    if collector_gap:
        monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', omit_completed)
    row, messages, captured = dispatch(recovery_task(None, 'read_only'))
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'database_timeout' and error.retryable is True
    assert row['executionEffect'] == effect
    assert row['status'] == captured[0].execution_status == 'error'
    assert len(observations[1]) == 1
