"""Streaming text preserves live snapshots, callbacks and incomplete outcomes."""
import json
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.stream_result import ProviderStreamState

pytestmark = pytest.mark.unit


def accumulator(**callbacks):
    body = {'model': 'gpt-5', 'messages': []}
    return SSEAccumulator(body, 'text-accumulation',
                          RawSSEDumper('gpt-5', 'text-accumulation', body),
                          None, time.time(), **callbacks)


def feed(acc, delta, finish=None):
    return acc.feed_payload(json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]}))


def test_live_string_snapshots_and_replacement_keep_subsequent_deltas():
    seen = []
    acc = accumulator(on_content=seen.append)
    assert not acc.produced_output()
    for value in ('content', 'thinking_text', 'thinking_signature'):
        setattr(acc, value, '')
    assert not acc.produced_output()
    acc.content, acc.thinking_text, acc.thinking_signature = 'prefix:', 'why:', 'sig:'
    frozen = acc.content
    for index, part in enumerate(('中', '\r\n', '\ud800', '🙂', '\x00', 'end')):
        feed(acc, {'content': part, 'reasoning_content': part, 'thinking_signature': part})
        expected = ''.join(('中', '\r\n', '\ud800', '🙂', '\x00', 'end')[:index + 1])
        assert acc.content == 'prefix:' + expected
        assert acc.thinking_text == 'why:' + expected
        assert acc.thinking_signature == 'sig:' + expected
        assert acc.produced_output()
    assert frozen == 'prefix:'
    assert ''.join(seen) == expected
    acc.content = 'replacement'
    feed(acc, {'content': '!'}, 'stop')
    acc.feed_payload('[DONE]')
    result = acc.finalize()
    assert result.message == {'role': 'assistant', 'content': 'replacement!',
                              'reasoning_content': 'why:' + expected,
                              'thinking_signature': 'sig:' + expected}
    assert result.state is ProviderStreamState.PROVIDER_FINISHED


@pytest.mark.parametrize('terminal,state', [
    ('finish', ProviderStreamState.PROVIDER_FINISHED),
    ('eof', ProviderStreamState.PREMATURE_CLOSE),
    ('abort', ProviderStreamState.CLIENT_ABORTED),
    ('malformed', ProviderStreamState.MALFORMED_STREAM),
])
def test_fragmented_text_keeps_callbacks_and_partial_terminal_evidence(terminal, state):
    content, thinking = [], []
    acc = accumulator(on_content=content.append, on_thinking=thinking.append)
    pieces = ['chunk-中文🙂\r\n' + str(index) for index in range(257)]
    for piece in pieces:
        feed(acc, {'content': piece, 'reasoning_details': [
            {'type': 'thinking', 'thinking': piece, 'signature': str(len(piece))}]})
    if terminal == 'malformed':
        acc.feed_payload('{broken')
    if terminal in ('finish', 'malformed'):
        feed(acc, {}, 'stop')
        acc.feed_payload('[DONE]')
    elif terminal == 'abort':
        acc.mark_aborted()
    result = acc.finalize()
    assert result.state is state
    assert content == thinking == pieces
    assert result.message['content'] == result.message['reasoning_content'] == ''.join(pieces)
    assert result.message['thinking_signature'] == ''.join(str(len(piece)) for piece in pieces)
    assert result.evidence.content_chars == result.evidence.reasoning_chars == sum(map(len, pieces))
    assert result.evidence.malformed_frame_count == (1 if terminal == 'malformed' else 0)


@pytest.mark.parametrize('attribute,field', [('content', 'content'),
    ('thinking_text', 'reasoning_content'), ('thinking_signature', 'thinking_signature')])
def test_clearing_a_live_string_does_not_resurrect_its_previous_parts(attribute, field):
    acc = accumulator()
    feed(acc, {field: 'first'})
    before = getattr(acc, attribute)
    setattr(acc, attribute, '')
    assert not acc.produced_output()
    feed(acc, {field: 'second'})
    assert before == 'first'
    assert getattr(acc, attribute) == 'second'


def test_callback_failure_keeps_the_accepted_partial_text_and_failure_identity():
    failure = RuntimeError('observer failed')

    def refuse(_text):
        raise failure

    acc = accumulator(on_content=refuse)
    with pytest.raises(RuntimeError) as raised:
        feed(acc, {'content': 'accepted before the observer failed'})
    assert raised.value is failure
    result = acc.finalize()
    assert result.message['content'] == 'accepted before the observer failed'
    assert result.state is ProviderStreamState.PREMATURE_CLOSE


def test_text_buffer_copy_work_grows_with_merge_depth_and_releases_fragments():
    import gc
    import weakref
    from lib.llm._stream_text import StreamTextBuffer

    copied = []

    class CountedText(str):
        def __add__(self, other):
            copied.append(len(self) + len(other))
            return CountedText(super().__add__(other))

    buffer = StreamTextBuffer()
    fragments = [CountedText(str(index)) for index in range(1024)]
    refs = [weakref.ref(text) for text in fragments]
    expected = ''.join(fragments)
    for fragment in fragments:
        buffer.append(fragment)
    assert sum(copied) <= len(expected) * (len(fragments).bit_length() - 1)
    value = buffer.getvalue()
    assert value == expected
    del fragments, fragment
    gc.collect()
    assert not any(ref() is not None for ref in refs)
    assert buffer.getvalue() is value
    buffer.append('tail')
    assert buffer.getvalue() == expected + 'tail'


@pytest.mark.parametrize('suffix', [(), ('second', 'third')])
def test_rejected_append_preserves_every_previously_accepted_fragment(suffix):
    from lib.llm._stream_text import StreamTextBuffer

    reject = False
    failure = MemoryError('injected text allocation failure')

    class RefusingText(str):
        def __add__(self, other):
            if reject:
                raise failure
            return RefusingText(super().__add__(other))

    buffer = StreamTextBuffer(RefusingText('first'))
    for part in suffix:
        buffer.append(part)
    reject = True
    with pytest.raises(MemoryError) as raised:
        buffer.append('rejected')
    assert raised.value is failure
    assert buffer.getvalue() == 'first' + ''.join(suffix)
