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
  5. L2 economics keeps the durable warm-read baseline through one or two
     transient cold rounds, but verified-cold round three supersedes it.

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
    import lib.tasks_pkg.cache_tracking._state as st
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
        st._cache_states[(1, conv, 999999)] = _make_state(200, read=50000)
    try:
        # Current real thread has NO entry for this conv → without the fix
        # this returns 0; with the fix it returns the sibling's 200-2.
        got = get_cache_prefix_count(conv, user_id=1)
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
        st._cache_states[_state_key(conv, user_id=1)] = _make_state(300, read=60000)
        st._cache_states[(1, conv, 111111)] = _make_state(999, read=60000)
    try:
        got = get_cache_prefix_count(conv, user_id=1)
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
        st._cache_states[(1, 'SOME-OTHER-CONV', 222222)] = _make_state(500, read=90000)
    try:
        got = get_cache_prefix_count(conv, user_id=1)
        assert got == 0, f'other conv must not leak (0), got {got}'
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_same_conversation_id_never_leaks_across_owners():
    """Owner identity is part of the cache-state namespace."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    conv = 'shared-external-id'
    with st._cache_lock:
        st._cache_states.clear()
        st._cache_states[(2, conv, 222222)] = _make_state(500, read=90000)
    try:
        assert get_cache_prefix_count(conv, user_id=1) == 0
        assert get_cache_prefix_count(conv, user_id=2) == 498
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_single_zero_round_does_not_open_guard():
    """★ THE 2026-08-01 INCIDENT INVARIANT. A sibling whose LAST round read
    AND wrote zero tokens must STILL contribute its boundary: one zero round
    does NOT prove the prefix is uncached (Anthropic write-visibility race,
    gateway stochastic miss, namespace flip, kimi's never-reported
    cache_write). The old gate (read>1000 or write>1000) collapsed here,
    letting micro_compact rewrite the just-sent prefix → the next round
    missed → the guard stayed down → a self-feeding re-bill loop (measured:
    conv ms9ow2tt calls 3→6). Only COLD_STREAK_GUARD_OPEN CONSECUTIVE cold
    rounds may open it (see test_cold_streak_opens_guard)."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    conv = 'conv-xthread-4'
    with st._cache_lock:
        st._cache_states.clear()
        # One zero-token round (streak=1) against a previously-sent prefix of
        # 400 messages: the guard must stay UP.
        _s = _make_state(400, read=0, write=0)
        _s.cold_streak = 1
        st._cache_states[(1, conv, 333333)] = _s
    try:
        got = get_cache_prefix_count(conv, user_id=1)
        assert got == 398, (
            f'one cold round must NOT open the guard (398), got {got}')
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_cold_streak_opens_guard():
    """After COLD_STREAK_GUARD_OPEN consecutive verifiably-cold rounds the
    guard legitimately OPENS — a genuinely dead cache makes prefix compaction
    free again (L1's purpose). Below the threshold it stays up."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    from lib.tasks_pkg.cache_tracking._state import COLD_STREAK_GUARD_OPEN
    conv = 'conv-xthread-5'
    with st._cache_lock:
        st._cache_states.clear()
        _s = _make_state(400, read=0, write=0)
        _s.cold_streak = COLD_STREAK_GUARD_OPEN
        st._cache_states[(1, conv, 444444)] = _s
        _s2 = _make_state(500, read=0, write=0)
        _s2.cold_streak = COLD_STREAK_GUARD_OPEN - 1
        st._cache_states[(1, conv, 555555)] = _s2
    try:
        # The streak-cold sibling contributes 0, but the below-threshold one
        # still holds its floor — max() semantics.
        got = get_cache_prefix_count(conv, user_id=1)
        assert got == 498, (
            f'below-threshold sibling must hold the floor (498), got {got}')
        # With ONLY streak-cold entries the boundary collapses to 0.
        with st._cache_lock:
            del st._cache_states[(1, conv, 555555)]
        got_cold = get_cache_prefix_count(conv, user_id=1)
        assert got_cold == 0, (
            f'streak-cold guard must open (0), got {got_cold}')
    finally:
        with st._cache_lock:
            st._cache_states.clear()


@pytest.mark.parametrize('cold_streak', [0, 1, 2])
def test_warm_read_keeps_durable_baseline_before_guard_opens(
    monkeypatch, cold_streak,
):
    """A transient miss must not make a warm prefix look free to rewrite."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking import _persist
    conv = f'warm-read-transient-{cold_streak}'

    monkeypatch.setattr(
        _persist, 'read_last_turn_cache_read',
        lambda _conv_id, *, user_id: 90_000)
    with st._cache_lock:
        st._cache_states.clear()
        current = _make_state(400, read=0, write=0)
        current.cold_streak = cold_streak
        st._cache_states[st._state_key(conv, user_id=1)] = current
    try:
        assert st.get_warm_cache_read(conv, user_id=1) == 90_000
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_verified_cold_current_task_overrides_stale_warm_fallbacks(monkeypatch):
    """Round three is newer evidence than durable or sibling warm history."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking import _persist
    conv = 'warm-read-proven-cold'

    monkeypatch.setattr(
        _persist, 'read_last_turn_cache_read',
        lambda _conv_id, *, user_id: 90_000)
    with st._cache_lock:
        st._cache_states.clear()
        current = _make_state(400, read=0, write=0)
        current.cold_streak = st.COLD_STREAK_GUARD_OPEN
        st._cache_states[st._state_key(conv, user_id=1)] = current
        st._cache_states[(1, conv, 999999)] = _make_state(
            500, read=120_000, write=0)
    try:
        assert st.get_warm_cache_read(conv, user_id=1) == 0
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def _usage(read=0, write=0):
    return {'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write}


def test_streak_bookkeeping_in_detect():
    """detect_cache_break drives the streak: cold rounds increment (capped at
    COLD_STREAK_GUARD_OPEN), any warm round (read OR write over the floor)
    resets, and a usage-less round leaves it unchanged (a failed call carries
    no cache signal and must not open the guard)."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._state import (
        COLD_STREAK_GUARD_OPEN, _state_key)
    conv = 'conv-streak-1'
    msgs = [{'role': 'system', 'content': 'S' * 200},
            {'role': 'user', 'content': 'u'}]
    with st._cache_lock:
        st._cache_states.clear()
    try:
        for i in range(1, COLD_STREAK_GUARD_OPEN + 2):
            detect_cache_break(conv, msgs, None, 'claude-opus-4',
                               usage=_usage(0, 0), user_id=1)
            got = st._cache_states[_state_key(conv, user_id=1)].cold_streak
            assert got == min(i, COLD_STREAK_GUARD_OPEN), (
                f'round {i}: streak should be {min(i, COLD_STREAK_GUARD_OPEN)}, '
                f'got {got}')
        # A warm round (write only — the Anthropic miss+rewrite shape) resets.
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(0, 50000), user_id=1)
        assert st._cache_states[_state_key(conv, user_id=1)].cold_streak == 0, (
            'a fresh cache_write must reset the streak (entry just re-created)')
        # A usage-less round does not move it.
        detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=None, user_id=1)
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(0, 0), user_id=1)
        got = st._cache_states[_state_key(conv, user_id=1)].cold_streak
        assert got == 1, (
            f'usage-less round must not increment (only the zero-usage round '
            f'after it counts), got {got}')
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_e2e_single_miss_keeps_prefix_frozen_then_recovers():
    """★ THE INCIDENT REPLAYED. Sequence measured on conv ms9ow2tt
    (2026-08-01, calls 3→6): a warm prefix, then one zero-token round
    (gateway miss), then L1's guard check, then recovery. With the hysteresis
    the guard stays UP through the transient miss — the prefix bytes the NEXT
    round sends are identical, so the entry is read back and the streak
    resets instead of looping."""
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    conv = 'conv-streak-e2e'
    # 9 messages → a sent prefix of 7 (EDITABLE_TAIL_COUNT=2).
    msgs = [{'role': 'system', 'content': 'S' * 200}]
    for i in range(4):
        msgs.append({'role': 'user', 'content': f'u{i}'})
        msgs.append({'role': 'assistant', 'content': f'a{i}'})
    with st._cache_lock:
        st._cache_states.clear()
    try:
        # Round 1: warm (a big write establishes the entry).
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(0, 90000), user_id=1)
        assert get_cache_prefix_count(conv, user_id=1) == 7
        # Round 2: the transient gateway miss (read=0, write=0). Guard must
        # stay UP — this is exactly where the old gate collapsed and let L1
        # rewrite msg[35]/msg[42].
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(0, 0), user_id=1)
        assert get_cache_prefix_count(conv, user_id=1) == 7, (
            'one transient miss opened the guard — the 2026-08-01 loop')
        # Round 3: still zero (write-visibility race). Guard STILL up.
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(0, 0), user_id=1)
        assert get_cache_prefix_count(conv, user_id=1) == 7
        # Round 4: the entry is read back (bytes never mutated) → streak
        # resets, guard remains.
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(85000, 0), user_id=1)
        assert get_cache_prefix_count(conv, user_id=1) == 7
        assert st._cache_states[(1, conv, __import__('threading').get_ident())].cold_streak == 0
    finally:
        with st._cache_lock:
            st._cache_states.clear()


def test_hwm_write_failure_warns_throttled():
    """FIX B-lite observability: a durable-floor WRITE failure must surface as
    a WARNING (degraded protection — it was DEBUG-silent when two production
    convs lost cachePrefixHWM), throttled to once per conv per window so a
    strained DB cannot spam error.log."""
    import logging
    import lib.tasks_pkg.cache_tracking._persist as _persist
    records: list[str] = []
    logger = logging.getLogger('lib.tasks_pkg.cache_tracking._persist')
    h = logging.Handler()
    h.emit = lambda rec: records.append(rec.getMessage())
    prev_level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(h)
    _persist._warn_last.clear()
    try:
        _persist._warn_throttled('convX', 1, '[CacheHWM] advance failed conv=%s: %s',
                                 'convX', 'boom')
        _persist._warn_throttled('convX', 1, '[CacheHWM] advance failed conv=%s: %s',
                                 'convX', 'boom-again')
        _persist._warn_throttled('convY', 1, '[CacheHWM] advance failed conv=%s: %s',
                                 'convY', 'boom')
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev_level)
        _persist._warn_last.clear()
    assert len(records) == 2, (
        f'first-per-conv must warn once each (throttled), got {records}')
    assert 'boom' in records[0] and 'convY' in records[1]


def test_gap_s_falls_back_to_sibling_timestamp():
    """BUG C (44/44 broken records 2026-08-01): a fresh-thread round-1 must
    report the TRUE cross-turn gap, not 0.0. Seed a sibling state for the same
    conv with a timestamp 100s in the past; the fresh thread's first record
    must carry gap_s ≈ 100."""
    import io
    import json as _json
    import logging
    import time as _time
    st = _fresh_state_module()
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    conv = 'conv-gap-1'
    msgs = [{'role': 'system', 'content': 'S' * 200},
            {'role': 'user', 'content': 'u'}]
    with st._cache_lock:
        st._cache_states.clear()
        _sib = _make_state(50, read=80000)
        _sib.last_update_time = _time.time() - 100.0
        st._cache_states[(1, conv, 999999)] = _sib
    _logger = logging.getLogger('lib.tasks_pkg.cache_tracking._detect')
    _buf = io.StringIO()
    _h = logging.StreamHandler(_buf)
    _prev = _logger.level
    _logger.setLevel(logging.INFO)
    _logger.addHandler(_h)
    try:
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage=_usage(0, 50000), user_id=1)
    finally:
        _logger.removeHandler(_h)
        _logger.setLevel(_prev)
        with st._cache_lock:
            st._cache_states.clear()
    rec = None
    for line in _buf.getvalue().splitlines():
        if '[CacheRoundRecord]' in line:
            rec = _json.loads(line.split('[CacheRoundRecord]', 1)[1].strip())
    assert rec is not None, 'no CacheRoundRecord emitted'
    assert rec['gap_s'] > 50.0, (
        f"fresh-thread round-1 must inherit the sibling's true gap (~100s), "
        f"got gap_s={rec['gap_s']} (the broken-0.0 bug)")


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
    import lib.tasks_pkg.compaction._constants as compaction_constants
    from lib.tasks_pkg.compaction._steps import (
        CompactionContext, make_constants, run_steps)
    import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401
    ctx = CompactionContext(
        messages=messages, conv_id='sim', task=None,
        constants=make_constants(compaction_constants, None),
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
    import lib.tasks_pkg.cache_tracking._persist as _persist
    store: dict[str, dict] = {}

    def _fake_read(conv_id, *, user_id):
        v = store.get(conv_id, {}).get('cachePrefixHWM')
        return v if isinstance(v, int) and v > 0 else 0

    def _fake_advance(conv_id, boundary, *, user_id):
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
    import lib.tasks_pkg.cache_tracking._persist as _persist
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    store = _patch_persist_with_fake_settings(mp)
    try:
        _persist.advance_persisted_boundary('c1', 100, user_id=1)
        assert store['c1']['cachePrefixHWM'] == 100
        _persist.advance_persisted_boundary('c1', 250, user_id=1)   # grows
        assert store['c1']['cachePrefixHWM'] == 250
        _persist.advance_persisted_boundary('c1', 40, user_id=1)    # smaller — ignored
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
    import lib.tasks_pkg.cache_tracking._persist as _persist
    conv = 'conv-restart-1'
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    store = _patch_persist_with_fake_settings(mp)
    try:
        # Turn A (some earlier process) persisted the boundary as a durable HWM.
        _persist.advance_persisted_boundary(conv, 300, user_id=1)
        # RESTART: memory is wiped — no CacheState entry for this conv at all.
        with st._cache_lock:
            st._cache_states.clear()
        # Fresh-thread round after restart: in-memory best=0, but the durable
        # floor restores 300 → guard stays up.
        got = get_cache_prefix_count(conv, user_id=1)
        assert got == 300, f'durable restore expected 300, got {got}'

        # NEUTER: hide the persisted signal (as if only the in-memory path
        # existed) → boundary collapses to 0, the restart-window bug.
        mp.setattr(
            _persist, 'read_persisted_boundary',
            lambda _c, *, user_id: 0,
        )
        got_neuter = get_cache_prefix_count(conv, user_id=1)
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
    import lib.tasks_pkg.cache_tracking._persist as _persist
    conv = 'conv-restart-2'
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    _patch_persist_with_fake_settings(mp)
    try:
        _persist.advance_persisted_boundary(conv, 100, user_id=1)  # stale-ish durable
        with st._cache_lock:
            st._cache_states.clear()
            # a LIVE warm sibling with a LARGER boundary (298)
            st._cache_states[(1, conv, 424242)] = _make_state(300, read=70000)
        got = get_cache_prefix_count(conv, user_id=1)
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
    import lib.tasks_pkg.cache_tracking._persist as _persist
    conv = 'conv-shrink-1'
    _persist._reset_read_cache_for_tests()
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    _patch_persist_with_fake_settings(mp)
    try:
        # An old, LARGE warm sibling boundary (simulating the pre-shrink turn).
        with st._cache_lock:
            st._cache_states.clear()
            st._cache_states[(1, conv, 505050)] = _make_state(400, read=80000)
        _persist.advance_persisted_boundary(conv, 398, user_id=1)  # durable HWM also large

        # History has since SHRUNK to 50 messages (post macro-compaction).
        clamped = get_cache_prefix_count(conv, current_msg_count=50, user_id=1)
        assert clamped == 50 - EDITABLE_TAIL_COUNT == 48, (
            f'clamp: boundary must fall to current 48, got {clamped}')
        # It must be < current_msg_count so is_in_cache_prefix leaves the tail
        # editable → micro_compact still runs.
        assert clamped < 50

        # NEUTER: no current_msg_count → clamp off → the stale monotonic value
        # (the bug: boundary >= real message count → compaction fully disabled).
        raw = get_cache_prefix_count(conv, user_id=1)  # current_msg_count=None
        assert raw >= 398 and raw >= 50, (
            f'NEUTER: without the clamp the stale boundary ({raw}) exceeds the '
            f'shrunk history (50) → micro_compact would be permanently disabled')
    finally:
        mp.undo()
        with st._cache_lock:
            st._cache_states.clear()
