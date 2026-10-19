"""Protocol termination is independent of transport EOF and chunk boundaries."""
from __future__ import annotations

import json
import random
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm._sse_stream import SSEByteStream
from lib.llm.anthropic_outbound import AnthropicSSETranslator
from lib.llm.responses_outbound import ResponsesSSETranslator
from lib.llm.stream_result import ProviderStreamState
from lib.llm.llm_errors import RetryableAPIError
from tests.sse.test_sse_core_parity import _run_async, _run_sync

pytestmark = pytest.mark.unit


def _event(payload, *, event=''):
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return ((f'event: {event}\n' if event else '') + f'data: {data}\n\n').encode()


def _response(protocol):
    """Return complete model work and its separate protocol terminal."""
    if protocol == 'openai':
        return (
            _event({'choices': [{'delta': {'content': 'complete 世界'}}]})
            + _event({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                      'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}),
            _event('[DONE]'),
        )
    if protocol == 'anthropic':
        events = [
            {'type': 'message_start', 'message': {
                'role': 'assistant', 'content': [],
                'usage': {'input_tokens': 7, 'output_tokens': 0}}},
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'text', 'text': 'complete 世界'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
             'usage': {'output_tokens': 3}},
        ]
        return (
            b''.join(_event(item, event=item['type']) for item in events),
            _event({'type': 'message_stop'}, event='message_stop'),
        )
    return (
        _event({'type': 'response.output_text.delta', 'output_index': 0,
                'delta': 'complete 世界'}, event='response.output_text.delta'),
        _event({'type': 'response.completed', 'response': {
            'id': 'response-test', 'status': 'completed',
            'output': [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'complete 世界'}]}],
            'usage': {'input_tokens': 7, 'output_tokens': 3}}},
            event='response.completed'),
    )


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


def _accumulate(chunks, protocol):
    translator = {
        'openai': lambda: None,
        'anthropic': AnthropicSSETranslator,
        'responses': ResponsesSSETranslator,
    }[protocol]()
    acc = SSEAccumulator({'model': 'test'}, 'test', _NoCapture(), translator,
                         time.monotonic())
    stream = SSEByteStream(acc)
    for chunk in chunks:
        if stream.feed(chunk):
            break
    stream.finalize()
    return acc.finalize()


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('newline', [b'\n', b'\r', b'\r\n'])
def test_every_two_part_cut_and_random_byte_partitions_have_identical_result(protocol, newline):
    prefix, terminal = _response(protocol)
    wire = (prefix + terminal + b'data: [DONE]\n\n').replace(b'\n', newline)
    expected = _accumulate([wire], protocol)
    assert expected.state is ProviderStreamState.PROVIDER_FINISHED
    partitions = [[wire[:cut], wire[cut:]] for cut in range(len(wire) + 1)]
    partitions.append([wire[index:index + 1] for index in range(len(wire))])
    for seed in range(5):
        rng = random.Random(seed)
        chunks = []
        offset = 0
        while offset < len(wire):
            size = rng.randint(1, 23)
            chunks.append(wire[offset:offset + size])
            offset += size
        partitions.append(chunks)
    for chunks in partitions:
        result = _accumulate(chunks, protocol)
        assert result.state is expected.state
        assert result.message == expected.message
        assert result.provider_finish_reason == expected.provider_finish_reason
        assert result.usage['prompt_tokens'] == 7
        assert result.usage['completion_tokens'] == 3
        assert result.evidence.sse_event_count == expected.evidence.sse_event_count
        assert result.evidence.malformed_frame_count == 0


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('tail', [
    b'data: ', b'data: [DONE]', b'data: [DONE]\n',
    b'data: \xf0\x9f', b'data: \xff\n\n',
    b'data: ' + b'x' * ((1 << 20) + 1) + b'\n\n',
], ids=['field', 'sentinel', 'delimiter', 'partial-utf8', 'invalid-utf8', 'oversized'])
def test_terminal_ignores_tail_even_in_same_transport_chunk(run, protocol, tail):
    prefix, terminal = _response(protocol)
    result = run([], protocol=protocol, raw_chunks=[prefix + terminal + tail])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'complete 世界'
    assert result.provider_finish_reason == 'stop'
    assert result.usage['prompt_tokens'] == 7
    assert result.usage['completion_tokens'] == 3
    assert result.evidence.done_seen
    assert result.evidence.malformed_frame_count == 0


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_terminal_tail_cannot_add_content_usage_or_error(run, protocol):
    prefix, terminal = _response(protocol)
    tail = (_event({'choices': [{'delta': {'content': 'DO NOT APPEND'}}],
                    'usage': {'prompt_tokens': 999}})
            + _event({'error': {'message': 'late error', 'http_code': 429}}))
    for chunks in ([prefix + terminal + tail], [prefix + terminal, tail]):
        result = run([], protocol=protocol, raw_chunks=chunks)
        assert result.state is ProviderStreamState.PROVIDER_FINISHED
        assert result.message['content'] == 'complete 世界'
        assert result.usage['prompt_tokens'] == 7


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('corrupt', [
    b'data: {invalid-json}\n\n', b'data: \xff\n\n',
    b'data: ' + b'x' * ((1 << 20) + 1) + b'\n\n',
], ids=['json', 'utf8', 'oversized'])
def test_terminal_never_erases_prior_corruption(run, protocol, corrupt):
    prefix, terminal = _response(protocol)
    result = run([], protocol=protocol,
                 raw_chunks=[prefix + corrupt + terminal + b'data: '])
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert not result.is_verified_complete
    assert result.evidence.malformed_frame_count >= 1
    assert result.message['content'] == 'complete 世界'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_incomplete_terminal_remains_malformed_at_eof(run, protocol):
    prefix, terminal = _response(protocol)
    result = run([], protocol=protocol, raw_chunks=[prefix + terminal[:-1]])
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert any(d.startswith('truncated_event:') for d in result.evidence.diagnostics)


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_finish_reason_alone_does_not_allow_discarding_pending_data(run):
    prefix, _terminal = _response('openai')
    result = run([], raw_chunks=[prefix + b'data: {"usage":'])
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.evidence.provider_finish_seen
    assert not result.evidence.done_seen


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_anthropic_tool_input_is_validated_before_terminal_stop(run):
    events = [
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'tool_use', 'id': 'call-1',
                           'name': 'read_files', 'input': {}}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'input_json_delta', 'partial_json': '{"path":'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}},
        {'type': 'message_stop'},
    ]
    wire = b''.join(_event(item, event=item['type']) for item in events)
    with pytest.raises(RetryableAPIError, match='incomplete at message stop'):
        run([], protocol='anthropic', raw_chunks=[wire])


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_complete_anthropic_tool_with_every_trailer_cut_preserves_payload_and_usage(run):
    events = [
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'tool_use', 'id': 'call-1',
                           'name': 'read_files', 'input': {}}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'input_json_delta', 'partial_json': '{"path":"README.md"}'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
         'usage': {'input_tokens': 7, 'output_tokens': 3}},
        {'type': 'message_stop'},
    ]
    wire = b''.join(_event(item, event=item['type']) for item in events)
    trailer = b'data: [DONE]\n\n'
    for cut in range(len(trailer) + 1):
        result = run([], protocol='anthropic',
                     raw_chunks=[wire + trailer[:cut], trailer[cut:]])
        assert result.state is ProviderStreamState.PROVIDER_FINISHED
        assert result.provider_finish_reason == 'tool_calls'
        assert result.message['tool_calls'] == [{
            'id': 'call-1', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{"path":"README.md"}'}}]
        assert result.usage['completion_tokens'] == 3
        assert result.evidence.malformed_frame_count == 0


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_responses_token_limit_terminal_keeps_length_semantics(run):
    wire = _event({'type': 'response.incomplete', 'response': {
        'status': 'incomplete', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': 'partial due to token limit'}]}],
        'incomplete_details': {'reason': 'max_output_tokens'},
        'usage': {'input_tokens': 7, 'output_tokens': 3}}})
    result = run([], protocol='responses', raw_chunks=[wire + b'data: '])
    assert result.provider_finish_reason == 'length'
    assert result.message['content'] == 'partial due to token limit'
    assert result.evidence.malformed_frame_count == 0
    assert result.evidence.done_seen


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('payload', [
    {'type': 'response.failed', 'response': {'error': {
        'type': 'server_error', 'message': 'synthetic provider failure'}}},
    {'type': 'response.incomplete', 'response': {'status': 'incomplete',
        'output': [], 'incomplete_details': {'reason': 'server_error'}}},
])
def test_responses_failure_terminal_is_not_success_even_with_broken_tail(run, payload):
    with pytest.raises(RetryableAPIError):
        run([], protocol='responses', raw_chunks=[_event(payload) + b'data: \xff'])


@pytest.mark.parametrize('prior_corruption', [False, True])
def test_client_abort_discards_pending_bytes_without_erasing_prior_faults(prior_corruption):
    acc = SSEAccumulator({'model': 'test'}, 'test', _NoCapture(), None, time.monotonic())
    stream = SSEByteStream(acc)
    if prior_corruption:
        stream.feed(b'data: \xff\n\n')
    stream.feed(b'data: {"private-incomplete-input":')
    acc.mark_aborted()
    stream.finalize(interrupted=True)
    result = acc.finalize()
    assert result.state is ProviderStreamState.CLIENT_ABORTED
    assert result.evidence.malformed_frame_count == int(prior_corruption)
    assert not any(d.startswith('truncated_event:') for d in result.evidence.diagnostics)


@pytest.mark.parametrize('interrupted', [False, True])
def test_finalization_is_idempotent_and_no_further_input_is_accepted(interrupted):
    acc = SSEAccumulator({'model': 'test'}, 'test', _NoCapture(), None, time.monotonic())
    stream = SSEByteStream(acc)
    stream.feed(b'data: incomplete')
    if interrupted:
        acc.mark_aborted()
    stream.finalize(interrupted=interrupted)
    first = acc.progress.evidence()
    stream.finalize(interrupted=interrupted)
    assert acc.progress.evidence().malformed_frame_count == first.malformed_frame_count
    assert not stream.stopped  # only consumer rejection/terminal sets stopped
    with pytest.raises(RuntimeError, match='finalized'):
        stream.feed(b'data: later\n\n')


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_protocol_rejection_is_a_stop_not_success(run):
    wire = _event({'type': 'message_start'}, event='message_start')
    result = run([], raw_chunks=[wire + b'data: '])
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert not result.evidence.done_seen
    assert result.evidence.malformed_frame_count == 1
    assert result.evidence.diagnostics[0].startswith('protocol_mismatch:')


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_terminal_without_finish_remains_premature_even_with_broken_tail(run, protocol):
    if protocol == 'openai':
        prefix = _event({'choices': [{'delta': {'content': 'unverified prefix'}}]})
        terminal = _event('[DONE]')
    else:
        prefix = _event({'type': 'content_block_start', 'index': 0,
                         'content_block': {'type': 'text', 'text': 'unverified prefix'}})
        terminal = _event({'type': 'message_stop'})
    result = run([], protocol=protocol, raw_chunks=[prefix + terminal + b'data: \xff'])
    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.message['content'] == 'unverified prefix'
    assert result.evidence.done_seen
    assert not result.evidence.provider_finish_seen
    assert not result.is_verified_complete


@pytest.mark.parametrize('ending', ['terminal', 'eof', 'done_without_finish', 'empty_stop'])
def test_malformed_anomaly_reports_bounded_category_without_provider_body(tmp_path, monkeypatch, ending):
    from lib.llm import diagnostics
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '')
    body = {'model': 'test'}
    dumper = diagnostics.RawSSEDumper('test', 'synthetic', body)
    acc = SSEAccumulator(body, 'synthetic', dumper, None, time.monotonic())
    stream = SSEByteStream(acc)
    prefix, terminal = _response('openai')
    if ending in {'done_without_finish', 'empty_stop'}:
        prefix = b''
    if ending == 'eof':
        prefix = _event({'choices': [{'delta': {'content': 'unverified prefix'}}]})
        terminal = b''
    if ending == 'empty_stop':
        prefix = _event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
    stream.feed(prefix + b'data: CANARY-PRIVATE-\xff\n\n' + terminal)
    stream.finalize()
    assert acc.finalize().state is ProviderStreamState.MALFORMED_STREAM
    logged = (tmp_path / 'raw_sse_anomaly.log').read_text()
    assert 'invalid_utf8:' in logged
    assert 'CANARY-PRIVATE' not in logged
    assert 'complete 世界' not in logged
