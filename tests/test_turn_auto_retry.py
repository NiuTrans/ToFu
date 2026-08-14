"""Turn-level auto-retry — self-heal over transient terminal errors.

Two layers:

1. The PURE verdict helper ``should_auto_retry_turn`` — the decision that a
   settled-error turn should be transparently re-run. Unit-tested in isolation
   (no DB / route harness), the project's preferred shape.
2. The orchestrator seam ``_maybe_auto_retry_turn`` — drives the re-run: it
   restores the pristine turn input, resets per-turn accumulators, emits
   ``retry_reset`` + ``phase:retrying``, and re-invokes ``run_task``. Tested
   with ``run_task`` monkeypatched to a spy so the heavy loop is not exercised.

NC bite (proven manually while writing this):
  • Flipping ``_AUTO_RETRY_KINDS`` to include 'quota' makes
    ``test_persistent_kinds_never_retry`` fail on the quota case.
  • Neutering the pristine-input restore in ``_maybe_auto_retry_turn`` (drop
    the ``task['messages'] = list(_pristine)`` line) makes
    ``test_seam_restores_pristine_input_on_retry`` fail.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.error_envelope import make_envelope  # noqa: E402
from lib.tasks_pkg.turn_retry import (  # noqa: E402
    _AUTO_RETRY_KINDS,
    _AUTO_TURN_RETRY_MAX,
    auto_turn_backoff_seconds,
    auto_turn_retry_max,
    should_auto_retry_turn,
)

# ── The persistent kinds we must NEVER auto-retry (a re-run just re-fails). ──
_NON_RETRY_KINDS = [
    'quota', 'permission', 'content_filter', 'invalid_image',
    'prompt_too_long', 'model_limit', 'internal', 'generic',
    'dispatch_exhausted', 'stream_only', 'tool_rounds_exhausted',
]


# ══════════════════ pure verdict helper ══════════════════

def test_retryable_kinds_retry_within_budget():
    for kind in sorted(_AUTO_RETRY_KINDS):
        env = make_envelope(kind, detail='x')
        retry, backoff = should_auto_retry_turn(env, attempt=0, cfg={})
        assert retry is True, f'{kind} should auto-retry on attempt 0'
        assert backoff > 0.0, f'{kind} retry must carry a positive backoff'


def test_persistent_kinds_never_retry():
    # NC: adding any of these to _AUTO_RETRY_KINDS flips one of these to True.
    for kind in _NON_RETRY_KINDS:
        env = make_envelope(kind, detail='x')
        retry, backoff = should_auto_retry_turn(env, attempt=0, cfg={})
        assert retry is False, f'{kind} must NOT auto-retry'
        assert backoff == 0.0


def test_budget_exhaustion_stops_retry():
    env = make_envelope('ratelimit', detail='429')
    # attempts 0 .. cap-1 retry; at cap it stops.
    for attempt in range(_AUTO_TURN_RETRY_MAX):
        retry, _ = should_auto_retry_turn(env, attempt=attempt, cfg={})
        assert retry is True, f'attempt {attempt} < cap should retry'
    retry, backoff = should_auto_retry_turn(
        env, attempt=_AUTO_TURN_RETRY_MAX, cfg={})
    assert retry is False, 'at cap the turn must give up (surface for manual)'
    assert backoff == 0.0


def test_opt_out_disables_retry():
    env = make_envelope('network', detail='reset')
    retry, _ = should_auto_retry_turn(
        env, attempt=0, cfg={'disableAutoTurnRetry': True})
    assert retry is False


def test_cfg_cap_override():
    assert auto_turn_retry_max({'autoTurnRetryMax': 0}) == 0
    assert auto_turn_retry_max({'autoTurnRetryMax': 7}) == 7
    assert auto_turn_retry_max({}) == _AUTO_TURN_RETRY_MAX
    assert auto_turn_retry_max(None) == _AUTO_TURN_RETRY_MAX
    # cap=0 → never retry even for a retryable kind
    env = make_envelope('ratelimit')
    retry, _ = should_auto_retry_turn(env, attempt=0, cfg={'autoTurnRetryMax': 0})
    assert retry is False


def test_none_and_non_dict_envelope_no_retry():
    assert should_auto_retry_turn(None, 0, {}) == (False, 0.0)
    assert should_auto_retry_turn('a string error', 0, {}) == (False, 0.0)


def test_backoff_is_monotonic_and_capped():
    prev = 0.0
    for attempt in range(1, 6):
        b = auto_turn_backoff_seconds(attempt)
        assert b > 0
        # capped at 30 + jitter(<1)
        assert b < 32.0
        if attempt <= 4:  # before the cap plateaus
            assert b >= prev - 1.0  # allow jitter overlap but broadly increasing
        prev = b


# ══════════════════ orchestrator seam ══════════════════

def _fresh_task(err_kind='ratelimit', pristine=None):
    err = make_envelope(err_kind, detail='x') if err_kind else None
    msgs = pristine if pristine is not None else [
        {'role': 'user', 'content': 'hello'}]
    return {
        'id': 'seamtask01',
        'convId': 'convseam',
        'aborted': False,
        'content': 'PARTIAL answer from failed attempt',
        'thinking': 'partial thinking',
        'error': err,
        'status': 'error',
        'finishReason': 'error',
        'toolRounds': [{'round': 0}],
        # run_task mutated this with injected system context + a partial round:
        'messages': [
            {'role': 'system', 'content': 'INJECTED CONTEXT'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'half done'},
        ],
        '_turn_input_messages': list(msgs),
        'model': 'aws.claude-opus-4.7',
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
    }


def test_seam_retries_and_reinvokes(monkeypatch):
    """A retryable error → _maybe_auto_retry_turn returns True, emits
    retry_reset + phase, and re-invokes run_task exactly once."""
    import lib.tasks_pkg.orchestrator as orch
    import lib.tasks_pkg.turn_retry as tr

    calls = {'n': 0}

    def _spy_run_task(task):
        calls['n'] += 1

    monkeypatch.setattr(orch, 'run_task', _spy_run_task)
    # Zero backoff so the test is instant.
    monkeypatch.setattr(tr, 'auto_turn_backoff_seconds', lambda attempt: 0.0)

    task = _fresh_task('ratelimit')
    retried = orch._maybe_auto_retry_turn(task, cfg={})
    assert retried is True
    assert calls['n'] == 1, 'run_task must be re-invoked once'

    types = [e.get('type') for e in task['events']]
    assert 'retry_reset' in types
    assert 'phase' in types
    resets = [e for e in task['events'] if e.get('type') == 'retry_reset']
    assert task.get('_contentEpoch') == 1, task
    assert resets[-1].get('contentEpoch') == 1, resets[-1]
    # accumulators reset to a clean running state
    assert task['_auto_turn_retry_count'] == 1
    # (content was reset before run_task; the spy didn't repopulate it)
    assert task['content'] == ''
    assert task['thinking'] == ''
    assert task['toolRounds'] == []


def test_seam_restores_pristine_input_on_retry(monkeypatch):
    """The re-run must start from the PRISTINE input, not the failed attempt's
    system-context-injected / partial-round write-back. (NC: dropping the
    restore line leaves the injected 3-message list.)"""
    import lib.tasks_pkg.orchestrator as orch
    import lib.tasks_pkg.turn_retry as tr

    seen = {}

    def _spy_run_task(task):
        seen['messages'] = list(task['messages'])

    monkeypatch.setattr(orch, 'run_task', _spy_run_task)
    monkeypatch.setattr(tr, 'auto_turn_backoff_seconds', lambda attempt: 0.0)

    pristine = [{'role': 'user', 'content': 'the original question'}]
    task = _fresh_task('timeout', pristine=pristine)
    orch._maybe_auto_retry_turn(task, cfg={})
    assert seen['messages'] == pristine, \
        'run_task must see the pristine input, not the injected/partial list'


def test_seam_non_retryable_finalizes(monkeypatch):
    """A persistent error → returns False, does NOT re-invoke run_task, so the
    caller finalizes and surfaces the error for manual retry."""
    import lib.tasks_pkg.orchestrator as orch

    calls = {'n': 0}
    monkeypatch.setattr(orch, 'run_task', lambda task: calls.__setitem__('n', calls['n'] + 1))

    task = _fresh_task('content_filter')
    retried = orch._maybe_auto_retry_turn(task, cfg={})
    assert retried is False
    assert calls['n'] == 0
    # error preserved for the caller to finalize
    assert task['error']['kind'] == 'content_filter'


def test_seam_abort_during_backoff_finalizes(monkeypatch):
    """If the user aborts during the backoff sleep, do NOT re-run."""
    import lib.tasks_pkg.orchestrator as orch
    import lib.tasks_pkg.stream_handler as sh

    calls = {'n': 0}
    monkeypatch.setattr(orch, 'run_task', lambda task: calls.__setitem__('n', calls['n'] + 1))

    task = _fresh_task('network')

    def _abort_during_sleep(seconds, t):
        t['aborted'] = True

    monkeypatch.setattr(sh, '_interruptible_sleep', _abort_during_sleep)
    retried = orch._maybe_auto_retry_turn(task, cfg={})
    assert retried is False
    assert calls['n'] == 0


def test_seam_budget_exhausted_finalizes(monkeypatch):
    """When _auto_turn_retry_count already at cap, no retry."""
    import lib.tasks_pkg.orchestrator as orch

    calls = {'n': 0}
    monkeypatch.setattr(orch, 'run_task', lambda task: calls.__setitem__('n', calls['n'] + 1))

    task = _fresh_task('ratelimit')
    task['_auto_turn_retry_count'] = _AUTO_TURN_RETRY_MAX
    retried = orch._maybe_auto_retry_turn(task, cfg={})
    assert retried is False
    assert calls['n'] == 0


if __name__ == '__main__':
    import traceback
    passed = failed = 0
    _g = dict(globals())
    for name, fn in _g.items():
        if name.startswith('test_') and callable(fn):
            try:
                import inspect
                if 'monkeypatch' in inspect.signature(fn).parameters:
                    continue  # needs pytest fixture
                fn()
                passed += 1
                print(f'PASS {name}')
            except Exception:
                failed += 1
                print(f'FAIL {name}')
                traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed (monkeypatch tests skipped in __main__)')
    sys.exit(0 if failed == 0 else 1)
