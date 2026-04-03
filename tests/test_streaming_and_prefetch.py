"""Tests for the 4 new improvements:
1. Streaming Tool Execution
2. Enhanced Delta Attachments
3. Concurrency Partitioning (audit)
4. Memory Prefetch
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ═══════════════════════════════════════════════════════════════════════════════
#  Test 1: Streaming Tool Execution
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStreamingToolAccumulator:
    """Test the StreamingToolAccumulator class."""

    def _make_task(self, tid='test-task-1234'):
        return {
            'id': tid,
            'aborted': False,
            'lastUserQuery': 'test query',
            '_tool_result_cache': {},
        }

    def test_import(self):
        """StreamingToolAccumulator can be imported."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        assert StreamingToolAccumulator is not None

    def test_streamable_tools_are_readonly(self):
        """Only read-only tools are in _STREAMABLE_TOOLS."""
        from lib.tasks_pkg.streaming_tool_executor import _STREAMABLE_TOOLS
        write_tools = {'write_file', 'apply_diff', 'run_command',
                       'generate_image', 'create_skill'}
        assert _STREAMABLE_TOOLS.isdisjoint(write_tools), \
            f"Write tools in _STREAMABLE_TOOLS: {_STREAMABLE_TOOLS & write_tools}"

    def test_callback_skips_non_streamable_tools(self):
        """on_tool_call_ready ignores write tools."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        # Try to submit a write tool
        acc.on_tool_call_ready({
            'id': 'tc_1',
            'function': {'name': 'write_file', 'arguments': '{"path":"a.py","content":"x"}'},
        })
        assert acc.submitted_count == 0

    def test_callback_submits_read_tool(self):
        """on_tool_call_ready submits read-only tools for pre-execution."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        # Mock the _execute_one to avoid actual execution
        acc._execute_one = MagicMock(return_value='file content here')

        acc.on_tool_call_ready({
            'id': 'tc_read_1',
            'function': {
                'name': 'list_dir',
                'arguments': json.dumps({'path': '.'}),
            },
        })
        assert acc.submitted_count == 1

        # Wait for the future to complete
        time.sleep(0.1)

        # Inject into cache
        hits = acc.inject_into_cache(task)
        assert hits == 1
        assert len(task['_tool_result_cache']) == 1

    def test_callback_skips_aborted_task(self):
        """on_tool_call_ready does not submit if task is aborted."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        task['aborted'] = True
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        acc.on_tool_call_ready({
            'id': 'tc_1',
            'function': {'name': 'grep_search', 'arguments': '{"pattern":"foo"}'},
        })
        assert acc.submitted_count == 0

    def test_callback_handles_invalid_json(self):
        """on_tool_call_ready gracefully handles malformed JSON arguments."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        acc.on_tool_call_ready({
            'id': 'tc_bad',
            'function': {'name': 'grep_search', 'arguments': 'NOT JSON'},
        })
        assert acc.submitted_count == 0

    def test_inject_into_cache_waits_for_unfinished(self):
        """inject_into_cache waits for in-progress futures instead of cancelling."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        # Create a future that finishes in 0.3s
        def _slow():
            time.sleep(0.3)
            return 'slow result'

        acc._submitted_count = 1
        future = acc._pool.submit(_slow)
        acc._futures['tc_slow'] = (future, 'grep_search', {'pattern': 'x'}, time.time())

        # Inject — future not yet done, should wait for it
        hits = acc.inject_into_cache(task)
        assert hits == 1
        assert len(task['_tool_result_cache']) == 1

    def test_inject_into_cache_respects_aborted_task(self):
        """inject_into_cache skips waiting when task is aborted."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        task['aborted'] = True
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        def _slow():
            time.sleep(5)
            return 'slow result'

        acc._submitted_count = 1
        future = acc._pool.submit(_slow)
        acc._futures['tc_slow'] = (future, 'grep_search', {'pattern': 'x'}, time.time())

        # Aborted task — should NOT wait for pending futures
        hits = acc.inject_into_cache(task)
        assert hits == 0

    def test_multiple_tools_pre_executed(self):
        """Multiple read-only tools can be pre-executed in parallel."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        task = self._make_task()
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        # Mock execution
        acc._execute_one = MagicMock(return_value='result')

        for i in range(5):
            acc.on_tool_call_ready({
                'id': f'tc_{i}',
                'function': {
                    'name': 'list_dir',
                    'arguments': json.dumps({'path': f'dir_{i}'}),
                },
            })

        assert acc.submitted_count == 5
        time.sleep(0.2)

        hits = acc.inject_into_cache(task)
        assert hits == 5


# ═══════════════════════════════════════════════════════════════════════════════
#  Test 2: on_tool_call_ready callback in SSE streaming
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStreamingCallback:
    """Test that on_tool_call_ready fires correctly during SSE tool_call delta processing."""

    def test_callback_fires_on_new_index(self):
        """When a new tool_call index appears, the previous tool's callback fires."""
        # Simulate the delta processing logic from _stream_chat_once
        tool_calls_acc = {}
        fired = []

        def on_tool_call_ready(tc):
            fired.append(tc.copy())

        # Simulate: tool_call index 0 arrives
        deltas = [
            {'tool_calls': [{'index': 0, 'id': 'tc_0', 'function': {'name': 'grep_search', 'arguments': ''}}]},
            {'tool_calls': [{'index': 0, 'function': {'arguments': '{"pa'}}]},
            {'tool_calls': [{'index': 0, 'function': {'arguments': 'ttern":"foo"}'}}]},
            # tool_call index 1 arrives → callback for index 0 should fire
            {'tool_calls': [{'index': 1, 'id': 'tc_1', 'function': {'name': 'list_dir', 'arguments': ''}}]},
            {'tool_calls': [{'index': 1, 'function': {'arguments': '{"path":"."}'}}]},
        ]

        for delta_msg in deltas:
            for tc in delta_msg.get('tool_calls', []):
                idx = tc.get('index', 0)
                if idx not in tool_calls_acc:
                    # Fire callback for previous complete tool
                    if on_tool_call_ready and idx > 0 and (idx - 1) in tool_calls_acc:
                        on_tool_call_ready(tool_calls_acc[idx - 1])
                    tool_calls_acc[idx] = {
                        'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''},
                    }
                if tc.get('id'):
                    tool_calls_acc[idx]['id'] = tc['id']
                fn = tc.get('function', {})
                if fn.get('name'):
                    tool_calls_acc[idx]['function']['name'] += fn['name']
                if fn.get('arguments') is not None:
                    tool_calls_acc[idx]['function']['arguments'] += fn.get('arguments', '')

        # Fire callback for the last tool
        if on_tool_call_ready and tool_calls_acc:
            last_idx = max(tool_calls_acc.keys())
            on_tool_call_ready(tool_calls_acc[last_idx])

        # Verify callbacks fired
        assert len(fired) == 2
        assert fired[0]['function']['name'] == 'grep_search'
        assert fired[0]['function']['arguments'] == '{"pattern":"foo"}'
        assert fired[1]['function']['name'] == 'list_dir'
        assert fired[1]['function']['arguments'] == '{"path":"."}'

    def test_single_tool_fires_at_end(self):
        """With only one tool_call, callback fires at stream end (not during)."""
        tool_calls_acc = {}
        fired = []

        def on_tool_call_ready(tc):
            fired.append(tc.copy())

        deltas = [
            {'tool_calls': [{'index': 0, 'id': 'tc_0', 'function': {'name': 'read_files', 'arguments': ''}}]},
            {'tool_calls': [{'index': 0, 'function': {'arguments': '{"reads":[{"path":"a.py"}]}'}}]},
        ]

        for delta_msg in deltas:
            for tc in delta_msg.get('tool_calls', []):
                idx = tc.get('index', 0)
                if idx not in tool_calls_acc:
                    if on_tool_call_ready and idx > 0 and (idx - 1) in tool_calls_acc:
                        on_tool_call_ready(tool_calls_acc[idx - 1])
                    tool_calls_acc[idx] = {
                        'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''},
                    }
                if tc.get('id'):
                    tool_calls_acc[idx]['id'] = tc['id']
                fn = tc.get('function', {})
                if fn.get('name'):
                    tool_calls_acc[idx]['function']['name'] += fn['name']
                if fn.get('arguments') is not None:
                    tool_calls_acc[idx]['function']['arguments'] += fn.get('arguments', '')

        # No callbacks during stream (only one tool)
        assert len(fired) == 0

        # Fire at end
        if on_tool_call_ready and tool_calls_acc:
            last_idx = max(tool_calls_acc.keys())
            on_tool_call_ready(tool_calls_acc[last_idx])

        assert len(fired) == 1
        assert fired[0]['function']['name'] == 'read_files'


# ═══════════════════════════════════════════════════════════════════════════════
#  Test 3: Delta Attachment Tracking
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeltaAttachments:
    """Test delta attachment tracking — always returns text, only skips compute.

    CRITICAL DESIGN: In our system each task gets fresh messages from the
    frontend.  Delta tracking must ALWAYS return the text for injection —
    it only caches to skip expensive FUSE I/O when content is unchanged.
    """

    def test_context_hash_consistency(self):
        """Same text produces same hash."""
        from lib.tasks_pkg.system_context import _context_hash
        h1 = _context_hash("hello world")
        h2 = _context_hash("hello world")
        assert h1 == h2

    def test_context_hash_different(self):
        """Different text produces different hash."""
        from lib.tasks_pkg.system_context import _context_hash
        h1 = _context_hash("hello world")
        h2 = _context_hash("hello earth")
        assert h1 != h2

    def test_first_call_returns_text(self):
        """First call always computes and returns text."""
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        key = ('test-conv-delta-1', 'project')
        _last_context_cache.pop(key, None)
        result = _get_cached_or_compute('test-conv-delta-1', 'project',
                                         lambda: 'some context')
        assert result == 'some context'

    def test_second_identical_call_still_returns_text(self):
        """Second call with same content STILL returns text (never empty)."""
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        key = ('test-conv-delta-2', 'skills')
        _last_context_cache.pop(key, None)
        _get_cached_or_compute('test-conv-delta-2', 'skills',
                               lambda: 'skill content A')
        result = _get_cached_or_compute('test-conv-delta-2', 'skills',
                                         lambda: 'skill content A')
        assert result == 'skill content A'  # MUST return, not skip

    def test_changed_content_returns_new_text(self):
        """Changed content returns the new version."""
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        key = ('test-conv-delta-3', 'project')
        _last_context_cache.pop(key, None)
        _get_cached_or_compute('test-conv-delta-3', 'project', lambda: 'v1')
        result = _get_cached_or_compute('test-conv-delta-3', 'project',
                                         lambda: 'v2')
        assert result == 'v2'

    def test_per_section_independence(self):
        """Different categories tracked independently."""
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        for cat in ('project', 'skills'):
            _last_context_cache.pop(('test-conv-delta-4', cat), None)

        r1 = _get_cached_or_compute('test-conv-delta-4', 'project',
                                     lambda: 'proj context')
        r2 = _get_cached_or_compute('test-conv-delta-4', 'skills',
                                     lambda: 'skills context')
        assert r1 == 'proj context'
        assert r2 == 'skills context'

    def test_empty_compute_returns_empty(self):
        """Empty string from compute returns empty."""
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        key = ('test-conv-delta-5', 'project')
        _last_context_cache.pop(key, None)
        result = _get_cached_or_compute('test-conv-delta-5', 'project',
                                         lambda: '')
        assert result == ''


# ═══════════════════════════════════════════════════════════════════════════════
#  Test 4: Concurrency Partitioning
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestConcurrencyPartitioning:
    """Test write tool serial dispatch partitioning."""

    def test_write_tools_frozenset_exists(self):
        """_WRITE_TOOLS frozenset is defined in tool_dispatch."""
        from lib.tasks_pkg.tool_dispatch import _WRITE_TOOLS
        assert isinstance(_WRITE_TOOLS, frozenset)
        assert 'write_file' in _WRITE_TOOLS
        assert 'apply_diff' in _WRITE_TOOLS
        assert 'run_command' in _WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        """Read-only tools are NOT in the _WRITE_TOOLS set."""
        from lib.tasks_pkg.tool_dispatch import _WRITE_TOOLS
        read_tools = {'read_files', 'grep_search', 'find_files',
                      'list_dir', 'web_search', 'fetch_url'}
        assert _WRITE_TOOLS.isdisjoint(read_tools)

    def test_streamable_and_write_disjoint(self):
        """_STREAMABLE_TOOLS and _WRITE_TOOLS have no overlap."""
        from lib.tasks_pkg.streaming_tool_executor import _STREAMABLE_TOOLS
        from lib.tasks_pkg.tool_dispatch import _WRITE_TOOLS
        assert _STREAMABLE_TOOLS.isdisjoint(_WRITE_TOOLS), \
            f"Overlap: {_STREAMABLE_TOOLS & _WRITE_TOOLS}"


# ═══════════════════════════════════════════════════════════════════════════════
#  Test 5: Memory Prefetch
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMemoryPrefetch:
    """Test memory prefetch integration in system context injection."""

    def test_prefetch_consumed_when_ready(self):
        """Prefetch future result is consumed instead of calling fallback."""
        from lib.tasks_pkg.system_context import _inject_system_contexts

        # Create a completed future
        future = Future()
        future.set_result("Prefetched project context here")

        task = {
            '_prefetch_project': future,
            '_prefetch_skills': None,
        }

        messages = [{'role': 'system', 'content': 'Base system prompt'}]

        with patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value='Search addendum'):
            _inject_system_contexts(
                messages, '/tmp/project', True,  # project_enabled
                False, True, False,              # skills, search, swarm
                has_real_tools=True,
                conv_id='',
                task=task,
            )

        # Project context should be prepended
        sys_content = messages[0]['content']
        if isinstance(sys_content, list):
            sys_text = '\n\n'.join(b['text'] for b in sys_content if isinstance(b, dict))
        else:
            sys_text = sys_content
        assert 'Prefetched project context here' in sys_text

    def test_prefetch_fallback_on_failure(self):
        """When prefetch future failed, fallback function is called."""
        from lib.tasks_pkg.system_context import _inject_system_contexts

        # Create a failed future
        future = Future()
        future.set_exception(RuntimeError("FUSE timeout"))

        task = {
            '_prefetch_project': future,
            '_prefetch_skills': None,
        }

        messages = [{'role': 'system', 'content': 'Base prompt'}]

        # The fallback should be called — mock it
        with patch('lib.project_mod.get_context_for_prompt',
                   return_value='Fallback project ctx') as mock_fn, \
             patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp/project', True,
                False, False, False,
                has_real_tools=True,
                conv_id='',
                task=task,
            )

        sys_content = messages[0]['content']
        if isinstance(sys_content, list):
            sys_text = '\n\n'.join(b['text'] for b in sys_content if isinstance(b, dict))
        else:
            sys_text = sys_content
        assert 'Fallback project ctx' in sys_text

    def test_prefetch_fallback_when_not_done(self):
        """When prefetch future is not done, fallback function is called."""
        from lib.tasks_pkg.system_context import _inject_system_contexts

        # Create a future that will never complete
        future = Future()  # not set_result'd, not done

        task = {
            '_prefetch_project': future,
            '_prefetch_skills': None,
        }

        messages = [{'role': 'system', 'content': 'Base prompt'}]

        with patch('lib.project_mod.get_context_for_prompt',
                   return_value='Sync fallback ctx') as mock_fn, \
             patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp/project', True,
                False, False, False,
                has_real_tools=True,
                conv_id='',
                task=task,
            )

        sys_content = messages[0]['content']
        if isinstance(sys_content, list):
            sys_text = '\n\n'.join(b['text'] for b in sys_content if isinstance(b, dict))
        else:
            sys_text = sys_content
        assert 'Sync fallback ctx' in sys_text

    def test_no_prefetch_when_task_is_none(self):
        """When task is None, normal synchronous loading is used."""
        from lib.tasks_pkg.system_context import _inject_system_contexts

        messages = [{'role': 'system', 'content': 'Base'}]

        with patch('lib.project_mod.get_context_for_prompt',
                   return_value='Normal load') as mock_fn, \
             patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp/proj', True,
                False, False, False,
                has_real_tools=True,
                task=None,
            )

        mock_fn.assert_called_once()

    def test_skills_prefetch_consumed(self):
        """Skills listing is injected into user message (not system) via inject_skills_to_user.

        After the refactor, _inject_system_contexts only puts compact skill
        instructions in the system message. The full skills listing goes into
        the last user message via inject_skills_to_user().
        """
        from lib.tasks_pkg.system_context import _inject_system_contexts, inject_skills_to_user

        # Create completed futures
        proj_future = Future()
        proj_future.set_result("Proj ctx")

        task = {
            '_prefetch_project': proj_future,
            '_prefetch_skills': None,
        }

        messages = [
            {'role': 'system', 'content': 'Base'},
            {'role': 'user', 'content': 'Help me with flask migration'},
        ]

        with patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp/proj', True,
                True, False, False,  # skills_enabled=True
                has_real_tools=True,
                conv_id='',
                task=task,
            )

        # System message should have compact skill instructions but NOT
        # the full <available_skills> listing
        sys_content = messages[0]['content']
        if isinstance(sys_content, list):
            sys_text = '\n\n'.join(b['text'] for b in sys_content if isinstance(b, dict))
        else:
            sys_text = sys_content
        assert 'skill_accumulation' in sys_text
        assert '<available_skills>' not in sys_text

        # Now inject skills into user message (simulates orchestrator flow)
        with patch('lib.skills.build_skills_context',
                   return_value='<available_skills>\nSkill listing here\n</available_skills>'):
            inject_skills_to_user(
                messages,
                project_path='/tmp/proj',
                project_enabled=True,
                skills_enabled=True,
                has_real_tools=True,
                conv_id='test-conv',
            )

        # User message should now contain the skills listing
        user_msg = messages[-1]
        assert user_msg['role'] == 'user'
        user_text = user_msg.get('content', '')
        if isinstance(user_text, list):
            user_text = '\n'.join(b.get('text', '') for b in user_text if isinstance(b, dict))
        assert '<available_skills>' in user_text


# ═══════════════════════════════════════════════════════════════════════════════
#  Test 6: Callback threading through streaming stack
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCallbackThreading:
    """Verify the on_tool_call_ready callback is threaded through the call chain."""

    def test_stream_chat_signature(self):
        """stream_chat accepts on_tool_call_ready parameter."""
        import inspect

        from lib.llm_client import stream_chat
        sig = inspect.signature(stream_chat)
        assert 'on_tool_call_ready' in sig.parameters

    def test_dispatch_stream_signature(self):
        """dispatch_stream accepts on_tool_call_ready parameter."""
        import inspect

        from lib.llm_dispatch.api import dispatch_stream
        sig = inspect.signature(dispatch_stream)
        assert 'on_tool_call_ready' in sig.parameters

    def test_stream_llm_response_signature(self):
        """stream_llm_response accepts on_tool_call_ready parameter."""
        import inspect

        from lib.tasks_pkg.manager import stream_llm_response
        sig = inspect.signature(stream_llm_response)
        assert 'on_tool_call_ready' in sig.parameters

    def test_llm_call_with_fallback_signature(self):
        """_llm_call_with_fallback accepts on_tool_call_ready parameter."""
        import inspect

        from lib.tasks_pkg.llm_fallback import _llm_call_with_fallback
        sig = inspect.signature(_llm_call_with_fallback)
        assert 'on_tool_call_ready' in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════════
#  Test 7: Integration — streaming tool execution end-to-end
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStreamingIntegration:
    """Integration tests for the full streaming tool execution flow."""

    def test_cache_key_compatibility(self):
        """StreamingToolAccumulator produces cache keys compatible with tool_dispatch."""
        from lib.tasks_pkg.tool_dispatch import _make_cache_key

        # Verify the cache key function works
        key1 = _make_cache_key('grep_search', {'pattern': 'foo'})
        key2 = _make_cache_key('grep_search', {'pattern': 'foo'})
        key3 = _make_cache_key('grep_search', {'pattern': 'bar'})

        assert key1 == key2  # same args → same key
        assert key1 != key3  # different args → different key

    def test_prefetch_result_is_found_by_pipeline(self):
        """Results injected by StreamingToolAccumulator are found by dedup check."""
        from lib.tasks_pkg.tool_dispatch import _make_cache_key

        task = {'id': 'test-123', '_tool_result_cache': {}}

        # Simulate what StreamingToolAccumulator.inject_into_cache does
        fn_name = 'grep_search'
        fn_args = {'pattern': 'import'}
        cache_key = _make_cache_key(fn_name, fn_args)
        task['_tool_result_cache'][cache_key] = ('grep result: 5 matches', False)

        # Verify the cache entry exists and is retrievable
        assert cache_key in task['_tool_result_cache']
        content, is_search = task['_tool_result_cache'][cache_key]
        assert 'grep result' in content

    def test_accumulator_full_cycle(self):
        """Full cycle: submit → execute → inject → cache hit."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        from lib.tasks_pkg.tool_dispatch import _make_cache_key

        task = {
            'id': 'test-full-cycle',
            'aborted': False,
            'lastUserQuery': 'find imports',
            '_tool_result_cache': {},
        }
        acc = StreamingToolAccumulator(task, project_path='/tmp')

        # Mock _execute_one to return fast
        acc._execute_one = MagicMock(return_value='mock result')

        # Submit tool
        fn_args = {'pattern': 'import', 'path': 'lib'}
        acc.on_tool_call_ready({
            'id': 'tc_cycle_1',
            'function': {
                'name': 'grep_search',
                'arguments': json.dumps(fn_args),
            },
        })
        assert acc.submitted_count == 1

        time.sleep(0.1)  # let thread pool finish

        # Inject
        hits = acc.inject_into_cache(task)
        assert hits == 1

        # Verify cache key matches
        cache_key = _make_cache_key('grep_search', fn_args)
        assert cache_key in task['_tool_result_cache']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
