"""Stable byte-level contract for the shared provider SSE framer."""

from __future__ import annotations

import random

import pytest

from lib.llm._sse_framer import SSEEvent, SSEFramer


pytestmark = pytest.mark.unit


def _frame(wire: bytes, cuts: list[int]) -> tuple[list[SSEEvent], object]:
    framer = SSEFramer()
    events: list[SSEEvent] = []
    offset = 0
    for size in cuts:
        events.extend(framer.feed(wire[offset:offset + size]))
        offset += size
    if offset < len(wire):
        events.extend(framer.feed(wire[offset:]))
    events.extend(framer.finalize())
    return events, framer.drain_issues()


def test_utf8_bom_crlf_multiline_and_done_survive_arbitrary_chunks():
    wire = (
        b'\xef\xbb\xbf: comment\r\n'
        b'id: event-7\r\nevent: delta\r\nretry: 1500\r\n'
        b'data: hello\r\ndata: '
        + '世界'.encode('utf-8')
        + b'\r\n\r\ndata: [DONE]\n\n'
    )
    partitions = [[1] * len(wire)]
    for seed in range(8):
        rng = random.Random(seed)
        remaining = len(wire)
        cuts = []
        while remaining:
            size = rng.randint(1, min(19, remaining))
            cuts.append(size)
            remaining -= size
        partitions.append(cuts)

    for cuts in partitions:
        events, issues = _frame(wire, cuts)
        assert events == [
            SSEEvent(
                data='hello\n世界', event='delta', event_id='event-7',
                retry_ms=1500,
            ),
            SSEEvent(data='[DONE]'),
        ]
        assert issues.count == 0


def test_one_chunk_can_contain_multiple_events_and_cr_delimiters():
    events, issues = _frame(
        b'data: one\r\rdata: two\n\ndata: three\r\n\r\n',
        [10_000],
    )
    assert [event.data for event in events] == ['one', 'two', 'three']
    assert issues.count == 0


def test_eof_tail_is_malformed_instead_of_implicitly_dispatched():
    events, issues = _frame(b'data: incomplete', [4, 3, 100])
    assert events == []
    assert issues.count == 1
    assert issues.diagnostics[0].startswith('truncated_event:')


def test_complete_keepalive_comment_at_eof_is_not_a_truncated_event():
    events, issues = _frame(b': keep-alive\n', [1] * 13)

    assert events == []
    assert issues.count == 0


def test_invalid_utf8_discards_only_its_event_and_resynchronizes():
    events, issues = _frame(
        b'data: good\n\ndata: bad-\xff\n\ndata: recovered\n\n',
        [10_000],
    )
    assert [event.data for event in events] == ['good', 'recovered']
    assert issues.count == 1
    assert issues.diagnostics[0].startswith('invalid_utf8:')
    assert 'bad-' not in issues.diagnostics[0]


def test_event_size_limit_is_hard_and_next_event_survives():
    framer = SSEFramer(max_event_bytes=32)
    events = framer.feed(
        b'data: ' + (b'x' * 80) + b'\n\ndata: safe\n\n')
    events.extend(framer.finalize())
    issues = framer.drain_issues()

    assert events == [SSEEvent(data='safe')]
    assert issues.count == 1
    assert issues.diagnostics[0].startswith('event_too_large:')
    assert len(issues.diagnostics[0]) <= 240


@pytest.mark.parametrize('tail', [b'data: ', b'\xff', b'\xf0\x9f', b'x' * 100])
def test_lazy_terminal_does_not_record_lookahead_faults(tail):
    framer = SSEFramer(max_event_bytes=32)
    events = framer.iter_events(b'data: [DONE]\n\n' + tail)
    assert next(events) == SSEEvent(data='[DONE]')
    events.close()
    framer.close()
    assert framer.finalize() == []
    assert framer.drain_issues().count == 0
    with pytest.raises(RuntimeError):
        framer.feed(b'data: later\n\n')


def test_intentional_close_keeps_prior_faults_without_manufacturing_eof():
    framer = SSEFramer()
    events = framer.iter_events(b'data: \xff\n\ndata: [DONE]\n\ndata: ')
    assert next(events).data == '[DONE]'
    events.close()
    framer.close()
    framer.finalize()
    issues = framer.drain_issues()
    assert issues.count == 1
    assert issues.diagnostics[0].startswith('invalid_utf8:')


@pytest.mark.parametrize('wrap', [bytes, bytearray, memoryview])
def test_lazy_terminal_keeps_decode_memory_bounded_independent_of_chunk_size(wrap):
    import tracemalloc

    # Caller-owned input is not parser retention; allocate it before measuring.
    wire = wrap(b'data: [DONE]\n\n' + b'x' * (8 << 20))
    tracemalloc.start()
    try:
        framer = SSEFramer()
        events = framer.iter_events(wire)
        assert next(events).data == '[DONE]'
        events.close()
        framer.close()
        _current, peak = tracemalloc.get_traced_memory()
        assert peak < 512 << 10
    finally:
        tracemalloc.stop()


def test_dense_invalid_utf8_keeps_bounded_work_and_exact_fault_evidence():
    import time

    framer = SSEFramer()
    # Four full decode slices: fail closed but do not retry decoding/copying
    # the remainder for each invalid byte. The budget is deliberately loose.
    invalid_size = 256 << 10
    wire = b'data: ' + b'\xff' * invalid_size + b'\n\ndata: recovered\n\n'
    started = time.monotonic()
    assert framer.feed(wire) == [SSEEvent(data='recovered')]
    assert time.monotonic() - started < 1.0
    issues = framer.drain_issues()
    assert issues.count == invalid_size
    assert issues.diagnostics == tuple(
        f'invalid_utf8: invalid UTF-8 near stream byte {offset}'
        for offset in range(6, 10))


@pytest.mark.parametrize('wire', [
    b'data: good\n\ndata: \xff\n\ndata: [DONE]\n\n',
    b'data: good\r\rdata: \xe4\xb8\r\rdata: [DONE]\r\r',
])
def test_fault_count_and_byte_offset_do_not_depend_on_transport_cuts(wire):
    expected_events, expected_issues = _frame(wire, [len(wire)])
    assert expected_issues.count >= 1
    for cut in range(len(wire) + 1):
        events, issues = _frame(wire, [cut, len(wire) - cut])
        assert events == expected_events
        assert issues == expected_issues
