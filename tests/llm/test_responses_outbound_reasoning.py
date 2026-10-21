"""Readable Responses reasoning snapshots reconcile once, independently of replay.

Synthetic provider events exercise the real translator, shared accumulator and
sync/async byte-stream shells; no provider, network or application storage.
"""
from __future__ import annotations

import json
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm._sse_stream import SSEByteStream
from lib.llm.llm_errors import RetryableAPIError
from lib.llm.responses_outbound import (
    ResponsesSSETranslator,
    responses_response_to_openai,
)
from lib.llm.stream_result import ProviderStreamState
from tests.sse.test_sse_core_parity import _run_async, _run_sync

pytestmark = pytest.mark.unit


class _NoCapture:
    enabled = False

    def line(self, *args, **kwargs):
        pass

    def payload(self, *args, **kwargs):
        pass

    def dump_anomaly(self, *args, **kwargs):
        pass

    def finish(self, *args, **kwargs):
        pass


def _acc(callback=None):
    return SSEAccumulator(
        {'model': 'synthetic'}, 'reasoning-test', _NoCapture(),
        ResponsesSSETranslator(model='synthetic'), time.monotonic(),
        on_thinking=callback)


def _feed(acc, events):
    for event in events:
        if acc.feed_line('data: ' + json.dumps(event, ensure_ascii=False)):
            break


def _route(channel='summary', *, item_id='rs-one', output_index=0, part_index=0):
    return {'item_id': item_id, 'output_index': output_index,
            'summary_index' if channel == 'summary' else 'content_index': part_index}


def _delta(text, channel='summary', **route):
    return {'type': ('response.reasoning_summary_text.delta' if channel == 'summary'
                     else 'response.reasoning_text.delta'),
            'delta': text, **_route(channel, **route)}


def _text_done(text, channel='summary', **route):
    return {'type': ('response.reasoning_summary_text.done' if channel == 'summary'
                     else 'response.reasoning_text.done'),
            'text': text, **_route(channel, **route)}


def _part_done(text, **route):
    return {'type': 'response.reasoning_summary_part.done',
            'part': {'type': 'summary_text', 'text': text}, **_route(**route)}


def _item(text, channel='summary', *, item_id='rs-one'):
    return {'type': 'reasoning', 'id': item_id, 'encrypted_content': 'opaque-only',
            channel: [{'type': 'summary_text' if channel == 'summary' else 'text',
                       'text': text}]}


def _added(*, item_id='rs-one', output_index=0):
    return {'type': 'response.output_item.added', 'output_index': output_index,
            'item': {'type': 'reasoning', 'id': item_id}}


def _item_done(item, output_index=0):
    return {'type': 'response.output_item.done', 'output_index': output_index,
            'item': item}


def _terminal(items, *, incomplete=False):
    response = {
        'id': 'response-synthetic', 'status': 'incomplete' if incomplete else 'completed',
        'output': [*items, {'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '42'}]}],
        'usage': {'input_tokens': 7, 'output_tokens': 13,
                  'output_tokens_details': {'reasoning_tokens': 9}},
    }
    if incomplete:
        response['incomplete_details'] = {'reason': 'max_output_tokens'}
    return {'type': 'response.incomplete' if incomplete else 'response.completed',
            'response': response}


def _wire(events):
    return b''.join(('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
                    for event in events)


@pytest.mark.parametrize(('channel', 'source'), [
    ('summary', 'text_done'), ('content', 'text_done'),
    ('summary', 'part_done'), ('summary', 'item_done'),
    ('content', 'item_done'), ('summary', 'terminal'), ('content', 'terminal'),
])
@pytest.mark.parametrize('prefix', ['', 'Thinking', 'Thinking summary 世界'])
def test_final_snapshots_recover_only_the_unseen_suffix(channel, source, prefix):
    complete = 'Thinking summary 世界'
    item = _item(complete, channel)
    callbacks = []
    acc = _acc(callbacks.append)
    _feed(acc, [_added(), *([_delta(prefix, channel)] if prefix else [])])
    if source != 'terminal':
        snapshot = {
            'text_done': lambda: _text_done(complete, channel),
            'part_done': lambda: _part_done(complete),
            'item_done': lambda: _item_done(item),
        }[source]()
        _feed(acc, [snapshot, snapshot])
        assert ''.join(callbacks) == complete
    terminal = _terminal([item])
    _feed(acc, [terminal])
    result = acc.finalize()
    nonstream = responses_response_to_openai(terminal['response'])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message == nonstream['choices'][0]['message']
    assert result.provider_finish_reason == 'stop'
    assert result.usage['completion_tokens_details'] == {'reasoning_tokens': 9}
    assert result.evidence.reasoning_chars == len(complete)
    assert callbacks == [text for text in (prefix, complete[len(prefix):]) if text]


@pytest.mark.parametrize('source', ['item_done', 'terminal'])
@pytest.mark.parametrize('identity', ['distinct', 'recycled', 'idless'])
def test_equal_text_at_distinct_item_and_part_positions_is_not_deduplicated(source, identity):
    items = [
        {'type': 'reasoning', 'encrypted_content': 'opaque-a',
         'summary': [{'text': 'same'}, {'text': ''}, {'text': 'same'}],
         'content': [{'text': 'plain 世界'}]},
        {'type': 'reasoning', 'encrypted_content': 'opaque-b',
         'summary': [{'text': None}, {'text': 'same'}]},
    ]
    if identity != 'idless':
        for index, item in enumerate(items):
            item['id'] = f'rs-{index}' if identity == 'distinct' else 'rs-recycled'
    callbacks = []
    acc = _acc(callbacks.append)
    if source == 'item_done':
        for index, item in enumerate(items):
            _feed(acc, [_item_done(item, index), _item_done(item, index)])
    terminal = _terminal(items)
    _feed(acc, [terminal])
    result = acc.finalize()
    expected = 'same\n\nsame\n\nplain 世界\n\nsame'
    assert result.message['reasoning_content'] == expected
    assert ''.join(callbacks) == expected
    assert result.message == responses_response_to_openai(
        terminal['response'])['choices'][0]['message']
    assert result.message['_responses_items'] == items


def test_replayed_item_and_part_starts_do_not_insert_extra_separators():
    callbacks = []
    acc = _acc(callbacks.append)
    part_added = {'type': 'response.reasoning_summary_part.added',
                  'part': {'type': 'summary_text', 'text': ''}, **_route()}
    item = {'type': 'reasoning', 'id': 'rs-one', 'summary': [
        {'text': 'first'}, {'text': 'first'}]}
    _feed(acc, [_added(), part_added, _delta('fi'), _added(), part_added,
                _text_done('first'), _text_done('first'),
                _part_done('first', part_index=1), _part_done('first', part_index=1),
                _item_done(item), _terminal([item])])
    assert acc.finalize().message['reasoning_content'] == 'first\n\nfirst'
    assert ''.join(callbacks) == 'first\n\nfirst'


@pytest.mark.parametrize('scope', ['items', 'parts'])
def test_interleaved_prefixes_wait_for_their_preceding_reasoning_snapshot(scope):
    callbacks = []
    acc = _acc(callbacks.append)
    second_route = ({'item_id': 'rs-two', 'output_index': 1}
                    if scope == 'items' else {'part_index': 1})
    _feed(acc, [_added(), *([_added(item_id='rs-two', output_index=1)]
                           if scope == 'items' else []),
                _delta('Think'), _delta('Think', **second_route)])
    assert callbacks == ['Think']
    first = _item('Think A')
    second = _item('Think B', item_id='rs-two')
    _feed(acc, [_text_done('Think A')])
    if scope == 'items':
        _feed(acc, [_item_done(first)])
        items = [first, second]
    else:
        first['summary'].append({'text': 'Think B'})
        items = [first]
    _feed(acc, [_text_done('Think B', **second_route),
                *([_item_done(second, 1)] if scope == 'items' else [_item_done(first)]),
                _terminal(items)])
    assert acc.finalize().message['reasoning_content'] == 'Think A\n\nThink B'
    assert ''.join(callbacks) == 'Think A\n\nThink B'


def test_late_item_id_binds_the_indexed_prefix_instead_of_repeating_it():
    acc = _acc()
    _feed(acc, [_delta('Think', item_id=None), _text_done('Think once'),
                _item_done(_item('Think once')), _terminal([_item('Think once')])])
    assert acc.finalize().message['reasoning_content'] == 'Think once'


def test_anonymous_delta_can_be_reconciled_when_terminal_reveals_identity():
    acc = _acc()
    _feed(acc, [{'type': 'response.reasoning_summary_text.delta', 'delta': 'Think'},
                _terminal([_item('Think once')])])
    assert acc.finalize().message['reasoning_content'] == 'Think once'


@pytest.mark.parametrize('event', [
    _delta({}), _text_done({}), _text_done(1, 'content'),
    _text_done('x', item_id=[]), _text_done('x', item_id='x' * 513),
    _text_done('x', output_index=True), _text_done('x', output_index=4096),
    _text_done('x', part_index=-1), _text_done('x', part_index=4096),
    _text_done('x', 'content', part_index=False),
    {'type': 'response.reasoning_summary_part.done', 'part': []},
    {'type': 'response.reasoning_summary_part.done', 'part': {'text': {}}},
    _item_done({'type': 'reasoning', 'summary': {}}),
    _item_done({'type': 'reasoning', 'summary': [[]]}),
    _item_done({'type': 'reasoning', 'content': [{'text': 1}]}),
    _terminal([{'type': 'reasoning', 'content': {}}]),
])
def test_malformed_reasoning_snapshots_return_typed_protocol_errors(event):
    chunks = ResponsesSSETranslator().translate(json.dumps(event))
    assert len(chunks) == 1
    assert chunks[0]['error']['type'] == 'server_error'
    assert chunks[0]['error']['http_code'] == '500'
    with pytest.raises(RetryableAPIError):
        _feed(_acc(), [event])


@pytest.mark.parametrize(('channel', 'source'), [
    ('summary', 'text_done'), ('summary', 'part_done'),
    ('summary', 'item_done'), ('summary', 'terminal'),
    ('content', 'text_done'), ('content', 'item_done'), ('content', 'terminal'),
])
@pytest.mark.parametrize('snapshot', ['', 'Thin', 'Different'])
def test_conflicting_or_shortened_reasoning_snapshot_is_never_spliced_in(channel, source, snapshot):
    acc = _acc()
    _feed(acc, [_added(), _delta('Thinking original', channel)])
    event = {
        'text_done': lambda: _text_done(snapshot, channel),
        'part_done': lambda: _part_done(snapshot),
        'item_done': lambda: _item_done(_item(snapshot, channel)),
        'terminal': lambda: _terminal([_item(snapshot, channel)]),
    }[source]()
    with pytest.raises(RetryableAPIError, match='reasoning.*prefix|prefix.*reasoning'):
        _feed(acc, [event])
    assert acc.thinking_text == 'Thinking original'
    assert not acc.saw_done


def test_explicit_item_and_output_index_cannot_resolve_to_different_reasoning_items():
    acc = _acc()
    _feed(acc, [_added(), _added(item_id='rs-two', output_index=1)])
    with pytest.raises(RetryableAPIError, match='reasoning.*identity|reasoning.*different'):
        _feed(acc, [_text_done('wrong item', item_id='rs-one', output_index=1)])


def test_recycled_reasoning_item_id_requires_output_index_for_routing():
    acc = _acc()
    _feed(acc, [_added(item_id='recycled', output_index=0),
                _added(item_id='recycled', output_index=1)])
    with pytest.raises(RetryableAPIError, match='ambiguous.*output_index'):
        _feed(acc, [{'type': 'response.reasoning_summary_text.done',
                     'item_id': 'recycled', 'summary_index': 0, 'text': 'ambiguous'}])


@pytest.mark.parametrize('metadata', [{}, {'summary': None}, {'summary': []},
                                     {'summary': [{'text': None}]}])
def test_metadata_only_reasoning_snapshots_do_not_erase_complete_deltas(metadata):
    acc = _acc()
    item = {'type': 'reasoning', 'id': 'rs-one', **metadata}
    _feed(acc, [_added(), _delta('available'),
                {**_text_done(None), 'text': None},
                _item_done(item), _terminal([item])])
    assert acc.finalize().message['reasoning_content'] == 'available'


def test_encrypted_only_empty_summaries_never_emit_text_or_paragraphs():
    callbacks = []
    acc = _acc(callbacks.append)
    items = [
        {'type': 'reasoning', 'id': 'rs-one', 'summary': [],
         'encrypted_content': 'opaque-a'},
        {'type': 'reasoning', 'id': 'rs-two', 'summary': [],
         'encrypted_content': 'opaque-b'},
    ]
    _feed(acc, [_added(),
                {'type': 'response.reasoning_summary_part.added', **_route()},
                _part_done(''), _text_done(None), _item_done(items[0]),
                _added(item_id='rs-two', output_index=1),
                _part_done('', item_id='rs-two', output_index=1),
                _item_done(items[1], 1), _terminal(items)])
    result = acc.finalize()
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message.get('reasoning_content', '') == ''
    assert callbacks == []
    assert result.evidence.reasoning_chars == 0
    assert result.usage['completion_tokens_details'] == {'reasoning_tokens': 9}
    assert result.message['_responses_items'] == items
    assert result.message == responses_response_to_openai(
        _terminal(items)['response'])['choices'][0]['message']


@pytest.mark.parametrize(('tail', 'expected'), [
    ('absent', ProviderStreamState.PREMATURE_CLOSE),
    ('truncated', ProviderStreamState.MALFORMED_STREAM),
    ('malformed', ProviderStreamState.MALFORMED_STREAM),
])
def test_reasoning_done_is_not_a_successful_response_terminal(tail, expected):
    acc = _acc()
    stream = SSEByteStream(acc)
    wire = _wire([_text_done('available summary')])
    terminal = _wire([_terminal([_item('available summary')])])
    if tail == 'truncated':
        wire += terminal[:-1]
    elif tail == 'malformed':
        wire += b'data: {invalid-json}\n\n' + terminal
    stream.feed(wire)
    stream.finalize()
    result = acc.finalize()
    assert result.state is expected
    assert not result.is_verified_complete
    assert result.message['reasoning_content'] == 'available summary'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('incomplete', [False, True])
@pytest.mark.parametrize('source', ['text_done', 'part_done', 'item_done', 'terminal'])
def test_final_only_reasoning_has_sync_async_nonstream_parity(run, incomplete, source):
    item = _item('Final-only summary 世界')
    terminal = _terminal([item], incomplete=incomplete)
    events = {
        'text_done': [_text_done('Final-only summary 世界')],
        'part_done': [_part_done('Final-only summary 世界')],
        'item_done': [_item_done(item)],
        'terminal': [],
    }[source] + [terminal]
    wire = _wire(events)
    result = run([], protocol='responses', raw_chunks=[wire[i:i + 1] for i in range(len(wire))])
    nonstream = responses_response_to_openai(terminal['response'])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message == nonstream['choices'][0]['message']
    assert result.provider_finish_reason == ('length' if incomplete else 'stop')
    assert result.usage['completion_tokens_details'] == {'reasoning_tokens': 9}


def test_nonempty_terminal_summary_cannot_drop_an_already_streamed_part():
    acc = _acc()
    _feed(acc, [_added(), _text_done('first'),
                _text_done('second', part_index=1)])
    with pytest.raises(RetryableAPIError, match='reasoning.*prefix'):
        _feed(acc, [_terminal([_item('first')])])
    assert acc.thinking_text == 'first\n\nsecond'


def test_out_of_order_reasoning_buffer_is_bounded_without_capping_live_text():
    from lib.llm._sse_framer import MAX_SSE_EVENT_BYTES

    acc = _acc()
    _feed(acc, [_added(), _added(item_id='rs-two', output_index=1)])
    chunk = 'x' * (MAX_SSE_EVENT_BYTES // 4)
    _feed(acc, [_delta(chunk, item_id='rs-two', output_index=1) for _ in range(4)])
    assert acc.thinking_text == ''
    with pytest.raises(RetryableAPIError, match='reasoning.*bound'):
        _feed(acc, [_delta('x', item_id='rs-two', output_index=1)])

    # In-order content is emitted immediately and retains only digest proofs;
    # the pending-text bound is not a total response-length limit.
    live = _acc()
    _feed(live, [_delta(chunk) for _ in range(5)])
    assert live.thinking_text == chunk * 5


def test_conflicting_terminal_part_does_not_leak_other_recovered_text():
    callbacks = []
    acc = _acc(callbacks.append)
    _feed(acc, [_added(), _delta('original')])
    with pytest.raises(RetryableAPIError):
        _feed(acc, [_terminal([{'type': 'reasoning', 'id': 'rs-one', 'summary': [
            {'text': 'original suffix'}, {'text': {}}]}])])
    assert callbacks == ['original']
    assert not acc.saw_done


@pytest.mark.parametrize('id_first', [True, False])
def test_item_id_only_and_output_index_only_events_bind_one_unambiguous_prefix(id_first):
    acc = _acc()
    first = {'output_index': None} if id_first else {'item_id': None}
    final = {'item_id': None} if id_first else {'output_index': None}
    item = _item('Think once')
    _feed(acc, [_delta('Think', **first), _text_done('Think once', **final),
                _item_done(item), _terminal([item])])
    assert acc.finalize().message['reasoning_content'] == 'Think once'


def test_unindexed_idless_item_snapshots_reconcile_by_occurrence_not_text_equality():
    acc = _acc()
    item = {'type': 'reasoning', 'summary': [{'text': 'same'}],
            'encrypted_content': 'opaque'}
    _feed(acc, [{'type': 'response.output_item.done', 'item': item},
                {'type': 'response.output_item.done', 'item': item},
                _terminal([item, item])])
    result = acc.finalize()
    assert result.message['reasoning_content'] == 'same\n\nsame'
    assert result.message['_responses_items'] == [item, item]


def test_streamed_summary_and_raw_reasoning_are_separate_ordered_paragraphs():
    acc = _acc()
    item = {'type': 'reasoning', 'id': 'rs-one',
            'summary': [{'text': 'summary complete'}],
            'content': [{'text': 'raw complete'}]}
    _feed(acc, [_added(), _delta('summary'), _delta('raw', 'content'),
                _text_done('summary complete'), _text_done('raw complete', 'content'),
                _item_done(item), _terminal([item])])
    result = acc.finalize()
    assert result.message['reasoning_content'] == 'summary complete\n\nraw complete'
    assert result.message == responses_response_to_openai(
        _terminal([item])['response'])['choices'][0]['message']


def test_terminal_cannot_insert_unseen_reasoning_before_an_emitted_output_position():
    acc = _acc()
    _feed(acc, [_text_done('later', item_id='rs-later', output_index=1)])
    with pytest.raises(RetryableAPIError, match='reasoning.*prefix|reasoning.*order'):
        _feed(acc, [_terminal([_item('earlier'), _item('later', item_id='rs-later')])])
    assert acc.thinking_text == 'later'
    assert not acc.saw_done


def test_closed_anonymous_done_binds_the_later_item_snapshot_once():
    acc = _acc()
    item = _item('done-only')
    _feed(acc, [{'type': 'response.reasoning_summary_text.done', 'text': 'done-only'},
                _item_done(item), _terminal([item])])
    assert acc.finalize().message['reasoning_content'] == 'done-only'


def test_terminal_idless_snapshot_binds_the_unindexed_started_occurrence():
    acc = _acc()
    _feed(acc, [{**_added(), 'output_index': None},
                _delta('Think', output_index=None),
                _terminal([{'type': 'reasoning', 'summary': [{'text': 'Think once'}]}])])
    assert acc.finalize().message['reasoning_content'] == 'Think once'


def test_replayed_item_start_empty_text_is_metadata_not_a_final_contradiction():
    acc = _acc()
    start = {**_added(), 'item': {'type': 'reasoning', 'id': 'rs-one',
                               'summary': [{'text': ''}]}}
    item = _item('Think once')
    _feed(acc, [start, _delta('Think'), start, _text_done('Think once'),
                _item_done(item), _terminal([item])])
    assert acc.finalize().message['reasoning_content'] == 'Think once'


def test_terminal_cannot_reorder_already_emitted_unindexed_reasoning_occurrences():
    acc = _acc()
    first, second = _item('A', item_id='rs-a'), _item('B', item_id='rs-b')
    _feed(acc, [{'type': 'response.output_item.done', 'item': first},
                {'type': 'response.output_item.done', 'item': second}])
    assert acc.thinking_text == 'A\n\nB'
    with pytest.raises(RetryableAPIError, match='reasoning.*order|reasoning.*prefix'):
        _feed(acc, [_terminal([second, first])])
    assert acc.thinking_text == 'A\n\nB'
    assert not acc.saw_done


@pytest.mark.parametrize('unindexed_text', ['', 'C'])
def test_partial_index_binding_cannot_reverse_emitted_order_behind_an_unknown_item(unindexed_text):
    acc = _acc()
    first, second = _item('A', item_id='rs-a'), _item('B', item_id='rs-b')
    unknown = _item(unindexed_text, item_id='rs-unknown')
    _feed(acc, [{'type': 'response.output_item.done', 'item': item}
                for item in (first, second, unknown)])
    expected = 'A\n\nB' + ('\n\nC' if unindexed_text else '')
    assert acc.thinking_text == expected
    _feed(acc, [_item_done(first, 1)])
    with pytest.raises(RetryableAPIError, match='reasoning.*order|reasoning.*prefix'):
        _feed(acc, [_item_done(second, 0), _terminal([])])
    assert acc.thinking_text == expected
    assert not acc.saw_done


@pytest.mark.parametrize('late_text', ['earlier', 'A extended'])
def test_mixed_index_snapshot_cannot_add_text_before_an_emitted_known_position(late_text):
    acc = _acc()
    first = _item('A', item_id='rs-a')
    later = _item('later', item_id='rs-later')
    unknown = _item('', item_id='rs-unknown')
    _feed(acc, [_item_done(first, 0),
                {'type': 'response.output_item.done', 'item': unknown},
                _item_done(later, 2)])
    assert acc.thinking_text == 'A\n\nlater'
    index = 0 if late_text == 'A extended' else 1
    item_id = 'rs-a' if index == 0 else 'rs-earlier'
    with pytest.raises(RetryableAPIError, match='reasoning.*order|reasoning.*prefix'):
        _feed(acc, [_item_done(_item(late_text, item_id=item_id), index), _terminal([])])
    assert acc.thinking_text == 'A\n\nlater'
    assert not acc.saw_done


@pytest.mark.parametrize('close_unknown', ['item', 'terminal'])
@pytest.mark.parametrize('indices', [(2, 1), (1, 2)])
def test_pending_mixed_index_drain_validates_new_emissions_before_callbacks(close_unknown, indices):
    callbacks = []
    acc = _acc(callbacks.append)
    unknown = {'type': 'reasoning', 'id': 'rs-unknown'}
    _feed(acc, [{'type': 'response.output_item.added', 'item': unknown},
                _item_done(_item('first', item_id='rs-first'), indices[0]),
                _item_done(_item('second', item_id='rs-second'), indices[1])])
    assert callbacks == []
    assert acc.thinking_text == ''
    release = ({'type': 'response.output_item.done', 'item': unknown}
               if close_unknown == 'item' else _terminal([]))
    if indices[0] > indices[1]:
        with pytest.raises(RetryableAPIError, match='reasoning.*order|reasoning.*prefix'):
            _feed(acc, [release])
        assert callbacks == []
        assert acc.thinking_text == ''
        assert not acc.saw_done
    else:
        _feed(acc, [release, _terminal([])])
        assert ''.join(callbacks) == 'first\n\nsecond'
        assert acc.finalize().state is ProviderStreamState.PROVIDER_FINISHED
