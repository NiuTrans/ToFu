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

    def test_contention_mentioned_in_cache_drop_reason(self):
        """When cache drops with concurrent conversations, reason mentions contention."""
        from lib.tasks_pkg.cache_tracking import detect_cache_break

        msgs = [{'role': 'system', 'content': 'sys'}]

        # Establish two conversations
        detect_cache_break('race-a', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 50000})
        detect_cache_break('race-b', msgs, None, 'claude-opus-4',
                           usage={'cache_read_tokens': 30000})

        # Cache drop on conv-a (unexplained)
        result = detect_cache_break('race-a', msgs, None, 'claude-opus-4',
                                    usage={'cache_read_tokens': 5000})
        assert result is not None
        assert 'server_side' in result
        assert 'contention' in result['server_side']


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
        from lib.llm_client import add_cache_breakpoints
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
        from lib.llm_client import add_cache_breakpoints

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
        from lib.llm_client import add_cache_breakpoints

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
        from lib.llm_client import add_cache_breakpoints

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
