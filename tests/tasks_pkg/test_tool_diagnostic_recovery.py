"""Unprintable diagnostics cannot replace an owned tool failure or its recovery."""
from copy import deepcopy
import json
import logging

import pytest

from lib.agent_core.storage_errors import StorageError
from lib.llm.llm_errors import AbortedError
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch, observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit


class Unprintable:
    def __str__(self):
        raise ValueError('diagnostic renderer failed')

    def __repr__(self):
        raise ValueError('diagnostic repr failed')


class UnprintableConnection(Unprintable, ConnectionResetError):
    pass


class UnprintableTimeout(Unprintable, TimeoutError):
    pass


class UnprintablePermission(Unprintable, PermissionError):
    pass


class UnprintableValue(Unprintable, ValueError):
    pass


class UnprintableStorage(Unprintable, StorageError):
    pass


class UnprintableCancellation(Unprintable, AbortedError):
    pass


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('failure,code,retryable', [
    (UnprintableConnection('socket closed'), 'tool_connection_failed', True),
    (UnprintableTimeout('deadline elapsed'), 'tool_execution_timeout', True),
    (UnprintablePermission('access refused'), 'tool_permission_denied', False),
    (UnprintableValue('runtime rejected input'), 'tool_execution_failed', False),
    (UnprintableStorage('database_busy', 'authority busy'), 'database_busy', True),
    (RuntimeError(Unprintable()), 'tool_execution_failed', False),
])
def test_unprintable_failure_keeps_original_classification_and_settlement(
        mode, failure, code, retryable, observations):
    calls = []

    def handler(*args):
        calls.append(True)
        raise failure

    task = recovery_task(handler, 'read_only', mode)
    row, messages, captured = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error.code == code and error.retryable is retryable
    assert type(failure).__name__ in error.message
    assert 'unprintable' in error.message.lower()
    assert 'diagnostic renderer failed' not in error.message
    assert row['status'] == captured[0].execution_status == 'error'
    assert [audit['outcome'] for audit in observations[1]] == ['error']
    assert calls == [True] and not task.get('_activeToolContexts')


@pytest.mark.parametrize('policy', ['idempotent', 'reconcile', 'missing', 'contradictory'])
def test_unprintable_failure_retains_whole_tool_replay_authority(policy, observations):
    def handler(*args):
        raise UnprintableConnection('reply lost')

    row, messages, _ = dispatch(recovery_task(handler, policy))
    error = tool_result_error(messages[0]['content'])
    assert error.retryable is (policy == 'idempotent')
    assert error.code == ('tool_connection_failed' if policy == 'idempotent' else 'tool_effect_unknown')
    if policy != 'idempotent':
        assert row['executionEffect']['operationId'] in error.message
        assert 'Do not repeat' in error.next_action
    assert row['executionEffect']['state'] == 'unknown'


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('owner_stop', [False, True])
def test_unprintable_control_failure_keeps_owner_cancellation(mode, owner_stop, observations):
    def handler(task, *args):
        task['aborted'] = owner_stop
        raise UnprintableCancellation('stopped at boundary')

    task = recovery_task(handler, 'read_only', mode)
    row, messages, captured = dispatch(task)
    expected_status = 'aborted' if owner_stop else 'error'
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_execution_cancelled' and error.retryable is False
    assert row['status'] == captured[0].execution_status == expected_status
    assert [audit['outcome'] for audit in observations[1]] == [expected_status]
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('collector_gap', [False, True])
@pytest.mark.parametrize('policy', ['read_only', 'reconcile'])
def test_worker_failure_diagnostics_keep_effects_through_collection(
        collector_gap, policy, observations, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline
    effect = {'operationId': 'original-operation', 'scope': 'process',
              'recovery': policy, 'state': 'unknown'}

    def failed_worker(task, tc, name, call_id, arguments, number, row, *args, **kwargs):
        row['executionEffect'] = deepcopy(effect)
        raise UnprintableConnection('worker lost its connection')

    def omit_completed(futures, *args):
        for future in futures:
            assert future.exception(timeout=5) is not None
        return iter(())

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', failed_worker)
    if collector_gap:
        monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', omit_completed)
    row, messages, captured = dispatch(recovery_task(None, policy))
    error = tool_result_error(messages[0]['content'])
    assert error.code == ('tool_connection_failed' if policy == 'read_only' else 'tool_effect_unknown')
    assert error.retryable is (policy == 'read_only')
    assert row['executionEffect'] == effect
    assert row['status'] == captured[0].execution_status == 'error'
    assert len(observations[1]) == 1


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_nested_gateway_keeps_sibling_evidence_when_diagnostic_text_is_broken(mode, observations):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    entered = []

    def handler(task, tc, name, call_id, *args):
        entered.append(call_id)
        if call_id == 'broken':
            raise UnprintableConnection('peer closed')
        return call_id, 'complete sibling evidence', False

    task = recovery_task(handler, 'read_only', mode)
    task['toolRounds'] = []
    task['_executable_tool_catalog'] = [{'type': 'function', 'function': {
        'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    calls = [{'id': identity, 'type': 'function', 'function': {'name': 'fetch_url',
        'arguments': json.dumps({'url': 'https://example.test/' + identity})}}
        for identity in ('broken', 'healthy')]
    results = _execute_call_batch(task, calls, cfg={'autoApply': True},
        project_path=None, project_enabled=False, model='test-model', llm_round=1)
    assert sorted(entered) == ['broken', 'healthy']
    assert [result['status'] for result in results] == ['error', 'done']
    error = tool_result_error(results[0]['error'])
    assert error.code == 'tool_connection_failed' and error.retryable is True
    assert results[1]['output'] == 'complete sibling evidence'
    assert not task.get('_activeToolContexts')


def test_failed_argument_preview_preserves_original_exception(observations):
    from lib.tasks_pkg.executor._execute import _handler_error_result
    row = {}
    call_id, content, _ = _handler_error_result(recovery_task(None, 'read_only'),
        'fetch_url', 'preview-call', {'unprintable': Unprintable()}, 1, row,
        ConnectionResetError('original transport failure'))
    error = tool_result_error(content)
    assert call_id == 'preview-call'
    assert error.code == 'tool_connection_failed' and error.retryable is True
    assert 'original transport failure' in error.message
    assert 'unprintable' in error.message.lower()
    assert row['status'] == 'error'


@pytest.mark.parametrize('diagnostic_owner', ['audit', 'presentation'])
def test_secondary_unprintable_diagnostic_failure_cannot_replace_tool_result(
        diagnostic_owner, observations, monkeypatch, caplog):
    from lib.tasks_pkg.executor import _execute
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise UnprintableValue('diagnostic observer failed')

    caplog.set_level(logging.DEBUG, logger=_execute.logger.name)
    if diagnostic_owner == 'audit':
        monkeypatch.setattr('lib.log.audit_log', fail)
    else:
        monkeypatch.setattr(_execute, '_finalize_tool_round', fail)
    _, content, _ = _execute._handler_error_result(recovery_task(None, 'read_only'),
        'fetch_url', 'observer-call', {}, 1, {}, ConnectionResetError('original source cause'))
    error = tool_result_error(content)
    assert error.code == 'tool_connection_failed' and error.retryable is True
    assert 'original source cause' in error.message
    assert calls == [True] and 'UnprintableValue' in caplog.text
