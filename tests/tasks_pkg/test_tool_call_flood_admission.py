"""Runaway provider responses stop at ingress; accepted twins keep their effects.

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
from lib.llm.tool_call_flood import ToolCallFloodGuard, identical_tool_call_limit
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


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('callback_enabled', [False, True])
def test_flood_stops_while_stream_is_open(monkeypatch, native, callback_enabled, task):
    monkeypatch.delenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', raising=False)
    with_acc = StreamingToolAccumulator(task, '.', round_num=0)
    acc = _sse(with_acc.on_tool_call_ready if callback_enabled else None,
               native=native)
    try:
        # The first repetition triggers the salvage: the guard flags the
        # accumulator and every subsequent payload is refused, so the stream
        # stops without any terminal frame from the provider.
        _feed_call(acc, 0, native=native)
        _feed_call(acc, 1, native=native)
        assert acc._flood_salvage is True
        for index in range(2, 919):
            _feed_call(acc, index, native=native)
        # No exception, no further accumulation, no extra UI rows.
        assert len(acc.tool_calls_acc) == 2
        assert len(task['toolRounds']) <= 1
    finally:
        with_acc.close()


def test_final_parser_collapses_callback_free_flood_before_rows(monkeypatch, task):
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '4')
    calls = [_call(i) for i in range(233)]
    msg = {'tool_calls': calls}
    parsed, _ = parse_tool_calls(msg, task, 0, 0, False)
    # 233 identical occurrences collapse to the first 3 (limit 4 − 1); the
    # repetition is evidence on the message, not an error and not extra rows.
    assert len(parsed) == 3
    assert len(task['toolRounds']) == 3
    assert msg.get('_flood_collapsed')
    assert sum(v['dropped'] for v in msg['_flood_collapsed'].values()) == 230


def test_native_mixed_batch_then_generation_loop_preserves_wire_identity(monkeypatch):
    """Distinct wire blocks precede a repeating suffix; none are retransmissions.

    The salvage triggers on the FIRST repeated signature (index 19 repeats
    ``experiment_or_url='0'``) and keeps the stream adoption honest.
    """
    monkeypatch.delenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', raising=False)
    announced = []
    acc = _sse(announced.append, native=True)
    name = 'mcp__llm__experiment_canvas_diagnose'
    for index in range(18 + 121):
        tool = 'execute_tools' if index == 0 else name
        arguments = ({'calls': [{'name': 'other_tool', 'arguments': {}}]}
                     if index == 0 else {'experiment_or_url': str(min(index, 17))})
        events = (
            {'type': 'content_block_start', 'index': index,
             'content_block': {'type': 'tool_use', 'id': f'{tool}_{index}_abcdef01',
                               'name': tool, 'input': {}}},
            {'type': 'content_block_delta', 'index': index,
             'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}},
            {'type': 'content_block_stop', 'index': index},
        )
        for event in events:
            acc.feed_line('data:' + json.dumps(event))
        if acc._flood_salvage:
            # min(18, 17) == 17 repeats the canvas call from index 17.
            assert index == 18
            break
    else:
        pytest.fail('A repeating native suffix must trigger the flood salvage')
    assert len(announced) == 18
    assert len({call['id'] for call in announced}) == 18
    assert announced[0]['function']['name'] == 'execute_tools'
    # The triggering occurrence was accumulated but never announced upstream.
    assert announced[-1]['id'] == f'{name}_17_abcdef01'
    result = acc.finalize()
    # Collapsed: execute_tools + 17 distinct canvas calls (indices 17/18 share
    # one signature and collapse to the first) = 18 adopted calls, not 139.
    assert len(result.message['tool_calls']) == 18
    assert result.message.get('_flood_collapsed')
    assert result.usage.get('_flood_salvage')



def test_accepted_twins_collapse_to_one(monkeypatch, task):
    """Repeated identical calls in one response execute exactly once.

    The old 'accepted twins execute independently' contract is replaced: an
    exact same-argument repetition is collapsed before dispatch, so the
    transcript and the tool/result pairing both see a single occurrence.
    """
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    monkeypatch.delenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', raising=False)
    early = StreamingToolAccumulator(task, '.', round_num=0)
    calls = [_call(i) for i in range(5)]
    try:
        # The early-announcer can only register the calls the stream actually
        # let through; a flood stops at the first repetition.
        early.on_tool_call_ready(calls[0])
        message = {'role': 'assistant', 'tool_calls': calls}
        parsed, _ = parse_tool_calls(message, task, 0, early.tool_round_num,
                                     False, early_announced=early.announced_tc_map)
        assert len(task['toolRounds']) == 1
        assert len([e for e in task['events'] if e.get('type') == 'tool_start']) == 1
        assert message.get('_flood_collapsed')
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
        assert executed == [calls[0]['id']]
        assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == [calls[0]['id']]
        assert all(row['status'] == 'done' for row in task['toolRounds'])
    finally:
        early.close()


def test_guard_counts_positions_not_callback_volume(monkeypatch):
    """Only distinct positions count; a repeated callback on one slot is one."""
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    guard = ToolCallFloodGuard()
    for _ in range(100):
        assert guard.observe(0, _call(0)) is None
    # The second DISTINCT position carrying the same signature triggers.
    error = guard.observe(1, _call(1))
    assert isinstance(error, ProviderToolCallFloodError)
    assert error.count == 2
    assert error.limit == 2
    # Once triggered the guard stays triggered without producing a new error.
    assert guard.observe(2, _call(2)) is error


def test_guard_replaces_provisional_slot_identity(monkeypatch):
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '3')
    guard = ToolCallFloodGuard()
    assert guard.observe(0, _call(0)) is None
    assert guard.observe(0, _call(0, '{"conv_id":"changed"}')) is None
    assert guard.observe(1, _call(1)) is None
    assert guard.observe(2, _call(2)) is None


def test_distinct_arguments_and_authorities_are_not_a_total_fanout_cap(monkeypatch):
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    guard = ToolCallFloodGuard()
    for index in range(100):
        assert guard.observe(index, _call(index, json.dumps({'conv_id': str(index)}))) is None
    assert guard.observe(100, _call(100, caller={'type': 'multi_agent', 'agent_name': '/root/a'})) is None
    assert guard.observe(101, _call(101, caller={'type': 'multi_agent', 'agent_name': '/root/b'})) is None



def test_fresh_physical_attempt_collapses_identical_calls(monkeypatch):
    """A fresh accumulator per attempt collapses repeats within ONE response."""
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    for _ in range(2):
        acc = _sse()
        _feed_call(acc, 0)
        _feed_call(acc, 1)
        acc.fire_final_tool_callback()
        result = acc.finalize()
        assert len(result.message['tool_calls']) == 1
        assert result.message.get('_flood_collapsed')
        assert result.usage.get('_flood_salvage')


def test_native_flood_evidence_links_wire_positions_and_archive(monkeypatch, caplog):
    """The trigger still captures wire identity + archive; nothing is raised."""
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    acc = _sse(native=True)
    reference = {'archiveId': 'raw-test', 'taskId': 'task-test',
                 'attemptId': 'attempt-test', 'turnId': 'turn-test', 'roundNum': 1}
    acc.raw_dumper.raw_archive_ref = reference
    _feed_call(acc, 0, '{"password":"private-synthetic-input"}', native=True)
    assert acc._flood_salvage is False
    _feed_call(acc, 1, '{"password":"private-synthetic-input"}', native=True)
    assert acc._flood_salvage is True
    # Evidence lives on the accumulator and was dumped to the raw anomaly log;
    # the adopted message carries the bounded marker instead of a raised error.
    assert acc._flood_evidence['tool'] == 'get_conversation'
    assert acc._flood_evidence['count'] == 2
    assert acc._flood_evidence['rawArchiveRef'] == reference
    incident = next(record for record in caplog.records
                    if getattr(record, 'tofu_event_name', '') == 'llm.tool_call_flood')
    assert incident.tofu_event_fields['trace_id'] == 'flood-trace'
    assert incident.tofu_event_fields['raw_archive_ref']['archiveId'] == 'raw-test'
    assert 'private-synthetic-input' not in caplog.text


def test_native_start_retransmission_is_observed_without_counting_another_call(monkeypatch):
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    acc = _sse(native=True)
    event = {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'tool_use', 'id': 'call_0',
                               'name': 'get_conversation', 'input': {'conv_id': 'example'}}}
    for _ in range(100):
        acc.feed_line('data: ' + json.dumps(event))
    _feed_call(acc, 1, native=True)
    # The second completed call with identical args triggers the salvage.
    _feed_call(acc, 2, native=True)
    assert acc._flood_salvage is True
    assert acc._flood_evidence['tool'] == 'get_conversation'
    assert acc._flood_evidence['count'] == 2


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


def test_interleaved_argument_placeholders_do_not_look_like_completed_twins(monkeypatch):
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    acc = _sse()
    for index in range(5):
        _feed_call(acc, index, '')
    for index in range(5):
        acc.feed_line('data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [
            {'index': index, 'function': {'arguments': json.dumps({'conv_id': str(index)})}}
        ]}}]}))
    assert len(acc.finalize().message['tool_calls']) == 5


def test_final_empty_argument_twins_collapse(monkeypatch):
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2')
    acc = _sse()
    for index in range(3):
        _feed_call(acc, index, '')
    result = acc.finalize()
    assert len(result.message['tool_calls']) == 1
    assert result.message.get('_flood_collapsed')
    assert result.usage.get('_flood_salvage')


def test_salvaged_prefix_without_provider_finish_still_requires_recovery(monkeypatch, task):
    """Collapsing duplicate calls cannot manufacture provider completion."""
    from lib.tasks_pkg.stream_handler.api import analyse_stream_result
    from lib.tasks_pkg.stream_handler import _budget
    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *args: None)
    task.update(content='', thinking='')
    monkeypatch.delenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', raising=False)
    acc = _sse()
    _feed_call(acc, 0)
    _feed_call(acc, 1)
    result = acc.finalize()
    msg = result.message
    assert msg.get('_flood_collapsed')
    usage = result.usage
    assert usage.get('_flood_salvage')
    assert usage.get('_missing_done')  # we aborted before any DONE marker
    decision = analyse_stream_result(
        msg, result.compatibility_finish_reason, task, 'flood-ad', 'test-model',
        0, 0, [], usage=usage)
    assert decision['action'] == 'continue'


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


@pytest.mark.parametrize(('value', 'expected'), [('invalid', 2), ('1', 2), ('99999', 4096)])
def test_limit_is_bounded(monkeypatch, value, expected):
    monkeypatch.setenv('TOFU_TOOL_CALL_FLOOD_HARD_CAP', value)
    assert identical_tool_call_limit() == expected


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
