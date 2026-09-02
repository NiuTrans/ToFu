#!/usr/bin/env python3
"""Round-1 boundary bucketing must survive a COLD in-memory state (the log-proven
gap): restart / stale-eviction / long gap wipe the per-``(owner,conv,thread)``
``CacheState`` siblings, so ``get_prev_turn_cache_read`` returns 0 and a genuine
collapsed round-1 is laundered into ``no_break`` instead of
``turn_boundary_rebill``.

WHY (the objective — "the instrument lies in exactly the case the question
cares about"):
  ``turn_boundary_break`` depends on ``_cross_turn_prev_read`` — the previous
  turn's final cached-prefix read. Today that comes ONLY from the in-memory
  ``_cache_states`` sibling scan. After a stale-eviction (>1h), a restart, or a
  multi-replica bounce the sibling is gone (``prev_rec_read=None`` in the log),
  the baseline is 0, ``turn_boundary_break`` short-circuits, and the collapsed /
  zero-read round-1 — the ACTUAL miss the objective is about — is silently
  bucketed ``no_break``. We already persist a durable per-conv boundary
  (``cachePrefixHWM``); this suite drives the SAME durability for the round-1
  read baseline (``lastTurnCacheRead``) so a boundary re-bill buckets honestly
  even from a cold process.

The failing-first test is ``test_db_baseline_used_when_memory_empty``:
  before the fix, an empty-memory + warm-DB-baseline + collapsed round-1 returns
  ``no_break``; after the fix it returns ``turn_boundary_rebill``.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_round1_durable_baseline.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit
OWNER = 1


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
    """Wipe ALL in-memory sibling states for conv → simulate cold process."""
    import lib.tasks_pkg.cache_tracking._state as _st
    with _st._cache_lock:
        for k in list(_st._cache_states):
            if k[0] == OWNER and k[1] == conv:
                _st._cache_states.pop(k, None)


def _cur_state(conv):
    import lib.tasks_pkg.cache_tracking._state as _st
    return _st._cache_states.get(_st._state_key(conv, user_id=OWNER))


def _patch_persist_with_fake_store(mp):
    """In-memory stand-in for the durable read-baseline store, mirroring the
    approach in test_cache_prefix_cross_thread_freeze.py. Returns the dict so a
    test can seed a prior turn's read WITHOUT a DB."""
    import lib.tasks_pkg.cache_tracking._persist as _persist
    store: dict[tuple[int, str], int] = {}

    def _fake_read(conv_id, *, user_id):
        v = store.get((user_id, conv_id))
        return v if isinstance(v, int) and v > 0 else 0

    def _fake_write(conv_id, cache_read, *, user_id):
        if not conv_id or not isinstance(cache_read, int) or cache_read <= 0:
            return
        # monotonic-max mirrors the real write's merge semantics
        key = (user_id, conv_id)
        store[key] = max(store.get(key, 0), cache_read)

    mp.setattr(_persist, 'read_last_turn_cache_read', _fake_read, raising=True)
    mp.setattr(_persist, 'write_last_turn_cache_read', _fake_write, raising=True)
    # get_prev_turn_cache_read imports these lazily from the _persist module,
    # so patching the module attribute is picked up on the next call.
    return store


def test_db_baseline_used_when_memory_empty():
    """FAILING-FIRST: memory is cold (no sibling), but a durable baseline says
    the previous turn read back 262k. This turn's round-1 collapses to the 79k
    static floor → a real boundary re-bill that MUST bucket turn_boundary_rebill
    (not laundered to no_break the way it is when the baseline is memory-only)."""
    from lib.tasks_pkg.cache_tracking._detect import (
        detect_cache_break,
        classify_verdict,
    )

    conv = 'round1-durable-collapsed'
    _clean(conv)
    mp = pytest.MonkeyPatch()
    store = _patch_persist_with_fake_store(mp)
    try:
        store[(OWNER, conv)] = 262_000

        usage = {'cache_read_input_tokens': 79_000,
                 'cache_creation_input_tokens': 190_000,
                 'input_tokens': 5_000}
        res = detect_cache_break(conv, _messages(), _tools(),
                                 'claude-opus-4', usage, user_id=OWNER)

        assert res is not None, (
            'cold-memory round-1 collapse must still be flagged via the durable '
            'baseline')
        assert 'turn_boundary_rebill' in res, (
            f'must bucket turn_boundary_rebill, not no_break: {res}')
        assert classify_verdict(res) == 'turn_boundary_rebill', classify_verdict(res)
        st = _cur_state(conv)
        assert st is not None and st.total_breaks == 1, (
            f'the boundary re-bill must count in total_breaks: '
            f'{st and st.total_breaks}')
    finally:
        mp.undo()
        _clean(conv)


def test_db_baseline_neutered_goes_red():
    """NEUTER: remove the DB fallback (read baseline forced to 0, as if only the
    in-memory path existed) → the SAME cold-memory collapse is no longer flagged
    → proves the durable fallback is load-bearing for this bucketing."""
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

    conv = 'round1-durable-neuter'
    _clean(conv)
    mp = pytest.MonkeyPatch()
    _patch_persist_with_fake_store(mp)
    import lib.tasks_pkg.cache_tracking._persist as _persist
    try:
        # NEUTER the durable read (hide the persisted signal entirely).
        mp.setattr(_persist, 'read_last_turn_cache_read',
                   lambda _c, *, user_id: 0,
                   raising=True)

        usage = {'cache_read_input_tokens': 79_000,
                 'cache_creation_input_tokens': 190_000,
                 'input_tokens': 5_000}
        res = detect_cache_break(conv, _messages(), _tools(),
                                 'claude-opus-4', usage, user_id=OWNER)
        assert res is None, (
            'with the durable fallback neutered, the collapse is uncounted again '
            f'(this is the bug the fix closes): {res}')
    finally:
        mp.undo()
        _clean(conv)


def test_cold_first_ever_call_no_db_baseline_is_benign():
    """A genuine first-ever call: memory empty AND no durable baseline (0) → must
    stay a benign first-time cache write, NOT a false boundary break."""
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

    conv = 'round1-durable-coldstart'
    _clean(conv)
    mp = pytest.MonkeyPatch()
    _patch_persist_with_fake_store(mp)  # store empty → read returns 0
    try:
        usage = {'cache_read_input_tokens': 79_000,
                 'cache_creation_input_tokens': 190_000,
                 'input_tokens': 5_000}
        res = detect_cache_break(conv, _messages(), _tools(),
                                 'claude-opus-4', usage, user_id=OWNER)
        assert res is None, (
            f'cold start with no durable baseline must not be a break: {res}')
        st = _cur_state(conv)
        assert st is not None and st.total_breaks == 0, st and st.total_breaks
    finally:
        mp.undo()
        _clean(conv)


def test_live_memory_baseline_takes_precedence_over_db():
    """When a LIVE sibling state exists, its read baseline is used (unchanged
    behavior); the durable fallback is consulted ONLY when memory is cold. Here
    memory says the prefix carried across (~261k) so there is NO drop and NO
    break — even though a (staler) durable value would have implied a collapse."""
    import time
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    import lib.tasks_pkg.cache_tracking._state as _st
    from lib.tasks_pkg.cache_tracking._state import CacheState

    conv = 'round1-durable-memwins'
    _clean(conv)
    mp = pytest.MonkeyPatch()
    store = _patch_persist_with_fake_store(mp)
    try:
        # Durable baseline is small/stale (would look like a huge collapse) …
        store[(OWNER, conv)] = 262_000
        # … but a LIVE sibling from the previous turn read ~261k this round too.
        sib = CacheState()
        sib.call_count = 5
        sib.last_cache_read_tokens = 261_000
        sib.message_count = 40
        sib.model = 'claude-opus-4'
        sib.last_update_time = time.time()
        with _st._cache_lock:
            _st._cache_states[(OWNER, conv, 999_000_222)] = sib

        usage = {'cache_read_input_tokens': 261_000,
                 'cache_creation_input_tokens': 3_000,
                 'input_tokens': 4_000}
        res = detect_cache_break(conv, _messages(), _tools(),
                                 'claude-opus-4', usage, user_id=OWNER)
        assert res is None, (
            f'a carried-over warm prefix (live baseline) must not be a break: {res}')
    finally:
        mp.undo()
        _clean(conv)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
