"""Recover completed receipts without replaying effects or inventing tool absence."""
from copy import deepcopy
from functools import partial
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.tool_dispatch import _pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2
from lib.tools.effect_recovery import effect_recovery_policy
from lib.tools.result_envelope import tool_result_error, typed_tool_error
from tests.tasks_pkg.test_tool_outcome_finality import (
    dispatch, observations, task_for,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('failed', [False, True])
def test_completed_receipt_survives_a_collector_gap(
        mode, failed, observations, monkeypatch):
    calls = []
    content = (typed_tool_error(
        'source_unavailable', retryable=True, next_action='Read another source.',
        message='original source failure').to_envelope_text()
        if failed else 'original completed observation')

    def handler(task, tc, name, call_id, *args):
        calls.append(call_id)
        return call_id, content, False

    # Fault injection at collection, after the real executor has returned.
    # No handler, result capture or settlement implementation is replaced.
    def omit_completed(futures, *_args):
        for future in futures:
            future.result(timeout=5)
        return iter(())

    monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', omit_completed)
    task = task_for(handler, mode)
    row, messages, settled = dispatch(task)

    assert calls == ['occurrence-1'], 'collection recovery re-executed the tool'
    assert row['status'] == ('error' if failed else 'done')
    assert settled[0].execution_status == ('error' if failed else 'ok')
    assert 'Unknown tool' not in messages[0]['content']
    if failed:
        assert tool_result_error(messages[0]['content']).code == 'source_unavailable'
    else:
        assert 'original completed observation' in messages[0]['content']
    assert len(observations[1]) == 1
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_collector_gap_retains_sibling_order_and_exactly_once_settlement(
        mode, observations, monkeypatch):
    calls = []

    def handler(task, tc, name, call_id, *args):
        calls.append(call_id)
        return call_id, f'observation-{call_id}', call_id == 'first'

    def omit_first(futures, *_args):
        completed = list(futures)
        for future in completed:
            future.result(timeout=5)
        return iter(completed[1:])

    monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', omit_first)
    task = task_for(handler, mode)
    parsed = []
    for number, call_id in enumerate(['first', 'second'], 1):
        arguments = {'url': f'https://example.test/{call_id}'}
        _, row, _ = _build_tool_round_entry(
            'fetch_url', arguments, call_id, '{}', number, False)
        row['llmRound'] = 0
        call = {'id': call_id, 'type': 'function',
                'function': {'name': 'fetch_url', 'arguments': '{}'}}
        parsed.append((call, 'fetch_url', call_id, arguments, number, row, None))
    task['toolRounds'] = [item[5] for item in parsed]
    messages, captured, search_results = [], [], []
    _pipeline.execute_tool_pipeline(
        task, parsed, {}, None, False, [], messages, search_results, 0, 'test-model',
        internal_result_sink=SimpleNamespace(
            capture=lambda call_id, value: captured.append((call_id, value))))

    assert sorted(calls) == ['first', 'second']
    assert [message['tool_call_id'] for message in messages] == ['first', 'second']
    assert all(f'observation-{message["tool_call_id"]}' in message['content']
               for message in messages)
    assert sorted(call_id for call_id, _ in captured) == ['first', 'second']
    assert all(value.execution_status == 'ok' for _, value in captured)
    assert [row['status'] for row in task['toolRounds']] == ['done', 'done']
    assert search_results == ['observation-first']
    assert len(observations[1]) == 2


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('declaration,retryable', [
    ('read', True), ('idempotent', True), ('write', False),
    ('absent', False), ('contradictory', False), ('changed_after_entry', False),
])
def test_unrecoverable_collection_gap_has_truthful_policy_scoped_feedback(
        mode, declaration, retryable, observations):
    task = task_for(lambda *_args: pytest.fail('missing receipt must not replay'), mode)
    documents = task['_toolContractDocumentsByName']
    if declaration == 'absent':
        documents.clear()
    elif declaration == 'contradictory':
        documents['fetch_url']['permission'] = 'write'
    elif declaration in {'write', 'idempotent'}:
        documents['fetch_url'] = ToolContractV2(
            name='fetch_url', parameters={'type': 'object', 'properties': {}},
            model_description='Declared operation.', permission='write',
            idempotency='idempotent' if declaration == 'idempotent' else 'non_idempotent',
        ).search_document()
    arguments = {'url': 'https://example.test/missing'}
    _, row, _ = _build_tool_round_entry('fetch_url', arguments, 'missing', '{}', 1, False)
    # A collection failure must not manufacture an execution-state transition.
    row['executionEffect'] = {
        'operationId': 'existing-operation', 'scope': 'process',
        'state': 'returned',
        'recovery': ('reconcile' if declaration == 'changed_after_entry' else
                     effect_recovery_policy(documents, 'fetch_url')),
    }
    effect = deepcopy(row['executionEffect'])
    task['toolRounds'] = [row]
    call = {'id': 'missing', 'type': 'function',
            'function': {'name': 'fetch_url', 'arguments': '{}'}}
    messages, captured = [], []
    settle = partial(
        _pipeline._settle_tool_result, settled_results={},
        internal_result_sink=SimpleNamespace(capture=lambda _id, value: captured.append(value)))
    _pipeline._append_ordered_tool_replay(
        [], {}, frozenset(), [], [], settle, [], messages, 'test-model',
        [(call, 'fetch_url', 'missing', arguments, 1, row, None)],
        0, task, task['id'], {}, {},
    )

    error = tool_result_error(messages[0]['content'])
    assert error is not None, 'collector failure lost its typed recovery guidance'
    assert error.code == 'tool_result_missing'
    assert error.retryable is retryable
    assert 'fetch_url' in error.message and 'missing' in error.message
    assert 'Unknown tool' not in messages[0]['content']
    assert 'independent' in error.next_action
    assert row['executionEffect'] == effect
    assert row['status'] == 'error' and captured[0].execution_status == 'error'
    assert len(observations[1]) == 1
