"""Uncaught tool failures carry factual recovery through real settlement."""
from copy import deepcopy
import json

import pytest

from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch, observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('failure,code,advice', [
    (ConnectionResetError('peer disconnected'), 'tool_connection_failed', 'gateway'),
    (FileNotFoundError('source disappeared'), 'tool_resource_missing', 'locate'),
    (PermissionError('interpreter is not executable'), 'tool_permission_denied', 'authorized'),
    (ValueError('runtime environment incomplete'), 'tool_execution_failed', 'repair'),
    (RuntimeError('429 timeout permission denied'), 'tool_execution_failed', 'repair'),
])
def test_handler_exception_has_typed_recovery_without_losing_its_cause(
        mode, failure, code, advice, observations):
    calls = []

    def handler(task, tc, name, call_id, *args):
        calls.append(call_id)
        raise failure

    task = recovery_task(handler, 'read_only', mode)
    row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error is not None
    assert error.code == code
    assert error.retryable is isinstance(failure, ConnectionError)
    if mode == 'v2':
        assert set(json.loads(messages[0]['content'])) == {
            'status', 'code', 'message', 'retryable', 'nextAction'}
    assert type(failure).__name__ in error.message and str(failure) in error.message
    assert advice in error.next_action.lower() and 'independent' in error.next_action
    assert 'corrected arguments' not in error.next_action
    assert row['status'] == 'error' and settled[0].execution_status == 'error'
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert calls == ['occurrence-1'] and not task.get('_activeToolContexts')


@pytest.mark.parametrize('policy', ['read_only', 'idempotent', 'reconcile', 'missing', 'contradictory'])
def test_connection_retry_uses_authoritative_effect_policy(policy, observations):
    def handler(*args):
        raise ConnectionResetError('connection lost after request')

    task = recovery_task(handler, policy)
    row, messages, _ = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    repeatable = policy in {'read_only', 'idempotent'}
    assert error is not None
    assert error.code == ('tool_connection_failed' if repeatable else 'tool_effect_unknown')
    assert error.retryable is repeatable
    assert row['executionEffect']['state'] == 'unknown'
    if not repeatable:
        assert row['executionEffect']['operationId'] in error.message
        assert 'Do not repeat' in error.next_action


def test_changed_contract_cannot_grant_retry_after_entry(observations):
    def handler(task, *args):
        document = task['_toolContractDocumentsByName']['fetch_url']
        document['effectRecovery'] = document['idempotency'] = 'idempotent'
        raise ConnectionResetError('connection lost')

    row, messages, _ = dispatch(recovery_task(handler, 'read_only'))
    error = tool_result_error(messages[0]['content'])
    assert error is not None and error.code == 'tool_effect_unknown'
    assert not error.retryable
    assert row['executionEffect']['recovery'] == 'read_only'


def test_direct_executor_keeps_typed_failure_and_one_audit(observations):
    from lib.tasks_pkg.executor import _execute_tool_one
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    from lib.tools.result_value import capture_result

    def handler(*args):
        raise ConnectionResetError('direct read disconnected')

    task = recovery_task(handler, 'read_only')
    _, row, _ = _build_tool_round_entry('fetch_url', {}, 'direct', '{}', 1, False)
    call_id, content, _ = _execute_tool_one(
        task, {}, 'fetch_url', 'direct', {}, 1, row, {}, None, False)
    error = tool_result_error(content)
    assert call_id == 'direct' and error.code == 'tool_connection_failed'
    assert error.retryable and capture_result(content).execution_status == 'error'
    assert row['status'] == 'error'
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert not task.get('_activeToolContexts')


def test_nested_gateway_retains_success_and_child_recovery(observations):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch

    def handler(task, tc, name, call_id, *args):
        if call_id == 'broken':
            raise ConnectionResetError('child source disconnected')
        return call_id, 'retained sibling result', False

    task = recovery_task(handler, 'read_only')
    task['toolRounds'] = []
    task['_executable_tool_catalog'] = [{'type': 'function', 'function': {
        'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    calls = [{'id': identity, 'type': 'function', 'function': {'name': 'fetch_url',
        'arguments': json.dumps({'url': 'https://example.test/' + identity})}}
        for identity in ('broken', 'healthy')]
    results = _execute_call_batch(task, calls, cfg={'autoApply': True},
        project_path=None, project_enabled=False, model='test-model', llm_round=1)
    assert [result['status'] for result in results] == ['error', 'done']
    error = tool_result_error(results[0]['error'])
    assert error.code == 'tool_connection_failed' and error.retryable
    assert results[1]['output'] == 'retained sibling result'
    assert [result.canonical.execution_status for result in results] == ['error', 'ok']
    assert sorted(event['outcome'] for event in observations[1]) == ['error', 'ok']
    assert not task.get('_activeToolContexts')


def test_cancelled_execution_never_suggests_resumption(observations):
    from lib.llm.llm_errors import AbortedError

    def handler(*args):
        raise AbortedError('owner stopped execution')

    _, messages, _ = dispatch(recovery_task(handler, 'read_only'))
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_execution_cancelled' and not error.retryable
    assert 'Respect the cancellation' in error.next_action
    assert 'Continue' not in error.next_action


@pytest.mark.parametrize('collector_gap', [False, True])
def test_worker_exception_keeps_effect_evidence_in_every_collection_lane(
        collector_gap, observations, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline

    effect = {'operationId': 'owned-operation', 'scope': 'process',
              'state': 'unknown', 'recovery': 'reconcile'}

    def fail_worker(task, tc, name, call_id, arguments, number, row, *args, **kwargs):
        row['executionEffect'] = deepcopy(effect)
        raise ConnectionResetError('receipt transport failed')

    def omit_completed(futures, *args):
        for future in futures:
            assert future.exception(timeout=5) is not None
        return iter(())

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', fail_worker)
    if collector_gap:
        monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', omit_completed)
    task = recovery_task(None, 'reconcile')
    row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error is not None and error.code == 'tool_effect_unknown'
    assert not error.retryable and 'owned-operation' in error.message
    assert row['executionEffect'] == effect
    assert row['status'] == 'error' and settled[0].execution_status == 'error'
    assert len(observations[1]) == 1
