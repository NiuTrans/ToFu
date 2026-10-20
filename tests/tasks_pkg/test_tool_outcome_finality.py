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


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('late_result', ['success', 'error'])
def test_late_worker_cannot_rewrite_a_published_timeout(
        mode, late_result, observations, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        entered.set()
        assert release.wait(5), 'test did not release its owned worker'
        if late_result == 'error':
            raise RuntimeError('LATE-WORKER-FAILURE')
        _finalize_tool_round(task, number, row, [{'content': 'LATE-WORKER-SUCCESS'}])
        return call_id, 'LATE-WORKER-SUCCESS', False

    def deadline_after_entry(futures, task, deadline):
        workers.extend(futures)
        assert entered.wait(5), 'worker did not enter before the injected deadline'
        raise TimeoutError('injected pool deadline')

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._pipeline._completed_tools_until_cancel',
        deadline_after_entry)
    task = task_for(handler, mode)
    try:
        row, messages, settled = dispatch(task)
        assert row['status'] == 'error'
        assert 'timed out' in messages[0]['content']
        assert settled[0].execution_status == 'error'
        fields = ('results', 'status', 'toolContent', 'toolResultEvidence', 'tEnd')
        published = deepcopy({field: row.get(field) for field in fields})
        event_count = len(observations[0])
    finally:
        release.set()
        for worker in workers:
            worker.result(timeout=5)

    assert {field: row.get(field) for field in fields} == published
    assert row['_runtimeState'] == 'error'
    assert len(observations[0]) == event_count
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert not task.get('_activeToolContexts'), 'late worker retained its runtime context'


@pytest.mark.parametrize('boundary', ['resolver', 'presentation'])
def test_timeout_closes_racing_handler_entry_and_presentation(
        boundary, observations, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers, handlers = [], []

    def pause():
        entered.set()
        assert release.wait(5), 'test did not release its owned producer'

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        handlers.append(call_id)
        _finalize_tool_round(task, number, row, [{'content': 'RACING-PRESENTATION'}])
        return call_id, 'RACING-PRESENTATION', False

    task = task_for(handler)
    if boundary == 'resolver':
        def resolve(_name):
            pause()
            return handler
        task['_tool_env'] = SimpleNamespace(resolve=resolve)
    else:
        monkeypatch.setattr(
            'lib.tasks_pkg.executor._finalize._attach_terminal_qr',
            lambda _results: pause())

    def expire(futures, *_args):
        workers.extend(futures)
        assert entered.wait(5)
        raise TimeoutError('injected race deadline')

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._pipeline._completed_tools_until_cancel', expire)
    try:
        row, messages, settled = dispatch(task)
        assert 'timed out' in messages[0]['content']
        assert settled[0].execution_status == 'error'
        frozen = deepcopy(row['results'])
        event_count = len(observations[0])
    finally:
        release.set()
        for worker in workers:
            worker.result(timeout=5)
    assert row['results'] == frozen
    assert row['status'] == row['_runtimeState'] == 'error'
    assert len(observations[0]) == event_count
    assert not task.get('_activeToolContexts')
    if boundary == 'resolver':
        assert handlers == [] and 'executionEffect' not in row
    else:
        assert handlers == ['occurrence-1']


@pytest.mark.parametrize('own_closed', [False, True])
def test_suppressed_tool_display_still_obeys_parent_revocation(own_closed, observations):
    from lib.agent_core.publication import ChildPublicationFence, ChildPublicationRevoked
    from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
    task = task_for(lambda *_args: None)
    _, row, _ = _build_tool_round_entry('fetch_url', {}, 'owned', '{}', 1, False)
    context = context_for_task(
        task, round_num=1, tool_call_id='owned', tool_name='fetch_url', round_entry=row)
    row['_toolResultPublicationClosed'] = own_closed
    parent = ChildPublicationFence()

    def publish(_event):
        parent.close()
        _finalize_tool_round(task, 1, row, [{'content': 'REVOKED-PARENT'}])

    try:
        with pytest.raises(ChildPublicationRevoked):
            parent.deliver(publish, None)
    finally:
        unregister_context(context)
    assert row['results'] is None
    assert observations[0] == []


def test_timeout_revokes_only_the_owning_gateway_child(observations, monkeypatch):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    entered, release = threading.Event(), threading.Event()
    workers = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        if call_id == 'slow':
            entered.set()
            assert release.wait(5)
        _finalize_tool_round(task, number, row, [{'content': call_id}])
        return call_id, call_id, False

    def completed_then_deadline(futures, *_args):
        workers.extend(futures)
        assert entered.wait(5)
        for worker, identity in futures.items():
            if identity[0] == 'fast':
                worker.result(timeout=5)
                yield worker
        raise TimeoutError('injected deadline after healthy sibling')

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._pipeline._completed_tools_until_cancel',
        completed_then_deadline)
    task = task_for(handler)
    task['toolRounds'] = []
    task['_executable_tool_catalog'] = [{
        'type': 'function', 'function': {'name': 'fetch_url',
        'parameters': {'type': 'object', 'properties': {}}}}]
    calls = [{'id': call_id, 'type': 'function',
              'function': {'name': 'fetch_url', 'arguments': '{}'}}
             for call_id in ('slow', 'fast')]
    try:
        receipts = _execute_call_batch(
            task, calls, cfg={'autoApply': True}, project_path=None,
            project_enabled=False, model='test-model', llm_round=1)
        assert [receipt['status'] for receipt in receipts] == ['error', 'done']
        assert [receipt.canonical.execution_status for receipt in receipts] == ['error', 'ok']
        assert receipts[1]['output'] == 'fast'
        assert len(task['_activeToolContexts']) == 1
        frozen = deepcopy([row['results'] for row in task['toolRounds']])
        event_count = len(observations[0])
    finally:
        release.set()
        for worker in workers:
            worker.result(timeout=5)
    assert [row['results'] for row in task['toolRounds']] == frozen
    assert len(observations[0]) == event_count
    assert sorted(event['outcome'] for event in observations[1]) == ['error', 'ok']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('name', ['fetch_url', 'write_file'])
@pytest.mark.parametrize('fault', ['missing', 'short', 'foreign_id', 'search_flag'])
def test_invalid_handler_receipt_fails_the_owned_occurrence(
        mode, name, fault, observations):
    from lib.tools.result_envelope import tool_result_error
    entered = []

    def handler(task, tc, tool, call_id, arguments, number, row, *rest):
        entered.append(call_id)
        _finalize_tool_round(task, number, row, [{'content': 'provisional output'}])
        return {
            'missing': None,
            'short': (call_id, 'UNRELATED-RESPONSE-599'),
            'foreign_id': ('another-call', 'UNRELATED-RESPONSE-599', False),
            'search_flag': (call_id, 'UNRELATED-RESPONSE-599', 'false'),
        }[fault]

    task = task_for(handler, mode, name=name,
                    permission='write' if name == 'write_file' else 'read')
    row, messages, settled = dispatch(task, name=name)
    assert entered == ['occurrence-1']
    assert row['status'] == 'error'
    assert row['_runtimeState'] == 'error'
    assert row['executionEffect']['state'] == 'unknown'
    assert [message['tool_call_id'] for message in messages] == ['occurrence-1']
    assert 'UNRELATED-RESPONSE-599' not in str(messages)
    assert 'Unknown tool' not in str(messages)
    error = tool_result_error(messages[0]['content'])
    assert error is not None
    assert error.code == (
        'tool_effect_unknown' if name == 'write_file' else 'tool_result_invalid')
    if name == 'write_file':
        assert error.retryable is False and 'Do not repeat' in error.next_action
    assert row['_cacheableResult'] is False
    assert len(settled) == 1 and settled[0].execution_status == 'error'
    assert len(observations[1]) == 1 and observations[1][0]['outcome'] == 'error'
    complete = [event for event in observations[0] if event['type'] == 'tool_complete']
    assert len(complete) == 1 and complete[0]['status'] == 'error'


@pytest.mark.parametrize('programmatic', [False, True])
def test_foreign_handler_id_cannot_overwrite_a_gateway_sibling(
        programmatic, observations):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    from lib.tools.programmatic import ProgrammaticResultBudget
    entered = []
    sibling_returned = threading.Event()

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        entered.append(call_id)
        if call_id == 'first':
            assert sibling_returned.wait(5), 'independent sibling did not run'
            return 'second', 'foreign receipt must be withheld', False
        sibling_returned.set()
        return call_id, 'owned sibling output', False

    task = task_for(handler)
    task['toolRounds'] = []
    task['_executable_tool_catalog'] = [{
        'type': 'function', 'function': {'name': 'fetch_url',
        'parameters': {'type': 'object',
                       'properties': {'url': {'type': 'string'}}}}}]
    calls = [{'id': call_id, 'type': 'function', 'function': {
        'name': 'fetch_url',
        'arguments': json.dumps({'url': f'https://example.test/{call_id}'})}}
        for call_id in ('first', 'second')]
    receipts = _execute_call_batch(
        task, calls, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, model='test-model', llm_round=1,
        program_result_budget=ProgrammaticResultBudget() if programmatic else None)
    assert sorted(entered) == ['first', 'second']
    assert [receipt['call_id'] for receipt in receipts] == ['first', 'second']
    assert [receipt['status'] for receipt in receipts] == ['error', 'done']
    assert [receipt.canonical.execution_status for receipt in receipts] == ['error', 'ok']
    assert 'owned sibling output' in receipts[1]['output']
    assert 'foreign receipt must be withheld' not in str(receipts)
    assert sorted(event['outcome'] for event in observations[1]) == ['error', 'ok']


@pytest.mark.parametrize('name', ['fetch_url', 'write_file'])
def test_direct_executor_rejects_a_foreign_receipt(name, observations):
    from lib.tasks_pkg.executor import _execute_tool_one
    from lib.tools.result_envelope import tool_result_error

    task = task_for(
        lambda *args: ('foreign', 'UNRELATED-RESPONSE-599', False),
        name=name, permission='write' if name == 'write_file' else 'read')
    _, row, _ = _build_tool_round_entry(name, {}, 'direct', '{}', 1, False)
    call_id, content, is_search = _execute_tool_one(
        task, {}, name, 'direct', {}, 1, row, {}, None, False)
    assert call_id == 'direct' and is_search is False
    assert tool_result_error(content) is not None
    assert 'UNRELATED-RESPONSE-599' not in content
    assert row['status'] == row['_runtimeState'] == 'error'
    assert row['executionEffect']['state'] == 'unknown'
    assert [event['outcome'] for event in observations[1]] == ['error']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_invalid_read_receipt_does_not_block_a_repaired_authorized_call(
        mode, observations):
    entered = []

    def handler(task, tc, name, call_id, *rest):
        entered.append(call_id)
        return ('foreign' if len(entered) == 1 else call_id), 'fresh read', False

    task = task_for(handler, mode)
    first, _, _ = dispatch(task)
    second, messages, _ = dispatch(task, call_id='occurrence-2', round_num=1)
    third, cached, _ = dispatch(task, call_id='occurrence-3', round_num=2)
    assert entered == ['occurrence-1', 'occurrence-2']
    assert [row['status'] for row in (first, second, third)] == ['error', 'done', 'done']
    assert 'fresh read' in messages[0]['content']
    assert 'fresh read' in cached[0]['content']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('container', [tuple, list])
def test_empty_handler_content_is_a_valid_receipt(mode, container, observations):
    def handler(task, tc, name, call_id, *rest):
        return container((call_id, '', False))

    row, messages, settled = dispatch(task_for(handler, mode))
    assert row['status'] == row['_runtimeState'] == 'done'
    assert row['executionEffect']['state'] == 'returned'
    assert settled[0].execution_status == 'ok'
    assert [message['tool_call_id'] for message in messages] == ['occurrence-1']
    assert [event['outcome'] for event in observations[1]] == ['ok']


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


def returned_failure(kind):
    from lib.mcp.result_content import MCPToolResult
    from lib.tools.result_envelope import typed_tool_error
    from lib.tools.result_value import TrustedResultText, capture_result

    if kind == 'mcp':
        return MCPToolResult('upstream unavailable', is_error=True)
    if kind == 'canonical':
        return TrustedResultText('upstream unavailable', canonical=capture_result(
            'upstream unavailable', execution_status='error'))
    error = typed_tool_error('upstream_unavailable', retryable=True,
                             next_action='Retry this read after recovery.',
                             message='upstream unavailable')
    return (error.to_model_text() if kind == 'typed_model'
            else error.to_envelope_text())


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('kind', ['mcp', 'canonical', 'typed_model', 'typed_envelope'])
def test_returned_failure_agrees_across_settlement_and_retry(mode, kind, observations):
    calls = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        content = returned_failure(kind)
        _finalize_tool_round(task, number, row, [{'content': content}])
        return call_id, content, False

    task = task_for(handler, mode)
    for index in range(2):
        row, messages, settled = dispatch(
            task, call_id=f'returned-{index}', round_num=index)
        assert row['status'] == 'error'
        assert row['_runtimeState'] == 'error'
        assert settled[0].execution_status == 'error'
        assert 'upstream unavailable' in messages[0]['content']
        if kind.startswith('typed_'):
            from lib.tools.result_envelope import tool_result_error
            error = tool_result_error(messages[0]['content'])
            assert error.code == 'upstream_unavailable'
            assert error.retryable is True
            if kind == 'typed_model':
                assert json.loads(messages[0]['content']) == json.loads(
                    returned_failure(kind))
    assert calls == ['returned-0', 'returned-1']
    complete = [event for event in observations[0]
                if event['type'] == 'tool_complete']
    assert [event['status'] for event in complete] == ['error', 'error']
    assert [audit['outcome'] for audit in observations[1]] == ['error', 'error']


@pytest.mark.parametrize('kind', ['mcp', 'canonical', 'typed_model', 'typed_envelope'])
def test_direct_executor_recognizes_returned_failure(kind, observations):
    from lib.tasks_pkg.executor import _execute_tool_one

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        content = returned_failure(kind)
        _finalize_tool_round(task, number, row, [{'content': content}])
        return call_id, content, False

    task = task_for(handler)
    _, row, _ = _build_tool_round_entry('fetch_url', {}, 'direct', '{}', 1, False)
    _execute_tool_one(task, {}, 'fetch_url', 'direct', {}, 1, row, {}, None, False)
    assert row['status'] == 'error'
    assert row['_runtimeState'] == 'error'
    assert [audit['outcome'] for audit in observations[1]] == ['error']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('kind', ['typed_model', 'typed_envelope'])
def test_error_shaped_document_is_successful_data_and_remains_cacheable(
        mode, kind, observations):
    # Identical bytes read from a file/API are data, without the host carrier.
    content = str(returned_failure(kind))
    calls = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        _finalize_tool_round(task, number, row, [{'content': content}])
        return call_id, content, False

    task = task_for(handler, mode)
    for index in range(2):
        row, messages, settled = dispatch(
            task, call_id=f'document-{index}', round_num=index)
        assert row['status'] == 'done'
        assert settled[0].execution_status == 'ok'
        assert json.loads(messages[0]['content']) == json.loads(content)
    assert calls == ['document-0']
    assert [audit['outcome'] for audit in observations[1]] == ['ok', 'ok']


@pytest.mark.parametrize('prior_verdict', ['rejected', 'aborted'])
def test_returned_error_preserves_explicit_non_success_verdict(prior_verdict, observations):
    from lib.tasks_pkg.executor import _execute_tool_one

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        content = returned_failure('typed_model')
        _finalize_tool_round(task, number, row, [{'content': content}], status=prior_verdict)
        return call_id, content, False

    task = task_for(handler)
    _, row, _ = _build_tool_round_entry('fetch_url', {}, 'direct', '{}', 1, False)
    _execute_tool_one(task, {}, 'fetch_url', 'direct', {}, 1, row, {}, None, False)
    assert row['status'] == prior_verdict
    assert [audit['outcome'] for audit in observations[1]] == [prior_verdict]


def test_returned_error_survives_presentation_failure(monkeypatch, observations):
    from lib.tasks_pkg.executor import _execute_tool_one

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        return call_id, returned_failure('typed_model'), False

    def broken_event(*args):
        raise OSError('display channel unavailable')

    monkeypatch.setattr('lib.tasks_pkg.executor._finalize.append_event', broken_event)
    task = task_for(handler)
    _, row, _ = _build_tool_round_entry('fetch_url', {}, 'direct', '{}', 1, False)
    _, content, _ = _execute_tool_one(
        task, {}, 'fetch_url', 'direct', {}, 1, row, {}, None, False)
    assert row['status'] == 'error' and row['_runtimeState'] == 'error'
    assert 'upstream unavailable' in content
    assert 'display channel unavailable' not in content
    assert [audit['outcome'] for audit in observations[1]] == ['error']


@pytest.mark.parametrize('kind', ['partial', 'batch'])
def test_incomplete_view_or_failed_child_does_not_fail_the_outer_operation(kind, observations):
    from lib.tools.result_envelope import ToolResultEnvelopeV2
    from lib.tools.result_value import CanonicalReceipt, capture_result, compose_result

    if kind == 'partial':
        content = ToolResultEnvelopeV2(
            status='partial', summary='bounded preview', truncated=True).to_envelope_text()
    else:
        child = CanonicalReceipt(
            name='fetch_url', call_id='child', output='failed child',
            canonical=capture_result('failed child', execution_status='error'))
        content = compose_result(json.dumps({'results': [child]}), [child])
    calls = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        calls.append(call_id)
        _finalize_tool_round(task, number, row, [{'content': content}])
        return call_id, content, False

    task = task_for(handler)
    for index in range(2):
        row, _, settled = dispatch(
            task, call_id=f'outer-{index}', round_num=index)
        assert row['status'] == 'done'
        assert settled[0].execution_status == 'ok'
    assert calls == ['outer-0']
    assert [audit['outcome'] for audit in observations[1]] == ['ok', 'ok']


@pytest.mark.parametrize('is_error', [False, True])
def test_mcp_protocol_flag_controls_display_and_adapter_log(
        is_error, monkeypatch, observations, caplog):
    from lib.mcp.result_content import MCPToolResult
    from lib.tasks_pkg.handlers._adapter import simple_call
    from lib.tasks_pkg.handlers.mcp import _settle_mcp_round_display

    monkeypatch.setattr('lib.mcp.project_names.ingest_tool_result', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.tool_display.compose_mcp_display',
                        lambda *a, **k: ('Remote read', None))
    monkeypatch.setattr('lib.tasks_pkg.tool_display._mcp._mcp_links', lambda *a, **k: {})
    content = MCPToolResult(
        'upstream unavailable' if is_error else 'MCP Error examples from the manual',
        is_error=is_error)

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        return simple_call(
            task, name, arguments, number, row, call_id,
            executor=lambda *args: content, source='MCP',
            post_build=lambda meta, result, args: _settle_mcp_round_display(
                row, meta, 'mcp__example__read', args, result, user_id=1))

    with caplog.at_level('INFO', logger='lib.tasks_pkg.handlers._adapter'):
        row, _, settled = dispatch(task_for(handler))
    assert row['status'] == ('error' if is_error else 'done')
    assert settled[0].execution_status == ('error' if is_error else 'ok')
    assert ('returned an error result' in caplog.text) is is_error
