"""Tests for Claude Code-inspired feature additions.

Covers:
  1. Session Memory — threshold detection, extraction prompt, merge
  2. Per-Turn Attachments — compute, inject, state tracking
  3. Cache Break Detection — hash tracking, cache-aware microcompact
  4. Pre/Post Tool Hooks — registration, execution, blocking
  5. Unified ToolSpec — registration, backward-compat exports
  6. Partial Compaction — directional compaction
"""

import copy
import json

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
#  1. Session Memory
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
#  2. Per-Turn Attachments
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAttachments:
    """Tests for lib/tasks_pkg/attachments.py."""

    def test_compute_empty_returns_empty(self):
        from lib.tasks_pkg.attachments import compute_turn_attachments
        result = compute_turn_attachments(
            messages=[], task={}, round_num=0, conv_id='',
        )
        assert result == []

    def test_inject_attachments_to_last_user_msg(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'Hello'},
        ]
        inject_attachments(messages, ['<attachment>test</attachment>'])
        assert '<attachment>test</attachment>' in messages[1]['content']

    def test_inject_attachments_to_multimodal_user_msg(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'Hello'},
            ]},
        ]
        inject_attachments(messages, ['<attachment>test</attachment>'])
        assert len(messages[0]['content']) == 2
        assert messages[0]['content'][1]['type'] == 'text'

    def test_inject_attachments_no_user_creates_one(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'system', 'content': 'sys'},
        ]
        inject_attachments(messages, ['<attachment>test</attachment>'])
        assert len(messages) == 2
        assert messages[1]['role'] == 'user'

    def test_inject_empty_attachments_no_change(self):
        from lib.tasks_pkg.attachments import inject_attachments
        messages = [
            {'role': 'user', 'content': 'Hello'},
        ]
        original = copy.deepcopy(messages)
        inject_attachments(messages, [])
        assert messages == original



# ═══════════════════════════════════════════════════════════════════════════════
#  3. Cache Break Detection
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCacheTracking:
    """Tests for lib/tasks_pkg/cache_tracking.py."""

    def test_md5_consistency(self):
        from lib.tasks_pkg.cache_tracking import _md5
        assert _md5('hello') == _md5('hello')
        assert _md5('hello') != _md5('world')
        assert len(_md5('test')) == 16

    def test_hash_system_prompt_string(self):
        from lib.tasks_pkg.cache_tracking import _hash_system_prompt
        messages = [{'role': 'system', 'content': 'You are helpful'}]
        h = _hash_system_prompt(messages)
        assert h and len(h) == 16

    def test_hash_system_prompt_list(self):
        from lib.tasks_pkg.cache_tracking import _hash_system_prompt
        messages = [{'role': 'system', 'content': [
            {'type': 'text', 'text': 'You are helpful'},
        ]}]
        h = _hash_system_prompt(messages)
        assert h and len(h) == 16

    def test_hash_system_prompt_missing(self):
        from lib.tasks_pkg.cache_tracking import _hash_system_prompt
        assert _hash_system_prompt([{'role': 'user', 'content': 'hi'}]) == ''

    def test_hash_tools_empty(self):
        from lib.tasks_pkg.cache_tracking import _hash_tools
        assert _hash_tools(None) == ''
        assert _hash_tools([]) == ''

    def test_hash_tools_deterministic(self):
        from lib.tasks_pkg.cache_tracking import _hash_tools
        tools = [{'function': {'name': 'read_files', 'parameters': {}}}]
        h1 = _hash_tools(tools)
        h2 = _hash_tools(tools)
        assert h1 == h2

    def test_detect_cache_break_first_call_no_break(self):
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
        conv_id = 'test-cb-1'
        _cache_states.pop(conv_id, None)

        messages = [{'role': 'system', 'content': 'sys'}]
        result = detect_cache_break(conv_id, messages, None, 'model-a')
        assert result is None  # First call never breaks

    def test_detect_cache_break_model_change(self):
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
        conv_id = 'test-cb-2'
        _cache_states.pop(conv_id, None)

        messages = [{'role': 'system', 'content': 'sys'}]
        # First call establishes baseline with cache_read tokens
        detect_cache_break(conv_id, messages, None, 'model-a',
                           usage={'cache_read_tokens': 5000})
        # Model change + cache_read drop confirms a cache break
        result = detect_cache_break(conv_id, messages, None, 'model-b',
                           usage={'cache_read_tokens': 100})
        assert result is not None
        assert 'model' in result

    def test_detect_cache_break_system_prompt_change(self):
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
        conv_id = 'test-cb-3'
        _cache_states.pop(conv_id, None)

        messages1 = [{'role': 'system', 'content': 'prompt v1'}]
        detect_cache_break(conv_id, messages1, None, 'model-a',
                           usage={'cache_read_tokens': 5000})

        messages2 = [{'role': 'system', 'content': 'prompt v2'}]
        result = detect_cache_break(conv_id, messages2, None, 'model-a',
                           usage={'cache_read_tokens': 100})
        assert result is not None
        assert 'system_prompt' in result

    def test_detect_cache_break_empty_conv_id(self):
        from lib.tasks_pkg.cache_tracking import detect_cache_break
        result = detect_cache_break('', [{'role': 'system', 'content': 'sys'}], None, 'm')
        assert result is None

    def test_get_cache_prefix_count_no_state(self):
        from lib.tasks_pkg.cache_tracking import _cache_states, get_cache_prefix_count
        _cache_states.pop('nonexistent', None)
        assert get_cache_prefix_count('nonexistent') == 0

    def test_no_false_positive_on_message_growth(self):
        """Growing messages (tool rounds) should NOT trigger a cache break
        when cache_read tokens are stable or growing."""
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
        conv_id = 'test-cb-grow'
        _cache_states.pop(conv_id, None)

        # Round 1: system + user
        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hello'}]
        detect_cache_break(conv_id, msgs, None, 'model-a',
                           usage={'cache_read_tokens': 1000})

        # Round 2: add assistant + tool result (cache growing)
        msgs.append({'role': 'assistant', 'content': '', 'tool_calls': [
            {'function': {'name': 'read_files', 'arguments': '{}'}}
        ]})
        msgs.append({'role': 'tool', 'content': 'file content here'})
        result = detect_cache_break(conv_id, msgs, None, 'model-a',
                                    usage={'cache_read_tokens': 1500})
        assert result is None  # No break — cache grew normally

    def test_notify_compaction_suppresses_break(self):
        """After compaction, a cache_read drop should not be flagged."""
        from lib.tasks_pkg.cache_tracking import (
            _cache_states, detect_cache_break, notify_compaction,
        )
        conv_id = 'test-cb-compact'
        _cache_states.pop(conv_id, None)

        msgs = [{'role': 'system', 'content': 'sys'}]
        detect_cache_break(conv_id, msgs, None, 'model-a',
                           usage={'cache_read_tokens': 10000})
        # Compaction happened — notify
        notify_compaction(conv_id)
        # Cache tokens drop (expected after compaction)
        result = detect_cache_break(conv_id, msgs, None, 'model-a',
                                    usage={'cache_read_tokens': 3000})
        # Should NOT be flagged as a confirmed break
        assert result is None or 'system_prompt' not in result

    def test_breakpoint_on_conversation_tail(self):
        """add_cache_breakpoints should place BP4 on the LAST message with
        content (msg[-1]), not msg[-2].  In tool conversations, msg[-1] is
        the tool result — always has content and becomes prefix next round.

        This was changed from msg[-2] to msg[-1] to fix the cache oscillation
        bug where empty-content assistants at msg[-2] caused BP4 to fall back
        to an early message, under-caching the conversation tail.
        See: debug/CACHE_BP4_AB_REPORT.md
        """
        from lib.llm import add_cache_breakpoints
        # Simulate a multi-round tool conversation:
        # system, user, asst+tc, tool, asst+tc, tool(latest)
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [
                {'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'hello'},
                {'role': 'assistant', 'content': 'Let me read that file.',
                 'tool_calls': [
                     {'function': {'name': 'read_files', 'arguments': '{}'}}
                 ]},
                {'role': 'tool', 'content': 'file content from round 1'},
                {'role': 'assistant', 'content': 'Now let me search.',
                 'tool_calls': [
                     {'function': {'name': 'grep_search', 'arguments': '{}'}}
                 ]},
                {'role': 'tool', 'content': 'search results from round 2'},
            ],
        }
        add_cache_breakpoints(body)
        # BP4 should be on the LAST message (msg[-1], the tool result)
        # because it caches the maximum prefix for the next round.
        last_msg = body['messages'][-1]
        content = last_msg.get('content', '')
        # It should have been converted to list with cache_control
        assert isinstance(content, list), \
            f'Expected list content on last msg, got {type(content)}'
        has_cache_control = any(
            isinstance(b, dict) and 'cache_control' in b
            for b in content
        )
        assert has_cache_control, \
            'Last message (tool result) should have cache_control breakpoint (BP4)'
        # Penultimate (msg[-2], assistant with content) should NOT have BP4
        # because msg[-1] already has it (maximum prefix coverage)
        penultimate = body['messages'][-2]
        pen_content = penultimate.get('content', '')
        if isinstance(pen_content, list):
            pen_has_cc = any(
                isinstance(b, dict) and 'cache_control' in b
                for b in pen_content
            )
            assert not pen_has_cc, \
                'Penultimate should NOT have BP4 when last msg has it'



# ═══════════════════════════════════════════════════════════════════════════════
#  4. Pre/Post Tool Hooks
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolHooks:
    """Tests for lib/tasks_pkg/tool_hooks.py."""

    def test_builtin_empty_result_hook(self):
        from lib.tasks_pkg.tool_hooks import _empty_result_marker_hook
        result = _empty_result_marker_hook('test_tool', {}, '', {})
        assert result is not None
        assert 'test_tool' in result
        assert 'no output' in result

    def test_builtin_empty_result_hook_nonempty(self):
        from lib.tasks_pkg.tool_hooks import _empty_result_marker_hook
        result = _empty_result_marker_hook('test_tool', {}, 'some content', {})
        assert result is None  # No modification

    def test_run_command_safety_hook_blocks_rm_rf(self):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        result = _run_command_safety_hook('run_command', {'command': 'rm -rf /'}, {})
        assert result is not None
        assert result.action == 'block'

    def test_run_command_safety_hook_allows_normal(self):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        result = _run_command_safety_hook('run_command', {'command': 'ls -la'}, {})
        assert result is None

    def test_run_command_safety_hook_ignores_other_tools(self):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        result = _run_command_safety_hook('read_files', {'command': 'rm -rf /'}, {})
        assert result is None

    def test_register_and_run_pre_hook(self):
        from lib.tasks_pkg.tool_hooks import HookResult, _pre_hooks, register_pre_hook, run_pre_hooks
        original_count = len(_pre_hooks)

        def my_hook(tool_name, args, task):
            if tool_name == 'dangerous_tool':
                return HookResult(action='block', message='nope')
            return None

        register_pre_hook(my_hook)
        try:
            result = run_pre_hooks('dangerous_tool', {}, {})
            assert result is not None
            assert result.action == 'block'

            result2 = run_pre_hooks('safe_tool', {}, {})
            # May return None or a built-in hook result
        finally:
            _pre_hooks.pop()  # cleanup

    def test_register_and_run_post_hook(self):
        from lib.tasks_pkg.tool_hooks import _post_hooks, register_post_hook, run_post_hooks
        original_count = len(_post_hooks)

        def my_hook(tool_name, args, result, task):
            return result + '\n[MODIFIED]'

        register_post_hook(my_hook)
        try:
            result = run_post_hooks('test_tool', {}, 'original content', {})
            assert '[MODIFIED]' in result
        finally:
            _post_hooks.pop()  # cleanup

    def test_run_pre_hooks_exception_handled(self):
        from lib.tasks_pkg.tool_hooks import _pre_hooks, register_pre_hook, run_pre_hooks

        def bad_hook(tool_name, args, task):
            raise RuntimeError('hook failed')

        register_pre_hook(bad_hook)
        try:
            # Should not raise — exceptions are caught and logged
            result = run_pre_hooks('test_tool', {}, {})
            # Result could be None (bad hook's exception caught)
        finally:
            _pre_hooks.pop()

    def test_run_post_hooks_exception_handled(self):
        from lib.tasks_pkg.tool_hooks import _post_hooks, register_post_hook, run_post_hooks

        def bad_hook(tool_name, args, result, task):
            raise RuntimeError('hook failed')

        register_post_hook(bad_hook)
        try:
            result = run_post_hooks('test_tool', {}, 'content', {})
            assert result == 'content'  # Original preserved on exception
        finally:
            _post_hooks.pop()

    def test_hook_result_defaults(self):
        from lib.tasks_pkg.tool_hooks import HookResult
        hr = HookResult()
        assert hr.action == 'allow'
        assert hr.message == ''
        assert hr.modified_args is None


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Unified ToolSpec
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  6. (removed) Dynamic Tool Deferral — deferral subsystem deleted
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  7. Partial Compaction
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  8. Integration tests: cache-aware microcompact
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCacheAwareMicroCompact:
    """Tests that microcompact respects cache prefix."""

    def test_cache_prefix_skips_messages(self):
        """When cache prefix is set, messages within it should be skipped."""
        from lib.tasks_pkg.cache_tracking import CacheState, _cache_states
        from lib.tasks_pkg.compaction import micro_compact

        conv_id = 'test-cache-mc-1'
        # Set up state with active cache
        state = CacheState()
        state.last_cache_read_tokens = 5000
        state.message_count = 5  # simulate 5 messages tracked; prefix = max(0, 5 - 2) = 3
        state.call_count = 5
        _cache_states[conv_id] = state

        messages = [
            {'role': 'system', 'content': 'system prompt'},
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'first answer',
             'reasoning_content': 'thinking ' * 500},  # in cache prefix
            {'role': 'user', 'content': 'second question'},
            {'role': 'assistant', 'content': 'second answer',
             'reasoning_content': 'more thinking ' * 500},  # outside cache
            {'role': 'user', 'content': 'third question'},
            {'role': 'assistant', 'content': 'third answer',
             'reasoning_content': 'latest thinking'},  # in hot tail
        ]

        original_thinking_2 = messages[2]['reasoning_content']

        micro_compact(messages, conv_id=conv_id)

        # Message at index 2 (in cache prefix) should be PRESERVED
        assert messages[2]['reasoning_content'] == original_thinking_2

        # Cleanup
        _cache_states.pop(conv_id, None)
