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

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.stream_handler.api import analyse_stream_result  # noqa: E402
from lib.tasks_pkg.stream_handler._budget import _EMPTY_STOP_RETRY_MAX  # noqa: E402
from lib.llm.stream_result import (  # noqa: E402
    ProviderStreamEvidence,
    ProviderStreamResult,
    ProviderStreamState,
)
from tests._registered_chat_task import registered_chat_task  # noqa: E402


pytestmark = pytest.mark.unit


def _fresh_task():
    import threading
    task = {
        'id': 'testtask',
        '_userId': 1,
        '_transientRuntime': True,
        'status': 'running',
        'aborted': False,
        'content': '',
        'thinking': '',
        'error': None,
        'events': [],
        'events_lock': threading.Lock(),
    }
    return task


def _analyse_registered(task, **kwargs):
    """Run one event-emitting analysis with an explicit registry lifecycle."""
    with registered_chat_task(task):
        return analyse_stream_result(task=task, **kwargs)


def test_zero_byte_round0_at_36s_now_retries():
    """The 2026-05-18 case: chunks=0 at 36 s on round 0 — must retry."""
    decision = _analyse_registered(
        _fresh_task(),
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': ''},
        last_finish_reason='stop',
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
    decision = _analyse_registered(
        _fresh_task(),
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': ''},
        last_finish_reason='stop',
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
    decision = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'a' * 400},
        last_finish_reason='stop',
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
    decision = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'a' * 400},
        last_finish_reason='stop',
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
    decision = _analyse_registered(
        _fresh_task(),
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': ''},
        last_finish_reason='stop',
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
    """The typed stream result projects its observed SSE count for recovery."""
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': ''},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.PREMATURE_CLOSE,
        evidence=ProviderStreamEvidence(sse_event_count=3),
    )
    projected = result.with_usage({})

    assert projected.usage['_chunks_received'] == 3


def test_typed_evidence_clears_contradictory_legacy_failure_markers():
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'complete'},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.PROVIDER_FINISHED,
        provider_finish_reason='stop',
        saw_done=True,
        saw_finish_reason=True,
        evidence=ProviderStreamEvidence(
            content_chars=8,
            content_chunks=1,
            provider_finish_seen=True,
            done_seen=True,
        ),
    )
    projected = result.with_usage({
        '_no_actionable_timeout': True,
        '_semantic_progress_timeout': True,
        '_malformed_stream': True,
        '_missing_done': True,
        '_stream_anomaly': True,
        '_empty_stop': True,
        '_tool_calls_void': 'filtered',
        '_semantic_idle_timeout_ms': 99_000,
        '_no_actionable_timeout_s': 99,
    })

    assert projected.usage['_stream_state'] == 'provider_finished'
    for stale_key in (
            '_no_actionable_timeout', '_semantic_progress_timeout',
            '_malformed_stream', '_missing_done', '_stream_anomaly',
            '_empty_stop', '_tool_calls_void',
            '_semantic_idle_timeout_ms', '_no_actionable_timeout_s'):
        assert stale_key not in projected.usage


def test_client_abort_projection_is_neutral_even_without_terminal_frames():
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': ''},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.CLIENT_ABORTED,
        evidence=ProviderStreamEvidence(client_aborted=True),
    ).with_usage({
        '_missing_done': True,
        '_missing_finish_reason': True,
        '_stream_anomaly': True,
    })

    assert result.usage['_stream_state'] == 'client_aborted'
    for stale_key in (
            '_missing_done', '_missing_finish_reason', '_stream_anomaly'):
        assert stale_key not in result.usage


def test_round0_semantic_progress_timeout_retries_with_truthful_direct_label(
        monkeypatch):
    from lib.tasks_pkg.stream_handler import _budget

    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *_args: None)
    task = _fresh_task()
    decision = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'x' * 200},
        last_finish_reason='stop',
        tid='test',
        model='kimi-k3',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_stream_state': 'semantic_progress_timeout',
            '_missing_done': True,
            '_semantic_progress_timeout': True,
            '_no_actionable_timeout': True,
            '_no_actionable_timeout_s': 300,
            '_no_actionable_request_elapsed_s': 1_200.2,
            '_no_actionable_stall_elapsed_s': 300.0,
            '_no_actionable_reasoning_chars': 200,
            '_no_actionable_reasoning_chunks': 40,
            '_chunks_received': 500,
            '_failure_stage': 'semantic_progress_timeout',
            '_network_route': {
                'routeId': 'direct:configured-bypass',
                'routeMode': 'direct',
                'decisionReason': 'configured_bypass',
            },
            '_dispatch': {'key': 'sankuai_key_1', 'model': 'kimi-k3'},
            'stream_elapsed_ms': 300_100,
            'trace_id': 'TRACE-PROGRESS-DEADLINE',
        },
    )

    assert decision['action'] == 'continue'
    assert task['_force_rotate_pair'] == ('sankuai_key_1', 'kimi-k3')
    phase_events = [event for event in task['events']
                    if event.get('type') == 'phase']
    assert phase_events[-1]['max'] == 2
    phase = [event for event in task['events']
             if event.get('type') == 'phase'][-1]
    assert phase['detailKey'] == 'stream.phase.semanticProgressTimeoutRetry'
    assert phase['errorKind'] == 'semantic_progress_timeout'
    assert phase['detailArgs']['elapsed'] == 300.0
    assert phase['detailArgs']['requestElapsed'] == 1_200.2
    assert 'no new reasoning progress' in phase['detail']
    assert phase['routeId'] == 'direct:configured-bypass'
    assert '代理超时' not in phase['detail']


def _semantic_progress_timeout_usage(key):
    return {
        '_stream_anomaly': True,
        '_stream_state': 'semantic_progress_timeout',
        '_missing_done': True,
        '_semantic_progress_timeout': True,
        '_no_actionable_timeout': True,
        '_no_actionable_timeout_s': 300,
        '_no_actionable_request_elapsed_s': 1_200,
        '_no_actionable_stall_elapsed_s': 300,
        '_no_actionable_reasoning_chars': 200,
        '_no_actionable_reasoning_chunks': 40,
        '_chunks_received': 500,
        '_failure_stage': 'semantic_progress_timeout',
        '_network_route': {
            'routeId': 'direct:configured-bypass',
            'routeMode': 'direct',
        },
        '_dispatch': {'key': key, 'model': 'kimi-k3'},
        'stream_elapsed_ms': 300_000,
        'trace_id': f'TRACE-{key}',
    }


def test_consecutive_semantic_timeout_stops_before_slot_cycle(monkeypatch):
    """One alternate slot is useful; cycling to the first failed slot is not."""
    from lib.tasks_pkg.stream_handler import _budget

    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *_args: None)
    task = _fresh_task()
    task['_premature_retry_count_phase'] = 0

    first = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'x' * 200},
        last_finish_reason='stop', tid='test', model='kimi-k3', round_num=0,
        _premature_retry_count=0, messages=[],
        usage=_semantic_progress_timeout_usage('sankuai_key_1'),
    )
    assert first['action'] == 'continue'
    assert task['_no_actionable_retry_streak'] == 1
    task.pop('_force_rotate_pair')  # next dispatch consumed the hint

    second = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'y' * 2_000},
        last_finish_reason='stop', tid='test', model='kimi-k3', round_num=1,
        _premature_retry_count=1, messages=[],
        usage=_semantic_progress_timeout_usage('sankuai_key_0'),
    )

    assert second['action'] == 'break'
    assert second['premature_retry_count'] == 1
    assert second['last_finish_reason'] == 'abnormal_stop'
    assert task['error']['autoRetryExhausted'] is True
    assert 'consecutive=1/1' in task['error']['detail']
    assert 'last_progress_age=300.0s' in task['error']['detail']
    assert '连续 300.0 秒没有新的推理进展' in task['error']['message']
    assert '_force_rotate_pair' not in task


def test_actionable_progress_resets_semantic_timeout_retry_streak(monkeypatch):
    """A later isolated failure keeps the remaining phase recovery chance."""
    from lib.tasks_pkg.stream_handler import _budget

    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda *_args: None)
    task = _fresh_task()
    task['_premature_retry_count_phase'] = 0
    first = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'x' * 200},
        last_finish_reason='stop', tid='test', model='kimi-k3', round_num=0,
        _premature_retry_count=0, messages=[],
        usage=_semantic_progress_timeout_usage('sankuai_key_1'),
    )
    assert first['action'] == 'continue'

    actionable = _analyse_registered(
        task,
        assistant_msg={
            'role': 'assistant',
            'content': '',
            'reasoning_content': '',
            'tool_calls': [{
                'id': 'call-1',
                'type': 'function',
                'function': {'name': 'read_file', 'arguments': '{}'},
            }],
        },
        last_finish_reason='tool_calls', tid='test', model='kimi-k3',
        round_num=1, _premature_retry_count=1, messages=[], usage={},
    )
    assert actionable['action'] == 'proceed'
    assert '_no_actionable_retry_streak' not in task

    later_failure = _analyse_registered(
        task,
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'z' * 200},
        last_finish_reason='stop', tid='test', model='kimi-k3', round_num=2,
        _premature_retry_count=1, messages=[],
        usage=_semantic_progress_timeout_usage('sankuai_key_0'),
    )
    assert later_failure['action'] == 'continue'
    assert later_failure['premature_retry_count'] == 2
    assert task['_no_actionable_retry_streak'] == 1


def test_active_long_reasoning_does_not_consume_stall_recovery_budget():
    """Total request age is inert when the provider finished actionably."""
    task = _fresh_task()
    task['_premature_retry_count_phase'] = 0

    decision = _analyse_registered(
        task,
        assistant_msg={
            'role': 'assistant',
            'content': '',
            'reasoning_content': 'active reasoning' * 2_000,
            'tool_calls': [{
                'id': 'call-long-reasoning',
                'type': 'function',
                'function': {'name': 'read_file', 'arguments': '{}'},
            }],
        },
        last_finish_reason='tool_calls',
        tid='test',
        model='kimi-k3',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_state': 'provider_finished',
            '_chunks_received': 4_000,
            'stream_elapsed_ms': 3_600_000,
            'trace_id': 'TRACE-LONG-ACTIVE-REASONING',
        },
    )

    assert decision['action'] == 'proceed'
    assert decision['premature_retry_count'] == 0
    assert task['error'] is None
    assert '_no_actionable_retry_streak' not in task
    assert '_force_rotate_pair' not in task


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
