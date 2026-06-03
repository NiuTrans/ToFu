"""Regression test: zero-byte gateway anomaly on round 0 must auto-retry.

Prior behaviour: ``_is_anomaly_empty`` had a ``round_num > 0`` guard, so
a sankuai-gateway zero-byte close on the FIRST round of a task surfaced
as a fatal "API流异常终止" error with NO retry. We split the predicate
so the zero-byte signature (no content + anomaly + tiny thinking + fast
elapsed) admits round 0, while the looser ``stream_anomaly + empty``
case keeps its historical first-round guard.

Bug evidence (from logs/raw_sse_anomaly.log line 43):
    [2026-05-12 13:51:42] ANOMALY reason=missing_done elapsed=4.51s
    summary={chunks: 0, finish_reason: stop, content_len: 0, thinking_len: 0}
    ring_lines=0 ring_bytes=0
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.stream_handler import (  # noqa: E402
    _PREMATURE_RETRY_MAX_CLASSIC,
    _PREMATURE_RETRY_MAX_ZERO_BYTE,
    analyse_stream_result,
)


def _fresh_task():
    import threading
    return {
        'id': 'testtask',
        'aborted': False,
        'content': '',
        'thinking': '',
        'error': None,
        'events': [],
        'events_lock': threading.Lock(),
    }


def _zero_byte_usage(elapsed_ms: int = 4500):
    return {
        '_stream_anomaly': True,
        '_empty_stop': True,
        'trace_id': 'M-TEST-1',
        'resp_trace_id': 'r-test-1',
        'stream_elapsed_ms': elapsed_ms,
    }


def test_zero_byte_round0_retries_now():
    """The bug: round 0 + zero-byte usage MUST trigger 'continue' (retry)."""
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=_fresh_task(),
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'continue', \
        f"Round-0 zero-byte should retry; got action={decision['action']!r}"
    assert decision['premature_retry_count'] == 1


def test_zero_byte_uses_large_cap():
    task = _fresh_task()
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=_PREMATURE_RETRY_MAX_ZERO_BYTE - 1,
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == _PREMATURE_RETRY_MAX_ZERO_BYTE


def test_zero_byte_eventually_breaks_after_cap():
    """When the (high) cap is exhausted, surface abnormal_stop."""
    task = _fresh_task()
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=_PREMATURE_RETRY_MAX_ZERO_BYTE,
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'abnormal_stop'
    # task['error'] is now a typed envelope dict; the trace_id is stamped
    # into both ``detail`` (human summary) and ``raw`` (full diagnostic).
    assert task['error'] and isinstance(task['error'], dict)
    assert task['error']['kind'] == 'abnormal_stop'
    _err_text = (task['error'].get('detail', '') + ' '
                 + task['error'].get('raw', ''))
    assert 'M-TEST-1' in _err_text


def test_classic_premature_uses_low_cap():
    """Classic premature close (>1000 chars thinking) keeps the small cap."""
    task = _fresh_task()
    decision = analyse_stream_result(
        assistant_msg={
            'role': 'assistant',
            'content': '',
            'reasoning_content': 'x' * 2000,
        },
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=1,
        _premature_retry_count=_PREMATURE_RETRY_MAX_CLASSIC,
        messages=[],
        usage={
            '_stream_anomaly': False,
            'trace_id': 'M-TEST-2',
            'stream_elapsed_ms': 30000,
        },
    )
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'premature_close'


def test_normal_empty_round0_does_not_retry():
    """Round 0 + finish=stop + empty + NO anomaly: legitimate empty stop,
    must not enter the retry path. (Content-filter detection happens upstream
    in llm_fallback; here we just confirm the analyser doesn't loop.)"""
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=_fresh_task(),
        tid='test',
        model='gpt-4o',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={'_stream_anomaly': False, 'stream_elapsed_ms': 100},
    )
    assert decision['action'] == 'break'


def test_phase_event_fields_for_zero_byte_round0():
    """Frontend dedup relies on attempt + bucket; ensure both are emitted."""
    task = _fresh_task()
    analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage=_zero_byte_usage(),
    )
    events = task.get('events') or []
    phase_events = [e for e in events if e.get('type') == 'phase']
    assert phase_events, 'expected a phase event to be appended'
    ev = phase_events[-1]
    assert ev['phase'] == 'retrying'
    assert ev['bucket'] == 'zero_byte'
    assert ev['attempt'] == 1
    assert ev['max'] == _PREMATURE_RETRY_MAX_ZERO_BYTE


if __name__ == '__main__':
    import traceback
    failed = 0
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                passed += 1
                print(f'PASS {name}')
            except Exception:
                failed += 1
                print(f'FAIL {name}')
                traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
