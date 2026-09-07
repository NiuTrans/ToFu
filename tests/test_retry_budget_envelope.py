"""PR3b / C1 tests — task-wide retry-budget envelope.

Two behaviours under test:

  1. **Per-phase counter scope** — the premature-retry counter survives
     across rounds within the same Worker / Planner phase via
     ``task['_premature_retry_count_phase']``, instead of resetting per
     round.  Replan / phase boundaries reset it (verified at the endpoint
     layer; here we confirm the analyser reads it correctly).

  2. **Force-rotate signal on unusable streams** — the analyser writes
     ``task['_force_rotate_pair'] = (key, model)`` after a zero-byte
     retry decision.  ``stream_llm_response`` is responsible for
     consuming and clearing the signal; we test the analyser side.

These tests mirror ``tests/test_zero_byte_round0_retry.py`` style (no
pytest dep, ``_MonkeyPatch`` shim) so they run as plain
``python tests/test_retry_budget_envelope.py``.

Run via pytest:  pytest tests/test_retry_budget_envelope.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.stream_handler.api import analyse_stream_result  # noqa: E402
from lib.tasks_pkg.stream_handler._budget import (  # noqa: E402
    _NO_ACTIONABLE_RETRY_MAX,
    _PREMATURE_RETRY_MAX_ZERO_BYTE,
)


def _fresh_task(*, phase_counter: int | None = None) -> dict:
    t = {
        'id': 'budget-test',
        'aborted': False,
        'content': '',
        'thinking': '',
        'error': None,
        'events': [],
        'events_lock': threading.Lock(),
    }
    if phase_counter is not None:
        t['_premature_retry_count_phase'] = phase_counter
    return t


def _zero_byte_usage(elapsed_ms: int = 4500, *, key='sankuai_key_0',
                     model='aws.claude-opus-4.7'):
    return {
        '_stream_anomaly': True,
        '_empty_stop': True,
        '_chunks_received': 0,
        'trace_id': 'M-BUDGET-TEST',
        'resp_trace_id': 'r-budget-1',
        'stream_elapsed_ms': elapsed_ms,
        '_dispatch': {'key': key, 'model': model, 'provider_id': 'sankuai'},
    }


# ─────────────────────────────────────────────────────────────────────
# 1) Zero-byte retry allowed under the per-signature cap
# ─────────────────────────────────────────────────────────────────────

def test_zero_byte_retry_allowed_under_cap():
    """With phase_counter=0, a zero-byte anomaly retries normally."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1


def test_empty_stop_retry_allowed_under_cap():
    """Empty-stop (thinking but no content) retries under the cap."""
    task = _fresh_task(phase_counter=0)
    usage = {
        '_stream_anomaly': False,
        '_empty_stop': True,
        'trace_id': 'M-EMPTY',
        'stream_elapsed_ms': 5000,
    }
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'x' * 200},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='glm-5.1',
        round_num=2,
        _premature_retry_count=0,
        messages=[],
        usage=usage,
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1


# ─────────────────────────────────────────────────────────────────────
# 2) per-phase counter scope: lifted from a local variable to task dict
# ─────────────────────────────────────────────────────────────────────

def test_phase_counter_overrides_local_argument():
    """When task['_premature_retry_count_phase'] is set, it overrides the
    legacy ``_premature_retry_count`` argument and is used as the source
    of truth.  Used by the orchestrator after PR3b."""
    task = _fresh_task(phase_counter=10)  # 10 retries already used in phase
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,    # ← caller passes 0; phase counter says 10
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 11
    # The task's phase counter must be persisted for the next round.
    assert task['_premature_retry_count_phase'] == 11


def test_phase_counter_exhausts_at_cap():
    """When task['_premature_retry_count_phase'] reaches the cap mid-phase,
    no more retries are issued — even if a fresh round_num arrives."""
    task = _fresh_task(phase_counter=_PREMATURE_RETRY_MAX_ZERO_BYTE)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=5,                  # ← later round, fresh from per-round scope
        _premature_retry_count=0,
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'abnormal_stop'


def test_phase_counter_absent_falls_back_to_local_argument():
    """Callers that don't set the phase counter (paper reports, swarm)
    keep working with the legacy local-variable counter."""
    task = _fresh_task()  # no _premature_retry_count_phase key
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=3,
        messages=[],
        usage=_zero_byte_usage(),
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 4


# ─────────────────────────────────────────────────────────────────────
# 3) force-rotate signal — analyser writes the slot it just zero-byte'd
# ─────────────────────────────────────────────────────────────────────

def test_zero_byte_writes_force_rotate_pair():
    """After a zero-byte 'continue' decision, the task dict must carry
    the (key, model) of the offending slot so the next dispatch avoids
    it.  Mirrors the gateway-5xx-treated-as-429 pattern."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage=_zero_byte_usage(key='sankuai_key_0', model='aws.claude-opus-4.7'),
    )
    assert decision['action'] == 'continue'
    assert task.get('_force_rotate_pair') == ('sankuai_key_0', 'aws.claude-opus-4.7')


def test_classic_premature_close_retries_on_another_slot():
    """A classic premature close (model produced thinking, then was cut
    mid-stream) avoids the just-failed slot once. Strict-model authority is
    preserved: dispatch may select only another slot for the same model, and
    relaxes the hint if no alternative exists."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={
            'role': 'assistant',
            'content': '',
            'reasoning_content': 'x' * 2000,
        },
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=1,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': False,
            'trace_id': 'M-CLASSIC',
            'stream_elapsed_ms': 30000,
            '_dispatch': {'key': 'sankuai_key_0', 'model': 'aws.claude-opus-4.7'},
        },
    )
    assert decision['action'] == 'continue'
    assert task.get('_force_rotate_pair') == (
        'sankuai_key_0', 'aws.claude-opus-4.7')


def test_classic_premature_close_retries_up_to_16():
    """Classic premature-close now keeps retrying (cap 16) instead of
    failing the task after 2 — a single dropped connection shouldn't
    zero out a whole SWE-bench instance. At count=15 it still retries;
    at count=16 it's exhausted."""
    from lib.tasks_pkg.stream_handler._budget import _PREMATURE_RETRY_MAX_CLASSIC
    assert _PREMATURE_RETRY_MAX_CLASSIC == 16

    def _run(count):
        task = _fresh_task(phase_counter=count)
        return analyse_stream_result(
            assistant_msg={'role': 'assistant', 'content': '',
                           'reasoning_content': 'x' * 2000},
            last_finish_reason='stop', task=task, tid='budget',
            model='aws.claude-opus-4.7', round_num=1,
            _premature_retry_count=count, messages=[],
            usage={'_stream_anomaly': False, 'trace_id': 'M-CLASSIC',
                   'stream_elapsed_ms': 30000},
        )

    assert _run(15)['action'] == 'continue'   # under cap → retry
    assert _run(16)['action'] == 'break'       # at cap → exhausted
    assert _run(16)['last_finish_reason'] == 'premature_close'


def test_no_actionable_exhaustion_does_not_multiply_into_whole_turn_retries():
    """A five-minute no-progress attempt gets only two alternate-slot retries.

    The exhaustion envelope keeps a manual Retry affordance but tells the
    outer auto-retry layer not to reset and replay the entire inner budget.
    """
    task = _fresh_task(phase_counter=_NO_ACTIONABLE_RETRY_MAX)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '',
                       'reasoning_content': 'x' * 200},
        last_finish_reason='stop', task=task, tid='budget',
        model='kimi-k3', round_num=0,
        _premature_retry_count=_NO_ACTIONABLE_RETRY_MAX, messages=[],
        usage={
            '_stream_anomaly': True,
            '_missing_done': True,
            '_no_actionable_timeout': True,
            '_chunks_received': 500,
            '_failure_stage': 'no_actionable_output',
            '_network_route': {
                'routeId': 'direct:configured-bypass',
                'routeMode': 'direct',
            },
            'stream_elapsed_ms': 300_000,
            'trace_id': 'M-NO-PROGRESS',
        },
    )

    assert decision['action'] == 'break'
    assert task['error']['kind'] == 'abnormal_stop'
    assert task['error']['retryable'] is True
    assert task['error']['autoRetryExhausted'] is True
    assert task['error']['failureStage'] == 'no_actionable_output'
    from lib.tasks_pkg.turn_retry import should_auto_retry_turn
    assert should_auto_retry_turn(task['error'], 0) == (False, 0.0)


def test_zero_byte_without_dispatch_metadata_skips_rotate():
    """If usage has no _dispatch metadata (older code path), force-rotate
    is silently skipped — the existing 429-style cooldown still rotates
    slots naturally."""
    task = _fresh_task(phase_counter=0)
    usage = _zero_byte_usage()
    usage.pop('_dispatch')
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='budget',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=0,
        messages=[],
        usage=usage,
    )
    assert decision['action'] == 'continue'
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
