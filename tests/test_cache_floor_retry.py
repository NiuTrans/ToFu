#!/usr/bin/env python3
"""Floor-collapse identical-resend mitigation — production wiring guard.

WHY (real-gateway evidence, this effort):
  A fraction of rounds report ``cache_read`` pinned at the system+tools FLOOR
  (whole body re-billed) even though the wire bytes are byte-IDENTICAL to a
  cached request and the block geometry is in-window. Four identical-byte
  replays collapse DIFFERENT rounds at 13-40% → SERVER-SIDE stochastic
  cache-write-visibility (SDK #1451), not a client layout bug. Resending the
  IDENTICAL body re-rolls the dice and recovers (harness: mrsfs9d6 20%->0%).

THE FIX under test (``lib/tasks_pkg/floor_retry.py`` + the resend loop in
``lib/tasks_pkg/manager/_stream.py::stream_llm_response``):
  On a byte-STABLE floor-collapse, when ``TOFU_CACHE_FLOOR_RETRY`` is enabled,
  resend the identical body up to N times; adopt the first recovered response.

Failing-first / NEUTER discipline:
  * ``test_resend_fires_and_recovers_when_enabled`` is RED without the resend
    loop (dispatch called once, floored usage kept) and GREEN with it.
  * ``test_no_resend_when_disabled`` is the NEUTER control — the env gate off
    means exactly one dispatch, proving the loop is gated (not always-on).
  * ``test_no_resend_when_prefix_changed`` proves we never resend on a body the
    client actually mutated (would be a wasted call / could mask a real break).
  * ``test_resend_stops_on_throttle`` proves a 503/throttle on the resend stops
    the loop (don't pile retries on an already-throttled gateway).

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_floor_retry.py
"""
from __future__ import annotations

import os
import sys
import threading as _thr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


_FLOOR_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 28654,
                'cache_creation_input_tokens': 42000, '_wire_fp': [{'k': 'a'}]}
_HIT_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 150000,
              'cache_creation_input_tokens': 1200, '_wire_fp': [{'k': 'a'}]}


def _seed_wire_fp(conv_id, fp):
    """Seed the cache-tracking state's PREVIOUS-round wire fingerprint so
    wire_prefix_stable(conv_id, usage) can compare against it."""
    from lib.tasks_pkg.cache_tracking import _cache_lock, _cache_states
    from lib.tasks_pkg.cache_tracking._state import CacheState, _state_key
    key = _state_key(conv_id)
    with _cache_lock:
        st = _cache_states.get(key)
        if st is None:
            st = CacheState()
            _cache_states[key] = st
        st.wire_fp = list(fp)


def _task(conv_id='cfr1'):
    return {'id': 'task-fr-1', 'convId': conv_id, 'content': '',
            'thinking': '', 'config': {}, 'events': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}


def _body():
    return {'model': 'aws.claude-opus-4.8',
            'messages': [{'role': 'system', 'content': 'S'},
                         {'role': 'user', 'content': 'go'}]}


# ── Pure predicate coverage ─────────────────────────────────────────────────

def test_is_floor_collapse_predicate():
    from lib.tasks_pkg import floor_retry as fr
    assert fr.is_floor_collapse(_FLOOR_USAGE) is True
    assert fr.is_floor_collapse(_HIT_USAGE) is False
    assert fr.is_floor_collapse({}) is False
    # big read + small write = healthy
    assert fr.is_floor_collapse(
        {'cache_read_tokens': 200000, 'cache_creation_input_tokens': 500}) is False


def test_floor_retry_max_clamped_and_gate(monkeypatch):
    from lib.tasks_pkg import floor_retry as fr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')
    assert fr.floor_retry_enabled() is False
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', 'on')
    assert fr.floor_retry_enabled() is True
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '99')
    assert fr.floor_retry_max() == 3   # hard-capped
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '-5')
    assert fr.floor_retry_max() == 0


def test_default_gate_is_ON_and_max_is_2(monkeypatch):
    """★ Failing-first guard for the DEFAULT FLIP (2026-07-20): with NO env
    var set, the mitigation must be ON and the resend cap must be 2 (so a
    collapse whose first resend hits a 503 still gets a second attempt).
    NEUTER: revert the module defaults to '0'/'1' and this test goes red."""
    from lib.tasks_pkg import floor_retry as fr
    monkeypatch.delenv('TOFU_CACHE_FLOOR_RETRY', raising=False)
    monkeypatch.delenv('TOFU_CACHE_FLOOR_RETRY_MAX', raising=False)
    assert fr.floor_retry_enabled() is True, (
        'the proven mitigation must be ON by default (objective: zero misses)')
    assert fr.floor_retry_max() == 2, (
        'default resend cap must be 2 to cover a 503-on-first-resend round')


def test_wire_prefix_stable_true_when_prefix_matches():
    from lib.tasks_pkg import floor_retry as fr
    _seed_wire_fp('cfr-stable', [{'k': 'a'}])
    assert fr.wire_prefix_stable('cfr-stable', {'_wire_fp': [{'k': 'a'}, {'k': 'b'}]}) is True


def test_wire_prefix_stable_false_when_prefix_changed():
    from lib.tasks_pkg import floor_retry as fr
    _seed_wire_fp('cfr-changed', [{'k': 'a'}])
    assert fr.wire_prefix_stable('cfr-changed', {'_wire_fp': [{'k': 'X'}, {'k': 'b'}]}) is False


# ── Integration: the resend loop in stream_llm_response ─────────────────────

def _run_stream(monkeypatch, *, enabled, dispatch_seq, conv_id='cfr-int',
                seed_fp=(({'k': 'a'}),)):
    """Drive stream_llm_response with a scripted dispatch sequence. Returns
    (call_count, final_usage)."""
    import lib.tasks_pkg.manager as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1' if enabled else '0')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    _seed_wire_fp(conv_id, list(seed_fp))
    calls = {'n': 0}
    seq = list(dispatch_seq)

    def _fake_dispatch(body, **kwargs):
        i = calls['n']
        calls['n'] += 1
        item = seq[min(i, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', dict(item))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        _msg, _fin, usage = _mgr.stream_llm_response(_task(conv_id), _body(), tag='FR')
    finally:
        _mgr.dispatch_stream = _orig
    return calls['n'], usage


def test_resend_fires_and_recovers_when_enabled(monkeypatch):
    """RED without the resend loop (only 1 dispatch, floored usage kept);
    GREEN with it (2 dispatches, recovered usage adopted)."""
    n, usage = _run_stream(monkeypatch, enabled=True,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE])
    assert n == 2, f'a byte-stable floor-collapse must trigger ONE resend; dispatches={n}'
    assert usage.get('cache_read_tokens') == 150000, (
        'the recovered resend usage must be adopted (cache hit), not the floored one')


def test_no_resend_when_disabled(monkeypatch):
    """NEUTER control: env gate OFF → exactly one dispatch, floored usage kept."""
    n, usage = _run_stream(monkeypatch, enabled=False,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE])
    assert n == 1, f'gate OFF must not resend; dispatches={n}'
    assert usage.get('cache_read_tokens') == 28654


def test_no_resend_when_prefix_changed(monkeypatch):
    """A floor-collapse whose wire prefix DIFFERS from the previous round is
    NOT the server-stochastic class — never resend (would be wasted)."""
    # Seed a DIFFERENT previous fingerprint so wire_prefix_stable is False.
    n, usage = _run_stream(monkeypatch, enabled=True,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE],
                           conv_id='cfr-diff', seed_fp=({'k': 'DIFFERENT'},))
    assert n == 1, f'must not resend on a client-changed prefix; dispatches={n}'


def test_resend_stops_on_throttle(monkeypatch):
    """A throttle/503 on the resend stops the loop (don't pile retries on an
    already-throttled gateway) — exactly one resend attempt, no third call."""
    from lib.llm_errors import RateLimitError
    n, usage = _run_stream(
        monkeypatch, enabled=True,
        dispatch_seq=[_FLOOR_USAGE, RateLimitError('429 throttled'), _HIT_USAGE])
    assert n == 2, f'must attempt one resend then STOP on throttle; dispatches={n}'
    # original floored usage retained (resend raised before returning usage)
    assert usage.get('cache_read_tokens') == 28654


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
