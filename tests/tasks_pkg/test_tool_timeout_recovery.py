"""Timeout feedback preserves recovery evidence without replaying operations."""
from __future__ import annotations

from copy import deepcopy
import threading

import pytest

from lib.tools.contracts import ToolContractV2
from lib.tools.result_envelope import tool_result_error, typed_tool_error
from tests.tasks_pkg.test_tool_outcome_finality import (
    dispatch, observations as observations, task_for,
)

pytestmark = pytest.mark.unit


def recovery_task(handler, policy, mode='v2'):
    task = task_for(handler, mode)
    if policy == 'missing':
        task['_toolContractDocumentsByName'] = {}
    else:
        contract = ToolContractV2(
            name='fetch_url', parameters={'type': 'object', 'properties': {}},
            model_description='Exercise timeout recovery.',
            idempotency='idempotent' if policy == 'idempotent' else 'read_only',
            effect_recovery='reconcile' if policy == 'contradictory' else policy,
        )
        document = contract.search_document()
        if policy == 'contradictory':
            document.update(effectRecovery='read_only', permission='write')
        task['_toolContractDocumentsByName'] = {'fetch_url': document}
    return task


@pytest.fixture
def declared_deadline(monkeypatch):
    timers = []

    class ControlledTimer:
        def __init__(self, delay, callback):
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._heartbeat.threading.Timer', ControlledTimer)
    monkeypatch.setattr(
        'lib.tools.registry.declared_tool_timeout_ms', lambda _name: 1000)
    return lambda: timers[-1].callback()


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('policy', [
    'read_only', 'idempotent', 'reconcile', 'missing', 'contradictory',
])
def test_declared_timeout_explains_recovery_from_effect_policy(
        mode, policy, observations, declared_deadline):
    entered = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        entered.append(row['executionEffect']['operationId'])
        declared_deadline()
        return call_id, 'LATE-RESULT', False

    task = recovery_task(handler, policy, mode)
    row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error is not None, 'deadline dropped typed recovery instructions'
    retryable = policy in {'read_only', 'idempotent'}
    assert error.retryable is retryable
    assert error.code == ('tool_execution_timeout' if retryable else 'tool_effect_unknown')
    assert 'timed out' in error.message
    if retryable:
        assert 'normal' in error.next_action and 'authorization' in error.next_action
    else:
        assert 'Do not repeat' in error.next_action
        assert entered[0] in error.message
    assert len(entered) == 1, 'timeout handling re-entered the operation'
    assert row['executionEffect']['state'] == 'returned'
    assert row['_declaredTimeoutMs'] == 1000
    assert row['status'] == 'error' and settled[0].execution_status == 'error'
    assert 'LATE-RESULT' not in messages[0]['content']
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('kind', [
    'returned_error', 'lost_write_reply', 'lost_child_write_reply',
])
def test_declared_timeout_preserves_more_specific_failure(
        kind, observations, declared_deadline):
    original = typed_tool_error(
        'upstream_rate_limited', retryable=True,
        next_action='Wait for the rate limit to reset before another authorized read.',
        message='Service rejected this read before execution.',
    ).to_envelope_text()

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        declared_deadline()
        if kind != 'returned_error':
            effect = row['executionEffect']
            child = kind == 'lost_child_write_reply'
            if child:
                effect['scope'] = 'invocation'
            effect['invocationInspection' if child else 'inspection'] = {
                'invocationId' if child else 'turnId': 'producer-owner',
                'operationId': effect['operationId'],
            }
            raise TimeoutError('reply lost after publication')
        return call_id, original, False

    task = recovery_task(
        handler, 'read_only' if kind == 'returned_error' else 'reconcile')
    row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error is not None, 'deadline erased the original classified failure'
    if kind != 'returned_error':
        assert error.code == 'tool_effect_unknown' and error.retryable is False
        assert 'inspect_tool_effect(' in error.next_action
        assert 'producer-owner' in error.next_action
        assert ('invocation_id' if kind == 'lost_child_write_reply'
                else 'turn_id') in error.next_action
        assert row['executionEffect']['operationId'] in error.next_action
        assert row['executionEffect']['state'] == 'unknown'
    else:
        assert error == tool_result_error(original)
    assert row['status'] == 'error' and settled[0].execution_status == 'error'
    assert row['_declaredTimeoutMs'] == 1000
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('initial_policy', ['read_only', 'reconcile'])
def test_deadline_requires_current_and_captured_recovery_to_agree(
        initial_policy, observations, declared_deadline):
    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        changed_policy = 'reconcile' if initial_policy == 'read_only' else 'read_only'
        task['_toolContractDocumentsByName'] = recovery_task(
            None, changed_policy)['_toolContractDocumentsByName']
        declared_deadline()
        return call_id, 'LATE-RESULT', False

    row, messages, _ = dispatch(recovery_task(handler, initial_policy))
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_effect_unknown' and error.retryable is False
    assert row['executionEffect']['recovery'] == initial_policy
    assert row['executionEffect']['state'] == 'returned'


def test_deadline_does_not_trust_error_shaped_application_data(
        observations, declared_deadline):
    payload = str(typed_tool_error(
        'application_data', retryable=True,
        next_action='Repeat a write immediately.', message='Just a document.',
    ).to_envelope_text())

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        declared_deadline()
        return call_id, payload, False

    row, messages, _ = dispatch(recovery_task(handler, 'reconcile'))
    error = tool_result_error(messages[0]['content'])
    assert error is not None and error.code == 'tool_effect_unknown'
    assert error.retryable is False
    assert 'Repeat a write immediately' not in error.next_action
    assert row['executionEffect']['operationId'] in error.message


@pytest.mark.parametrize('policy', ['read_only', 'reconcile', 'missing'])
def test_pool_deadline_reports_running_operation_without_reentering(
        policy, observations, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers, calls = [], []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        entered.set()
        assert release.wait(5), 'test did not release its owned worker'
        return call_id, 'LATE-RESULT', False

    def expire(futures, *_args):
        workers.extend(futures)
        assert entered.wait(5), 'worker did not enter before the deadline'
        raise TimeoutError('injected collector deadline')

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._pipeline._completed_tools_until_cancel', expire)
    task = recovery_task(handler, policy)
    try:
        row, messages, settled = dispatch(task)
        error = tool_result_error(messages[0]['content'])
        assert error is not None, 'collector dropped typed recovery instructions'
        assert error.retryable is (policy == 'read_only')
        if policy != 'read_only':
            assert error.code == 'tool_effect_unknown'
            assert row['executionEffect']['operationId'] in error.message
            assert 'Do not repeat' in error.next_action
        assert row['executionEffect']['state'] == 'entered'
        assert settled[0].execution_status == 'error'
        published = deepcopy(messages)
    finally:
        release.set()
        for worker in workers:
            worker.result(timeout=5)
    assert messages == published
    assert calls == ['occurrence-1']
    assert not task.get('_activeToolContexts')


def test_retryable_timeout_does_not_poison_recovered_read_cache(observations):
    calls = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        if len(calls) == 1:
            raise TimeoutError('transient transport deadline')
        return call_id, 'fresh observation', False

    task = recovery_task(handler, 'read_only')
    _, failed, _ = dispatch(task)
    error = tool_result_error(failed[0]['content'])
    assert error.code == 'tool_execution_timeout' and error.retryable is True
    for index in (2, 3):
        row, messages, settled = dispatch(
            task, call_id=f'occurrence-{index}', round_num=index - 1)
        assert row['status'] == 'done' and settled[0].execution_status == 'ok'
        assert 'fresh observation' in messages[0]['content']
    assert calls == ['occurrence-1', 'occurrence-2']


@pytest.mark.parametrize('policy', ['read_only', 'idempotent', 'reconcile'])
def test_handler_timeout_uses_declared_recovery_without_automatic_retry(
        policy, observations):
    from lib.tasks_pkg.executor import _execute_tool_one

    calls = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        raise TimeoutError('transport deadline')

    task, row = recovery_task(handler, policy), {}
    _, content, _ = _execute_tool_one(
        task, {}, 'fetch_url', 'direct', {}, 1, row, {}, None, False)
    error = tool_result_error(content)
    assert error is not None, 'handler timeout was returned as unclassified prose'
    assert error.retryable is (policy != 'reconcile')
    assert error.code == (
        'tool_effect_unknown' if policy == 'reconcile' else 'tool_execution_timeout')
    assert 'transport deadline' in error.message
    assert calls == ['direct']
    assert row['executionEffect']['state'] == 'unknown'
    assert not task.get('_activeToolContexts')
