"""tests/test_cache_prefix_cross_thread_freeze.py

Turn-boundary prefix-cache kill: a NEW user turn runs on a NEW ``run_task``
worker thread, and ``get_cache_prefix_count`` keys CacheState per
``(conv_id, thread_id)``. So on that turn's first round the current thread has
NO warm state → the boundary used to collapse to 0 → ``micro_compact``'s prefix
guard went OFF → it compacted cold history the gateway STILL had cached from
the previous turn (within TTL) → the whole prefix's wire bytes changed →
guaranteed miss re-billing ~all of it.

ROOT-CAUSE INVARIANT (the fix): a message that has been sent as part of a
cached prefix must have FROZEN wire bytes on every later round, regardless of
which worker thread the round runs on. The cached prefix is a CONVERSATION
fact, not a thread fact, so ``get_cache_prefix_count`` falls back to the MAX
boundary any sibling-thread state for the SAME conv holds when the current
thread is cold. Raising the floor only ever PROTECTS more messages from
compaction — cache-safe by construction.

Pins:
  1. cross-thread fallback returns the prior thread's boundary when the
     current thread is cold (the fix).
  2. own warm state still wins (no regression to the same-thread path).
  3. an unrelated conv's state never leaks into the boundary.
  4. END-TO-END freeze: a tool_result that entered the cached prefix on
     turn A is NOT compacted (wire bytes frozen) on turn B's fresh-thread
     round-1 — and the NEUTER (fallback short-circuited to 0) proves the
     guard would otherwise rewrite it.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
        tests/test_cache_prefix_cross_thread_freeze.py
"""

from __future__ import annotations

import copy
import threading

import pytest

pytestmark = pytest.mark.unit


def _fresh_state_module():
    from lib.tasks_pkg.cache_tracking import _state as st
    return st


def _make_state(msg_count, read=0, write=0):
    from lib.tasks_pkg.cache_tracking._state import CacheState
    s = CacheState()
    s.message_count = msg_count
    s.last_cache_read_tokens = read
    s.last_cache_write_tokens = write
    s.call_count = 1
    return s


def test_cross_thread_fallback_returns_prior_thread_boundary():
    """The fix: current thread cold → fall back to a warm sibling thread's
    boundary for the same conv."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    conv = 'conv-xthread-1'
    with st._cache_lock:
        st._cache_states.clear()
        # A warm entry under a DIFFERENT (fake) thread id for the same conv.
        st._cache_states[(conv, 999999)] = _make_state(200, read=50000)
    try:
        # Current real thread has NO entry for this conv → without the fix
        # this returns 0; with the fix it returns the sibling's 200-2.
        got = get_cache_prefix_count(conv)
        assert got == 198, f'expected cross-thread fallback 198, got {got}'
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_own_warm_state_still_wins():
    """No regression: when the current thread HAS warm state, use it directly
    (not a stale sibling)."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    from lib.tasks_pkg.cache_tracking._state import _state_key
    conv = 'conv-xthread-2'
    with st._cache_lock:
        st._cache_states.clear()
        st._cache_states[_state_key(conv)] = _make_state(300, read=60000)
        st._cache_states[(conv, 111111)] = _make_state(999, read=60000)
    try:
        got = get_cache_prefix_count(conv)
        assert got == 298, f'own warm state must win (298), got {got}'
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_other_conv_never_leaks():
    """A different conversation's warm state must NOT raise this conv's
    boundary."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    conv = 'conv-xthread-3'
    with st._cache_lock:
        st._cache_states.clear()
        st._cache_states[('SOME-OTHER-CONV', 222222)] = _make_state(500, read=90000)
    try:
        got = get_cache_prefix_count(conv)
        assert got == 0, f'other conv must not leak (0), got {got}'
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_cold_sibling_not_used():
    """A sibling entry that was itself cold (no read/write >1000) contributes
    0 — we only protect against a genuinely-cached prior prefix."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    conv = 'conv-xthread-4'
    with st._cache_lock:
        st._cache_states.clear()
        st._cache_states[(conv, 333333)] = _make_state(400, read=0, write=0)
    try:
        got = get_cache_prefix_count(conv)
        assert got == 0, f'cold sibling contributes 0, got {got}'
    finally:
        with st._cache_lock:
            st._cache_states.clear()


# ── END-TO-END freeze + NEUTER ──────────────────────────────────────────────

def _build_convo_with_cold_tool():
    """A conversation whose EARLY tool_result is long (compactable) and sits
    deep in the prefix (well before the hot tail)."""
    msgs = [{'role': 'system', 'content': 'S' * 300}]
    # 45 tool turns so the early ones fall outside MICRO_HOT_TAIL (40).
    for i in range(45):
        msgs.append({'role': 'assistant', 'tool_calls': [
            {'id': f't{i}', 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'tool_call_id': f't{i}', 'name': 'read_files',
                     'content': ('LINE\n' * 800)})  # ~4000 chars → compactable
    return msgs


def _run_compaction(messages, prefix_count):
    import lib.tasks_pkg.compaction as _pkg
    from lib.tasks_pkg.compaction._steps import (
        CompactionContext, make_constants, run_steps)
    import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401
    ctx = CompactionContext(
        messages=messages, conv_id='sim', task=None,
        constants=make_constants(_pkg, None),
        cache_prefix_count=prefix_count, ignore_cache_prefix=False,
        stamp_fn=lambda *a, **k: None)
    return run_steps(['strip_thinking', 'compact_tool_results',
                      'strip_cold_images'], ctx)


def test_e2e_prefix_frozen_across_fresh_thread_turn():
    """A tool_result that entered the cached prefix on turn A must have its
    wire bytes frozen on turn B's fresh-thread round-1 (guard resolves the
    cross-thread boundary). NEUTER: force the boundary to 0 (the old cold-
    thread behaviour) and the SAME message IS rewritten."""
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, first_changed_index)

    built = _build_convo_with_cold_tool()
    n = len(built)
    from lib.tasks_pkg.cache_tracking._detect import EDITABLE_TAIL_COUNT
    prior_boundary = max(0, n - EDITABLE_TAIL_COUNT)  # what a warm sibling holds

    # Turn A cached this exact prefix (no mutation — the reference bytes).
    fp_cached = canonical_messages(built)

    # ── FIX path: fresh-thread round-1 resolves the cross-thread boundary. ──
    work_fix = copy.deepcopy(built)
    _run_compaction(work_fix, prefix_count=prior_boundary)
    fci_fix = first_changed_index(fp_cached, canonical_messages(work_fix))
    assert fci_fix == -1, (
        f'FIX: prefix must be byte-frozen across the fresh-thread turn, but '
        f'msg {fci_fix} changed')

    # ── NEUTER: boundary collapses to 0 (old cold-thread behaviour). ──
    work_neuter = copy.deepcopy(built)
    saved = _run_compaction(work_neuter, prefix_count=0)
    fci_neuter = first_changed_index(fp_cached, canonical_messages(work_neuter))
    assert saved > 0 and 0 <= fci_neuter < prior_boundary, (
        f'NEUTER: with boundary=0 the guard must rewrite an already-cached '
        f'prefix msg (saved={saved}, first_changed={fci_neuter}, '
        f'boundary={prior_boundary})')


# ═══════════════════════════════════════════════════════════════════════════
# DURABLE boundary — survives restart / replica switch (the in-memory fallback
# is NOT "forever"; _cache_states is process memory). settings.cachePrefixHWM.
# ═══════════════════════════════════════════════════════════════════════════

def _patch_persist_with_fake_settings(monkeypatch):
    """Back _persist's read/write with an in-memory settings dict (no DB), so
    we can drive the durable path deterministically. Returns the store dict."""
    from lib.tasks_pkg.cache_tracking import _persist
    store: dict[str, dict] = {}

    def _fake_read(conv_id):
        v = store.get(conv_id, {}).get('cachePrefixHWM')
        return v if isinstance(v, int) and v > 0 else 0

    def _fake_advance(conv_id, boundary):
        if not conv_id or boundary <= 0:
            return
        cur = store.setdefault(conv_id, {}).get('cachePrefixHWM', 0)
        if boundary > cur:
            store[conv_id]['cachePrefixHWM'] = boundary  # monotonic

    monkeypatch.setattr(_persist, 'read_persisted_boundary', _fake_read)
    monkeypatch.setattr(_persist, 'advance_persisted_boundary', _fake_advance)
    # _prefix imports read_persisted_boundary lazily from the module, so the
    # monkeypatch on the module attribute is picked up on next call.
    return store


def test_persist_monotonic_advance():
    """advance_persisted_boundary only ever RAISES (monotonic high-water)."""
    from lib.tasks_pkg.cache_tracking import _persist
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    store = _patch_persist_with_fake_settings(mp)
    try:
        _persist.advance_persisted_boundary('c1', 100)
        assert store['c1']['cachePrefixHWM'] == 100
        _persist.advance_persisted_boundary('c1', 250)   # grows
        assert store['c1']['cachePrefixHWM'] == 250
        _persist.advance_persisted_boundary('c1', 40)    # smaller — ignored
        assert store['c1']['cachePrefixHWM'] == 250
    finally:
        mp.undo()


def test_get_prefix_count_restores_from_persisted_after_restart():
    """RESTART SIMULATION (the load-bearing case): after _cache_states is
    CLEARED (= process restart / new replica), a fresh-thread round finds NO
    in-memory sibling, but get_cache_prefix_count must RESTORE the boundary
    from the durable persisted high-water mark. NEUTER: short-circuit the
    persisted read → the boundary collapses to 0 (bug reproduced)."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    from lib.tasks_pkg.cache_tracking import _persist
    conv = 'conv-restart-1'
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    store = _patch_persist_with_fake_settings(mp)
    try:
        # Turn A (some earlier process) persisted the boundary as a durable HWM.
        _persist.advance_persisted_boundary(conv, 300)
        # RESTART: memory is wiped — no CacheState entry for this conv at all.
        with st._cache_lock:
            st._cache_states.clear()
        # Fresh-thread round after restart: in-memory best=0, but the durable
        # floor restores 300 → guard stays up.
        got = get_cache_prefix_count(conv)
        assert got == 300, f'durable restore expected 300, got {got}'

        # NEUTER: hide the persisted signal (as if only the in-memory path
        # existed) → boundary collapses to 0, the restart-window bug.
        mp.setattr(_persist, 'read_persisted_boundary', lambda _c: 0)
        got_neuter = get_cache_prefix_count(conv)
        assert got_neuter == 0, (
            f'NEUTER: without the durable read the post-restart boundary must '
            f'collapse to 0 (bug), got {got_neuter}')
    finally:
        mp.undo()
        with st._cache_lock:
            st._cache_states.clear()


def test_persisted_floor_maxed_with_memory_sibling():
    """The durable read is a FLOOR combined via max() with the in-memory
    sibling — a live larger in-memory boundary is not lowered by a stale
    smaller persisted one, and vice-versa."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    from lib.tasks_pkg.cache_tracking import _persist
    conv = 'conv-restart-2'
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    _patch_persist_with_fake_settings(mp)
    try:
        _persist.advance_persisted_boundary(conv, 100)  # stale-ish durable
        with st._cache_lock:
            st._cache_states.clear()
            # a LIVE warm sibling with a LARGER boundary (298)
            st._cache_states[(conv, 424242)] = _make_state(300, read=70000)
        got = get_cache_prefix_count(conv)
        assert got == 298, f'max(mem 298, persisted 100) == 298, got {got}'
    finally:
        mp.undo()
        with st._cache_lock:
            st._cache_states.clear()


def test_boundary_clamped_when_history_shrinks():
    """HISTORY-SHRINK GUARD: a monotonic HWM/sibling boundary must NEVER
    exceed the messages that exist this round. After an L2/L3 macro-compaction
    or edit-and-resend the conv can drop from ~400 to ~50 messages; without the
    clamp get_cache_prefix_count returns the stale 400 → is_in_cache_prefix is
    True for every real index → micro_compact permanently disabled → context
    explosion. NEUTER: drop current_msg_count (clamp off) → boundary=stale
    (bug); compaction would be fully suppressed."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    from lib.tasks_pkg.cache_tracking._detect import EDITABLE_TAIL_COUNT
    from lib.tasks_pkg.cache_tracking import _persist
    conv = 'conv-shrink-1'
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    _patch_persist_with_fake_settings(mp)
    try:
        # An old, LARGE warm sibling boundary (simulating the pre-shrink turn).
        with st._cache_lock:
            st._cache_states.clear()
            st._cache_states[(conv, 505050)] = _make_state(400, read=80000)
        _persist.advance_persisted_boundary(conv, 398)  # durable HWM also large

        # History has since SHRUNK to 50 messages (post macro-compaction).
        clamped = get_cache_prefix_count(conv, current_msg_count=50)
        assert clamped == 50 - EDITABLE_TAIL_COUNT == 48, (
            f'clamp: boundary must fall to current 48, got {clamped}')
        # It must be < current_msg_count so is_in_cache_prefix leaves the tail
        # editable → micro_compact still runs.
        assert clamped < 50

        # NEUTER: no current_msg_count → clamp off → the stale monotonic value
        # (the bug: boundary >= real message count → compaction fully disabled).
        raw = get_cache_prefix_count(conv)  # current_msg_count=None
        assert raw >= 398 and raw >= 50, (
            f'NEUTER: without the clamp the stale boundary ({raw}) exceeds the '
            f'shrunk history (50) → micro_compact would be permanently disabled')
    finally:
        mp.undo()
        with st._cache_lock:
            st._cache_states.clear()
