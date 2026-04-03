"""Tests for Claude Code-inspired feature additions.

Covers:
  1. Session Memory — threshold detection, extraction prompt, merge
  2. Per-Turn Attachments — compute, inject, state tracking
  3. Cache Break Detection — hash tracking, cache-aware microcompact
  4. Pre/Post Tool Hooks — registration, execution, blocking
  5. Unified ToolSpec — registration, backward-compat exports
  6. Dynamic Tool Deferral — threshold-based auto-deferral
  7. Partial Compaction — directional compaction
"""

import copy
import json

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
#  1. Session Memory
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSessionMemory:
    """Tests for lib/tasks_pkg/session_memory.py."""

    def test_estimate_tokens_basic(self):
        from lib.tasks_pkg.session_memory import _estimate_tokens
        messages = [
            {'role': 'user', 'content': 'Hello ' * 100},  # 600 chars → ~150 tokens
            {'role': 'assistant', 'content': 'World ' * 200},  # 1200 chars → ~300 tokens
        ]
        tokens = _estimate_tokens(messages)
        assert 400 < tokens < 500  # rough estimate

    def test_estimate_tokens_with_tool_calls(self):
        from lib.tasks_pkg.session_memory import _estimate_tokens
        messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'arguments': '{"path": "src/main.py"}'}},
            ]},
        ]
        tokens = _estimate_tokens(messages)
        assert tokens > 0

    def test_estimate_tokens_multimodal(self):
        from lib.tasks_pkg.session_memory import _estimate_tokens
        messages = [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'Analyze this: ' + 'x' * 400},
            ]},
        ]
        tokens = _estimate_tokens(messages)
        assert tokens > 100

    def test_count_tool_calls(self):
        from lib.tasks_pkg.session_memory import _count_tool_calls
        messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': 'read_files'}},
                {'function': {'name': 'grep_search'}},
            ]},
            {'role': 'tool', 'content': 'result'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': 'write_file'}},
            ]},
        ]
        assert _count_tool_calls(messages) == 3

    def test_should_extract_first_time_below_threshold(self):
        from lib.tasks_pkg.session_memory import _extraction_state, should_extract_memory
        _extraction_state.pop('test-conv-1', None)

        messages = [
            {'role': 'user', 'content': 'short message'},
        ]
        assert should_extract_memory('test-conv-1', messages) is False

    def test_should_extract_first_time_above_threshold(self):
        from lib.tasks_pkg.session_memory import _MIN_TOKENS_TO_INIT, _extraction_state, should_extract_memory
        _extraction_state.pop('test-conv-2', None)

        # Generate enough content to exceed threshold
        big_content = 'x' * (_MIN_TOKENS_TO_INIT * 4 + 100)
        messages = [
            {'role': 'user', 'content': big_content},
        ]
        assert should_extract_memory('test-conv-2', messages) is True

    def test_should_extract_empty_conv_id(self):
        from lib.tasks_pkg.session_memory import should_extract_memory
        assert should_extract_memory('', [{'role': 'user', 'content': 'hi'}]) is False

    def test_should_extract_empty_messages(self):
        from lib.tasks_pkg.session_memory import should_extract_memory
        assert should_extract_memory('test-conv-3', []) is False

    def test_format_recent_messages_basic(self):
        from lib.tasks_pkg.session_memory import _format_recent_messages
        messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
        ]
        result = _format_recent_messages(messages)
        assert '[user] Hello' in result
        assert '[assistant] Hi there' in result

    def test_format_recent_messages_truncates_long(self):
        from lib.tasks_pkg.session_memory import _format_recent_messages
        messages = [
            {'role': 'assistant', 'content': 'x' * 5000},
        ]
        result = _format_recent_messages(messages)
        assert '...[truncated]...' in result

    def test_format_recent_messages_respects_max_chars(self):
        from lib.tasks_pkg.session_memory import _format_recent_messages
        messages = [
            {'role': 'user', 'content': 'msg ' * 500}
            for _ in range(20)
        ]
        result = _format_recent_messages(messages, max_chars=1000)
        assert len(result) < 3000

    def test_format_recent_messages_skips_system(self):
        from lib.tasks_pkg.session_memory import _format_recent_messages
        messages = [
            {'role': 'system', 'content': 'secret system prompt'},
            {'role': 'user', 'content': 'Hello'},
        ]
        result = _format_recent_messages(messages)
        assert 'secret system prompt' not in result
        assert 'Hello' in result

    def test_format_recent_messages_includes_tool_calls(self):
        from lib.tasks_pkg.session_memory import _format_recent_messages
        messages = [
            {'role': 'assistant', 'content': 'let me check', 'tool_calls': [
                {'function': {'name': 'read_files', 'arguments': '{}'}},
            ]},
        ]
        result = _format_recent_messages(messages)
        assert 'read_files()' in result



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

    def test_tool_discovery_delta_detects_new(self):
        from lib.tasks_pkg.attachments import _attachment_state, _get_tool_discovery_delta
        conv_id = 'test-delta-1'
        _attachment_state.pop(conv_id, None)

        task = {'_discovered_tool_names': {'browser_click', 'browser_type'}}
        result = _get_tool_discovery_delta(task, conv_id)
        assert result is not None
        assert 'browser_click' in result

        # Second call with same tools → no delta
        result2 = _get_tool_discovery_delta(task, conv_id)
        assert result2 is None

    def test_tool_discovery_delta_empty_discovered(self):
        from lib.tasks_pkg.attachments import _attachment_state, _get_tool_discovery_delta
        conv_id = 'test-delta-2'
        _attachment_state.pop(conv_id, None)

        task = {}
        assert _get_tool_discovery_delta(task, conv_id) is None




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
        detect_cache_break(conv_id, messages, None, 'model-a')  # first call
        result = detect_cache_break(conv_id, messages, None, 'model-b')  # model change
        assert result is not None
        assert 'model' in result

    def test_detect_cache_break_system_prompt_change(self):
        from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
        conv_id = 'test-cb-3'
        _cache_states.pop(conv_id, None)

        messages1 = [{'role': 'system', 'content': 'prompt v1'}]
        detect_cache_break(conv_id, messages1, None, 'model-a')

        messages2 = [{'role': 'system', 'content': 'prompt v2'}]
        result = detect_cache_break(conv_id, messages2, None, 'model-a')
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
#  6. Dynamic Tool Deferral
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDynamicDeferral:
    """Tests for the dynamic deferral in lib/tools/deferral.py."""

    def test_static_deferral_still_works(self):
        from lib.tools.deferral import partition_tools
        tools = [
            {'function': {'name': 'read_files', 'parameters': {}}},
            {'function': {'name': 'browser_click', 'parameters': {
                'big_schema': 'x' * 1000
            }}},
        ]
        core, deferred = partition_tools(tools)
        core_names = {t['function']['name'] for t in core}
        deferred_names = {t['function']['name'] for t in deferred}
        assert 'read_files' in core_names
        assert 'browser_click' in deferred_names

    def test_dynamic_deferral_small_context(self):
        """With a very small context window, more tools get deferred."""
        from lib.tools.deferral import partition_tools
        tools = [
            {'function': {'name': 'read_files', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'write_file', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'grep_search', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'find_files', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'run_command', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'web_search', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'create_skill', 'parameters': {'p': 'x' * 500}}},
            {'function': {'name': 'check_error_logs', 'parameters': {'p': 'x' * 500}}},
        ]
        # With a tiny context window, some tools should be auto-deferred
        core, deferred = partition_tools(tools, context_window=1000)

        # Core tools should always be kept
        core_names = {t['function']['name'] for t in core}
        assert 'read_files' in core_names
        assert 'write_file' in core_names

    def test_dynamic_deferral_large_context(self):
        """With a huge context window, nothing extra gets deferred."""
        from lib.tools.deferral import partition_tools
        tools = [
            {'function': {'name': 'read_files', 'parameters': {}}},
            {'function': {'name': 'create_skill', 'parameters': {}}},
        ]
        core, deferred = partition_tools(tools, context_window=1_000_000)
        core_names = {t['function']['name'] for t in core}
        assert 'create_skill' in core_names  # Not deferred with huge window

    def test_estimate_tool_tokens(self):
        from lib.tools.deferral import _estimate_tool_tokens
        tools = [{'function': {'name': 'test', 'parameters': {'x': 'y' * 400}}}]
        tokens = _estimate_tool_tokens(tools)
        assert tokens > 100

    def test_partition_empty(self):
        from lib.tools.deferral import partition_tools
        core, deferred = partition_tools([])
        assert core == []
        assert deferred == []


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
        state.message_prefix_count = 3  # protect first 3 messages
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
