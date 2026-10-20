"""Provider positions retain independent effects; legacy flood errors stay scoped.

Exercise the real wire translator, SSE parser, early UI accumulator, final
parser and pipeline. Replace only expensive tools/provider transport seams.
"""

import asyncio
import importlib
import json
import threading
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.llm_errors import ProviderToolCallFloodError
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from tests._registered_chat_task import registered_chat_task

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'flood-admission', 'convId': 'conv-flood', '_userId': 1,
            'status': 'running', 'aborted': False, 'model': 'test-model',
            'config': {}, 'events': [], 'toolRounds': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock()}


@pytest.fixture
def task():
    task = _task()
    with registered_chat_task(task):
        yield task


def _call(index, args='{"conv_id":"example"}', *, caller=None):
    call = {'id': f'call_{index}', 'type': 'function',
            'function': {'name': 'get_conversation', 'arguments': args}}
    if caller is not None:
        call['caller'] = caller
    return call


def _sse(callback=None, *, native=False):
    body = {'model': 'test-model', 'messages': []}
    return SSEAccumulator(
        body, 'flood-trace', RawSSEDumper('test-model', 'flood-trace', body),
        AnthropicSSETranslator() if native else None, time.monotonic(),
        on_tool_call_ready=callback)


def _feed_call(acc, index, args='{"conv_id":"example"}', *, native=False):
    if native:
        for event in (
            {'type': 'content_block_start', 'index': index,
             'content_block': {'type': 'tool_use', 'id': f'call_{index}',
                               'name': 'get_conversation', 'input': {}}},
            {'type': 'content_block_delta', 'index': index,
             'delta': {'type': 'input_json_delta', 'partial_json': args}},
            {'type': 'content_block_stop', 'index': index},
        ):
            acc.feed_line('data: ' + json.dumps(event))
    else:
        call = _call(index, args)
        call['index'] = index
        acc.feed_line('data: ' + json.dumps(
            {'choices': [{'delta': {'tool_calls': [call]}}]}))



def _finish(acc, *, native=False):
    if native:
        for event in (
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
             'usage': {'output_tokens': 37}},
            {'type': 'message_stop'},
        ):
            acc.feed_line('data: ' + json.dumps(event))
    else:
        acc.feed_line('data: ' + json.dumps({'choices': [
            {'delta': {}, 'finish_reason': 'tool_calls'}],
            'usage': {'completion_tokens': 37}}))
        acc.feed_line('data: [DONE]')
    acc.fire_final_tool_callback()
    return acc.finalize()


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('callback_enabled', [False, True])
def test_equal_arguments_preserve_positions_and_terminal_usage(
        monkeypatch, native, callback_enabled, task):
    # Retired deployments may still carry this value; it cannot erase actions.
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    early = StreamingToolAccumulator(task, '.', round_num=0)
    acc = _sse(early.on_tool_call_ready if callback_enabled else None, native=native)
    try:
        for index in range(7):
            _feed_call(acc, index, native=native)
        result = _finish(acc, native=native)
        assert [call['id'] for call in result.message['tool_calls']] == [
            f'call_{i}' for i in range(7)]
        assert result.usage['completion_tokens'] == 37
        assert not result.usage.get('_missing_done')
        from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
        parsed, _ = parse_tool_calls(
            result.message, task, 0, early.tool_round_num, False,
            early_announced=early.announced_tc_map)
        assert len(parsed) == len(task['toolRounds']) == 7
        assert len({id(row[5]) for row in parsed}) == 7
        assert len([e for e in task['events'] if e['type'] == 'tool_start']) == 7
    finally:
        early.close()


@pytest.mark.parametrize('identity', ['fresh', 'recycled', 'blank'])
def test_callback_free_parser_preserves_every_position(monkeypatch, task, identity):
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '4')
    calls = [_call(i if identity == 'fresh' else 0) for i in range(33)]
    if identity == 'blank':
        for call in calls: call['id'] = ''
    message = {'tool_calls': calls}
    parsed, _ = parse_tool_calls(message, task, 0, 0, False)
    assert message['tool_calls'] == calls
    assert len(parsed) == len(task['toolRounds']) == 33
    assert len({id(row[5]) for row in parsed}) == 33


@pytest.mark.parametrize('identity', ['fresh', 'recycled', 'blank'])
def test_equal_calls_execute_and_settle_independently(monkeypatch, task, identity):
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    early = StreamingToolAccumulator(task, '.', round_num=0)
    calls = [_call(i if identity == 'fresh' else 0) for i in range(3)]
    if identity == 'blank':
        for call in calls: call['id'] = ''
    try:
        early.on_tool_call_ready(calls[0])
        message = {'role': 'assistant', 'tool_calls': calls}
        parsed, _ = parse_tool_calls(message, task, 0, early.tool_round_num,
                                     False, early_announced=early.announced_tc_map)
        executed = []

        def execute(task, tc, name, call_id, args, rn, row, *a, **kw):
            from lib.tasks_pkg.executor import _finalize_tool_round
            executed.append(call_id)
            _finalize_tool_round(task, rn, row,
                                 [{'type': 'text', 'content': f'RESULT:{call_id}'}])
            return call_id, f'RESULT:{call_id}', False

        monkeypatch.setattr(pipeline, '_execute_tool_one', execute)
        monkeypatch.setattr(pipeline, '_execute_tool_one_in_pool', execute)
        monkeypatch.setattr(pipeline, '_post_tool_snapshot_enabled', lambda _: False)
        messages = [message]
        pipeline.execute_tool_pipeline(
            task, parsed, cfg={}, project_path='.', project_enabled=False,
            tool_list=[], messages=messages, all_search_results_text=[],
            round_num=0, model='test-model')
        ids = [call['id'] for call in message['tool_calls']]
        assert len(ids) == len(set(ids)) == len(executed) == 3
        assert sorted(executed) == sorted(ids)
        assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == ids
        assert len(task['toolRounds']) == 3
        assert all(row['status'] == 'done' for row in task['toolRounds'])
    finally:
        early.close()


@pytest.mark.parametrize('native', [False, True])
def test_same_slot_retransmission_preserves_only_one_occurrence(native):
    acc = _sse(native=native)
    if native:
        event = {'type': 'content_block_start', 'index': 0,
                 'content_block': {'type': 'tool_use', 'id': 'call_0',
                                   'name': 'get_conversation',
                                   'input': {'conv_id': 'example'}}}
    else:
        call = _call(0); call['index'] = 0
        event = {'choices': [{'delta': {'tool_calls': [call]}}]}
    for _ in range(20):
        acc.feed_line('data: ' + json.dumps(event))
    _feed_call(acc, 1, native=native)
    _feed_call(acc, 2, native=native)
    calls = _finish(acc, native=native).message['tool_calls']
    assert [call['id'] for call in calls] == ['call_0', 'call_1', 'call_2']
    assert [json.loads(call['function']['arguments']) for call in calls] == [
        {'conv_id': 'example'}] * 3


def test_interleaved_arguments_keep_equal_and_different_occurrences():
    acc = _sse()
    for index in range(5):
        _feed_call(acc, index, '')
    for index in range(5):
        acc.feed_line('data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [
            {'index': index, 'function': {'arguments': json.dumps({'conv_id': str(index % 2)})}}
        ]}}]}))
    result = _finish(acc)
    assert [json.loads(call['function']['arguments']) for call in
            result.message['tool_calls']] == [{'conv_id': str(i % 2)} for i in range(5)]


def test_empty_arguments_keep_each_final_occurrence():
    acc = _sse()
    for index in range(3):
        _feed_call(acc, index, '')
    assert len(_finish(acc).message['tool_calls']) == 3


def test_complete_arguments_without_provider_finish_still_require_recovery(monkeypatch, task):
    from lib.tasks_pkg.stream_handler.api import analyse_stream_result
    from lib.tasks_pkg.stream_handler import _budget
    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *args: None)
    task.update(content='', thinking='')
    acc = _sse()
    _feed_call(acc, 0)
    _feed_call(acc, 1)
    result = acc.finalize()
    assert len(result.message['tool_calls']) == 2
    assert result.usage.get('_missing_done')
    decision = analyse_stream_result(
        result.message, result.compatibility_finish_reason, task, 'flood-ad',
        'test-model', 0, 0, [], usage=result.usage)
    assert decision['action'] == 'continue'


def test_fatal_flood_settles_pending_cards_before_done_and_persist(monkeypatch, task):
    import lib.tasks_pkg.orchestrator._post_loop as post_loop
    import lib.tasks_pkg.manager as manager
    import lib.tasks_pkg.compaction._settlement as compaction
    monkeypatch.setattr(post_loop, '_maybe_auto_retry_turn', lambda *args: False)
    monkeypatch.setattr(compaction, 'settle_compaction_usage', lambda *args: None)
    monkeypatch.setattr(manager, 'stamp_chat_task_terminal', lambda *args, **kwargs: None)
    persisted = []
    monkeypatch.setattr(post_loop, 'persist_task_result',
                        lambda task: persisted.append(task['toolRounds'][0]['status']))
    task['toolRounds'].append({'toolName': 'get_conversation', 'roundNum': 1,
                              'status': 'searching', 'toolCallId': 'call_0'})
    error = ProviderToolCallFloodError(tool_name='get_conversation', count=33, limit=32)
    assert post_loop.handle_task_fatal(task, error) is False
    assert persisted == ['aborted']
    assert task['toolRounds'][0]['status'] == 'aborted'
    events = [event['type'] for event in task['events']]
    assert events[-1] == 'done'
    assert 'tool_result' in events[:-1]
    assert task['error']['kind'] == 'tool_loop'



@pytest.mark.parametrize('asynchronous', [False, True])
def test_transport_never_retries_flood(monkeypatch, asynchronous):
    attempts = []
    error = ProviderToolCallFloodError(tool_name='get_conversation', count=33, limit=32)
    module = importlib.import_module('lib.llm.astream' if asynchronous else 'lib.llm.stream')

    def fail(*a, **kw):
        attempts.append(kw.get('attempt'))
        raise error

    if asynchronous:
        async def async_fail(*a, **kw):
            return fail(*a, **kw)
        monkeypatch.setattr(module, '_async_stream_chat_once', async_fail)
        invoke = lambda: asyncio.run(module.async_stream_chat({'model': 'test-model'}))
    else:
        monkeypatch.setattr(module, '_stream_chat_once', fail)
        invoke = lambda: module.stream_chat({'model': 'test-model'})
    with pytest.raises(ProviderToolCallFloodError) as caught:
        invoke()
    assert caught.value is error
    assert attempts == [0]



def test_failure_is_request_scoped_and_displayed_as_upstream_error():
    from lib.error_envelope import from_exception
    from lib.tasks_pkg.llm_fallback._call import _is_request_payload_error
    from lib.tasks_pkg.turn_retry import should_auto_retry_turn
    error = ProviderToolCallFloodError(tool_name='get_conversation', count=33, limit=32)
    assert _is_request_payload_error(error)
    envelope = from_exception(error)
    assert envelope['kind'] == 'tool_loop'
    assert envelope['providerErrorCode'] == 'tool_call_flood'
    assert should_auto_retry_turn(envelope, 0, {}) == (False, 0.0)
    assert 'conv_id' not in str(error)



@pytest.mark.parametrize('prior_tools', [False, True])
def test_root_loop_does_not_fallback_or_rescue_flood(monkeypatch, task, prior_tools):
    import lib.tasks_pkg.llm_fallback._call as fallback
    error = ProviderToolCallFloodError(tool_name='get_conversation', count=33, limit=32)
    streams = []

    def fail(task, body, **kw):
        streams.append(body['model'])
        raise error

    def unexpected_rescue(*a, **kw):
        pytest.fail('Flood must not launch pool rescue')

    monkeypatch.setattr(fallback, 'stream_llm_response', fail)
    monkeypatch.setattr(fallback, '_get_fallback_model', lambda _: 'fallback-model')
    monkeypatch.setattr(fallback, '_attempt_pool_rescue', unexpected_rescue)
    args = (task, {'model': 'test-model'}, 'test-model', 2, 512,
            prior_tools, None, [{'role': 'user', 'content': 'continue'}],
            'low', False, {}, [])
    if prior_tools:
        result = fallback._llm_call_with_fallback(*args)
        assert result['_loop_action'] == 'break'
        assert result['finish_reason'] == 'error'
        assert task['error']['kind'] == 'tool_loop'
    else:
        with pytest.raises(ProviderToolCallFloodError) as caught:
            fallback._llm_call_with_fallback(*args)
        assert caught.value is error
    assert streams == ['test-model']
