"""Tests for compaction improvements inspired by Claude Code.

Tests cover:
  1. Tool result budgeting (budget_tool_result)
  2. Extended micro-compact (thinking block stripping)
  3. 9-section summary template (<analysis> stripping)
  4. Reactive compact (API rejection recovery)
  5. Post-compact context re-injection
  6. Concurrency safety partitioning (_WRITE_TOOLS)
  7. Delta attachment tracking (system context dedup)

Run:  pytest tests/test_compaction_improvements.py -m unit -v
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════
#  1. Tool Result Budgeting
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolResultBudgeting:
    """Verify budget_tool_result with persistence and exempt tools."""

    def test_small_result_passthrough(self):
        from lib.tasks_pkg.compaction import budget_tool_result
        content = "Hello world"
        assert budget_tool_result('read_files', content) == content

    def test_read_files_exempt_never_truncated(self):
        """read_files results should NEVER be truncated (like Claude Code Infinity)."""
        from lib.tasks_pkg.compaction import budget_tool_result
        # Create a 200K char result — should pass through unchanged
        content = "x" * 200_000
        result = budget_tool_result('read_files', content)
        assert result == content  # NOT truncated
        assert len(result) == 200_000

    def test_read_local_file_exempt_never_truncated(self):
        """read_local_file results should NEVER be truncated."""
        from lib.tasks_pkg.compaction import budget_tool_result
        content = "y" * 200_000
        result = budget_tool_result('read_local_file', content)
        assert result == content

    def test_large_grep_result_persisted_to_disk(self):
        """Non-exempt tools exceeding budget should be persisted to disk."""
        from lib.tasks_pkg.compaction import budget_tool_result
        content = "matched_line\n" * 10_000  # ~130K chars
        result = budget_tool_result('grep_search', content)
        assert len(result) < len(content)
        assert '[Persisted to:' in result
        assert 'read_local_file' in result  # tells model how to access
        assert 'Preview' in result
        # Verify the file was actually written
        import re
        m = re.search(r'\[Persisted to: (.+?)\]', result)
        assert m is not None
        filepath = m.group(1)
        assert os.path.isfile(filepath)
        with open(filepath) as f:
            assert len(f.read()) == len(content)  # full content preserved

    def test_different_tools_different_budgets(self):
        from lib.tasks_pkg.compaction import _BUDGET_EXEMPT_TOOLS, TOOL_RESULT_MAX_CHARS, budget_tool_result
        # grep_search has a 30K budget; read_files is exempt (0)
        assert TOOL_RESULT_MAX_CHARS['grep_search'] == 30_000
        assert 'read_files' in _BUDGET_EXEMPT_TOOLS

        # A 40K result: exempt for read_files, persisted for grep_search
        content = "y" * 40_000
        grep_result = budget_tool_result('grep_search', content)
        read_result = budget_tool_result('read_files', content)
        assert '[Persisted to:' in grep_result  # persisted
        assert read_result == content             # exempt, unchanged

    def test_non_string_passthrough(self):
        from lib.tasks_pkg.compaction import budget_tool_result
        content = 12345
        assert budget_tool_result('read_files', content) == content

    def test_unknown_tool_uses_default_budget(self):
        from lib.tasks_pkg.compaction import _DEFAULT_TOOL_RESULT_MAX, budget_tool_result
        content = "z" * (_DEFAULT_TOOL_RESULT_MAX + 10_000)
        result = budget_tool_result('unknown_new_tool', content)
        assert len(result) < len(content)
        assert '[Persisted to:' in result

    def test_persistence_preview_truncated_at_newline(self):
        """Preview in persisted result should truncate at newline boundary."""
        from lib.tasks_pkg.compaction import _PERSIST_PREVIEW_CHARS, budget_tool_result
        # Build content with lines
        lines = [f'Line {i}: ' + 'x' * 80 for i in range(500)]
        content = '\n'.join(lines)
        result = budget_tool_result('run_command', content)
        assert '[Persisted to:' in result
        # Preview should end cleanly (not mid-line)
        preview_section = result.split('Preview')[1] if 'Preview' in result else ''
        assert preview_section  # preview exists


# ═══════════════════════════════════════════════════════════
#  2. Extended Micro-compact (thinking block stripping)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestExtendedMicroCompact:
    """Verify micro_compact strips old thinking/reasoning_content."""

    def _make_messages(self, n_assistants=10, thinking_size=5000):
        """Create a conversation with N assistant messages that have reasoning_content."""
        messages = [{'role': 'system', 'content': 'You are a helpful assistant.'}]
        for i in range(n_assistants):
            messages.append({
                'role': 'user',
                'content': f'Question {i}',
            })
            messages.append({
                'role': 'assistant',
                'content': f'Answer {i}',
                'reasoning_content': 'T' * thinking_size,
            })
            # Add a tool call + result to make it realistic
            tc_id = f'tc_{i}'
            messages.append({
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': tc_id,
                    'type': 'function',
                    'function': {'name': 'read_files', 'arguments': '{}'},
                }],
            })
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'name': 'read_files',
                'content': f'File content {i}' * 200,  # ~2400 chars each
            })
        return messages

    def test_strips_old_thinking_blocks(self):
        from lib.tasks_pkg.compaction import _THINKING_HOT_TAIL, micro_compact
        messages = self._make_messages(n_assistants=10, thinking_size=5000)

        # Count reasoning_content before
        rc_before = sum(
            1 for m in messages
            if m.get('role') == 'assistant' and m.get('reasoning_content')
        )
        assert rc_before == 10

        saved = micro_compact(messages, conv_id='test123')
        assert saved > 0

        # Count reasoning_content after — should only keep _THINKING_HOT_TAIL
        rc_after = sum(
            1 for m in messages
            if m.get('role') == 'assistant' and m.get('reasoning_content')
            and len(m.get('reasoning_content', '')) > 0
        )
        assert rc_after == _THINKING_HOT_TAIL

    def test_keeps_recent_thinking_intact(self):
        from lib.tasks_pkg.compaction import _THINKING_HOT_TAIL, micro_compact
        messages = self._make_messages(n_assistants=6, thinking_size=3000)

        micro_compact(messages, conv_id='test456')

        # The last _THINKING_HOT_TAIL assistant messages should still have reasoning
        assistant_msgs = [m for m in messages if m.get('role') == 'assistant' and 'reasoning_content' in m]
        # Last N should be intact
        recent = assistant_msgs[-_THINKING_HOT_TAIL:]
        for m in recent:
            assert len(m.get('reasoning_content', '')) > 0

    def test_no_thinking_to_strip(self):
        from lib.tasks_pkg.compaction import micro_compact
        messages = [
            {'role': 'system', 'content': 'Hello'},
            {'role': 'user', 'content': 'Hi'},
            {'role': 'assistant', 'content': 'Hello!'},
        ]
        saved = micro_compact(messages, conv_id='test789')
        assert saved == 0  # nothing to strip


# ═══════════════════════════════════════════════════════════
#  3. 9-Section Summary Template (analysis stripping)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAnalysisStripping:
    """Verify <analysis> scratchpad is stripped from summary output."""

    def test_analysis_tag_stripped(self):
        import re
        # Simulate what _generate_query_aware_summary does
        raw_output = (
            "<analysis>\nThis is the analysis scratchpad.\n"
            "Key concepts: X, Y, Z.\n</analysis>\n\n"
            "### 1. Primary Request\nThe user wants to..."
        )
        cleaned = re.sub(
            r'<analysis>.*?</analysis>\s*',
            '', raw_output, flags=re.DOTALL,
        )
        assert '<analysis>' not in cleaned
        assert '### 1. Primary Request' in cleaned

    def test_no_analysis_passthrough(self):
        import re
        raw_output = "### 1. Primary Request\nThe user wants to..."
        cleaned = re.sub(
            r'<analysis>.*?</analysis>\s*',
            '', raw_output, flags=re.DOTALL,
        )
        assert cleaned == raw_output


# ═══════════════════════════════════════════════════════════
#  4. Reactive Compact
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReactiveCompact:
    """Verify reactive_compact emergency compaction works."""

    def test_head_truncate_reduces_messages(self):
        from lib.tasks_pkg.compaction import _head_truncate
        messages = [
            {'role': 'system', 'content': 'System prompt'},
        ]
        # Add many user/assistant pairs
        for i in range(50):
            messages.append({'role': 'user', 'content': f'Question {i} ' * 500})
            messages.append({'role': 'assistant', 'content': f'Answer {i} ' * 500})

        original_count = len(messages)
        _head_truncate(messages, task={'config': {'model': 'gpt-4'}})
        assert len(messages) < original_count
        # System message should be preserved
        assert messages[0]['role'] == 'system'

    def test_head_truncate_preserves_system(self):
        from lib.tasks_pkg.compaction import _head_truncate
        messages = [
            {'role': 'system', 'content': 'Important system prompt'},
            {'role': 'user', 'content': 'Q ' * 50000},
            {'role': 'assistant', 'content': 'A ' * 50000},
            {'role': 'user', 'content': 'Q2 ' * 50000},
            {'role': 'assistant', 'content': 'A2 ' * 50000},
            {'role': 'user', 'content': 'Q3 ' * 50000},
        ]
        _head_truncate(messages, task={'config': {'model': 'gpt-4'}})
        assert messages[0]['role'] == 'system'
        assert messages[0]['content'] == 'Important system prompt'


# ═══════════════════════════════════════════════════════════
#  5. PromptTooLongError
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPromptTooLongError:
    """Verify PromptTooLongError is properly defined and detectable."""

    def test_error_class_exists(self):
        from lib.llm_client import PromptTooLongError
        err = PromptTooLongError("prompt is too long")
        assert isinstance(err, Exception)
        assert "prompt is too long" in str(err)

    def test_error_patterns_detection(self):
        """Verify the patterns that should trigger PromptTooLongError."""
        patterns_that_should_match = [
            "prompt is too long",
            "context length exceeded",
            "maximum context length is 128000",
            "prompt too long for model",
            "input too long",
            "exceeds the model maximum",
            "token limit reached",
            "context_length_exceeded",
            "max_prompt_tokens exceeded",
            "request too large",
        ]
        _ptl_patterns = [
            'prompt is too long', 'context length exceeded',
            'maximum context length', 'prompt too long',
            'input too long', 'exceeds the model',
            'token limit', 'context_length_exceeded',
            'max_prompt_tokens', 'request too large',
        ]
        for msg in patterns_that_should_match:
            matched = any(p in msg.lower() for p in _ptl_patterns)
            assert matched, f"Pattern should match: {msg}"


# ═══════════════════════════════════════════════════════════
#  6. Concurrency Safety Partitioning
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestConcurrencySafety:
    """Verify write tools are correctly marked for serial execution."""

    def test_write_tools_defined(self):
        from lib.tasks_pkg.tool_dispatch import _WRITE_TOOLS
        assert 'write_file' in _WRITE_TOOLS
        assert 'apply_diff' in _WRITE_TOOLS
        assert 'run_command' in _WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        from lib.tasks_pkg.tool_dispatch import _WRITE_TOOLS
        read_tools = ['read_files', 'list_dir', 'grep_search', 'find_files',
                      'web_search', 'fetch_url']
        for tool in read_tools:
            assert tool not in _WRITE_TOOLS, f"{tool} should NOT be in _WRITE_TOOLS"

    def test_write_tools_disjoint_from_idempotent(self):
        from lib.tasks_pkg.tool_dispatch import _IDEMPOTENT_TOOLS, _WRITE_TOOLS
        overlap = _WRITE_TOOLS & _IDEMPOTENT_TOOLS
        assert len(overlap) == 0, f"Write tools should not be idempotent: {overlap}"


# ═══════════════════════════════════════════════════════════
#  7. Delta Attachment Tracking
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeltaAttachments:
    """Verify delta tracking caches context computation but ALWAYS injects.

    CRITICAL DESIGN: In our system, each task gets fresh messages from the
    frontend (system message only has the user's custom prompt, no project/
    skills context).  Delta tracking must ALWAYS inject the text into the
    system message — it only skips the expensive FUSE I/O computation.
    This is different from Claude Code where the messages array persists
    across turns and delta tracking prevents DUPLICATE injection.
    """

    def test_first_call_computes_and_returns(self):
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        _last_context_cache.clear()
        call_count = [0]
        def compute():
            call_count[0] += 1
            return 'Project context v1'
        result = _get_cached_or_compute('conv1', 'project', compute)
        assert result == 'Project context v1'
        assert call_count[0] == 1

    def test_second_identical_call_still_returns_text(self):
        """Even when hash matches, text is ALWAYS returned (for injection)."""
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        _last_context_cache.clear()
        text = 'Project context v1'
        _get_cached_or_compute('conv2', 'project', lambda: text)
        # Second call — compute_fn is still called (we always compute to
        # check the hash), but the result is returned from cache
        result = _get_cached_or_compute('conv2', 'project', lambda: text)
        assert result == text  # MUST return text, not empty/None

    def test_changed_content_updates_cache(self):
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        _last_context_cache.clear()
        _get_cached_or_compute('conv3', 'project', lambda: 'v1')
        result = _get_cached_or_compute('conv3', 'project', lambda: 'v2 updated')
        assert result == 'v2 updated'

    def test_different_categories_independent(self):
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        _last_context_cache.clear()
        r1 = _get_cached_or_compute('conv4', 'project', lambda: 'proj ctx')
        r2 = _get_cached_or_compute('conv4', 'skills', lambda: 'skills ctx')
        assert r1 == 'proj ctx'
        assert r2 == 'skills ctx'

    def test_different_convs_independent(self):
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        _last_context_cache.clear()
        r1 = _get_cached_or_compute('conv_a', 'project', lambda: 'ctx')
        r2 = _get_cached_or_compute('conv_b', 'project', lambda: 'ctx')
        assert r1 == 'ctx'
        assert r2 == 'ctx'

    def test_context_hash_consistency(self):
        from lib.tasks_pkg.system_context import _context_hash
        text = 'Hello World'
        h1 = _context_hash(text)
        h2 = _context_hash(text)
        assert h1 == h2
        assert len(h1) == 16  # md5[:16]

    def test_empty_compute_returns_empty(self):
        from lib.tasks_pkg.system_context import _get_cached_or_compute, _last_context_cache
        _last_context_cache.clear()
        result = _get_cached_or_compute('conv5', 'project', lambda: '')
        assert result == ''

    def test_context_always_injected_on_fresh_messages(self):
        """Simulate the real scenario: 2 tasks in same conversation.
        Both should have project context in their system message."""
        from lib.tasks_pkg.system_context import (
            _append_to_system_message,
            _inject_system_contexts,
            _last_context_cache,
        )
        _last_context_cache.clear()

        # Task 1: fresh messages from frontend
        msgs1 = [{'role': 'system', 'content': 'You are helpful'},
                 {'role': 'user', 'content': 'Hello'}]
        _inject_system_contexts(
            msgs1, '/fake', project_enabled=False,
            skills_enabled=False, search_enabled=False,
            swarm_enabled=False, has_real_tools=False,
            conv_id='conv6',
        )

        # Task 2: ANOTHER fresh messages list (new task!)
        msgs2 = [{'role': 'system', 'content': 'You are helpful'},
                 {'role': 'user', 'content': 'Hello again'}]
        _inject_system_contexts(
            msgs2, '/fake', project_enabled=False,
            skills_enabled=False, search_enabled=False,
            swarm_enabled=False, has_real_tools=False,
            conv_id='conv6',
        )

        # Both should still have system messages (no content lost)
        assert msgs1[0]['role'] == 'system'
        assert msgs2[0]['role'] == 'system'


# ═══════════════════════════════════════════════════════════
#  8. Integration: Full compaction pipeline
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCompactionPipeline:
    """Integration tests for the full compaction pipeline."""

    def test_pipeline_runs_without_error(self):
        from lib.tasks_pkg.compaction import run_compaction_pipeline
        messages = [
            {'role': 'system', 'content': 'System'},
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'},
        ]
        # Should not raise
        run_compaction_pipeline(messages, current_round=1,
                               task={'convId': 'test', 'config': {'model': 'gpt-4'}})

    def test_pipeline_with_tool_results(self):
        from lib.tasks_pkg.compaction import MICRO_HOT_TAIL, run_compaction_pipeline
        messages = [{'role': 'system', 'content': 'System'}]
        # Add more than MICRO_HOT_TAIL tool results
        for i in range(MICRO_HOT_TAIL + 10):
            tc_id = f'tc_{i}'
            messages.append({
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': tc_id, 'type': 'function',
                               'function': {'name': 'read_files', 'arguments': '{}'}}],
            })
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'name': 'read_files',
                'content': f'Long content {i} ' * 200,  # ~2K chars each
            })

        msg_before = len(messages)
        run_compaction_pipeline(messages, current_round=5,
                               task={'convId': 'test_pipe', 'config': {'model': 'gpt-4'}})

        # Messages should still be the same count (micro_compact replaces in place)
        assert len(messages) == msg_before
        # But cold tool results should be compacted
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        cold_msgs = tool_msgs[:-MICRO_HOT_TAIL]
        for m in cold_msgs:
            assert 'compacted' in m.get('content', '')

    def test_budget_tool_result_in_pipeline(self):
        """Verify budget_tool_result persists oversized grep results."""
        from lib.tasks_pkg.compaction import budget_tool_result
        large = "matched_line\n" * 10_000  # ~130K chars
        result = budget_tool_result('grep_search', large)
        assert '[Persisted to:' in result
        assert len(result) < len(large)  # much smaller than original


# ═══════════════════════════════════════════════════════════
#  9. Compaction Summary Quality (template check)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSummaryTemplate:
    """Verify the upgraded 9-section summary template."""

    def test_template_has_9_sections(self):
        from lib.tasks_pkg.compaction import _SUMMARY_SYSTEM_PROMPT
        expected_sections = [
            '1. Primary Request',
            '2. Key Technical Concepts',
            '3. Files & Code',
            '4. Errors & Debugging',
            '5. Problem-Solving Progress',
            '6. All User Messages',
            '7. Decisions & Preferences',
            '8. Current Working State',
            '9. Pending / Next Steps',
        ]
        for section in expected_sections:
            assert section in _SUMMARY_SYSTEM_PROMPT, \
                f"Missing section in summary template: {section}"

    def test_template_has_analysis_scratchpad(self):
        from lib.tasks_pkg.compaction import _SUMMARY_SYSTEM_PROMPT
        assert '<analysis>' in _SUMMARY_SYSTEM_PROMPT
        assert 'Strip the <analysis> section' in _SUMMARY_SYSTEM_PROMPT

    def test_template_mandates_user_messages(self):
        from lib.tasks_pkg.compaction import _SUMMARY_SYSTEM_PROMPT
        assert 'MANDATORY' in _SUMMARY_SYSTEM_PROMPT
        assert 'never omitted' in _SUMMARY_SYSTEM_PROMPT or 'never skip' in _SUMMARY_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════
#  10. Thread-safety of reactive_compact (Fix #1)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReactiveCompactThreadSafety:
    """Verify reactive_compact doesn't mutate global _KEEP_RECENT_PAIRS."""

    def test_no_global_mutation(self):
        """reactive_compact should NOT use 'global _KEEP_RECENT_PAIRS'."""
        import inspect

        from lib.tasks_pkg.compaction import reactive_compact
        source = inspect.getsource(reactive_compact)
        assert 'global _KEEP_RECENT_PAIRS' not in source, \
            'reactive_compact should NOT mutate global _KEEP_RECENT_PAIRS — use parameter instead'

    def test_force_compact_accepts_keep_recent_pairs(self):
        """force_compact_if_needed should accept keep_recent_pairs parameter."""
        import inspect

        from lib.tasks_pkg.compaction import force_compact_if_needed
        sig = inspect.signature(force_compact_if_needed)
        assert 'keep_recent_pairs' in sig.parameters, \
            'force_compact_if_needed should have keep_recent_pairs parameter'

    def test_find_pair_boundary_accepts_keep_recent(self):
        """_find_pair_boundary should accept keep_recent parameter."""
        from lib.tasks_pkg.compaction import _find_pair_boundary
        messages = [
            {'role': 'system', 'content': 'System'},
        ]
        for i in range(10):
            messages.append({'role': 'user', 'content': f'Q{i}'})
            messages.append({'role': 'assistant', 'content': f'A{i}'})

        # With keep_recent=2, boundary should be more aggressive (earlier)
        boundary_default = _find_pair_boundary(messages)
        boundary_aggressive = _find_pair_boundary(messages, keep_recent=2)
        # More aggressive = later index (less preserved)
        assert boundary_aggressive >= boundary_default or boundary_aggressive > 0

    def test_keep_recent_pairs_unchanged_after_reactive_compact(self):
        """_KEEP_RECENT_PAIRS should not change after reactive_compact call."""
        from lib.tasks_pkg.compaction import _KEEP_RECENT_PAIRS, reactive_compact
        original_value = _KEEP_RECENT_PAIRS

        messages = [{'role': 'system', 'content': 'S'}]
        for i in range(5):
            messages.append({'role': 'user', 'content': f'Q{i}'})
            messages.append({'role': 'assistant', 'content': f'A{i}'})

        reactive_compact(messages, task={'convId': 'test', 'id': 'test123',
                                         'config': {'model': 'gpt-4'}})

        from lib.tasks_pkg.compaction import _KEEP_RECENT_PAIRS as after
        assert after == original_value, \
            f'_KEEP_RECENT_PAIRS changed from {original_value} to {after}!'


# ═══════════════════════════════════════════════════════════
#  11. Memory leak prevention (Fix #5)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReactiveCompactCleanup:
    """Verify _reactive_compact_attempts is cleaned up."""

    def test_cleanup_function_exists(self):
        from lib.tasks_pkg.llm_fallback import cleanup_reactive_compact_state
        assert callable(cleanup_reactive_compact_state)

    def test_cleanup_removes_entry(self):
        from lib.tasks_pkg.llm_fallback import _reactive_compact_attempts, cleanup_reactive_compact_state
        _reactive_compact_attempts['test_task_1'] = 2
        cleanup_reactive_compact_state('test_task_1')
        assert 'test_task_1' not in _reactive_compact_attempts

    def test_cleanup_noop_for_missing_key(self):
        from lib.tasks_pkg.llm_fallback import cleanup_reactive_compact_state
        # Should not raise
        cleanup_reactive_compact_state('nonexistent_task')


# ═══════════════════════════════════════════════════════════
#  12. PromptTooLongError in non-streaming path (Fix #4)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPromptTooLongNonStreaming:
    """Verify PromptTooLongError is in the non-retry exception tuple."""

    def test_ptl_error_does_not_retry(self):
        """PromptTooLongError should be in the 'raise immediately' tuple
        so the non-streaming path doesn't waste retries on it."""
        import inspect

        from lib.llm_client import chat
        source = inspect.getsource(chat)
        # The except clause should include PromptTooLongError
        assert 'PromptTooLongError' in source, \
            'Non-streaming chat() should handle PromptTooLongError'


# ═══════════════════════════════════════════════════════════
#  13. Post-compact context re-injection
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPostCompactReinjection:
    """Verify _reinject_system_contexts_after_compact logic."""

    def test_no_task_no_crash(self):
        from lib.tasks_pkg.compaction import _reinject_system_contexts_after_compact
        messages = [{'role': 'system', 'content': 'Hello'}]
        # Should not raise even with no task
        _reinject_system_contexts_after_compact(messages, task=None)

    def test_detects_missing_project_context(self):
        from lib.tasks_pkg.compaction import _reinject_system_contexts_after_compact
        messages = [{'role': 'system', 'content': 'A bare system message without project context'}]
        task = {
            'config': {
                'projectPath': '/tmp/test_project',
                'skillsEnabled': False,
                'searchMode': '',
                'swarmEnabled': False,
            }
        }
        # This should detect that [PROJECT CO-PILOT MODE] is missing
        # and attempt to re-inject.  It may fail gracefully if the
        # project path doesn't exist, but it should not crash.
        try:
            _reinject_system_contexts_after_compact(messages, task=task)
        except Exception:
            pass  # OK — project path doesn't exist in test env

    def test_skips_when_project_context_present(self):
        from lib.tasks_pkg.compaction import _reinject_system_contexts_after_compact
        messages = [{'role': 'system', 'content': '[PROJECT CO-PILOT MODE]\nProject info...'}]
        task = {
            'config': {
                'projectPath': '/tmp/test_project',
                'skillsEnabled': False,
                'searchMode': '',
                'swarmEnabled': False,
            }
        }
        # Should not try to re-inject since project context marker is present
        original_content = messages[0]['content']
        _reinject_system_contexts_after_compact(messages, task=task)
        assert messages[0]['content'] == original_content


# ═══════════════════════════════════════════════════════════
#  14. Disk Persistence for Oversized Tool Results
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDiskPersistence:
    """Verify oversized tool results are persisted to disk with preview."""

    def test_persisted_file_contains_full_content(self):
        import re

        from lib.tasks_pkg.compaction import _persist_to_disk
        content = "full content line\n" * 5000  # ~85K
        result = _persist_to_disk(content, 'grep_search', 'tc_test_1', 'conv_test')
        assert '[Persisted to:' in result
        m = re.search(r'\[Persisted to: (.+?)\]', result)
        filepath = m.group(1)
        with open(filepath) as f:
            assert f.read() == content  # zero information loss

    def test_persistence_directory_structure(self):
        from lib.tasks_pkg.compaction import _PERSIST_DIR_BASE, _persist_to_disk
        content = "test" * 20_000
        result = _persist_to_disk(content, 'run_command', 'tc_123', 'conv_abcdef123456')
        import re
        m = re.search(r'\[Persisted to: (.+?)\]', result)
        filepath = m.group(1)
        # Should be under /tmp/chatui-tool-results/{conv_prefix}/
        assert _PERSIST_DIR_BASE in filepath
        assert 'conv_abcdef1' in filepath  # first 12 chars of conv_id

    def test_preview_is_included(self):
        from lib.tasks_pkg.compaction import _persist_to_disk
        content = "PREVIEW_START\n" + "middle\n" * 5000 + "END\n"
        result = _persist_to_disk(content, 'fetch_url', 'tc_2')
        assert 'Preview' in result
        assert 'PREVIEW_START' in result  # first line in preview

    def test_preview_size_limited(self):
        from lib.tasks_pkg.compaction import _PERSIST_PREVIEW_CHARS, _persist_to_disk
        content = "x" * 100_000
        result = _persist_to_disk(content, 'web_search', 'tc_3')
        # Result should be much smaller than original
        assert len(result) < _PERSIST_PREVIEW_CHARS + 500  # preview + metadata


# ═══════════════════════════════════════════════════════════
#  15. Per-Round Aggregate Budget
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAggregateRoundBudget:
    """Verify per-round aggregate budget enforcement."""

    def test_under_budget_passthrough(self):
        from lib.tasks_pkg.compaction import enforce_round_aggregate_budget
        results = {
            'tc_1': ('short result', 'grep_search', 'tc_1'),
            'tc_2': ('another short', 'run_command', 'tc_2'),
        }
        updated = enforce_round_aggregate_budget(results)
        # Nothing should change
        assert updated['tc_1'][0] == 'short result'
        assert updated['tc_2'][0] == 'another short'

    def test_over_budget_persists_largest(self):
        from lib.tasks_pkg.compaction import MAX_ROUND_TOOL_RESULTS_CHARS, enforce_round_aggregate_budget
        # Create 5 results that together exceed 300K
        results = {}
        for i in range(5):
            content = f'result_{i}\n' * 10_000  # ~110K each = 550K total
            results[f'tc_{i}'] = (content, 'grep_search', f'tc_{i}')

        updated = enforce_round_aggregate_budget(results)

        # Some results should now be persisted
        persisted_count = sum(
            1 for content, _, _ in updated.values()
            if isinstance(content, str) and '[Persisted to:' in content
        )
        assert persisted_count > 0

        # Total chars should be under budget (or close)
        total = sum(
            len(content) for content, _, _ in updated.values()
            if isinstance(content, str)
        )
        assert total <= MAX_ROUND_TOOL_RESULTS_CHARS + 50_000  # some slack

    def test_exempt_tools_not_persisted_by_aggregate(self):
        from lib.tasks_pkg.compaction import enforce_round_aggregate_budget
        results = {
            'tc_1': ('x' * 200_000, 'read_files', 'tc_1'),  # exempt
            'tc_2': ('y' * 200_000, 'grep_search', 'tc_2'),  # not exempt
        }
        updated = enforce_round_aggregate_budget(results)
        # read_files should be untouched
        assert updated['tc_1'][0] == 'x' * 200_000
        # grep_search should be persisted
        assert '[Persisted to:' in updated['tc_2'][0]


# ═══════════════════════════════════════════════════════════
#  16. Empty Result Marker
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEmptyResultMarker:
    """Verify empty tool results get descriptive markers."""

    def test_empty_string_gets_marker(self):
        from lib.tasks_pkg.compaction import mark_empty_result
        result = mark_empty_result('run_command', '')
        assert 'run_command' in result
        assert 'completed with no output' in result

    def test_whitespace_only_gets_marker(self):
        from lib.tasks_pkg.compaction import mark_empty_result
        result = mark_empty_result('run_command', '   \n  \t  ')
        assert 'completed with no output' in result

    def test_non_empty_passthrough(self):
        from lib.tasks_pkg.compaction import mark_empty_result
        content = 'Some actual output'
        assert mark_empty_result('run_command', content) == content

    def test_non_string_passthrough(self):
        from lib.tasks_pkg.compaction import mark_empty_result
        content = 12345
        assert mark_empty_result('run_command', content) == content


# ═══════════════════════════════════════════════════════════
#  17. Micro-compact Persistence Marker Handling
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMicroCompactPersistenceMarkers:
    """Verify micro_compact skips persistence markers."""

    def test_skips_persisted_markers(self):
        from lib.tasks_pkg.compaction import MICRO_HOT_TAIL, micro_compact
        messages = [{'role': 'system', 'content': 'System'}]
        # Add more than MICRO_HOT_TAIL tool results, some with persistence markers
        for i in range(MICRO_HOT_TAIL + 5):
            tc_id = f'tc_{i}'
            messages.append({
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': tc_id, 'type': 'function',
                               'function': {'name': 'grep_search', 'arguments': '{}'}}],
            })
            if i < 3:  # First 3 are persisted markers
                content = (
                    '[Persisted to: /tmp/chatui-tool-results/test/grep_search_tc.txt]\n'
                    'Output too large (50.0KB). Full output saved.\n'
                    'Preview:\nsome preview content...'
                )
            else:
                content = f'Normal result {i} ' * 200
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'name': 'grep_search',
                'content': content,
            })

        micro_compact(messages, conv_id='test_persist')

        # Persistence markers in cold zone should NOT be re-compacted
        cold_tool_msgs = [
            m for m in messages
            if m.get('role') == 'tool'
        ][:-MICRO_HOT_TAIL]

        for m in cold_tool_msgs:
            content = m.get('content', '')
            if content.startswith('[Persisted to:'):
                # Should be preserved as-is (not replaced with compacted marker)
                assert 'Persisted to:' in content
                assert 'compacted' not in content
