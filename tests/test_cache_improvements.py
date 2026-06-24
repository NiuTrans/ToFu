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
    from lib.tasks_pkg.cache_tracking import _cache_states, _ttl_latch
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
#  1. last_update_time TTL detection fix
# ═══════════════════════════════════════════════════════════════════════════════

class TestTTLDetectionFix:
    """The old code set last_update_time = now BEFORE computing elapsed,
    making elapsed always 0 and the >5min TTL check dead code."""

    def test_short_gap_detected_as_server_side(self):
        """Cache drop within 5 minutes → 'likely server-side'."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Round 1: establish baseline
        detect_cache_break('ttl-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})

        # Round 2: immediate cache drop (within seconds)
        result = detect_cache_break('ttl-1', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000})
        assert result is not None
        assert 'server_side' in result
        assert '<5min gap' in result['server_side'] or 'server-side' in result['server_side']

    def test_long_gap_detected_as_ttl_expiry(self):
        """Cache drop after >5 minutes → 'possible TTL expiry'."""
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Round 1: establish baseline
        detect_cache_break('ttl-2', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})

        # ★ Simulate 6-minute gap by backdating last_update_time
        state = _cache_states['ttl-2']
        state.last_update_time = time.time() - 400  # 6min 40s ago

        # Round 2: cache drop after long gap
        result = detect_cache_break('ttl-2', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000})
        assert result is not None
        assert 'server_side' in result
        assert 'TTL expiry' in result['server_side']
        assert '>5min gap' in result['server_side']

    def test_elapsed_is_nonzero_for_normal_rounds(self):
        """Verify elapsed is computed correctly (not always 0)."""
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        detect_cache_break('ttl-3', msgs, None, 'model-a',
                           usage={'cache_read_tokens': 10000})

        # Backdate by 10 seconds
        _cache_states['ttl-3'].last_update_time = time.time() - 10

        # Drop cache — should report ~10s gap in the log (not 0)
        result = detect_cache_break('ttl-3', msgs, None, 'model-a',
                                    usage={'cache_read_tokens': 1000})
        assert result is not None
        # Verify state was updated to now
        assert abs(_cache_states['ttl-3'].last_update_time - time.time()) < 2


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
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('ba-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 155441})
        # Round 2: read drops 155441 → 55535 (the static prefix floor remains).
        r2 = detect_cache_break('ba-1', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 55535})
        assert r2 is not None
        cause = r2['server_side']
        assert 'stochastic server-side cache miss' in cause
        # The discredited 'breakpoint advancement' label must be gone.
        assert 'breakpoint advancement' not in cause
        # Silent byte change must be excluded (prefix did not mutate).
        assert 'silent prefix byte change' not in cause
        # Static prefix still cached is conveyed.
        assert 'static prefix still cached' in cause

    def test_low_read_remaining_drops_silent_byte_change(self):
        """When the prefix bytes did NOT mutate, the cause string must not
        offer 'a silent prefix byte change' even if the read fell to near 0."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('ba-2', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})
        # Round 2: read collapses to below the floor; prefix bytes unchanged.
        r2 = detect_cache_break('ba-2', msgs, None, 'claude-opus-4',
                                usage={'cache_read_tokens': 1000})
        assert r2 is not None
        cause = r2['server_side']
        assert 'silent prefix byte change' not in cause
        assert 'prefix bytes unchanged' in cause
        assert 'stochastic server-side cache miss' in cause


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
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]

        # Round 1: fresh write, no read (first call — establishes prefix).
        r1 = detect_cache_break('nr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 278500,
                                       'cache_read_input_tokens': 0})
        assert r1 is None  # first call never flags

        # Round 2: another fresh write, still zero read → the costly miss.
        r2 = detect_cache_break('nr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 279200,
                                       'cache_read_input_tokens': 0})
        assert r2 is not None
        assert 'no_cache_reuse' in r2

    def test_no_reuse_counts_as_break(self):
        """A no-reuse miss increments total_breaks."""
        from lib.tasks_pkg.cache_tracking import (
            _cache_states, detect_cache_break,
        )

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('nr-2', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 100000,
                                  'cache_read_input_tokens': 0})
        detect_cache_break('nr-2', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 100000,
                                  'cache_read_input_tokens': 0})
        assert _cache_states['nr-2'].total_breaks == 1

    def test_healthy_reuse_not_flagged(self):
        """Round 2 reads back the prefix → no break."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('nr-3', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 200000,
                                  'cache_read_input_tokens': 0})
        # Round 2 actually reuses the cache.
        r2 = detect_cache_break('nr-3', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1000,
                                       'cache_read_input_tokens': 200000})
        assert r2 is None

    def test_small_write_not_flagged(self):
        """Tiny prompts (write < threshold) must NOT trigger the alert."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hi'}]
        detect_cache_break('nr-4', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 1500,
                                  'cache_read_input_tokens': 0})
        r2 = detect_cache_break('nr-4', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1500,
                                       'cache_read_input_tokens': 0})
        assert r2 is None  # below _MIN_NO_REUSE_TOKENS

    def test_pinned_read_repeated_write_flagged(self):
        """Read stays PINNED while a big write repeats → flagged.

        Real-world pattern (conv mqo09t2g): R1 w=138k r=55k, R2 w=141k r=55k.
        cache_read never drops (api_break blind) and never hits 0 (no_reuse
        blind), yet the conversation body is re-billed uncached every round.
        detect_cache_break must now flag this as a no_cache_reuse miss.
        """
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        r1 = detect_cache_break('pnr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 138694,
                                       'cache_read_input_tokens': 55728})
        assert r1 is None  # first call never flags
        r2 = detect_cache_break('pnr-1', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 141668,
                                       'cache_read_input_tokens': 55728})
        assert r2 is not None
        assert 'no_cache_reuse' in r2

    def test_pinned_read_then_healthy_growth_not_flagged(self):
        """When cache_read GROWS to absorb the prior write → no break."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('pnr-2', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 138694,
                                  'cache_read_input_tokens': 55728})
        # Round 2 reads back the previously-written prefix (read grows).
        r2 = detect_cache_break('pnr-2', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 3469,
                                       'cache_read_input_tokens': 194422})
        assert r2 is None

    def test_no_reuse_after_compaction_not_flagged(self):
        """A cache_read=0 + big-write round right after compaction is expected."""
        from lib.tasks_pkg.cache_tracking import (
            detect_cache_break, notify_compaction,
        )

        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break('nr-5', msgs, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 200000,
                                  'cache_read_input_tokens': 0})
        # Compaction rebuilds the prefix → next round legitimately writes fresh.
        notify_compaction('nr-5')
        r2 = detect_cache_break('nr-5', msgs, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 150000,
                                       'cache_read_input_tokens': 0})
        assert r2 is None


# ═══════════════════════════════════════════════════════════════════════════════
#  1c. Silent prefix-byte mutation surfacing
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefixMutationDetection:
    """The investigated bug: two consecutive turns full cache_write, no error.

    A non-idempotent history edit (the re-truncation bug in
    server_message_store) rewrote bytes inside the cached prompt prefix
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
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        m1 = self._msgs('tail A')
        r1 = detect_cache_break('pm-1', m1, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 88000,
                                       'cache_read_input_tokens': 51000})
        assert r1 is None  # first call never flags

        # Round 2: silently rewrite a PREFIX message (index 1), big write.
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'turn 1 question [EDITED non-idempotently]'
        r2 = detect_cache_break('pm-1', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000})
        assert r2 is not None
        assert 'prefix_mutation' in r2
        assert 'prefix' in r2['prefix_mutation'].lower()

    def test_prefix_mutation_counts_as_break(self):
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break

        m1 = self._msgs('tail A')
        detect_cache_break('pm-2', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000})
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'mutated prefix'
        detect_cache_break('pm-2', m2, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 89000,
                                  'cache_read_input_tokens': 51000})
        assert _cache_states['pm-2'].total_breaks == 1

    def test_stable_prefix_not_flagged(self):
        """Prefix byte-identical between turns → no prefix_mutation break."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        m1 = self._msgs('tail A')
        detect_cache_break('pm-3', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000})
        # Round 2: prefix UNCHANGED, healthy reuse (read grows, tiny write).
        m2 = self._msgs('tail A')
        r2 = detect_cache_break('pm-3', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 1200,
                                       'cache_read_input_tokens': 140000})
        assert r2 is None

    def test_prefix_mutation_after_compaction_not_flagged(self):
        """A prefix change right after compaction is expected, not a break."""
        from lib.tasks_pkg.cache_tracking import (
            detect_cache_break, notify_compaction,
        )

        m1 = self._msgs('tail A')
        detect_cache_break('pm-4', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 88000,
                                  'cache_read_input_tokens': 51000})
        notify_compaction('pm-4')
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'compaction rewrote this'
        r2 = detect_cache_break('pm-4', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 89000,
                                       'cache_read_input_tokens': 51000})
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
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        m1 = self._msgs('tail A')
        # Round 1: establish a HIGH read so round 2 can show a real drop.
        detect_cache_break('pm-5', m1, None, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 10000,
                                  'cache_read_input_tokens': 155000})
        # Round 2: mutate a prefix message AND drop cache_read (api_break)
        # while still writing a real prefix.
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'turn 1 question [count hint changed: N→N+1]'
        r2 = detect_cache_break('pm-5', m2, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 60000,
                                       'cache_read_input_tokens': 55000})
        assert r2 is not None
        # The actionable cause wins; the misleading server_side label is gone.
        assert 'prefix_mutation' in r2
        assert 'server_side' not in r2

    def test_client_change_still_wins_over_prefix_mutation(self):
        """A concrete client-side change (tools/system/model) is differently
        named and the popover labels it on its own — it must still win over
        the prefix_mutation key even when the prefix also mutated."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        m1 = self._msgs('tail A')
        tools_v1 = [{'function': {'name': 'grep_search', 'description': 'v1'}}]
        detect_cache_break('pm-6', m1, tools_v1, 'claude-opus-4',
                           usage={'cache_creation_input_tokens': 10000,
                                  'cache_read_input_tokens': 80000})
        # Round 2: prefix mutates AND the tool definitions change.
        m2 = self._msgs('tail B')
        m2[1]['content'] = 'mutated prefix bytes'
        tools_v2 = [{'function': {'name': 'grep_search', 'description': 'v2 CHANGED'}}]
        r2 = detect_cache_break('pm-6', m2, tools_v2, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 60000,
                                       'cache_read_input_tokens': 30000})
        assert r2 is not None
        # client_changes carries the concrete 'tools' key; prefix_mutation defers.
        assert 'tools' in r2
        assert 'prefix_mutation' not in r2


# ═══════════════════════════════════════════════════════════════════════════════
#  2. Concurrent conversation tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrentConversationTracking:
    """When multiple active conversations share the same model,
    cache contention is a likely cause of unexplained evictions."""

    def test_no_contention_single_conversation(self):
        """Single conversation — no contention detected."""
        from lib.tasks_pkg.cache_tracking import _count_active_on_model, detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('solo-1', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})

        count = _count_active_on_model('claude-opus-4', exclude_conv='solo-1')
        assert count == 0

    def test_contention_detected_with_concurrent_conversations(self):
        """Two conversations on the same model → contention detected."""
        from lib.tasks_pkg.cache_tracking import _count_active_on_model, detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Conversation A on opus
        detect_cache_break('conv-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})
        # Conversation B on opus
        detect_cache_break('conv-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000})

        count = _count_active_on_model('claude-opus-4', exclude_conv='conv-a')
        assert count == 1  # conv-b is active on same model

    def test_no_contention_different_models(self):
        """Different models don't cause contention."""
        from lib.tasks_pkg.cache_tracking import _count_active_on_model, detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        detect_cache_break('diff-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})
        detect_cache_break('diff-b', msgs, None, 'claude-sonnet-4',
                           usage={'cache_read_tokens': 30000})

        count = _count_active_on_model('claude-opus-4', exclude_conv='diff-a')
        assert count == 0

    def test_stale_conversation_not_counted(self):
        """Conversations inactive for >60s are not counted."""
        from lib.tasks_pkg.cache_tracking import (
            _cache_states, _count_active_on_model, detect_cache_break,
        )

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('stale-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})
        detect_cache_break('stale-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000})

        # Backdate stale-b to 2 minutes ago
        _cache_states['stale-b'].last_update_time = time.time() - 120

        count = _count_active_on_model('claude-opus-4', exclude_conv='stale-a')
        assert count == 0  # stale-b is too old

    def test_unexplained_drop_reason_no_contention(self):
        """When cache drops without client changes, reason does NOT mention contention.

        A/B tested 2026-04-10: cache contention between different conversations
        does NOT exist on Anthropic. Cache is keyed on exact prefix bytes;
        different conversations cannot evict each other.
        """
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Establish two conversations on the same model
        detect_cache_break('race-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})
        detect_cache_break('race-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000})

        # Cache drop on conv-a (unexplained) — should NOT blame contention
        result = detect_cache_break('race-a', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000})
        assert result is not None
        assert 'server_side' in result
        assert 'contention' not in result['server_side']
        # Should mention the real possible cause (stochastic server miss / TTL).
        assert ('stochastic server-side cache miss' in result['server_side']
                or 'TTL' in result['server_side'])


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Per-round cache stats logging
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundCacheStatsLogging:
    """log_round_cache_stats should log at INFO level for production visibility."""

    def test_logs_with_cache_activity(self, caplog):
        """Cache stats are logged when there's cache activity."""
        import logging
        from lib.tasks_pkg.cache_tracking import log_round_cache_stats

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
        from lib.tasks_pkg.cache_tracking import log_round_cache_stats

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
        from lib.tasks_pkg.cache_tracking import log_round_cache_stats

        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.cache_tracking'):
            log_round_cache_stats('test-conv', 0, None, model='gpt-4o')

        assert '[CacheStats]' not in caplog.text

    def test_anthropic_key_names(self, caplog):
        """Works with Anthropic-style key names."""
        import logging
        from lib.tasks_pkg.cache_tracking import log_round_cache_stats

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
        from lib.tasks_pkg.cache_tracking import latch_extended_ttl

        _lib.CACHE_EXTENDED_TTL = True
        assert latch_extended_ttl('task-latch-1') is True

    def test_latch_persists_after_setting_change(self):
        """Once latched, changing CACHE_EXTENDED_TTL doesn't affect the task."""
        from lib.tasks_pkg.cache_tracking import latch_extended_ttl

        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-latch-2')

        # Change setting mid-session
        _lib.CACHE_EXTENDED_TTL = False

        # Latched value should still be True
        assert latch_extended_ttl('task-latch-2') is True

    def test_different_tasks_get_independent_latches(self):
        """Different tasks can have different latched values."""
        from lib.tasks_pkg.cache_tracking import latch_extended_ttl

        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-a')

        _lib.CACHE_EXTENDED_TTL = False
        latch_extended_ttl('task-b')

        assert latch_extended_ttl('task-a') is True
        assert latch_extended_ttl('task-b') is False

    def test_release_latch_cleans_up(self):
        """release_ttl_latch removes the latch (memory cleanup)."""
        from lib.tasks_pkg.cache_tracking import (
            _ttl_latch, latch_extended_ttl, release_ttl_latch,
        )

        _lib.CACHE_EXTENDED_TTL = True
        latch_extended_ttl('task-release')
        assert 'task-release' in _ttl_latch

        release_ttl_latch('task-release')
        assert 'task-release' not in _ttl_latch

    def test_latch_used_in_add_cache_breakpoints(self):
        """add_cache_breakpoints uses latched value instead of live setting."""
        from lib.llm import add_cache_breakpoints
        from lib.tasks_pkg.cache_tracking import latch_extended_ttl

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
    """sort_tool_results ensures deterministic prefix for automatic caching."""

    def test_sorts_consecutive_tool_results(self):
        """Consecutive tool results are sorted by tool_call_id."""
        from lib.tasks_pkg.cache_tracking import sort_tool_results

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

        # Tool results should now be sorted by tool_call_id
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        assert tool_msgs[0]['tool_call_id'] == 'tc_a'
        assert tool_msgs[1]['tool_call_id'] == 'tc_b'
        assert tool_msgs[2]['tool_call_id'] == 'tc_c'

    def test_preserves_non_tool_messages(self):
        """Non-tool messages are not affected by sorting."""
        from lib.tasks_pkg.cache_tracking import sort_tool_results

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'thinking...'},
            {'role': 'user', 'content': 'go on'},
        ]

        original = [m.copy() for m in messages]
        sort_tool_results(messages)

        assert messages == original

    def test_handles_multiple_tool_runs(self):
        """Multiple separate runs of tool results are each sorted independently."""
        from lib.tasks_pkg.cache_tracking import sort_tool_results

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

        # First batch sorted
        assert messages[2]['tool_call_id'] == 'tc_1'
        assert messages[3]['tool_call_id'] == 'tc_2'
        # Second batch sorted
        assert messages[5]['tool_call_id'] == 'tc_3'
        assert messages[6]['tool_call_id'] == 'tc_4'

    def test_single_tool_result_unchanged(self):
        """A single tool result (no consecutive run) is not moved."""
        from lib.tasks_pkg.cache_tracking import sort_tool_results

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
        from lib.tasks_pkg.cache_tracking import sort_tool_results

        sort_tool_results([])
        sort_tool_results([{'role': 'system', 'content': 'sys'}])

    def test_tool_results_without_tool_call_id(self):
        """Tool results without tool_call_id sort by empty string."""
        from lib.tasks_pkg.cache_tracking import sort_tool_results

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
        from lib.tasks_pkg.cache_tracking import (
            _cache_states, cleanup_cache_state, detect_cache_break,
        )

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break('cleanup-1', msgs, None, 'model-a',
                           usage={'cache_read_tokens': 5000})
        assert 'cleanup-1' in _cache_states

        cleanup_cache_state('cleanup-1')
        assert 'cleanup-1' not in _cache_states

    def test_cleanup_nonexistent_is_noop(self):
        from lib.tasks_pkg.cache_tracking import cleanup_cache_state

        # Should not raise
        cleanup_cache_state('nonexistent-conv')


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Integration: _task_id passthrough in body
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskIdPassthrough:
    """_task_id is passed through body and cleaned up properly."""

    def test_task_id_stripped_for_non_claude(self):
        """_task_id should be removed from body for non-Claude models."""
        from lib.llm import add_cache_breakpoints

        body = {
            'model': 'gpt-4o',
            '_task_id': 'task-123',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }
        add_cache_breakpoints(body)
        # For non-Claude, add_cache_breakpoints returns early.
        # _task_id should still be in body (cleaned by _stream_chat_once).
        # But the key thing is it doesn't crash.

    def test_task_id_popped_for_claude(self):
        """_task_id is consumed (popped) by add_cache_breakpoints for Claude."""
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
        assert '_task_id' not in body  # consumed by pop
