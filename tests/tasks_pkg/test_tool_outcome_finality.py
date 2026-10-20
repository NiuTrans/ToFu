"""Actual dispatch settlement owns failure verdicts, cache admission and audits."""
from __future__ import annotations

from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2

pytestmark = pytest.mark.unit


@pytest.fixture
def observations(monkeypatch):
    events, audits = [], []
    emit = lambda task, event: events.append(deepcopy(event))
    monkeypatch.setattr('lib.tasks_pkg.executor._finalize.append_event', emit)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.append_event', emit)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._heartbeat.append_event', emit)
    monkeypatch.setattr(
        'lib.tasks_pkg.tool_call_outcome.audit_log',
        lambda event, **fields: audits.append({'event': event, **fields}))
    return events, audits


def task_for(handler, mode='v2', *, name='fetch_url', permission='read'):
    contract = ToolContractV2(
        name=name, parameters={'type': 'object', 'properties': {}},
        model_description='Exercise one real dispatch occurrence.',
        permission=permission)
    return {
        'id': 'outcome-finality', 'convId': 'outcome-finality-conversation',
        '_userId': 1, 'status': 'running', 'aborted': False, 'model': 'test-model',
        'events': [], 'events_lock': threading.Lock(), '_attended': False,
        'config': {'tools': {'resultEnvelope': mode}},
        '_tool_env': SimpleNamespace(resolve=lambda requested: handler),
        '_toolContractDocumentsByName': {name: contract.search_document()},
    }


def dispatch(task, *, name='fetch_url', call_id='occurrence-1', round_num=0):
    arguments = ({'url': 'https://example.test/result'} if name == 'fetch_url'
                 else {'path': '/tmp/outcome-finality.txt', 'content': 'requested'})
    _, row, _ = _build_tool_round_entry(
        name, arguments, call_id, json.dumps(arguments), round_num + 1, False)
    row['llmRound'] = round_num
    task.setdefault('toolRounds', []).append(row)
    call = {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}
    messages, settled = [], []
    execute_tool_pipeline(
        task, [(call, name, call_id, arguments, row['roundNum'], row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[],
        round_num=round_num, model='test-model',
        internal_result_sink=SimpleNamespace(
            capture=lambda call_id, value: settled.append(value)))
    return row, messages, settled


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_exception_after_handler_presentation_is_failed_everywhere(mode, observations):
    events, audits = observations

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        _finalize_tool_round(task, number, row, [{'content': 'provisional output'}])
        raise RuntimeError('receipt decoding failed after presentation')

    task = task_for(handler, mode)
    row, messages, settled = dispatch(task)
    assert row['_runtimeState'] == 'error'
    assert row['status'] == 'error'
    complete = [event for event in events if event['type'] == 'tool_complete']
    assert len(complete) == 1 and complete[0]['status'] == 'error'
    assert 'receipt decoding failed after presentation' in messages[0]['content']
    assert len(audits) == 1 and audits[0]['outcome'] == 'error'
    assert settled[0].execution_status == 'error'


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('failure_path', ['raise', 'return_error'])
def test_failed_read_does_not_poison_a_later_authorized_retry(
        mode, failure_path, observations):
    calls = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        if len(calls) == 1:
            if failure_path == 'raise':
                raise RuntimeError('temporarily unavailable')
            _finalize_tool_round(task, number, row,
                                 [{'content': 'temporarily unavailable'}],
                                 status='error')
            return call_id, 'temporarily unavailable', False
        _finalize_tool_round(task, number, row, [{'content': 'fresh result'}])
        return call_id, 'fresh result', False

    task = task_for(handler, mode)
    failed, first_messages, _ = dispatch(task)
    recovered, messages, _ = dispatch(task, call_id='occurrence-2', round_num=1)
    assert failed['status'] == 'error'
    assert 'temporarily unavailable' in first_messages[0]['content']
    assert calls == ['occurrence-1', 'occurrence-2']
    assert recovered['status'] == 'done'
    assert 'fresh result' in messages[0]['content']
    cached, cached_messages, _ = dispatch(
        task, call_id='occurrence-3', round_num=2)
    assert calls == ['occurrence-1', 'occurrence-2']
    assert cached['status'] == 'done'
    assert 'fresh result' in cached_messages[0]['content']
    assert [row['outcome'] for row in observations[1]] == ['error', 'ok', 'ok']


def test_lost_write_result_retains_uncertainty_in_every_failure_surface(observations):
    from lib.tools.result_envelope import tool_result_error
    effects = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        effects.append(row['executionEffect']['operationId'])
        _finalize_tool_round(task, number, row, [{'content': 'write response pending'}])
        raise RuntimeError('reply lost after effect')

    task = task_for(handler, name='write_file', permission='write')
    row, messages, settled = dispatch(task, name='write_file')
    error = tool_result_error(messages[0]['content'])
    assert effects == [row['executionEffect']['operationId']]
    assert row['executionEffect']['state'] == 'unknown'
    assert row['status'] == 'error'
    assert error.code == 'tool_effect_unknown' and error.retryable is False
    assert 'Do not repeat' in error.next_action
    assert settled[0].execution_status == 'error'
    assert len(observations[1]) == 1
    assert observations[1][0]['outcome'] == 'error'
    assert tool_result_error(row['results'][0]['content']).code == error.code


def test_declared_timeout_overrules_early_success_and_is_not_cached(monkeypatch, observations):
    calls = []

    class ImmediateTimer:
        def __init__(self, delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

        def cancel(self):
            pass

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._heartbeat.threading.Timer', ImmediateTimer)
    monkeypatch.setattr('lib.tools.registry.declared_tool_timeout_ms', lambda name: 1)
    monkeypatch.setattr(
        'lib.tasks_pkg.tool_runtime.active_context_for_call',
        lambda *args, **kwargs: SimpleNamespace(request_resource_cancellation=lambda: None))

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        _finalize_tool_round(task, number, row, [{'content': 'late output'}])
        return call_id, 'late output', False

    task = task_for(handler)
    for index in range(2):
        row, messages, settled = dispatch(
            task, call_id=f'timed-{index}', round_num=index)
        assert row['status'] == 'error'
        assert 'timed out' in messages[0]['content']
        assert settled[0].execution_status == 'error'
    assert calls == ['timed-0', 'timed-1']
    assert [row['outcome'] for row in observations[1]] == ['error', 'error']


@pytest.mark.parametrize('fails', [False, True])
def test_direct_executor_consumers_keep_one_final_audit(fails, observations):
    from lib.tasks_pkg.executor import _execute_tool_one

    during_handler = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        _finalize_tool_round(task, number, row, [{'content': 'handler output'}])
        during_handler.extend(observations[1])
        if fails:
            raise RuntimeError('direct consumer lost receipt')
        return call_id, 'handler output', False

    task = task_for(handler)
    _, row, _ = _build_tool_round_entry('fetch_url', {}, 'direct', '{}', 1, False)
    _execute_tool_one(task, {}, 'fetch_url', 'direct', {}, 1, row, {}, None, False)
    assert during_handler == []
    assert len(observations[1]) == 1
    assert observations[1][0]['outcome'] == ('error' if fails else 'ok')
