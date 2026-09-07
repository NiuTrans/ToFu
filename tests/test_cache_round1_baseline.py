#!/usr/bin/env python3
"""Round-1 (new-turn) cache-break statistics honesty suite.

WHY (the objective — "the cache-stats system is too optimistic"):
  ``_cache_states`` is keyed per ``(owner, conv_id, thread_id)`` and every new user
  turn runs on a fresh ``run_task`` worker thread → a fresh ``CacheState`` with
  ``call_count == 0``. ``_classify_break``'s three predicates ALL gate on
  ``call_count > 0``, so the FIRST round of every turn is structurally exempt:
  the previous turn left a warm ~Nk-token cached prefix, this round's read
  collapses toward the static floor (real money re-billed), yet it is never
  counted in ``total_breaks`` and never bucketed — it wears the "first-cache
  warm-up" hat. ``get_prev_turn_cache_read`` already recovers the previous
  turn's final cached-prefix read across the thread boundary, but it was ONLY
  wired into the frontend write-breakdown, never into ``detect_cache_break``.

  The fix feeds that cross-turn baseline as the round-1 ``prev_cache_read`` so a
  genuine boundary re-bill is classified (``turn_boundary_rebill`` bucket) and
  counted — WITHOUT crying wolf on a genuine first-ever call (baseline 0) or a
  round-1 whose warm prefix DID carry across the boundary (no read drop).

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_round1_baseline.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit
OWNER = 1

_FAKE_PREV_TURN_TID = 999_000_111  # a thread id that is NOT the test thread


def _messages():
    return [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': 'first turn question'},
        {'role': 'assistant', 'content': 'the answer to the first turn'},
        {'role': 'user', 'content': 'second turn question, a brand new turn'},
    ]


def _tools():
    return [{'type': 'function',
             'function': {'name': 'read_files', 'description': 'read',
                          'parameters': {}}}]


def _clean(conv):
    import lib.tasks_pkg.cache_tracking._state as _st
    with _st._cache_lock:
        for k in list(_st._cache_states):
            if k[0] == OWNER and k[1] == conv:
                _st._cache_states.pop(k, None)


def _seed_prev_turn(conv, *, last_read, last_write=1000, calls=5):
    """Seed a PREVIOUS turn's CacheState under a DIFFERENT (fake) thread id so
    the current test thread sees a fresh round-1 state for ``conv``."""
    import lib.tasks_pkg.cache_tracking._state as _st
    from lib.tasks_pkg.cache_tracking._state import CacheState
    sib = CacheState()
    sib.call_count = calls
    sib.last_cache_read_tokens = last_read
    sib.last_cache_write_tokens = last_write
    sib.message_count = 40
    sib.model = 'claude-opus-4'
    sib.last_update_time = time.time()
    with _st._cache_lock:
        _st._cache_states[(OWNER, conv, _FAKE_PREV_TURN_TID)] = sib


def _cur_state(conv):
    import lib.tasks_pkg.cache_tracking._state as _st
    return _st._cache_states.get(_st._state_key(conv, user_id=OWNER))


def test_round1_boundary_rebill_is_flagged_and_bucketed():
    """Prev turn left a warm 262k prefix; this turn's round-1 reads back only
    79k → a real boundary re-bill. It MUST now be flagged as a break, counted
    in total_breaks, and bucketed as turn_boundary_rebill (NOT laundered to
    server_side)."""
    from lib.tasks_pkg.cache_tracking._detect import (
        detect_cache_break,
        classify_verdict,
    )

    conv = 'round1-boundary-flagged'
    _clean(conv)
    _seed_prev_turn(conv, last_read=262_000)

    usage = {'cache_read_input_tokens': 79_000,
             'cache_creation_input_tokens': 190_000,
             'input_tokens': 5_000}
    res = detect_cache_break(
        conv, _messages(), _tools(), 'claude-opus-4', usage, user_id=OWNER)

    assert res is not None, 'round-1 boundary re-bill must be flagged as a break'
    assert 'turn_boundary_rebill' in res, (
        f'must be named a turn-boundary re-bill, not laundered to server_side: {res}')
    assert classify_verdict(res) == 'turn_boundary_rebill', classify_verdict(res)

    st = _cur_state(conv)
    assert st is not None and st.total_breaks == 1, (
        f'the boundary re-bill must count in total_breaks: {st and st.total_breaks}')
    _clean(conv)


def test_genuine_first_call_no_baseline_is_benign_write():
    """A genuine first-ever call (NO sibling turn → baseline 0) must remain a
    benign first-time cache write, NOT a false break (cold-start honesty)."""
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

    conv = 'round1-cold-start'
    _clean(conv)  # no sibling seeded

    usage = {'cache_read_input_tokens': 79_000,
             'cache_creation_input_tokens': 190_000,
             'input_tokens': 5_000}
    res = detect_cache_break(
        conv, _messages(), _tools(), 'claude-opus-4', usage, user_id=OWNER)

    assert res is None, f'cold start (no prior turn) must not be a break: {res}'
    st = _cur_state(conv)
    assert st is not None and st.total_breaks == 0, st and st.total_breaks
    _clean(conv)


def test_round1_warm_prefix_carried_across_boundary_no_break():
    """Prev turn left 262k AND this turn's round-1 still reads ~262k (the warm
    prefix carried across the boundary) → NO drop → NO break. Proves the fix
    does not cry wolf when the boundary prefix survived."""
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

    conv = 'round1-prefix-survived'
    _clean(conv)
    _seed_prev_turn(conv, last_read=262_000)

    usage = {'cache_read_input_tokens': 261_000,      # ~held, tiny variation
             'cache_creation_input_tokens': 3_000,
             'input_tokens': 4_000}
    res = detect_cache_break(
        conv, _messages(), _tools(), 'claude-opus-4', usage, user_id=OWNER)

    assert res is None, f'a carried-over warm prefix must not be a break: {res}'
    _clean(conv)


def test_round1_baseline_below_floor_not_substituted():
    """NEUTER-style gate: a sibling whose final read is BELOW the miss floor
    (< _MIN_CACHE_MISS_TOKENS) provides no usable baseline → no substitution →
    no break. Proves the substitution is gated on a real prior warm prefix."""
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

    conv = 'round1-baseline-too-small'
    _clean(conv)
    _seed_prev_turn(conv, last_read=1_000)  # below _MIN_CACHE_MISS_TOKENS (2000)

    usage = {'cache_read_input_tokens': 200,
             'cache_creation_input_tokens': 190_000,
             'input_tokens': 5_000}
    res = detect_cache_break(
        conv, _messages(), _tools(), 'claude-opus-4', usage, user_id=OWNER)

    assert res is None, (
        f'a sub-floor prior read is not a usable baseline → no break: {res}')
    _clean(conv)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
