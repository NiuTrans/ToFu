"""Regression: a round-0 empty stop must RETRY (empty_stop bucket), not be
declared a terminal ``content_filter``.

Root cause (proven by ``debug/repro_conv_empty_stop.py``): the sankuai gateway
occasionally returns a 200-OK ``finish_reason=stop`` with no visible content on
a very large prompt. This is TRANSIENT — replaying the identical request gets
clean content. The old heuristic in ``lib/tasks_pkg/llm_fallback.py`` labelled
ANY round-0 empty stop ``content_filter`` (terminal, NO retry), so a transient
empty dead-ended as a fabricated safety block and the conversation could not
continue.

A GENUINE policy violation arrives as :class:`ContentFilterError` (HTTP 450)
and stays terminal — that path is unchanged.

These tests lock:
  1. ``_flag_empty_stop_for_retry`` sets the ``_empty_stop`` / ``_stream_anomaly``
     flags on an unflagged round-0 empty stop (whitespace-only body, or a
     zero-chunk clean ``[DONE]``), and is a no-op on round > 0 / non-empty /
     already-flagged.
  2. End-to-end: the flagged usage, fed to ``analyse_stream_result``, yields
     ``action='continue'`` (retry) — NOT a terminal break.  This is the
     behaviour the old ``content_filter`` label prevented.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.llm_fallback._retry import _flag_empty_stop_for_retry  # noqa: E402
from lib.tasks_pkg.stream_handler.api import analyse_stream_result  # noqa: E402

pytestmark = pytest.mark.unit


def _task():
    return {'content': '', 'thinking': ''}


# ── 1) helper unit cases ──────────────────────────────────────────────

def test_zero_chunk_empty_stop_flagged():
    """The production case: finish=stop, 0 content, 0 chunks, no anomaly flag
    from the stream layer → helper flags it for retry."""
    usage = {'_chunks_received': 0, 'stream_elapsed_ms': 500}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'stop', _task(), 0, usage) is True
    assert usage['_empty_stop'] is True
    assert usage['_stream_anomaly'] is True


def test_whitespace_only_body_flagged():
    """Whitespace-only content is truthy (so _sse_core did NOT flag it) but
    strips to empty → the helper must still flag it."""
    usage = {}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': '   \n\t'}, 'stop', _task(), 0, usage) is True
    assert usage['_stream_anomaly'] is True


def test_already_flagged_by_stream_layer_is_noop():
    """When _sse_core already set _stream_anomaly, the existing retry machinery
    handles it — the helper is a no-op (returns False, leaves usage as-is)."""
    usage = {'_stream_anomaly': True}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'stop', _task(), 0, usage) is False


def test_round_gt0_left_alone():
    """Empty content after tool calls (round > 0) is legitimate — never flagged."""
    usage = {}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'stop', _task(), 2, usage) is False
    assert usage == {}


def test_non_empty_content_not_flagged():
    usage = {}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': 'hello'}, 'stop', _task(), 0, usage) is False


def test_task_content_prefix_not_flagged():
    """A Continue turn seeds task['content'] with the preserved prefix — that is
    NOT an empty round, so the helper must leave it."""
    usage = {}
    task = {'content': 'preserved prefix', 'thinking': ''}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'stop', task, 0, usage) is False


def test_non_stop_finish_not_flagged():
    usage = {}
    assert _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'length', _task(), 0, usage) is False


# ── 2) end-to-end: flagged usage → analyse_stream_result RETRIES ──────

def _fresh_analyse_task():
    return {
        'id': 'empty-stop-test', 'aborted': False, 'content': '',
        'thinking': '', 'error': None, 'events': [],
        'events_lock': threading.Lock(),
    }


def test_flagged_empty_stop_retries_not_terminal():
    """The whole point: after the helper flags a round-0 empty stop,
    analyse_stream_result RETRIES it (action='continue') instead of the old
    terminal content_filter break."""
    usage = {'_chunks_received': 0, 'stream_elapsed_ms': 800,
             'trace_id': 'M-EMPTY-STOP'}
    # helper flags it (mutates usage in place, exactly as llm_fallback does)
    flagged = _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'stop', _task(), 0, usage)
    assert flagged is True

    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=_fresh_analyse_task(),
        tid='test',
        model='aws.claude-opus-4.8',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage=usage,
    )
    # zero-byte/empty-stop bucket → retry, NOT a terminal content_filter.
    assert decision['action'] == 'continue'
    assert decision['last_finish_reason'] != 'content_filter'


def test_flagged_empty_stop_surfaces_abnormal_not_content_filter_when_exhausted():
    """Once the retry budget is exhausted the round surfaces as abnormal_stop —
    an honest transient-failure label — never a fabricated content_filter."""
    from lib.tasks_pkg.stream_handler._budget import _PREMATURE_RETRY_MAX_ZERO_BYTE
    usage = {'_chunks_received': 0, 'stream_elapsed_ms': 800,
             'trace_id': 'M-EMPTY-STOP-EX'}
    _flag_empty_stop_for_retry(
        {'role': 'assistant', 'content': ''}, 'stop', _task(), 0, usage)
    task = _fresh_analyse_task()
    task['_premature_retry_count_phase'] = _PREMATURE_RETRY_MAX_ZERO_BYTE
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.8',
        round_num=0,
        _premature_retry_count=_PREMATURE_RETRY_MAX_ZERO_BYTE,
        messages=[],
        usage=usage,
    )
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'abnormal_stop'
    assert decision['last_finish_reason'] != 'content_filter'


if __name__ == '__main__':
    import traceback
    failed = passed = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn(); passed += 1; print(f'PASS {name}')
            except Exception:
                failed += 1; print(f'FAIL {name}'); traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
