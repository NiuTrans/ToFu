"""Regression test for the 2026-05-18 stream-anomaly retry widening.

Three production fingerprints from logs/raw_sse_anomaly.log that previously
slipped through the retry net and surfaced to users as "API流异常终止":

  1. Slow zero-byte (chunks_received=0, elapsed > 15 s).  Pre-fix the
     ``_is_zero_byte`` predicate required ``stream_elapsed_ms < 15000``,
     so the 8/22 chunks=0 cases that took 15-37 s were classified as
     "expensive classic" and capped at 2 retries — and on round 0, the
     classic predicate doesn't match (no thinking), so they got 0
     retries and broke straight to abnormal_stop.

  2. ``empty_stop`` (model said finish=stop with no content).  GLM-5.1
     and MiniMax models occasionally emit thinking but no body and
     close cleanly.  Pre-fix this was not retried at all.

  3. Round-0 zero-byte at any elapsed.  The 2026-05-18 23:39:01 case
     (chunks=0, elapsed 36.3 s, model=aws.claude-opus-4.7) hit the
     legacy < 15 s gate and surfaced as "异常中断".

The fix wires ``_chunks_received`` through usage so the analyser can
detect zero-byte deterministically (regardless of elapsed time), widens
the legacy-fallback elapsed bound to 60 s, and adds a small empty-stop
retry budget.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.stream_handler import (  # noqa: E402
    _EMPTY_STOP_RETRY_MAX,
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


def test_zero_byte_round0_at_36s_now_retries():
    """The 2026-05-18 case: chunks=0 at 36 s on round 0 — must retry."""
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=_fresh_task(),
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_missing_done': True,
            '_chunks_received': 0,
            'stream_elapsed_ms': 36340,
            'trace_id': 'TRACE-2026-05-18',
        },
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1


def test_legacy_fallback_widens_to_60s():
    """When the LLM client doesn't propagate ``_chunks_received`` (older
    cluster builds), the fallback heuristic still admits 36 s zero-byte.
    """
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=_fresh_task(),
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_missing_done': True,
            'stream_elapsed_ms': 36000,
            'trace_id': 'TRACE-LEGACY',
        },
    )
    assert decision['action'] == 'continue'


def test_empty_stop_retries_on_glm_thinking_only():
    """GLM-5.1 emits 397 chars of thinking and then finish=stop with
    empty content. Pre-fix this surfaced as abnormal_stop with no retry.
    """
    task = _fresh_task()
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'a' * 400},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='glm-5.1',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_empty_stop': True,
            '_chunks_received': 44,
            'stream_elapsed_ms': 62680,
            'trace_id': 'TRACE-GLM',
        },
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1
    # Phase event must distinguish empty_stop from zero_byte
    phase_events = [e for e in task['events'] if e.get('type') == 'phase']
    assert phase_events
    assert phase_events[-1]['bucket'] == 'empty_stop'
    assert phase_events[-1]['attempt'] == 1
    assert phase_events[-1]['max'] == _EMPTY_STOP_RETRY_MAX


def test_empty_stop_eventually_breaks():
    """After _EMPTY_STOP_RETRY_MAX retries, surface abnormal_stop."""
    task = _fresh_task()
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'a' * 400},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='glm-5.1',
        round_num=0,
        _premature_retry_count=_EMPTY_STOP_RETRY_MAX,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_empty_stop': True,
            '_chunks_received': 44,
            'stream_elapsed_ms': 62680,
            'trace_id': 'TRACE-GLM-EX',
        },
    )
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'abnormal_stop'
    # task['error'] is now a typed envelope dict; the trace_id is stamped
    # into both ``detail`` (human summary) and ``raw`` (full diagnostic).
    assert task['error'] and isinstance(task['error'], dict)
    assert task['error']['kind'] == 'abnormal_stop'
    _err_text = (task['error'].get('detail', '') + ' '
                 + task['error'].get('raw', ''))
    assert 'TRACE-GLM-EX' in _err_text


def test_empty_stop_with_zero_byte_does_not_double_count():
    """A zero-byte event also has _empty_stop=True (when finish=stop
    came through). The zero-byte path must take precedence so retries
    use the larger zero-byte budget, not the small empty-stop one.
    """
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=_fresh_task(),
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_empty_stop': True,
            '_chunks_received': 0,
            'stream_elapsed_ms': 4500,
            'trace_id': 'TRACE-OVERLAP',
        },
    )
    assert decision['action'] == 'continue'
    # If zero-byte path won, retry counter is 1 against the large cap
    assert decision['premature_retry_count'] == 1


def test_chunks_received_field_is_propagated_from_llm_client():
    """Smoke check that the LLM client emits ``_chunks_received`` so
    the stream handler can consume it. We read the module to verify
    the field-set keyword exists at the source-level (cheap regression
    against accidental removal).

    The SSE finalize logic was unified into ``lib/llm/_sse_core.py``
    (shared by the sync ``stream.py`` and async ``astream.py`` shells),
    so the field is now set there.
    """
    import os
    src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'lib/llm/_sse_core.py',
    )
    with open(src) as f:
        content = f.read()
    assert "usage['_chunks_received']" in content, (
        "lib/llm/_sse_core.py must set usage['_chunks_received'] so "
        "stream_handler can detect zero-byte gateway hangs deterministically."
    )


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
