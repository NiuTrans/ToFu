"""Tests for exponential-backoff pacing on zero-byte gateway retries.

Companion to test_zero_byte_round0_retry.py. The retry decision logic
itself is covered there; this module focuses on:

  - the per-attempt backoff schedule produced by ``_zero_byte_backoff_seconds``
  - the phase event carrying ``backoff_s``
  - the actual sleep call (mocked out so the test stays fast)
  - interruption by ``task['aborted']``
  - classic premature-close retries DO NOT sleep (no backoff in that bucket)
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg import stream_handler  # noqa: E402
from lib.tasks_pkg.stream_handler import (  # noqa: E402
    _PREMATURE_RETRY_MAX_CLASSIC,
    _ZERO_BYTE_BACKOFF_BASE_S,
    _ZERO_BYTE_BACKOFF_MAX_S,
    _interruptible_sleep,
    _zero_byte_backoff_seconds,
    analyse_stream_result,
)


def _fresh_task():
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


def test_backoff_schedule_doubles_until_cap():
    """Base sleep should double per attempt, then plateau at the cap."""
    # Sample the schedule with jitter stripped: we just check the floor
    # equals the deterministic exponential base.
    floors = []
    for attempt in range(1, 8):
        # The function returns base + uniform(0, 0.5).  Run many trials
        # and take the min — equals the base for that attempt.
        sample_min = min(_zero_byte_backoff_seconds(attempt) for _ in range(50))
        floors.append(sample_min)

    expected = [
        _ZERO_BYTE_BACKOFF_BASE_S * 1,    # attempt 1: 0.5s
        _ZERO_BYTE_BACKOFF_BASE_S * 2,    # attempt 2: 1.0s
        _ZERO_BYTE_BACKOFF_BASE_S * 4,    # attempt 3: 2.0s
        _ZERO_BYTE_BACKOFF_BASE_S * 8,    # attempt 4: 4.0s
        _ZERO_BYTE_BACKOFF_MAX_S,         # attempt 5: 8.0s (capped)
        _ZERO_BYTE_BACKOFF_MAX_S,         # attempt 6: capped
        _ZERO_BYTE_BACKOFF_MAX_S,         # attempt 7: capped
    ]
    for got, want in zip(floors, expected):
        # Min over 50 samples should be within 0.05s of the base.
        assert abs(got - want) < 0.1, f'expected ~{want}, got {got}'


def test_backoff_includes_jitter():
    """Two calls for the same attempt should usually differ (jitter)."""
    samples = [_zero_byte_backoff_seconds(1) for _ in range(30)]
    assert max(samples) > min(samples), 'jitter looks absent'
    # All samples must be in [base, base+0.5).
    for s in samples:
        assert _ZERO_BYTE_BACKOFF_BASE_S <= s < _ZERO_BYTE_BACKOFF_BASE_S + 0.5


def test_zero_byte_retry_calls_sleep_with_backoff(monkeypatch):
    """The retry path must invoke a sleep before continuing."""
    sleeps = []

    def fake_sleep(seconds, task):
        sleeps.append(seconds)

    monkeypatch.setattr(stream_handler, '_interruptible_sleep', fake_sleep)

    task = _fresh_task()
    decision = analyse_stream_result(
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
    assert decision['action'] == 'continue'
    assert len(sleeps) == 1, f'expected 1 sleep call, got {len(sleeps)}'
    # Attempt 1 → base 0.5s + jitter [0, 0.5) → in [0.5, 1.0).
    assert 0.5 <= sleeps[0] < 1.0, f'sleep duration {sleeps[0]} out of range'


def test_phase_event_carries_backoff_s(monkeypatch):
    monkeypatch.setattr(stream_handler, '_interruptible_sleep',
                        lambda s, t: None)

    task = _fresh_task()
    analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=0,
        _premature_retry_count=2,
        messages=[],
        usage=_zero_byte_usage(),
    )
    phase = [e for e in task['events'] if e.get('type') == 'phase'][-1]
    assert phase['bucket'] == 'zero_byte'
    assert 'backoff_s' in phase
    # Attempt 3 → base 2.0s + jitter → in [2.0, 2.5).
    assert 2.0 <= phase['backoff_s'] < 2.5


def test_classic_premature_retry_uses_backoff(monkeypatch):
    """Classic premature-close retries are now paced with the SAME
    exponential-backoff schedule as zero-byte.

    Rationale: the classic cap was raised from 2 → 16
    (``_PREMATURE_RETRY_MAX_CLASSIC``), so retrying a dropped connection
    up to 16 times with NO backoff would hammer the gateway in
    milliseconds. Backoff is the necessary companion to the higher cap.
    (Earlier design: cap=2, no backoff — that's what the prior version of
    this test asserted.)"""
    sleeps = []
    monkeypatch.setattr(stream_handler, '_interruptible_sleep',
                        lambda s, t: sleeps.append(s))

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
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': False,
            'trace_id': 'M-TEST-2',
            'stream_elapsed_ms': 30000,
        },
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1
    # Attempt 1 → base 0.5s + jitter [0, 0.5) → exactly one sleep in [0.5, 1.0).
    assert len(sleeps) == 1, f'classic retry should sleep once, got {sleeps}'
    assert 0.5 <= sleeps[0] < 1.0, f'sleep {sleeps[0]} out of attempt-1 range'


def test_late_round_stream_anomaly_does_not_sleep(monkeypatch):
    """The late-round stream-anomaly bucket keeps the historical
    NO-backoff behaviour (only zero-byte + classic premature-close are
    paced). This is an empty round with a stream anomaly on round > 0 that
    is NOT a zero-byte hang (substantial elapsed, no thinking)."""
    sleeps = []
    monkeypatch.setattr(stream_handler, '_interruptible_sleep',
                        lambda s, t: sleeps.append(s))

    task = _fresh_task()
    decision = analyse_stream_result(
        assistant_msg={
            'role': 'assistant',
            'content': '',
            'reasoning_content': '',
        },
        last_finish_reason='stop',
        task=task,
        tid='test',
        model='aws.claude-opus-4.7',
        round_num=2,
        _premature_retry_count=0,
        messages=[],
        usage={
            '_stream_anomaly': True,
            '_empty_stop': False,
            'trace_id': 'M-TEST-3',
            # Long elapsed + a real chunk count so this is NOT classified
            # zero-byte (which requires _chunks_received==0 or the stub path).
            '_chunks_received': 12,
            'stream_elapsed_ms': 90000,
        },
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1
    assert sleeps == [], f'late-round anomaly should not sleep, got {sleeps}'


def test_interruptible_sleep_returns_promptly_on_abort():
    """If task['aborted'] flips mid-sleep the helper exits within ~150 ms."""
    task = _fresh_task()

    def flip():
        time.sleep(0.05)
        task['aborted'] = True

    threading.Thread(target=flip, daemon=True).start()
    t0 = time.monotonic()
    _interruptible_sleep(5.0, task)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f'abort interrupt took {elapsed:.2f}s (>0.5s)'


# ---- Minimal pytest-free runner (matches sibling test file's style) ----

class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        original = getattr(target, name)
        setattr(target, name, value)
        self._undo.append(lambda: setattr(target, name, original))

    def undo_all(self):
        for fn in reversed(self._undo):
            fn()


if __name__ == '__main__':
    import inspect
    import traceback
    failed = 0
    passed = 0
    for name, fn in list(globals().items()):
        if not (name.startswith('test_') and callable(fn)):
            continue
        mp = _MonkeyPatch()
        try:
            sig = inspect.signature(fn)
            if 'monkeypatch' in sig.parameters:
                fn(mp)
            else:
                fn()
            passed += 1
            print(f'PASS {name}')
        except Exception:
            failed += 1
            print(f'FAIL {name}')
            traceback.print_exc()
        finally:
            mp.undo_all()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
