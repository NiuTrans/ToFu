"""Tests for cache optimization improvements (2026-04-06).

Covers:
  1. last_update_time TTL detection fix — elapsed computation before state update
  2. Concurrent conversation tracking — cache contention detection
  3. Per-round cache stats logging at INFO level
  4. Session-stable TTL latch — prevents mid-session cache key shift
  5. Cache-aware tool result ordering — deterministic prefix for automatic caching
  6. cleanup_cache_state — memory management
"""

import time
import threading

import pytest

import lib as _lib


# ═══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_cache_state():
    """Reset cache state between tests."""
    from lib.tasks_pkg.cache_tracking._state import _cache_states
    from lib.tasks_pkg.cache_tracking._ttl import _ttl_latch
    _cache_states.clear()
    _ttl_latch.clear()
    yield
    _cache_states.clear()
    _ttl_latch.clear()


@pytest.fixture(autouse=True)
def _disable_extended_ttl():
    """Disable extended TTL by default for test isolation."""
    original = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = False
    yield
    _lib.CACHE_EXTENDED_TTL = original



# ═══════════════════════════════════════════════════════════════════════════════
#  0. Per-thread cache-state isolation (Bug B — concurrent agent-loop collision)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrentConvStateIsolation:
    """N concurrent agent loops under ONE conversation (swarm / flow /
    orchestration fan-out) must NOT clobber each other's prefix baseline.

    Before the fix, _cache_states was keyed by conv_id alone, so every
    worker thread sharing a conversation overwrote the same CacheState each
    round — producing the incoherent 'PREFIX MUTATION DETECTED' spam (call
    counts + message lengths jumping between threads) and cost
    misattribution. The fix keys state by (owner, conv_id, thread_id)."""

    def test_two_threads_same_conv_get_distinct_state(self):
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        conv = 'shared-conv'
        # Each thread walks its OWN growing message list under the SAME conv.
        results = {}

        def _worker(name, n_msgs):
            msgs = [{'role': 'system', 'content': 'sys'}]
            for i in range(n_msgs):
                msgs.append({'role': 'user', 'content': f'{name} u{i}'})
                msgs.append({'role': 'assistant', 'content': f'{name} a{i}'})
                detect_cache_break(conv, msgs, None, 'model-a',
                                   usage={'cache_read_tokens': 5000}, user_id=1)
            results[name] = (threading.get_ident(), len(msgs))

        t1 = threading.Thread(target=_worker, args=('A', 6))
        t2 = threading.Thread(target=_worker, args=('B', 10))
        t1.start(); t2.start(); t1.join(); t2.join()

        # Both threads' states coexist under the same conv_id, keyed apart.
        keys = [k for k in _cache_states if k[0] == 1 and k[1] == conv]
        assert len(keys) == 2, (
            f'expected 2 thread-distinct states for one conv, got {len(keys)}: '
            f'{keys} — conv-only keying collapses concurrent loops into one')
        # The two states tracked DIFFERENT message counts (no clobber).
        counts = sorted(_cache_states[k].message_count for k in keys)
        assert counts == [13, 21], (  # (6*2)+1 and (10*2)+1
            f'per-thread message_count clobbered: {counts}')

    def test_no_false_prefix_mutation_across_threads(self):
        """The concrete production symptom: thread B's larger prefix compared
        against thread A's baseline used to log PREFIX MUTATION. With
        per-thread keying, each thread only ever compares against its own
        prior round, so growth is clean (no break)."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        conv = 'shared-conv-2'
        breaks = {}

        def _worker(name, n_msgs):
            msgs = [{'role': 'system', 'content': 'sys'},
                    {'role': 'user', 'content': f'{name} hello'}]
            detect_cache_break(conv, msgs, None, 'model-a',
                               usage={'cache_read_tokens': 1000}, user_id=1)
            b = 0
            for i in range(n_msgs):
                msgs.append({'role': 'assistant', 'content': '',
                             'tool_calls': [{'function': {'name': 'read_files',
                                                          'arguments': '{}'}}]})
                msgs.append({'role': 'tool', 'content': f'{name} result {i}'})
                r = detect_cache_break(conv, msgs, None, 'model-a',
                                       usage={'cache_read_tokens': 1000 + 100 * i}, user_id=1)
                if r and 'prefix_mutation' in r:
                    b += 1
            breaks[name] = b

        t1 = threading.Thread(target=_worker, args=('A', 5))
        t2 = threading.Thread(target=_worker, args=('B', 9))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert breaks == {'A': 0, 'B': 0}, (
            f'cross-thread false prefix_mutation detected: {breaks} — '
            'concurrent loops under one conv are clobbering the baseline')


# ═══════════════════════════════════════════════════════════════════════════════
#  0b. Pre-first-call compaction must bridge the task-thread boundary

class TestPreCallCompactionBoundary:
    """Compaction may run before a new task thread's first provider call.

    The previous turn still has a warm sibling ``CacheState``, but the new
    thread does not get its own state until ``detect_cache_break`` normally
    runs after the provider response. A compaction notification in that gap
    must materialize the new thread's guard; otherwise its expected read drop
    is misclassified as ``turn_boundary_rebill`` against the sibling baseline.
    """

    @staticmethod
    def _messages():
        return [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'compacted continuation'},
        ]

    def test_notify_bridges_empty_current_thread_state(self):
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        from lib.tasks_pkg.cache_tracking._roi import notify_compaction
        from lib.tasks_pkg.cache_tracking._state import (
            CacheState,
            _cache_states,
            _state_key,
        )

        conv = 'pre-call-compact-boundary'
        current_key = _state_key(conv, user_id=1)
        sibling = CacheState()
        sibling.call_count = 4
        sibling.last_cache_read_tokens = 204_800
        sibling.last_update_time = time.time()
        sibling_key = (1, conv, current_key[2] + 10_000_000)
        _cache_states[sibling_key] = sibling
        assert current_key not in _cache_states

        notify_compaction(conv, user_id=1)
        current = _cache_states.get(current_key)
        assert current is not None and current.compaction_pending, (
            'a pre-call compaction must leave a guard on the new task thread')

        result = detect_cache_break(
            conv, self._messages(), None, 'claude-opus-4',
            usage={'cache_read_input_tokens': 0,
                   'cache_creation_input_tokens': 9_700},
            user_id=1, durable=False)
        assert result is None, (
            f'the expected post-compaction drop was falsely classified: {result}')
        assert current.total_breaks == 0
        assert current.compaction_pending is False

    def test_notify_existing_state_keeps_o1_path(self, monkeypatch):
        import lib.tasks_pkg.cache_tracking._roi as roi
        from lib.tasks_pkg.cache_tracking._state import (
            CacheState,
            _cache_states,
            _state_key,
        )

        conv = 'existing-state-compact-o1'
        state = CacheState()
        state.call_count = 3
        _cache_states[_state_key(conv, user_id=1)] = state

        def _unexpected_cross_thread_probe(*_args, **_kwargs):
            raise AssertionError(
                'existing-state compaction must not scan/persist a baseline')

        monkeypatch.setattr(
            roi, 'get_prev_turn_cache_read', _unexpected_cross_thread_probe)
        roi.notify_compaction(conv, user_id=1)
        assert state.compaction_pending is True

    def test_steady_round_skips_cross_thread_baseline_scan(self, monkeypatch):
        import lib.tasks_pkg.cache_tracking._detect as detect_module
        from lib.tasks_pkg.cache_tracking._state import (
            CacheState,
            _cache_states,
            _state_key,
        )

        conv = 'steady-round-no-sibling-scan'
        state = CacheState()
        state.call_count = 2
        state.last_cache_read_tokens = 50_000
        state.last_update_time = time.time()
        _cache_states[_state_key(conv, user_id=1)] = state

        def _unexpected_cross_thread_probe(*_args, **_kwargs):
            raise AssertionError(
                'steady rounds must not scan/persist a round-1 baseline')

        monkeypatch.setattr(
            detect_module, 'get_prev_turn_cache_read',
            _unexpected_cross_thread_probe)
        result = detect_module.detect_cache_break(
            conv, self._messages(), None, 'claude-opus-4',
            usage={'cache_read_input_tokens': 50_000,
                   'cache_creation_input_tokens': 0},
            user_id=1, durable=False)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
#  1. last_update_time TTL detection fix
# ═══════════════════════════════════════════════════════════════════════════════

class TestTTLDetectionFix:
    """The old code set last_update_time = now BEFORE computing elapsed,
    making elapsed always 0 and the >5min TTL check dead code."""

    def test_short_gap_detected_as_server_side(self):
        """Cache drop within 5 minutes → 'likely server-side'."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Round 1: establish baseline
        detect_cache_break('ttl-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)

        # Round 2: immediate cache drop (within seconds)
        result = detect_cache_break('ttl-1', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000}, user_id=1)
        assert result is not None
        assert 'server_side' in result
        assert '<5min gap' in result['server_side'] or 'server-side' in result['server_side']

    def test_long_gap_detected_as_ttl_expiry(self):
        """Cache drop after >5 minutes → 'possible TTL expiry'."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Round 1: establish baseline
        detect_cache_break('ttl-2', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)

        # ★ Simulate 6-minute gap by backdating last_update_time
        state = _cache_states[_state_key('ttl-2', user_id=1)]
        state.last_update_time = time.time() - 400  # 6min 40s ago

        # Round 2: cache drop after long gap
        result = detect_cache_break('ttl-2', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000}, user_id=1)
        assert result is not None
        assert 'server_side' in result
        assert 'TTL expiry' in result['server_side']
        assert '>5min gap' in result['server_side']

    def test_elapsed_is_nonzero_for_normal_rounds(self):
        """Verify elapsed is computed correctly (not always 0)."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        detect_cache_break('ttl-3', msgs, None, 'model-a',
                           usage={'cache_read_tokens': 10000}, user_id=1)

        # Backdate by 10 seconds
        _cache_states[_state_key('ttl-3', user_id=1)].last_update_time = time.time() - 10

        # Drop cache — should report ~10s gap in the log (not 0)
        result = detect_cache_break('ttl-3', msgs, None, 'model-a',
                                    usage={'cache_read_tokens': 1000}, user_id=1)
        assert result is not None
        # Verify state was updated to now
        assert abs(_cache_states[_state_key('ttl-3', user_id=1)].last_update_time - time.time()) < 2


# ═══════════════════════════════════════════════════════════════════════════════
#  1a2. api_break cause disambiguation — single confident reason, not 3-way "or"
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiBreakCauseDisambiguation:
    """The old fallback string listed all three candidates at once
    ('breakpoint advancement, server-side eviction, OR a silent prefix byte
    change'), which is effectively no answer. detect_cache_break now narrows
    to ONE confident cause using two facts it already holds: whether the
    prefix bytes mutated, and whether a substantial cache_read remains."""

    def test_substantial_read_remaining_blames_stochastic_server_miss(self):
        """A DROP that still leaves a big read (static prefix cached) → a
        stochastic server-side cache miss, NOT a silent byte change and NOT
        the old misleading 'breakpoint advancement' wording (disproven
        2026-06-23 by identical-prompt live replay)."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('ba-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 155441}, user_id=1)
        # Round 2: read drops 155441 → 55535 (the static prefix floor remains).
        r2 = detect_cache_break('ba-1', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 55535}, user_id=1)
        assert r2 is not None
        cause = r2['server_side']
        # No wire fingerprint was supplied → the server-side verdict must be
        # honestly marked UNPROVEN (reached by elimination, not proof). The
        # 2026-07 wire-fingerprint upgrade forbids an unproven 'server-side'
        # claim from masquerading as fact.
        assert 'server-side' in cause
        assert 'UNPROVEN' in cause
        # The discredited 'breakpoint advancement' label must be gone.
        assert 'breakpoint advancement' not in cause
        # Silent byte change must be excluded (prefix did not mutate).
        assert 'silent prefix byte change' not in cause
        # Static prefix still cached is conveyed.
        assert 'static prefix still cached' in cause

    def test_low_read_remaining_drops_silent_byte_change(self):
        """When the prefix bytes did NOT mutate, the cause string must not
        offer 'a silent prefix byte change' even if the read fell to near 0."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('ba-2', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)
        # Round 2: read collapses to below the floor; prefix bytes unchanged.
        r2 = detect_cache_break('ba-2', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 1000}, user_id=1)
        assert r2 is not None
        cause = r2['server_side']
        assert 'silent prefix byte change' not in cause
        # No wire fingerprint → honestly-hedged, UNPROVEN server/TTL wording.
        assert 'UNPROVEN' in cause
        assert 'server-side' in cause


# ═══════════════════════════════════════════════════════════════════════════════
#  1a2. Wire-fingerprint traceability (2026-07) — PROVE server-side vs. name culprit
# ═══════════════════════════════════════════════════════════════════════════════

class TestWireFingerprintVerdict:
    """The core upgrade: detect_cache_break no longer reaches its miss verdict
    by ELIMINATION. When usage carries the authoritative post-translation wire
    fingerprint (`_wire_fp`):
      * fingerprint identical to last round + read drop → the miss is NOT a
        client change; it is named an 'upstream cache eviction' (shared-pool
        LRU on one key), explicitly NOT a random server fault.
      * fingerprint differs → names the exact client-caused culprit.
    """

    def _fp(self, msgs):
        from lib.tasks_pkg.wire_fingerprint import (
            canonical_messages, static_prefix_hash,
        )
        return canonical_messages(msgs), static_prefix_hash(msgs)

    def test_identical_wire_proves_server_side(self):
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        msgs = [{'role': 'system', 'content': 'sys'}]
        wire = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        fp, st = self._fp(wire)
        detect_cache_break('wire-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 90000,
                                  '_wire_fp': fp, '_wire_static': st}, user_id=1)
        # Round 2: read DROPS but the wire bytes are IDENTICAL → THIS round is
        # not a client-side prefix change. The verdict names an upstream cache
        # MISS as a possibility WITHOUT over-claiming a single confident cause
        # (no 'eviction PROVEN', no 'NOT a random server failure' assertion),
        # and points to the client-side branches as the systemic dominant class.
        r2 = detect_cache_break('wire-1', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 40000,
                                       '_wire_fp': fp, '_wire_static': st}, user_id=1)
        assert r2 is not None
        cause = r2['server_side']
        assert 'byte-identical' in cause
        assert 'NOT a client-side prefix change' in cause
        assert 'upstream cache miss' in cause
        # Must NOT over-claim: no confident eviction verdict, no 'not a random
        # server failure' assertion, no PROVEN/UNPROVEN label.
        assert 'upstream cache eviction' not in cause
        assert 'NOT a random server failure' not in cause
        assert 'PROVEN' not in cause
        assert 'UNPROVEN' not in cause

    def test_changed_wire_names_culprit_not_server_side(self):
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        msgs = [{'role': 'system', 'content': 'sys'}]
        wire1 = [{'role': 'system', 'content': 'sys'},
                 {'role': 'user', 'content': 'hello'},
                 {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ORIGINAL'}]
        # Round 2: the c1 tool result's bytes were mutated in the prefix.
        wire2 = [{'role': 'system', 'content': 'sys'},
                 {'role': 'user', 'content': 'hello'},
                 {'role': 'tool', 'tool_call_id': 'c1', 'content': 'MUTATED!!'},
                 {'role': 'user', 'content': 'next'}]
        fp1, st1 = self._fp(wire1)
        fp2, st2 = self._fp(wire2)
        detect_cache_break('wire-2', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 90000,
                                  'cache_creation_input_tokens': 50000,
                                  '_wire_fp': fp1, '_wire_static': st1}, user_id=1)
        r2 = detect_cache_break('wire-2', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 40000,
                                       'cache_creation_input_tokens': 50000,
                                       '_wire_fp': fp2, '_wire_static': st2}, user_id=1)
        assert r2 is not None
        # A real prefix mutation → surfaced under prefix_mutation, NOT server_side.
        assert 'prefix_mutation' in r2
        assert 'server_side' not in r2
        # The culprit names the exact changed tool_result.
        assert 'tool_result' in r2['prefix_mutation']

    def test_benign_wrapping_flip_not_flagged(self):
        """NC-adjacent: a message whose content flips str ↔ [{type:text}]
        between rounds (moving cache marker) must NOT be flagged as a client
        prefix mutation — the server prefix-matches tokenized content, and the
        canonicaliser erases the wrapping. The resulting byte-identical read
        drop is then named an upstream eviction, not a client culprit. This is
        the exact false-positive the reconstruction hash and the old len-2
        probe were blind to."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        msgs = [{'role': 'system', 'content': 'sys'}]
        # Round 1: tail tool result WRAPPED (marker landed on it).
        wire1 = [{'role': 'system', 'content': 'sys'},
                 {'role': 'user', 'content': 'hi'},
                 {'role': 'tool', 'tool_call_id': 'c1',
                  'content': [{'type': 'text', 'text': 'RESULT',
                               'cache_control': {'type': 'ephemeral'}}]}]
        # Round 2: same message UNWRAPPED to a bare string (marker moved off).
        wire2 = [{'role': 'system', 'content': 'sys'},
                 {'role': 'user', 'content': 'hi'},
                 {'role': 'tool', 'tool_call_id': 'c1', 'content': 'RESULT'},
                 {'role': 'user', 'content': 'next'}]
        fp1, st1 = self._fp(wire1)
        fp2, st2 = self._fp(wire2)
        detect_cache_break('wire-3', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 90000,
                                  'cache_creation_input_tokens': 50000,
                                  '_wire_fp': fp1, '_wire_static': st1}, user_id=1)
        r2 = detect_cache_break('wire-3', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 40000,
                                       'cache_creation_input_tokens': 50000,
                                       '_wire_fp': fp2, '_wire_static': st2}, user_id=1)
        assert r2 is not None
        # The wrapping flip is erased → NOT a prefix mutation → the byte-
        # identical read drop is named an upstream cache MISS possibility, not a
        # client culprit and not an over-claimed eviction verdict.
        assert 'prefix_mutation' not in r2
        assert 'server_side' in r2
        assert 'NOT a client-side prefix change' in r2['server_side']
        assert 'upstream cache miss' in r2['server_side']
        assert 'upstream cache eviction' not in r2['server_side']


# ═══════════════════════════════════════════════════════════════════════════════
#  1b. "Cache written but never read back" (no-reuse) detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoReuseDetection:
    """The motivating bug: two rounds, both ~279k cache_write, zero cache_read.

    The api_break check only fires on a DROP from a prior HIGH read, so it was
    structurally blind to "writes a fresh prefix every round, reads nothing".
    detect_cache_break must now flag this as a no_cache_reuse miss.
    """

    def test_full_write_zero_read_flagged(self):
        """Round 1 writes big, round 2 writes big + reads 0 → no_cache_reuse."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]

        # Round 1: fresh write, no read (first call — establishes prefix).
        r1 = detect_cache_break('nr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 278500,
                                       'cache_read_input_tokens': 0}, user_id=1)
        assert r1 is None  # first call never flags

        # Round 2: another fresh write, still zero read → the costly miss.
        r2 = detect_cache_break('nr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 279200,
                                       'cache_read_input_tokens': 0}, user_id=1)
        assert r2 is not None
        assert 'no_cache_reuse' in r2

    def test_no_reuse_counts_as_break(self):
        """A no-reuse miss increments total_breaks."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('nr-2', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 100000,
                                  'cache_read_input_tokens': 0}, user_id=1)
        detect_cache_break('nr-2', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 100000,
                                  'cache_read_input_tokens': 0}, user_id=1)
        assert _cache_states[_state_key('nr-2', user_id=1)].total_breaks == 1

    def test_healthy_reuse_not_flagged(self):
        """Round 2 reads back the prefix → no break."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('nr-3', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 200000,
                                  'cache_read_input_tokens': 0}, user_id=1)
        # Round 2 actually reuses the cache.
        r2 = detect_cache_break('nr-3', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1000,
                                       'cache_read_input_tokens': 200000}, user_id=1)
        assert r2 is None

    def test_small_write_not_flagged(self):
        """Tiny prompts (write < threshold) must NOT trigger the alert."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hi'}]
        detect_cache_break('nr-4', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 1500,
                                  'cache_read_input_tokens': 0}, user_id=1)
        r2 = detect_cache_break('nr-4', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1500,
                                       'cache_read_input_tokens': 0}, user_id=1)
        assert r2 is None  # below _MIN_NO_REUSE_TOKENS

    def test_pinned_read_repeated_write_flagged(self):
        """Read stays PINNED while a big write repeats → flagged.

        Real-world pattern (conv mqo09t2g): R1 w=138k r=55k, R2 w=141k r=55k.
        cache_read never drops (api_break blind) and never hits 0 (no_reuse
        blind), yet the conversation body is re-billed uncached every round.
        detect_cache_break must now flag this as a no_cache_reuse miss.
        """
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        r1 = detect_cache_break('pnr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 138694,
                                       'cache_read_input_tokens': 55728}, user_id=1)
        assert r1 is None  # first call never flags
        r2 = detect_cache_break('pnr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 141668,
                                       'cache_read_input_tokens': 55728}, user_id=1)
        assert r2 is not None
        assert 'no_cache_reuse' in r2

    def test_pinned_read_then_healthy_growth_not_flagged(self):
        """When cache_read GROWS to absorb the prior write → no break."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('pnr-2', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 138694,
                                  'cache_read_input_tokens': 55728}, user_id=1)
        # Round 2 reads back the previously-written prefix (read grows).
        r2 = detect_cache_break('pnr-2', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 3469,
                                       'cache_read_input_tokens': 194422}, user_id=1)
        assert r2 is None

    def test_no_reuse_after_compaction_not_flagged(self):
        """A cache_read=0 + big-write round right after compaction is expected."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        from lib.tasks_pkg.cache_tracking._roi import notify_compaction

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('nr-5', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 200000,
                                  'cache_read_input_tokens': 0}, user_id=1)
        # Compaction rebuilds the prefix → next round legitimately writes fresh.
        notify_compaction('nr-5', user_id=1)
        r2 = detect_cache_break('nr-5', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 150000,
                                       'cache_read_input_tokens': 0}, user_id=1)
        assert r2 is None


# ═══════════════════════════════════════════════════════════════════════════════
#  1c. Silent prefix-byte mutation surfacing
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefixMutationDetection:
    """The investigated bug: two consecutive turns full cache_write, no error.

    A non-idempotent history edit (the re-truncation bug in
    a history rebuild rewrote bytes inside the cached prompt prefix
    between turns. detect_cache_break already HASHED the prefix and logged
    'PREFIX MUTATION DETECTED', but never RETURNED it — so the round carried
    no cacheBreak and the cost popover showed a big write with no cause.
    It must now surface as a 'prefix_mutation' break the frontend can label.
    """

    def _msgs(self, tail_text):
        # 4 messages: system + 2 prefix msgs (hashed: msg_count-2 = 2) + tail.
        return [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'turn 1 question'},
            {'role': 'assistant', 'content': 'turn 1 answer'},
            {'role': 'user', 'content': tail_text},
        ]

    def test_prefix_byte_change_surfaced(self):
        """Round 2 mutates a prefix message + writes big → prefix_mutation."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('tail A')
        r1 = detect_cache_break('pm-1', m1, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 88000,
                                       'cache_read_input_tokens': 51000}, user_id=1)
        assert r1 is None  # first call never flags

        # Round 2: silently rewrite a PREFIX message (index 1), big write.
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'turn 1 question [EDITED non-idempotently]'
        r2 = detect_cache_break('pm-1', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000}, user_id=1)
        assert r2 is not None
        assert 'prefix_mutation' in r2
        assert 'prefix' in r2['prefix_mutation'].lower()

    def test_prefix_mutation_counts_as_break(self):
        from lib.tasks_pkg.cache_tracking._state import _cache_states
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('tail A')
        detect_cache_break('pm-2', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'mutated prefix'
        detect_cache_break('pm-2', m2, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 89000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        from lib.tasks_pkg.cache_tracking._state import _state_key as _sk_pm
        assert _cache_states[_sk_pm('pm-2', user_id=1)].total_breaks == 1

    def test_stable_prefix_not_flagged(self):
        """Prefix byte-identical between turns → no prefix_mutation break."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('tail A')
        detect_cache_break('pm-3', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        # Round 2: prefix UNCHANGED, healthy reuse (read grows, tiny write).
        m2 = self._msgs('tail A')
        r2 = detect_cache_break('pm-3', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1200,
                                       'cache_read_input_tokens': 140000}, user_id=1)
        assert r2 is None

    def test_prefix_mutation_after_compaction_not_flagged(self):
        """A prefix change right after compaction is expected, not a break."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
        from lib.tasks_pkg.cache_tracking._roi import notify_compaction

        m1 = self._msgs('tail A')
        detect_cache_break('pm-4', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        notify_compaction('pm-4', user_id=1)
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'compaction rewrote this'
        r2 = detect_cache_break('pm-4', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000}, user_id=1)
        assert r2 is None  # compaction_pending suppresses the flag

    def test_prefix_mutation_wins_over_api_break(self):
        """A round that BOTH mutates the prefix AND drops cache_read must be
        labeled prefix_mutation, NOT the generic server_side 'breakpoint
        advancement'.

        This is the exact mislabel the cost popover showed on memory-CRUD
        turns: the system-prefix memory-count hint changed (real byte
        mutation) on a round whose cache_read also fell, so the old guard
        `prefix_mutation_break and not api_break` suppressed the mutation and
        the round fell through to {'server_side': 'breakpoint advancement…'}.
        Prefix mutation is the more CERTAIN, actionable cause and must win.
        """
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('tail A')
        # Round 1: establish a HIGH read so round 2 can show a real drop.
        detect_cache_break('pm-5', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 10000,
                                  'cache_read_input_tokens': 155000}, user_id=1)
        # Round 2: mutate a prefix message AND drop cache_read (api_break)
        # while still writing a real prefix.
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'turn 1 question [count hint changed: N→N+1]'
        r2 = detect_cache_break('pm-5', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 60000,
                                       'cache_read_input_tokens': 55000}, user_id=1)
        assert r2 is not None
        # The actionable cause wins; the misleading server_side label is gone.
        assert 'prefix_mutation' in r2
        assert 'server_side' not in r2

    def test_client_change_still_wins_over_prefix_mutation(self):
        """A concrete client-side change (tools/system/model) is differently
        named and the popover labels it on its own — it must still win over
        the prefix_mutation key even when the prefix also mutated."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('tail A')
        tools_v1 = [{'function': {'name': 'grep_search', 'description': 'v1'}}]
        detect_cache_break('pm-6', m1, tools_v1, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 10000,
                                  'cache_read_input_tokens': 80000}, user_id=1)
        # Round 2: prefix mutates AND the tool definitions change.
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'mutated prefix bytes'
        tools_v2 = [{'function': {'name': 'grep_search', 'description': 'v2 CHANGED'}}]
        r2 = detect_cache_break('pm-6', m2, tools_v2, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 60000,
                                       'cache_read_input_tokens': 30000}, user_id=1)
        assert r2 is not None
        # client_changes carries the concrete 'tools' key; prefix_mutation defers.
        assert 'tools' in r2
        assert 'prefix_mutation' not in r2

    def test_stable_wire_tools_reuse_detailed_hashes(self, monkeypatch):
        """Stable final tools bytes must not reserialize every source schema.

        A changed provider-bound digest still refreshes both source hash tables
        and preserves exact per-tool attribution. Malformed evidence takes the
        conservative fallback instead of suppressing source comparison.
        """
        import lib.tasks_pkg.cache_tracking._detect as detect_module
        from lib.tasks_pkg.wire_fingerprint import (
            canonical_messages,
            wire_byte_region,
        )

        original_hash_tools = detect_module._hash_tools
        original_hash_tools_per_tool = detect_module._hash_tools_per_tool
        calls = {'aggregate': 0, 'detail': 0}

        def _count_hash_tools(tools):
            calls['aggregate'] += 1
            return original_hash_tools(tools)

        def _count_hash_tools_per_tool(tools):
            calls['detail'] += 1
            return original_hash_tools_per_tool(tools)

        monkeypatch.setattr(detect_module, '_hash_tools', _count_hash_tools)
        monkeypatch.setattr(
            detect_module, '_hash_tools_per_tool', _count_hash_tools_per_tool)

        messages = self._msgs('tail')
        tools_v1 = [{
            'function': {
                'name': 'grep_search',
                'description': 'v1',
                'parameters': {'type': 'object'},
            },
        }]
        tools_v2 = [{
            'function': {
                'name': 'grep_search',
                'description': 'v2 changed',
                'parameters': {'type': 'object'},
            },
        }]

        def _usage(tools, *, cache_read, cache_write):
            return {
                'cache_read_input_tokens': cache_read,
                'cache_creation_input_tokens': cache_write,
                '_wire_fp': canonical_messages(messages),
                '_wire_region': wire_byte_region(None, tools),
            }

        detect_module.detect_cache_break(
            'tool-wire-reuse', messages, tools_v1, 'claude-opus-4',
            usage=_usage(tools_v1, cache_read=80000, cache_write=10000),
            user_id=1,
        )
        assert calls == {'aggregate': 1, 'detail': 1}

        stable = detect_module.detect_cache_break(
            'tool-wire-reuse', messages, tools_v1, 'claude-opus-4',
            usage=_usage(tools_v1, cache_read=140000, cache_write=1200),
            user_id=1,
        )
        assert stable is None
        assert calls == {'aggregate': 1, 'detail': 1}

        changed = detect_module.detect_cache_break(
            'tool-wire-reuse', messages, tools_v2, 'claude-opus-4',
            usage=_usage(tools_v2, cache_read=30000, cache_write=60000),
            user_id=1,
        )
        assert changed is not None
        assert '~grep_search' in changed['tools']
        assert calls == {'aggregate': 2, 'detail': 2}

        malformed_usage = _usage(
            tools_v2, cache_read=150000, cache_write=1000)
        malformed_usage['_wire_region'] = {'tools': 'not-a-valid-digest'}
        detect_module.detect_cache_break(
            'tool-wire-reuse', messages, tools_v2, 'claude-opus-4',
            usage=malformed_usage,
            user_id=1,
        )
        assert calls == {'aggregate': 3, 'detail': 3}


# ═══════════════════════════════════════════════════════════════════════════════
#  2. Concurrent conversation tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrentConversationTracking:
    """When multiple active conversations share the same model,
    cache contention is a likely cause of unexplained evictions."""

    def test_no_contention_single_conversation(self):
        """Single conversation — no contention detected."""
        from lib.tasks_pkg.cache_tracking._ttl import _count_active_on_model
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('solo-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)

        count = _count_active_on_model('claude-opus-4', exclude_conv='solo-1')
        assert count == 0

    def test_contention_detected_with_concurrent_conversations(self):
        """Two conversations on the same model → contention detected."""
        from lib.tasks_pkg.cache_tracking._ttl import _count_active_on_model
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Conversation A on opus
        detect_cache_break('conv-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)
        # Conversation B on opus
        detect_cache_break('conv-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000}, user_id=1)

        count = _count_active_on_model('claude-opus-4', exclude_conv='conv-a')
        assert count == 1  # conv-b is active on same model

    def test_no_contention_different_models(self):
        """Different models don't cause contention."""
        from lib.tasks_pkg.cache_tracking._ttl import _count_active_on_model
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        detect_cache_break('diff-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)
        detect_cache_break('diff-b', msgs, None, 'claude-sonnet-4',
                           usage={'cache_read_tokens': 30000}, user_id=1)

        count = _count_active_on_model('claude-opus-4', exclude_conv='diff-a')
        assert count == 0

    def test_stale_conversation_not_counted(self):
        """Conversations inactive for >60s are not counted."""
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
        )
        from lib.tasks_pkg.cache_tracking._ttl import _count_active_on_model
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('stale-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)
        detect_cache_break('stale-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000}, user_id=1)

        # Backdate stale-b to 2 minutes ago
        _cache_states[_state_key('stale-b', user_id=1)].last_update_time = time.time() - 120

        count = _count_active_on_model('claude-opus-4', exclude_conv='stale-a')
        assert count == 0  # stale-b is too old

    def test_unexplained_drop_reason_no_contention(self):
        """When cache drops without client changes, reason does NOT mention contention.

        A/B tested 2026-04-10: cache contention between different conversations
        does NOT exist on Anthropic. Cache is keyed on exact prefix bytes;
        different conversations cannot evict each other.
        """
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Establish two conversations on the same model
        detect_cache_break('race-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000}, user_id=1)
        detect_cache_break('race-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000}, user_id=1)

        # Cache drop on conv-a (unexplained) — should NOT blame contention
        result = detect_cache_break('race-a', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000}, user_id=1)
        assert result is not None
        assert 'server_side' in result
        assert 'contention' not in result['server_side']
        # Should mention the real possible cause (server miss / TTL), honestly
        # marked UNPROVEN since no wire fingerprint was captured.
        assert ('server-side' in result['server_side']
                or 'TTL' in result['server_side'])
        assert 'UNPROVEN' in result['server_side']


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Per-round cache stats logging
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundCacheStatsLogging:
    """log_round_cache_stats should log at INFO level for production visibility."""

    def test_logs_with_cache_activity(self, caplog):
        """Cache stats are logged when there's cache activity."""
        import logging
        from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats

        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.cache_tracking'):
            log_round_cache_stats(
                'test-conv', 0,
                {'prompt_tokens': 100, 'cache_write_tokens': 5000, 'cache_read_tokens': 15000},
                model='claude-opus-4', tid='task-123',
            )

        assert '[CacheStats]' in caplog.text
        assert 'cache_w=5000' in caplog.text
        assert 'cache_r=15000' in caplog.text
        assert 'hit=75%' in caplog.text

    def test_no_log_without_cache_activity(self, caplog):
        """No log when there's no cache activity."""
        import logging
        from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats

        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.cache_tracking'):
            log_round_cache_stats(
                'test-conv', 0,
                {'prompt_tokens': 100},
                model='gpt-4o', tid='task-456',
            )

        assert '[CacheStats]' not in caplog.text

    def test_no_log_without_usage(self, caplog):
        """No log when usage is None."""
        import logging
        from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats

        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.cache_tracking'):
            log_round_cache_stats('test-conv', 0, None, model='gpt-4o')

        assert '[CacheStats]' not in caplog.text

    def test_anthropic_key_names(self, caplog):
        """Works with Anthropic-style key names."""
        import logging
        from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats

        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.cache_tracking'):
            log_round_cache_stats(
                'test-conv', 2,
                {
                    'input_tokens': 50,
                    'cache_creation_input_tokens': 3000,
                    'cache_read_input_tokens': 12000,
                },
                model='claude-sonnet-4', tid='task-789',
            )

        assert 'cache_w=3000' in caplog.text
        assert 'cache_r=12000' in caplog.text
        assert 'R3' in caplog.text  # round_num + 1


# ═══════════════════════════════════════════════════════════════════════════════
#  4. Session-stable TTL latch
# ═══════════════════════════════════════════════════════════════════════════════

class TestTTLLatch:
    """TTL latch prevents mid-session cache key changes."""

    def test_latch_captures_initial_value(self):
        """First call latches the current CACHE_EXTENDED_TTL value."""
        from lib.tasks_pkg.cache_tracking._ttl import latch_extended_ttl

        _lib.CACHE_EXTENDED_TTL = True
        assert latch_extended_ttl('task-latch-1') is True

    def test_latch_persists_after_setting_change(self):
        """Once latched, changing CACHE_EXTENDED_TTL doesn't affect the task."""
        from lib.tasks_pkg.cache_tracking._ttl import latch_extended_ttl

        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-latch-2')

        # Change setting mid-session
        _lib.CACHE_EXTENDED_TTL = False

        # Latched value should still be True
        assert latch_extended_ttl('task-latch-2') is True

    def test_different_tasks_get_independent_latches(self):
        """Different tasks can have different latched values."""
        from lib.tasks_pkg.cache_tracking._ttl import latch_extended_ttl

        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-a')

        _lib.CACHE_EXTENDED_TTL = False
        latch_extended_ttl('task-b')

        assert latch_extended_ttl('task-a') is True
        assert latch_extended_ttl('task-b') is False

    def test_release_latch_cleans_up(self):
        """release_ttl_latch removes the latch (memory cleanup)."""
        from lib.tasks_pkg.cache_tracking._ttl import (
            _ttl_latch,
            latch_extended_ttl,
            release_ttl_latch,
        )

        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-release')
        assert 'task-release' in _ttl_latch

        release_ttl_latch('task-release')
        assert 'task-release' not in _ttl_latch

    def test_latch_used_in_add_cache_breakpoints(self):
        """add_cache_breakpoints uses latched value instead of live setting."""
        from lib.llm import add_cache_breakpoints
        from lib.tasks_pkg.cache_tracking._ttl import latch_extended_ttl

        # Latch with extended TTL ON
        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-bp-latch')

        # Now change setting to OFF
        _lib.CACHE_EXTENDED_TTL = False

        # Build body with _task_id
        body = {
            'model': 'claude-sonnet-4-20250514',
            '_task_id': 'task-bp-latch',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'hello'},
            ],
        }
        add_cache_breakpoints(body)

        # Should use latched TTL (True) → BP1 should have ttl='1h'
        sys_msg = body['messages'][0]
        content = sys_msg.get('content', '')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and 'cache_control' in block:
                    assert block['cache_control'].get('ttl') == '1h'
                    break
            else:
                pytest.fail('No cache_control found on system message')

    def test_no_task_id_uses_live_setting(self):
        """Without _task_id, add_cache_breakpoints uses live CACHE_EXTENDED_TTL."""
        from lib.llm import add_cache_breakpoints

        _lib.CACHE_EXTENDED_TTL = False

        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'hello'},
            ],
        }
        add_cache_breakpoints(body)

        # Should use live TTL (False) → BP1 should NOT have ttl='1h'
        sys_msg = body['messages'][0]
        content = sys_msg.get('content', '')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and 'cache_control' in block:
                    assert 'ttl' not in block['cache_control']
                    break


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Cache-aware tool result ordering
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolResultOrdering:
    """The retired sorter preserves historical result order."""

    def test_preserves_consecutive_tool_results(self):
        from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'tc_c', 'function': {'name': 'tool_c'}},
                {'id': 'tc_a', 'function': {'name': 'tool_a'}},
                {'id': 'tc_b', 'function': {'name': 'tool_b'}},
            ]},
            {'role': 'tool', 'tool_call_id': 'tc_c', 'content': 'result c'},
            {'role': 'tool', 'tool_call_id': 'tc_a', 'content': 'result a'},
            {'role': 'tool', 'tool_call_id': 'tc_b', 'content': 'result b'},
        ]

        sort_tool_results(messages)

        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        assert [m['tool_call_id'] for m in tool_msgs] == [
            'tc_c', 'tc_a', 'tc_b']

    def test_preserves_non_tool_messages(self):
        """Non-tool messages are not affected by sorting."""
        from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'thinking...'},
            {'role': 'user', 'content': 'go on'},
        ]

        original = [m.copy() for m in messages]
        sort_tool_results(messages)

        assert messages == original

    def test_handles_multiple_tool_runs_without_reordering(self):
        from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'content': '', 'tool_calls': []},
            # First batch (out of order)
            {'role': 'tool', 'tool_call_id': 'tc_2', 'content': 'r2'},
            {'role': 'tool', 'tool_call_id': 'tc_1', 'content': 'r1'},
            # Intervening assistant + tool_calls
            {'role': 'assistant', 'content': '', 'tool_calls': []},
            # Second batch (out of order)
            {'role': 'tool', 'tool_call_id': 'tc_4', 'content': 'r4'},
            {'role': 'tool', 'tool_call_id': 'tc_3', 'content': 'r3'},
        ]

        sort_tool_results(messages)

        assert messages[2]['tool_call_id'] == 'tc_2'
        assert messages[3]['tool_call_id'] == 'tc_1'
        assert messages[5]['tool_call_id'] == 'tc_4'
        assert messages[6]['tool_call_id'] == 'tc_3'

    def test_single_tool_result_unchanged(self):
        """A single tool result (no consecutive run) is not moved."""
        from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'content': ''},
            {'role': 'tool', 'tool_call_id': 'tc_1', 'content': 'r1'},
            {'role': 'assistant', 'content': 'done'},
        ]

        original = [m.copy() for m in messages]
        sort_tool_results(messages)

        assert messages == original

    def test_empty_messages(self):
        """Empty messages list is handled gracefully."""
        from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results

        empty = []
        system_only = [{'role': 'system', 'content': 'sys'}]
        sort_tool_results(empty)
        sort_tool_results(system_only)
        assert empty == []
        assert system_only == [{'role': 'system', 'content': 'sys'}]

    def test_tool_results_without_tool_call_id(self):
        """Tool results without tool_call_id sort by empty string."""
        from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results

        messages = [
            {'role': 'tool', 'content': 'result b'},
            {'role': 'tool', 'tool_call_id': 'tc_a', 'content': 'result a'},
        ]

        # Should not raise
        sort_tool_results(messages)
        # The one without id sorts first (empty string < 'tc_a')
        assert messages[0].get('tool_call_id') is None or messages[0].get('tool_call_id', '') == ''


# ═══════════════════════════════════════════════════════════════════════════════
#  6. cleanup_cache_state
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanupCacheState:
    """cleanup_cache_state removes per-conversation cache tracking."""

    def test_cleanup_removes_state(self):
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
            cleanup_cache_state,
        )
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('cleanup-1', msgs, None, 'model-a',
                           usage={'cache_read_tokens': 5000}, user_id=1)
        assert _state_key('cleanup-1', user_id=1) in _cache_states

        cleanup_cache_state('cleanup-1', user_id=1)
        # cleanup_cache_state drops ALL thread-keyed entries for the conv.
        assert not any(k[0] == 1 and k[1] == 'cleanup-1'
                       for k in _cache_states)

    def test_cleanup_nonexistent_is_noop(self):
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            cleanup_cache_state,
        )

        before = dict(_cache_states)
        cleanup_cache_state('nonexistent-conv', user_id=1)
        assert _cache_states == before


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Integration: _task_id passthrough in body
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
#  Precise prefix-mutation attribution (point 2: name the exact culprit)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefixCulpritAttribution:
    """detect_cache_break must name the EXACT (message_index, field) that
    changed inside the cache prefix — not collapse it into a guessed
    'stochastic server-side miss OR TTL OR silent edit' string."""

    def _msgs(self, q_text, a_text, tail_text):
        return [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': q_text},
            {'role': 'assistant', 'content': a_text},
            {'role': 'user', 'content': tail_text},
        ]

    def test_fallback_builds_one_field_baseline_per_round(self, monkeypatch):
        """No-wire fallback keeps exact attribution with one history pass."""
        import lib.tasks_pkg.cache_tracking._detect as detect_module

        real_hash_fields = detect_module._hash_prefix_fields
        hash_calls = []

        def _counted_hash(messages, prefix_count):
            hash_calls.append(prefix_count)
            return real_hash_fields(messages, prefix_count)

        monkeypatch.setattr(
            detect_module, '_hash_prefix_fields', _counted_hash)
        first = self._msgs('question', 'answer', 'tail A')
        detect_module.detect_cache_break(
            'cul-one-pass', first, None, 'claude-opus-4',
            usage={'cache_creation_input_tokens': 88_000,
                   'cache_read_input_tokens': 51_000},
            user_id=1,
        )
        second = self._msgs('question [EDITED]', 'answer', 'tail B')
        result = detect_module.detect_cache_break(
            'cul-one-pass', second, None, 'claude-opus-4',
            usage={'cache_creation_input_tokens': 89_000,
                   'cache_read_input_tokens': 51_000},
            user_id=1,
        )

        assert len(hash_calls) == 2
        assert result is not None and 'prefix_mutation' in result
        assert 'msg[1].content' in result['prefix_mutation']

    def test_fallback_baseline_is_fixed_width_and_payload_free(self):
        """Retained fallback state is bounded by fields, not payload bytes."""
        import copy

        from lib.tasks_pkg.cache_tracking._hashing import (
            _PREFIX_FIELD_NAMES,
            _diff_prefix_fields,
            _hash_prefix_fields,
        )

        large_text = 'private-payload-' + ('x' * 250_000)
        messages = [{
            'role': 'assistant',
            'content': large_text,
            'reasoning_content': large_text,
            'reasoning_details': [{
                'type': 'reasoning.text',
                'metadata': {'z': 2, 'a': 1},
                'text': large_text,
            }],
            'thinking_signature': 'signature',
            'tool_calls': [{
                'id': 'call-1',
                'function': {
                    'name': 'read_file',
                    'arguments': '{"path":"large.txt"}',
                },
            }],
        }]
        original = copy.deepcopy(messages)

        baseline = _hash_prefix_fields(messages, 1)
        assert messages == original
        assert len(baseline) == 1
        assert isinstance(baseline[0], tuple)
        assert len(baseline[0]) == len(_PREFIX_FIELD_NAMES)
        assert all(value is None or type(value) is int
                   for value in baseline[0])
        assert large_text not in repr(baseline)

        # Canonical key order remains irrelevant for reasoning_details, while
        # a real nested value change retains exact field attribution.
        reordered = copy.deepcopy(messages)
        reordered[0]['reasoning_details'][0]['metadata'] = {'a': 1, 'z': 2}
        assert _hash_prefix_fields(reordered, 1) == baseline
        changed = copy.deepcopy(reordered)
        changed[0]['reasoning_details'][0]['metadata']['z'] = 3
        assert _diff_prefix_fields(
            baseline, _hash_prefix_fields(changed, 1),
        ) == ['msg[0].reasoning_details']

    def test_content_change_named(self):
        """A content edit on prefix msg[1] is reported as msg[1].content."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('turn 1 question', 'turn 1 answer', 'tail A')
        detect_cache_break('cul-1', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        m2 = self._msgs('turn 1 question [EDITED]', 'turn 1 answer', 'tail B')
        r2 = detect_cache_break('cul-1', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000}, user_id=1)
        assert r2 is not None and 'prefix_mutation' in r2
        cause = r2['prefix_mutation']
        assert 'msg[1].content' in cause, cause
        # The guessing-ladder fallback strings must NOT appear.
        assert 'stochastic' not in cause.lower()

    def test_tool_calls_field_distinguished_from_content(self):
        """Mutating a tool_calls argument names msg[i].tool_calls, not content."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        def _m(args, tail):
            return [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'do it'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'id': 'tc1', 'type': 'function',
                                 'function': {'name': 'grep_search',
                                              'arguments': args}}]},
                {'role': 'tool', 'tool_call_id': 'tc1', 'content': 'result'},
                {'role': 'user', 'content': tail},
            ]

        m1 = _m('{"pattern": "foo"}', 'tail A')
        detect_cache_break('cul-2', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        # Same content text, but tool_call arguments mutated → must be named
        # as tool_calls, not content.
        m2 = _m('{"pattern": "bar"}', 'tail B')
        r2 = detect_cache_break('cul-2', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000}, user_id=1)
        assert r2 is not None and 'prefix_mutation' in r2
        cause = r2['prefix_mutation']
        assert 'tool_calls' in cause, cause

    def test_stable_prefix_no_culprit(self):
        """Byte-identical prefix → no break, no fabricated culprit."""
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        m1 = self._msgs('q', 'a', 'tail A')
        detect_cache_break('cul-3', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)
        m2 = self._msgs('q', 'a', 'tail A')
        r2 = detect_cache_break('cul-3', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1200,
                                       'cache_read_input_tokens': 140000}, user_id=1)
        assert r2 is None


# ═══════════════════════════════════════════════════════════════════════════════
#  L1 compaction must NOT mask a real break (point 2: notify only in-prefix)
# ═══════════════════════════════════════════════════════════════════════════════

class TestL1DoesNotMaskRealBreak:
    """run_compaction_pipeline must only raise compaction_pending for
    mutations that touch the cached prefix. A benign out-of-prefix L1
    pass (saved>0, compacted=False) must leave detection ENABLED so a
    co-occurring real break (prefix mutation / read drop) still surfaces."""

    def _msgs(self, q_text, tail_text):
        return [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': q_text},
            {'role': 'assistant', 'content': 'answer'},
            {'role': 'user', 'content': tail_text},
        ]

    def test_pipeline_l1_only_does_not_set_pending(self, monkeypatch):
        """saved>0 with compacted=False / adv_saved=0 must NOT notify."""
        import lib.tasks_pkg.compaction._pipeline as pl
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
            CacheState,
        )

        # Seed a cache state so we can observe compaction_pending.
        st = CacheState()
        st.call_count = 1
        _cache_states[_state_key('l1m-1', user_id=1)] = st

        # L1 reports saved>0; L2/advanced do nothing.
        monkeypatch.setattr(pl, 'micro_compact', lambda *a, **k: 5000)
        monkeypatch.setattr(pl, 'force_compact_if_needed', lambda *a, **k: False)

        task = {'convId': 'l1m-1', '_userId': 1, 'config': {}}
        pl.run_compaction_pipeline(self._msgs('q', 'tail'), 1, task=task)
        assert st.compaction_pending is False, (
            'benign out-of-prefix L1 must not raise compaction_pending')

    def test_pipeline_l2_compaction_does_set_pending(self, monkeypatch):
        """A real L2 force-compact (compacted=True) MUST still notify."""
        import lib.tasks_pkg.compaction._pipeline as pl
        from lib.tasks_pkg.cache_tracking._state import (
            _cache_states,
            _state_key,
            CacheState,
        )

        st = CacheState()
        st.call_count = 1
        _cache_states[_state_key('l1m-2', user_id=1)] = st

        monkeypatch.setattr(pl, 'micro_compact', lambda *a, **k: 0)
        monkeypatch.setattr(pl, 'force_compact_if_needed', lambda *a, **k: True)
        # _reinject runs after compacted=True; make it a no-op.
        monkeypatch.setattr(pl, 'recompose_context_after_compaction',
                            lambda *a, **k: None)

        task = {'convId': 'l1m-2', '_userId': 1, 'config': {}}
        pl.run_compaction_pipeline(self._msgs('q', 'tail'), 1, task=task)
        assert st.compaction_pending is True

    def test_real_break_surfaces_despite_cooccurring_l1(self, monkeypatch):
        """End-to-end: a prefix mutation on the SAME round as a benign L1
        pass must still be reported (not masked)."""
        import lib.tasks_pkg.compaction._pipeline as pl
        from lib.tasks_pkg.cache_tracking._detect import detect_cache_break

        monkeypatch.setattr(pl, 'micro_compact', lambda *a, **k: 5000)
        monkeypatch.setattr(pl, 'force_compact_if_needed', lambda *a, **k: False)
        task = {'convId': 'l1m-3', '_userId': 1, 'config': {}}

        # Round 1: establish prefix.
        m1 = self._msgs('turn 1 question', 'tail A')
        pl.run_compaction_pipeline(m1, 1, task=task)
        detect_cache_break('l1m-3', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000}, user_id=1)

        # Round 2: a benign L1 pass runs AND the prefix is mutated + big write.
        m2 = self._msgs('turn 1 question [EDITED]', 'tail B')
        pl.run_compaction_pipeline(m2, 2, task=task)
        r2 = detect_cache_break('l1m-3', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000}, user_id=1)
        assert r2 is not None and 'prefix_mutation' in r2, (
            'L1 saved>0 wrongly masked the real prefix-mutation break')


# ═══════════════════════════════════════════════════════════════════════════════
#  sort_tool_results must not reorder inside the cache prefix (point 3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSortToolResultsPrefixGate:
    """The compatibility API never reorders any prefix or tail run."""

    def test_prefix_run_not_reordered(self, monkeypatch):
        import lib.tasks_pkg.cache_tracking._prefix as ct

        # Pretend the first 4 messages are inside the cache prefix.
        monkeypatch.setattr(
            ct, 'get_cache_prefix_count',
            lambda cid, current_msg_count=None, *, user_id: 4)
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'tool_calls': [{'id': 'b'}]},
            {'role': 'tool', 'tool_call_id': 'b', 'content': 'B'},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'A'},
            {'role': 'tool', 'tool_call_id': 'z', 'content': 'Z'},
            {'role': 'tool', 'tool_call_id': 'y', 'content': 'Y'},
        ]
        ct.sort_tool_results(messages, conv_id='gate-1', user_id=7)
        # Indices 2,3 are inside the prefix → must stay B,A (unsorted).
        assert messages[2]['tool_call_id'] == 'b'
        assert messages[3]['tool_call_id'] == 'a'

    def test_tail_run_not_sorted(self, monkeypatch):
        import lib.tasks_pkg.cache_tracking._prefix as ct

        # No prefix tracked still preserves author/model result order.
        monkeypatch.setattr(
            ct, 'get_cache_prefix_count',
            lambda cid, current_msg_count=None, *, user_id: 0)
        messages = [
            {'role': 'assistant', 'tool_calls': [{'id': 'z'}]},
            {'role': 'tool', 'tool_call_id': 'z', 'content': 'Z'},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'A'},
        ]
        ct.sort_tool_results(messages, conv_id='gate-2', user_id=7)
        assert messages[1]['tool_call_id'] == 'z'
        assert messages[2]['tool_call_id'] == 'a'

    def test_missing_owner_fails_closed_without_reordering(self):
        import lib.tasks_pkg.cache_tracking._prefix as ct

        messages = [
            {'role': 'tool', 'tool_call_id': 'b', 'content': 'B'},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'A'},
        ]
        ct.sort_tool_results(messages, conv_id='owned-conversation')
        assert [row['tool_call_id'] for row in messages] == ['b', 'a']


class TestTaskIdPassthrough:
    """_task_id is passed through body and cleaned up properly."""

    def test_task_id_stripped_for_non_claude(self):
        """_task_id should not crash the non-Claude early-return path."""
        from lib.llm import add_cache_breakpoints

        body = {
            'model': 'gpt-4o',
            '_task_id': 'task-123',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }
        add_cache_breakpoints(body)
        # For non-Claude, add_cache_breakpoints returns early and leaves the
        # body byte-for-byte unchanged. _task_id is stripped later at the
        # OpenAI serialization boundary in prepare_request.
        assert body == {
            'model': 'gpt-4o',
            '_task_id': 'task-123',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }

    def test_task_id_survives_add_cache_breakpoints_for_claude(self):
        """_task_id must SURVIVE add_cache_breakpoints (read non-destructively).

        Regression guard for the mrne3bqe cache miss: the streaming retry loop
        re-feeds the SAME body to add_cache_breakpoints on every 429/503
        attempt. If _task_id were popped on attempt 1, attempt 2+ would fall
        back to the live global CACHE_EXTENDED_TTL and flip the cache_control
        TTL / beta header mid-task → different cache key → full prefix miss.
        Keeping _task_id on the body is what makes the latch decision stable
        across retries. It is stripped only at the serialization boundary
        (prepare_request), covered by test_task_id_stripped_at_openai_boundary.
        """
        from lib.llm import add_cache_breakpoints

        body = {
            'model': 'claude-sonnet-4-20250514',
            '_task_id': 'task-456',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hi'},
            ],
        }
        add_cache_breakpoints(body)
        assert body.get('_task_id') == 'task-456', (
            'add_cache_breakpoints must NOT pop _task_id — a retry reusing the '
            'same body would then lose the TTL latch and evict the prefix cache')

    def test_task_id_stripped_at_openai_boundary(self):
        """prepare_request removes _task_id on the OpenAI path (raw body is
        serialized to the gateway) — but NOT before add_cache_breakpoints has
        read it, and the read stays non-destructive across attempts."""
        from lib.llm._sse_core import prepare_request

        body = {
            'model': 'gpt-4o',
            '_task_id': 'task-789',
            'messages': [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hi'},
            ],
        }
        plan = prepare_request(body, attempt=0, log_prefix='[t]',
                               base_url='https://gw.example/v1',
                               api_protocol='openai')
        assert '_task_id' not in plan.body, (
            '_task_id must be stripped before the OpenAI body is serialized')



# ═══════════════════════════════════════════════════════════════════════════════
#  Representation invariance — a marker moving off a message must NOT change the
#  message's content BYTES (str↔block flip root cause of early-round pin).
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarkerRepresentationInvariance:
    """Root cause of "wrote-but-can't-read-back" early-round cache pin: when the
    mid-anchor quantum-advances, the previously-anchored tool_result was rebuilt
    from a bare ``str`` (marker wrap removed) → its wire bytes flipped
    str↔[{type:text}]. For a ``tool`` role that flip survives
    openai_body_to_anthropic, so the cached prefix no longer byte-matches and
    the server can't extend the prior entry. Phase 0.5 normalizes every markable
    str content to the single-block form UP FRONT, so anchoring only adds/removes
    the ``cache_control`` key — content bytes are invariant."""

    @staticmethod
    def _strip_cc(content):
        """Content with cache_control removed from every block (what the server
        prefix-matches on and what _msg_bytes compares)."""
        if isinstance(content, list):
            return [{k: v for k, v in b.items() if k != 'cache_control'}
                    if isinstance(b, dict) else b for b in content]
        return content

    def _mk_body(self, n_tool_msgs):
        """A long tool loop: system + user + N (assistant,tool) pairs, big enough
        that the mid-anchor arms and lands on a tool_result."""
        msgs = [{'role': 'system', 'content': 'system prompt ' * 20},
                {'role': 'user', 'content': 'kick off'}]
        for i in range(n_tool_msgs):
            msgs.append({'role': 'assistant', 'content': '',
                         'tool_calls': [{'id': f'c{i}', 'type': 'function',
                                         'function': {'name': 'read_files',
                                                      'arguments': '{}'}}]})
            msgs.append({'role': 'tool', 'tool_call_id': f'c{i}',
                         'content': f'tool result payload {i} ' + ('x ' * 50)})
        return {'model': 'claude-opus-4-20250514', 'messages': msgs}

    def test_unanchored_tool_result_bytes_are_invariant(self):
        """The message the mid-anchor SITS on in round A but MOVES OFF in round B
        must have byte-identical (cache_control-stripped) content in both."""
        from lib.llm import add_cache_breakpoints

        # Round A: 40 tool pairs → mid-anchor lands somewhere mid-history.
        bodyA = self._mk_body(40)
        add_cache_breakpoints(bodyA)
        # Find a mid-history tool_result that carries a marker in round A.
        anchored_idx = None
        for i, m in enumerate(bodyA['messages']):
            if m.get('role') != 'tool':
                continue
            c = m.get('content')
            if isinstance(c, list) and any(
                    isinstance(b, dict) and 'cache_control' in b for b in c):
                anchored_idx = i
                break
        assert anchored_idx is not None, 'no anchored tool_result in round A'
        bytesA = self._strip_cc(bodyA['messages'][anchored_idx]['content'])

        # Round B: 8 more pairs appended → the quantized mid-anchor jumps forward,
        # moving OFF anchored_idx. Rebuild from scratch (as build_body does each
        # round from the persistent list) so the content starts as bare str again.
        bodyB = self._mk_body(48)
        add_cache_breakpoints(bodyB)
        mB = bodyB['messages'][anchored_idx]
        assert mB.get('role') == 'tool'
        # It must NOT carry a marker now (anchor moved) ...
        cB = mB['content']
        has_marker = isinstance(cB, list) and any(
            isinstance(b, dict) and 'cache_control' in b for b in cB)
        assert not has_marker, 'anchor did not move off — test setup invalid'
        # ... yet its content bytes (sans cache_control) are IDENTICAL to round A.
        assert self._strip_cc(cB) == bytesA, (
            'un-anchored tool_result bytes changed between rounds (str↔block '
            'flip regression)')

    def test_plain_assistant_midhistory_content_is_normalized(self):
        """A plain-text assistant turn (model prose, no tool_calls) sitting in
        MID-HISTORY — NOT the tail, NOT where any anchor lands — must still be
        normalized to block form by Phase 0.5. This isolates Phase 0.5 from the
        tail/mid placement branches, which independently wrap str→list when they
        stamp a marker (that wrapping is itself the flip source). Only Phase 0.5
        can turn THIS un-marked assistant message into block form."""
        from lib.llm import add_cache_breakpoints

        msgs = [{'role': 'system', 'content': 'system prompt ' * 20},
                {'role': 'user', 'content': 'kick off'}]
        # An early plain-text assistant turn (index 2) — deep in the prefix,
        # far from the tail and from the quantized mid-anchor's trail zone.
        msgs.append({'role': 'assistant', 'content': 'early prose answer'})
        target_idx = len(msgs) - 1
        for i in range(30):
            msgs.append({'role': 'assistant', 'content': '',
                         'tool_calls': [{'id': f'c{i}', 'type': 'function',
                                         'function': {'name': 'read_files',
                                                      'arguments': '{}'}}]})
            msgs.append({'role': 'tool', 'tool_call_id': f'c{i}',
                         'content': f'result {i} ' + ('x ' * 40)})
        body = {'model': 'claude-opus-4-20250514', 'messages': msgs}
        add_cache_breakpoints(body)
        target = body['messages'][target_idx]
        assert target.get('role') == 'assistant'
        # It carries NO marker (not tail/mid/system) — so only Phase 0.5 could
        # have turned it into block form.
        c = target['content']
        has_marker = isinstance(c, list) and any(
            isinstance(b, dict) and 'cache_control' in b for b in c)
        assert not has_marker, 'target unexpectedly anchored — test setup invalid'
        assert isinstance(c, list), (
            'mid-history plain assistant content not normalized (Phase 0.5 gap)')

    def test_assistant_with_tool_calls_nonempty_content_is_normalized(self):
        """★ THE {content} FLOOR-MISS FIX (commit ab161bf) — updated baseline.

        An assistant turn that carries tool_calls AND non-empty ``str`` content
        (the prose-before-run_command shape) is now NORMALIZED to single-block
        form UP FRONT by Phase 0.5. This is intentional and load-bearing: the
        old carve-out left the ``str`` un-normalized, so the tail phase wrapped
        it into a block the round it was the tail and it reverted to a bare
        ``str`` once buried prefix — a canonical-INVISIBLE ``str``↔block flip on
        ``content`` that re-billed the cached prefix EVERY round (the dominant
        residual floor-miss the field tracer proved as ``…(run_command){content}``).
        Normalizing up front makes anchoring only toggle a ``cache_control`` key
        on an already-list block, so the turn's content bytes are invariant
        whether or not it is the tail. The tool_use blocks still drive
        ``_assistant_blocks``' last-block marker hoist, so the marker logic is
        undisturbed.

        (This test formerly asserted the PRE-fix behaviour — that such content
        stays a bare ``str``. That baseline was superseded by ab161bf; the
        assertion is flipped to the current, correct contract.)"""
        from lib.llm import add_cache_breakpoints

        msgs = [{'role': 'system', 'content': 'sys ' * 20},
                {'role': 'user', 'content': 'go'},
                {'role': 'assistant', 'content': 'thinking out loud',
                 'tool_calls': [{'id': 'c0', 'type': 'function',
                                 'function': {'name': 'read_files',
                                              'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'c0', 'content': 'r'}]
        body = {'model': 'claude-opus-4-20250514', 'messages': msgs}
        add_cache_breakpoints(body)
        am = body['messages'][2]
        assert am.get('tool_calls'), 'tool_calls must be preserved'
        # Non-empty content is normalized to the single-block form so its
        # cache_control-stripped bytes are invariant across rounds.
        assert am['content'] == [{'type': 'text', 'text': 'thinking out loud'}], (
            'assistant-with-tool_calls NON-EMPTY content must be normalized to '
            f'single-block form (the {{content}} floor-miss fix) — got '
            f'{am["content"]!r}')

    def test_assistant_with_tool_calls_empty_content_stays_str(self):
        """The carve-out that SURVIVES the {content} fix: EMPTY assistant content
        is left as a bare ``str`` (never fabricated into a ``[{text:''}]`` block
        the model never wrote). The normalization guard is
        ``isinstance(content, str) and content`` — empty string is falsy, so it
        is skipped. This is the invariant the old test's spirit protected, now
        scoped to the empty case where it is actually correct."""
        from lib.llm import add_cache_breakpoints

        msgs = [{'role': 'system', 'content': 'sys ' * 20},
                {'role': 'user', 'content': 'go'},
                {'role': 'assistant', 'content': '',
                 'tool_calls': [{'id': 'c0', 'type': 'function',
                                 'function': {'name': 'read_files',
                                              'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'c0', 'content': 'r'}]
        body = {'model': 'claude-opus-4-20250514', 'messages': msgs}
        add_cache_breakpoints(body)
        am = body['messages'][2]
        assert am.get('tool_calls'), 'tool_calls must be preserved'
        assert am['content'] == '', (
            'EMPTY assistant-with-tool_calls content must stay a bare str — a '
            f'fabricated empty text block is the carve-out — got {am["content"]!r}')

    def test_markable_tool_content_is_always_block_form(self):
        """After add_cache_breakpoints every non-empty tool/user message is in
        single-block list form regardless of whether it got a marker — so the
        Anthropic translation is stable and _msg_bytes never flips."""
        from lib.llm import add_cache_breakpoints

        body = self._mk_body(30)
        add_cache_breakpoints(body)
        for m in body['messages']:
            if m.get('role') in ('tool', 'user') and m.get('content'):
                assert isinstance(m['content'], list), (
                    f'{m.get("role")} content not normalized to block form: '
                    f'{type(m["content"])}')
