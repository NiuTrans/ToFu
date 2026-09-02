#!/usr/bin/env python3
"""Phase-C ratchet — L2 (force-summary) cache-ROI instrumentation.

WHY
---
An L2 force-summary compaction deliberately rewrites the cached prefix to fit
the window. That is a known, expected prefix bust, so ``notify_compaction``
suppresses break classification. The detector otherwise attributes only
observed wire changes.
But "expected" is not "free": the summary drops prefix tokens (the SAVED half)
yet forces the whole fresh prefix to be re-written on the following round (the
RE-BILLED half). Before retuning ``_SUMMARY_TRIGGER_RATIO`` we must MEASURE the
net of those two halves — not guess.

The two halves are separated in time:
  * ``record_l2_compaction`` stashes the SAVED half at the compaction event
    (tokens dropped + the cache_read that was in flight and is now busted).
  * ``detect_cache_break`` on the FOLLOWING round pairs it with the RE-BILLED
    half (that round's ``cache_write``) and emits ONE
    ``audit_log('l2_cache_roi', ...)`` with BOTH sides populated.

This ratchet drives the REAL functions and asserts the emitted metric has BOTH
halves (not just the cheap-to-measure "saved" side). Double-neutered.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _capture_roi(fn):
    """Run fn() with the ROI owner's audit logger spied; return the
    list of ('l2_cache_roi', details) captured."""
    import lib.tasks_pkg.cache_tracking._roi as _ct
    captured = []
    _orig = _ct.audit_log

    def _spy(event, **details):
        if event == 'l2_cache_roi':
            captured.append(details)
        return _orig(event, **details)

    _ct.audit_log = _spy
    try:
        fn()
    finally:
        _ct.audit_log = _orig
    return captured


def test_l2_roi_emits_both_halves():
    """★ THE MEASUREMENT. A real L2 event (record_l2_compaction) followed by a
    real detect_cache_break must emit ONE l2_cache_roi metric carrying BOTH the
    saved half (tokens_dropped) AND the re-billed half (cache_write_rebilled) —
    plus a computed net."""
    import lib.tasks_pkg.cache_tracking._roi as _ct
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_states,
        _state_key,
    )
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._roi import record_l2_compaction
    conv = 'l2roi-both'
    _cache_states.clear()
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'u1'}]

    def _run():
        # Round 1: establish a warm prefix (large cache_read in flight).
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage={'cache_read_input_tokens': 120000,
                                  'cache_creation_input_tokens': 0}, user_id=1)
        # L2 fires: summary drops ~80k tokens off the prefix.
        record_l2_compaction(conv, tokens_before=200000, tokens_after=120000,
                             msgs_before=40, msgs_after=12, user_id=1)
        # Round 2 (post-summary): the fresh prefix is re-written → cache_write.
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage={'cache_read_input_tokens': 0,
                                  'cache_creation_input_tokens': 118000}, user_id=1)

    rec = _capture_roi(_run)
    assert len(rec) == 1, f'expected exactly one l2_cache_roi metric, got {len(rec)}'
    d = rec[0]
    # SAVED half populated.
    assert d.get('tokens_dropped') == 80000, f'saved half wrong: {d}'
    assert d.get('cache_read_busted') == 120000, (
        f'busted-read (saved half) not captured: {d}')
    # RE-BILLED half populated — the whole point (not just the saved side).
    assert d.get('cache_write_rebilled') == 118000, (
        f're-billed half MISSING or wrong — measurement is half-blind: {d}')
    # Net computed = dropped - re-billed.
    assert d.get('net_tokens') == 80000 - 118000, f'net wrong: {d}'
    assert d.get('outcome') == 'paired', f'observed re-bill must be outcome=paired: {d}'
    # Pending record cleared so it fires exactly once.
    st = _cache_states.get(_state_key(conv, user_id=1))
    assert st is not None and st.pending_l2_roi is None, (
        'pending_l2_roi not cleared — would double-emit')
    _ok('L2 ROI metric carries BOTH saved (dropped + busted read) and '
        're-billed (cache_write) halves + net')


def test_execute_compact_tool_fires_recorder():
    """★ WIRING (not just the primitive). The REAL force-compact wrapper must
    CALL record_l2_compaction after injecting the synthetic summary pair, so
    the saved-half measurement describes the actual next request. Drive the
    real wrapper with the LLM summary stubbed (no network) and spy it."""
    import lib.tasks_pkg.compaction._layer2._compact as _l2
    fired = []
    _orig_rec = None
    import lib.tasks_pkg.cache_tracking._roi as _ct
    _orig_rec = _ct.record_l2_compaction
    _orig_summary = _l2._generate_query_aware_summary
    # Stub the cheap-model summary so the mutation path runs without an LLM.
    _l2._generate_query_aware_summary = (
        lambda *a, **k: 'STUBBED SUMMARY of earlier turns.')
    _ct.record_l2_compaction = lambda conv_id, **kw: fired.append((conv_id, kw))
    # Build a message list with 3 turns so there is an old prefix to summarize
    # while the current turn is preserved. Keep it large enough that the
    # deliberately forced compaction has positive payback after its tool pair.
    msgs = [{'role': 'system', 'content': 'sys'}]
    for i in range(3):
        msgs.append({'role': 'user', 'content': f'question {i} ' + 'x' * 2000})
        msgs.append({'role': 'assistant', 'content': f'answer {i} ' + 'y' * 2000})
    task = {'id': 'tk-l2wire', 'convId': 'cv-l2wire', '_userId': 1,
            'config': {}}
    try:
        # force=True bypasses the threshold gate; this test covers recorder
        # wiring and final request accounting, not the trigger policy.
        _l2.force_compact_if_needed(
            msgs, task=task, force=True, keep_recent_pairs=1)
        assert fired, (
            'force_compact_if_needed did NOT fire record_l2_compaction — the '
            'ROI recorder has no production caller (dead scaffolding).')
        conv_id, kw = fired[0]
        assert conv_id == 'cv-l2wire'
        # The saved-half args must be real numbers from the mutation.
        assert kw.get('tokens_before', 0) > kw.get('tokens_after', 0), (
            f'recorder fired with non-shrinking token counts: {kw}')
        assert kw.get('msgs_before', 0) >= kw.get('msgs_after', 0), (
            f'recorder fired with growing msg counts: {kw}')
        assert kw.get('user_id') == 1
    finally:
        _l2._generate_query_aware_summary = _orig_summary
        _ct.record_l2_compaction = _orig_rec
    _ok('real force_compact_if_needed fires record_l2_compaction (wiring, '
        'not just the primitive)')


def test_l2_roi_no_emit_without_event():
    """A plain round with NO preceding L2 event emits NO l2_cache_roi metric
    (the instrumentation is event-scoped, not per-round noise)."""
    from lib.tasks_pkg.cache_tracking._state import _cache_states
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    conv = 'l2roi-none'
    _cache_states.clear()
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'u1'}]

    def _run():
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage={'cache_read_input_tokens': 5000}, user_id=1)
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage={'cache_read_input_tokens': 6000}, user_id=1)

    rec = _capture_roi(_run)
    assert rec == [], f'emitted an ROI metric with no L2 event: {rec}'
    _ok('no L2 event → no l2_cache_roi metric (event-scoped, not per-round)')


def test_l2_roi_flushed_at_cleanup_when_no_following_round():
    """★ ANTI-BIAS. An L2 event that is the LAST cache-relevant act of a session
    (task completes / conv idle before another detect_cache_break) must STILL
    emit an l2_cache_roi metric — marked outcome='no_following_round' with the
    re-billed half UNOBSERVED — rather than being silently dropped. Late/last
    L2 fires are the most likely ones (context grows monotonically), so
    dropping them biases the retune dataset."""
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_states,
        cleanup_cache_state,
    )
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._roi import record_l2_compaction
    conv = 'l2roi-flush-cleanup'
    _cache_states.clear()
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'u1'}]

    def _run():
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage={'cache_read_input_tokens': 90000}, user_id=1)
        record_l2_compaction(conv, tokens_before=200000, tokens_after=110000,
                             msgs_before=40, msgs_after=10, user_id=1)
        # NO following detect_cache_break — session ends here.
        cleanup_cache_state(conv, user_id=1)

    rec = _capture_roi(_run)
    assert len(rec) == 1, f'late L2 event was DROPPED, not flushed: {len(rec)}'
    d = rec[0]
    assert d.get('outcome') == 'no_following_round', f'wrong outcome: {d}'
    assert d.get('tokens_dropped') == 90000, f'saved half lost on flush: {d}'
    assert d.get('cache_write_rebilled') is None, (
        f're-billed half must be UNOBSERVED (None), not faked: {d}')
    assert d.get('net_tokens') is None, (
        f'net must be None when re-bill unobserved (excludable from dist): {d}')
    _ok('late/last-round L2 event is FLUSHED at cleanup (unobserved), not '
        'dropped — dataset unbiased')


def test_l2_roi_second_event_flushes_first():
    """★ NO-CLOBBER. Two L2 compactions in the same round-gap: the second
    record_l2_compaction must FLUSH the first's unpaired record (unobserved)
    before stashing its own, so the first event's ROI is counted, not lost."""
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_states,
        _state_key,
    )
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._roi import record_l2_compaction
    conv = 'l2roi-double'
    _cache_states.clear()
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'u1'}]

    def _run():
        detect_cache_break(conv, msgs, None, 'claude-opus-4',
                           usage={'cache_read_input_tokens': 90000}, user_id=1)
        # First L2 (e.g. reactive) — drops 90k.
        record_l2_compaction(conv, tokens_before=200000, tokens_after=110000,
                             msgs_before=40, msgs_after=10, user_id=1)
        # Second L2 (e.g. proactive) in the SAME gap — drops 30k. Must flush #1.
        record_l2_compaction(conv, tokens_before=110000, tokens_after=80000,
                             msgs_before=10, msgs_after=6, user_id=1)

    rec = _capture_roi(_run)
    assert len(rec) == 1, (
        f'the first L2 event was clobbered, not flushed: got {len(rec)} metrics')
    d = rec[0]
    # The flushed one is the FIRST event (90k dropped), unobserved.
    assert d.get('tokens_dropped') == 90000, f'flushed the wrong event: {d}'
    assert d.get('outcome') == 'no_following_round', f'wrong outcome: {d}'
    assert d.get('cache_write_rebilled') is None, f're-bill must be unobserved: {d}'
    # The SECOND event is now the pending record (30k), still awaiting a round.
    st = _cache_states.get(_state_key(conv, user_id=1))
    assert st is not None and st.pending_l2_roi is not None, (
        'second event should be stashed as the new pending record')
    assert st.pending_l2_roi.get('tokens_dropped') == 30000, (
        f'pending record is not the second event: {st.pending_l2_roi}')
    _ok('a second L2 event FLUSHES the first (unobserved), never clobbers it')


def test_l2_roi_cold_conv_is_noop():
    """record_l2_compaction on a conv with NO cache state (cold) is a no-op —
    nothing was cached to bust, so there is no re-bill to pair. Must not crash
    and must not stash a dangling pending record."""
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_states,
        _state_key,
    )
    from lib.tasks_pkg.cache_tracking._roi import record_l2_compaction
    conv = 'l2roi-cold'
    _cache_states.clear()
    record_l2_compaction(conv, tokens_before=100, tokens_after=50,
                         msgs_before=4, msgs_after=2, user_id=1)
    assert _cache_states.get(_state_key(conv, user_id=1)) is None, (
        'cold conv should not have a CacheState materialized by ROI recording')
    _ok('cold conv record_l2_compaction is a safe no-op')


def main():
    print()
    print(_color('═══ Phase-C L2 cache-ROI ratchet ═══', '36'))
    print()
    tests = [
        test_l2_roi_emits_both_halves,
        test_execute_compact_tool_fires_recorder,
        test_l2_roi_no_emit_without_event,
        test_l2_roi_flushed_at_cleanup_when_no_following_round,
        test_l2_roi_second_event_flushes_first,
        test_l2_roi_cold_conv_is_noop,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} L2-ROI TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
