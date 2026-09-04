"""Focused tests for the reusable tool-progress sink.

``ToolProgressSink`` wraps ``append_event``, coalesces high-frequency output
chunks, keeps a bounded reconnect snapshot, and is presentation-only. These
tests inject a collector callback so they run without a live task runtime.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.project_mod.config import MAX_COMMAND_OUTPUT


pytestmark = pytest.mark.unit


def _collector():
    events: list[dict] = []

    def append(_task, event):
        events.append(event)
        return len(events)

    return events, append


def _make_sink(append, **kwargs):
    from lib.tasks_pkg.tool_runtime.progress import ToolProgressSink

    defaults = {
        'round_entry': {'toolCallId': 'call-1', 'toolName': 'run_command'},
        'append_event_fn': append,
        'coalesce_ms': 1000.0,
        'coalesce_bytes': 4096,
    }
    defaults.update(kwargs)
    return ToolProgressSink(
        {'id': 'task-1'},
        round_num=3,
        tool_call_id='call-1',
        tool_name='run_command',
        **defaults,
    )


# ── Frame shape + per-call sequence ────────────────────────
def test_emitted_frame_carries_versioned_correlation_fields():
    events, append = _collector()
    sink = _make_sink(append, coalesce_bytes=1)
    sink.publish('stdout', 'ok')
    assert sink.emitted == 1

    event = events[0]
    assert event['type'] == 'tool_progress'
    assert event['contractVersion'] == 'tofu.tool-progress/v1'
    assert event['version'] == 1
    assert event['taskId'] == 'task-1'
    assert event['roundNum'] == 3
    assert event['toolCallId'] == 'call-1'
    assert event['toolName'] == 'run_command'
    assert event['seq'] == 1
    assert event['stream'] == 'stdout'
    assert event['chunk'] == 'ok'
    assert event['bytes'] == 2
    assert event['chars'] == 2
    assert event['spooling'] is False
    assert event['truncated'] is False


def test_seq_is_monotonic_across_flushes():
    events, append = _collector()
    sink = _make_sink(append, coalesce_bytes=1)
    sink.publish('stdout', 'a')
    sink.publish('stdout', 'b')
    sink.flush()
    assert [event['seq'] for event in events] == [1, 2]
    assert sink.stats['seq'] == 2


# ── Coalescing + stream ordering ───────────────────────────
def test_consecutive_same_stream_chunks_merge_without_reordering():
    events, append = _collector()
    sink = _make_sink(append)
    sink.publish('stdout', 'a')
    sink.publish('stdout', 'b')
    sink.publish('stderr', 'c')
    assert sink.emitted == 0  # trailing window, nothing flushed yet
    assert sink.flush() == 2

    assert [(e['stream'], e['chunk']) for e in events] == [
        ('stdout', 'ab'), ('stderr', 'c')]
    assert events[0]['spooling'] is True  # two chunks folded into one frame
    assert events[0]['seq'] == 1
    assert events[1]['spooling'] is False
    assert events[1]['seq'] == 2


def test_interleaved_streams_keep_observed_order():
    events, append = _collector()
    sink = _make_sink(append)
    sink.publish('stdout', 'a')
    sink.publish('stderr', 'b')
    sink.publish('stdout', 'c')
    sink.flush()
    assert [(e['stream'], e['chunk']) for e in events] == [
        ('stdout', 'a'), ('stderr', 'b'), ('stdout', 'c')]


def test_byte_threshold_flushes_immediately():
    events, append = _collector()
    sink = _make_sink(append, coalesce_ms=10_000, coalesce_bytes=4)
    sink.publish('stdout', 'abcd')
    assert sink.emitted == 1
    assert events[0]['chunk'] == 'abcd'


# ── Bounded reconnect snapshot ─────────────────────────────
def test_reconnect_snapshot_is_bounded_and_flagged():
    events, append = _collector()
    round_entry = {'toolCallId': 'call-1', 'toolName': 'run_command'}
    sink = _make_sink(append, round_entry=round_entry)
    raw = 'A' * 80_000 + 'M' * 120_000 + 'Z' * 80_000

    sink.publish('stdout', raw)
    sink.flush()

    partial = round_entry['_partialOutput']
    assert len(partial) <= MAX_COMMAND_OUTPUT
    assert partial.startswith('A' * 1000)
    assert partial.endswith('Z' * 1000)
    assert 'live output truncated: 280,000 chars total' in partial
    assert round_entry['_partialOutputTotalChars'] == len(raw)
    assert round_entry['_partialOutputTruncated'] is True
    assert ''.join(e['chunk'] for e in events) == raw
    assert all(e['truncated'] is True for e in events)


def test_reconnect_snapshot_is_exact_below_budget():
    events, append = _collector()
    round_entry = {'toolCallId': 'call-2', 'toolName': 'run_command'}
    sink = _make_sink(append, round_entry=round_entry)
    sink.publish('stdout', 'prefix')
    sink.publish('stderr', '-suffix')
    sink.flush()

    assert round_entry['_partialOutput'] == 'prefix-suffix'
    assert round_entry['_partialOutputTotalChars'] == len('prefix-suffix')
    assert '_partialOutputTruncated' not in round_entry
    assert all(e['truncated'] is False for e in events)


# ── flush / close / drop accounting ────────────────────────
def test_flush_drains_pending_tail():
    events, append = _collector()
    sink = _make_sink(append)
    sink.publish('stdout', 'a')
    assert sink.emitted == 0
    assert sink.flush() == 1
    assert sink.emitted == 1
    assert events[0]['chunk'] == 'a'


def test_close_drains_and_stamps_terminal_reason():
    events, append = _collector()
    sink = _make_sink(append)
    sink.publish('stdout', 'tail')
    assert sink.close(terminal_reason='cancelled') == 1
    assert sink.closed is True
    assert events[-1]['terminalReason'] == 'cancelled'
    # Idempotent and sealed against later publishes.
    assert sink.close() == 0
    sink.publish('stdout', 'late')
    assert sink.emitted == 1
    assert sink.stats['dropped'] == 1


def test_close_with_reason_and_no_pending_emits_terminal_frame():
    events, append = _collector()
    sink = _make_sink(append)
    assert sink.close(terminal_reason='completed') == 1
    assert events[0]['chunk'] == ''
    assert events[0]['terminalReason'] == 'completed'


def test_empty_chunks_are_dropped():
    _events, append = _collector()
    sink = _make_sink(append)
    sink.publish('stdout', '')
    sink.publish('stderr', None)
    sink.flush()
    assert sink.emitted == 0
    assert sink.stats['dropped'] == 2


def test_callback_failure_is_nonfatal():
    events: list[dict] = []

    def boom(_task, _event):
        raise RuntimeError('progress sink down')

    sink = _make_sink(boom)
    sink.publish('stdout', 'a')
    sink.publish('stdout', 'b')
    # Neither publish nor the final flush may raise.
    assert sink.flush() == 0
    assert sink.close() == 0
    assert sink.stats['failed_emits'] >= 1
    assert sink.emitted == 0
    assert events == []


# ── UTF-8 split handling ───────────────────────────────────
def test_multibyte_utf8_split_across_byte_chunks():
    events, append = _collector()
    sink = _make_sink(append)
    # '€' (U+20AC) is 0xE2 0x82 0xAC in UTF-8.
    sink.publish('stdout', b'\xe2\x82')
    sink.publish('stdout', b'\xac')
    assert sink.flush() == 1
    assert events[0]['chunk'] == '€'
    assert events[0]['bytes'] == 3
    assert events[0]['chars'] == 1


def test_bytes_and_chars_are_utf8_aware():
    events, append = _collector()
    sink = _make_sink(append, coalesce_bytes=1)
    sink.publish('stdout', 'héllo')  # 5 chars, 6 UTF-8 bytes
    assert events[0]['chars'] == 5
    assert events[0]['bytes'] == 6


# ── Stats ──────────────────────────────────────────────────
def test_stats_count_emitted_coalesced_dropped():
    events, append = _collector()
    sink = _make_sink(append)
    sink.publish('stdout', 'a')
    sink.publish('stdout', 'b')
    sink.publish('stdout', 'c')  # three chunks coalesce into one frame
    sink.publish('stdout', '')
    sink.flush()

    stats = sink.stats
    assert stats['emitted'] == 1
    assert stats['coalesced'] == 2  # 3 raw chunks folded into 1 frame
    assert stats['dropped'] == 1    # the empty chunk
    assert stats['raw_chunks'] == 3
    assert stats['observed_chars'] == 3
    assert stats['observed_bytes'] == 3
    assert len(events) == 1


# ── Thin context API ───────────────────────────────────────
def test_progress_sink_for_context_reads_correlation():
    events, append = _collector()
    context = SimpleNamespace(
        task={'id': 'ctx-task'},
        round_num=7,
        tool_call_id='ctx-call',
        tool_name='ctx_tool',
        round_entry={'toolCallId': 'ctx-call', 'toolName': 'ctx_tool'},
    )

    from lib.tasks_pkg.tool_runtime.progress import progress_sink_for_context

    sink = progress_sink_for_context(
        context, append_event_fn=append, coalesce_bytes=1)
    sink.publish('stdout', 'x')
    assert events[0]['taskId'] == 'ctx-task'
    assert events[0]['roundNum'] == 7
    assert events[0]['toolCallId'] == 'ctx-call'
    assert events[0]['toolName'] == 'ctx_tool'


def test_bind_tool_progress_sink_binds_to_context():
    events, append = _collector()
    bound: dict = {}
    context = SimpleNamespace(
        task={'id': 'ctx-task'},
        round_num=7,
        tool_call_id='ctx-call',
        tool_name='ctx_tool',
        round_entry={'toolCallId': 'ctx-call', 'toolName': 'ctx_tool'},
    )

    def bind(sink):
        bound['sink'] = sink

    context.bind_progress_sink = bind  # type: ignore[attr-defined]

    from lib.tasks_pkg.tool_runtime.progress import bind_tool_progress_sink

    sink = bind_tool_progress_sink(
        context, append_event_fn=append, coalesce_bytes=1)
    assert bound['sink'] is sink
    sink.publish('stdout', 'x')
    assert events[0]['toolCallId'] == 'ctx-call'
