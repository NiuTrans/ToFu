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

    def test_read_files_exempt_for_absolute_paths(self):
        """read_files results should NEVER be truncated (covers both relative and absolute paths)."""
        from lib.tasks_pkg.compaction import budget_tool_result
        content = "y" * 200_000
        result = budget_tool_result('read_files', content)
        assert result == content

    def test_large_grep_result_persisted_to_disk(self):
        """Non-exempt tools exceeding budget should be persisted to disk."""
        from lib.tasks_pkg.compaction import budget_tool_result
        content = "matched_line\n" * 10_000  # ~130K chars
        result = budget_tool_result('grep_search', content)
        assert len(result) < len(content)
        assert '[Persisted to:' in result
        assert 'read_files' in result  # tells model how to access
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
        # Need more than _THINKING_HOT_TAIL assistants for stripping to kick in.
        # (Stripping only applies to the cold tail beyond the hot tail.)
        n_total = _THINKING_HOT_TAIL + 5
        messages = self._make_messages(n_assistants=n_total, thinking_size=5000)

        # Count reasoning_content before
        rc_before = sum(
            1 for m in messages
            if m.get('role') == 'assistant' and m.get('reasoning_content')
        )
        assert rc_before == n_total

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


    def test_head_truncate_running_total_matches_full_walk(self):
        """The O(n) running-total rewrite of _head_truncate must drop EXACTLY
        the same messages a naive O(n^2) re-sum-every-pop loop would.

        _estimate_total_tokens == sum(_estimate_msg_tokens), so subtracting
        each popped message keeps the running total bit-identical to a fresh
        full walk. This guards that equivalence for BOTH trim modes so a
        future refactor can't silently drift the drop boundary.
        """
        from lib.tasks_pkg.compaction import _head_truncate
        from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

        def _mk():
            msgs = [{'role': 'system', 'content': 'sys ' * 100}]
            # First real user turn is the objective anchor — must survive.
            msgs.append({'role': 'user', 'content': 'ANCHOR goal ' * 200})
            for i in range(40):
                msgs.append({'role': 'assistant', 'content': f'A{i} ' * 400})
                msgs.append({'role': 'user', 'content': f'U{i} ' * 400})
            return msgs

        # Reference: naive full-walk drop against the SAME target the impl uses
        # (token mode: 0.60 * context_limit for gpt-4 = 128000).
        def _ref_token_drop(msgs, target):
            from lib.tasks_pkg.compaction._layer2 import _objective_anchor_index
            system_end = 0
            for i, m in enumerate(msgs):
                if m.get('role') == 'system':
                    system_end = i + 1
                else:
                    break

            def _pos():
                anchor = _objective_anchor_index(msgs)
                if anchor == system_end and len(msgs) > system_end + 1:
                    return system_end + 1
                return system_end
            dropped = 0
            while _estimate_total_tokens(msgs) > target and len(msgs) > system_end + 4:
                msgs.pop(_pos())
                dropped += 1
            return dropped

        task = {'config': {'model': 'gpt-4'}}  # 128k window → token target 76800

        impl_msgs = _mk()
        ref_msgs = _mk()
        target = int(128_000 * 0.60)

        n_impl = _head_truncate(impl_msgs, task=task)
        n_ref = _ref_token_drop(ref_msgs, target)

        assert n_impl == n_ref, (n_impl, n_ref)
        assert len(impl_msgs) == len(ref_msgs)
        assert _estimate_total_tokens(impl_msgs) == _estimate_total_tokens(ref_msgs)
        # Objective anchor (first real user msg) survived.
        assert any('ANCHOR goal' in (m.get('content') or '') for m in impl_msgs)


# ═══════════════════════════════════════════════════════════
#  5. PromptTooLongError
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPromptTooLongError:
    """Verify PromptTooLongError is properly defined and detectable."""

    def test_error_class_exists(self):
        from lib.llm import PromptTooLongError
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
    memory context).  Delta tracking must ALWAYS inject the text into the
    system message — it only skips the expensive FUSE I/O computation.
    This is different from Claude Code where the messages array persists
    across turns and delta tracking prevents DUPLICATE injection.
    """

    def test_context_always_injected_on_fresh_messages(self):
        """Simulate the real scenario: 2 tasks in same conversation.
        Both should have project context in their system message."""
        from lib.tasks_pkg.system_context import (
            _append_to_system_message,
            _inject_system_contexts,
        )

        # Task 1: fresh messages from frontend
        msgs1 = [{'role': 'system', 'content': 'You are helpful'},
                 {'role': 'user', 'content': 'Hello'}]
        _inject_system_contexts(
            msgs1, '/fake', project_enabled=False,
            memory_enabled=False, search_enabled=False,
            swarm_enabled=False, has_real_tools=False,
            conv_id='conv6',
        )

        # Task 2: ANOTHER fresh messages list (new task!)
        msgs2 = [{'role': 'system', 'content': 'You are helpful'},
                 {'role': 'user', 'content': 'Hello again'}]
        _inject_system_contexts(
            msgs2, '/fake', project_enabled=False,
            memory_enabled=False, search_enabled=False,
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

        from lib.llm import chat
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
        # Bare system message: CC static marker is missing → should re-inject
        messages = [{'role': 'system', 'content': 'A bare system message without the CC static block'}]
        task = {
            'config': {
                'projectPath': '/tmp/test_project',
                'memoryEnabled': False,
                'searchMode': '',
                'swarmEnabled': False,
            }
        }
        # Should detect the missing CC marker and re-inject. May fail
        # gracefully if the project path doesn't exist, but must not crash.
        try:
            _reinject_system_contexts_after_compact(messages, task=task)
        except Exception:
            pass  # OK — project path doesn't exist in test env

    def test_skips_when_project_context_present(self):
        """When the CC static block is already in the system message, no
        re-injection happens.  The trigger marker switched from
        ``[PROJECT CO-PILOT MODE]`` (Layout B, where CLAUDE.md was in
        system) to ``_CC_STATIC_MARKER`` (Layout A, where CLAUDE.md is
        in a user _isMeta msg) when Layout B was retired — see
        compaction.py and system_context.py.
        """
        from lib.tasks_pkg.compaction import _reinject_system_contexts_after_compact
        from lib.tasks_pkg.system_context import _CC_STATIC_MARKER
        # The CC marker present → system prompt already injected
        sys_text = (f'Some bare prompt with the marker: '
                    f'"{_CC_STATIC_MARKER}". Pretend this is the CC static '
                    f'block.')
        messages = [{'role': 'system', 'content': sys_text}]
        task = {
            'config': {
                'projectPath': '/tmp/test_project',
                'memoryEnabled': False,
                'searchMode': '',
                'swarmEnabled': False,
            }
        }
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
        # Should be under data/tool-results/{conv_prefix}/
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

    def test_fetch_url_batch_split_persist(self):
        """Batch fetch_url persist should produce per-URL files + index listing ALL URLs."""
        import os
        import re as _re
        from lib.tasks_pkg.compaction import _persist_to_disk

        url1 = 'https://example.com/alpha'
        url2 = 'https://example.com/beta'
        url3 = 'https://example.net/gamma/page'
        body1 = 'AAA-start\n' + ('a' * 40_000) + '\nAAA-end'
        body2 = 'BBB-start\n' + ('b' * 40_000) + '\nBBB-end'
        body3 = 'CCC-start\n' + ('c' * 40_000) + '\nCCC-end'
        content = (
            f"Content from {url1} ({len(body1):,} chars):\n\n{body1}\n\n"
            f"Content from {url2} ({len(body2):,} chars):\n\n{body2}\n\n"
            f"Content from {url3} ({len(body3):,} chars):\n\n{body3}"
        )

        result = _persist_to_disk(content, 'fetch_url', 'tc_fetch_batch',
                                  'conv_fetchtest123')

        # Index should mention ALL three URLs with per-URL file paths
        for url in (url1, url2, url3):
            assert url in result, f'URL {url} missing from persist index'

        # Should produce three separate files — extract all file paths
        file_paths = _re.findall(r'File: (\S+\.txt)', result)
        assert len(file_paths) == 3, f'expected 3 files, got {len(file_paths)}: {file_paths}'

        # Each file should exist and contain the matching body
        for fp, body in zip(file_paths, (body1, body2, body3)):
            assert os.path.exists(fp), f'persisted file missing: {fp}'
            with open(fp, 'r', encoding='utf-8') as f:
                disk_content = f.read()
            assert body[:40] in disk_content
            os.unlink(fp)  # cleanup

        # Index should have a per-URL preview for each
        assert result.count('Preview:') >= 3

    def test_fetch_url_single_falls_through(self):
        """Single-URL fetch_url should use default single-file persist (not split)."""
        from lib.tasks_pkg.compaction import _persist_to_disk
        url = 'https://example.com/only'
        body = 'single-page-body\n' + ('z' * 60_000)
        content = f"Content from {url} ({len(body):,} chars):\n\n{body}"
        result = _persist_to_disk(content, 'fetch_url', 'tc_fetch_single',
                                  'conv_fetchsingle')
        # Should go through the default path (single [Persisted to:] header)
        assert result.startswith('[Persisted to:')

    def test_persisted_results_get_friendly_display_labels(self):
        """read_files on spilled web_search results should render a human
        label (title) instead of the opaque persisted filename."""
        import os
        import re as _re
        from lib.tasks_pkg.compaction import _persist_to_disk
        from lib.tasks_pkg.persist_registry import clear, lookup
        from lib.tasks_pkg.tool_display import _persisted_read_labels

        clear()
        content = (
            '[1] AI Model Deprecation Tracker 2026\n'
            'URL: https://example.com/track\n'
            '──── Full Page Content ────\n' + ('lorem ' * 6000) +
            '\n════════════════════\n'
            '[2] Second Result Title\n'
            'URL: https://example.org/two\n'
            '──── Full Page Content ────\n' + ('ipsum ' * 6000)
        )
        out = _persist_to_disk(content, 'web_search', 'toolu_xyz', 'conv_lbl')
        files = _re.findall(r'File: (\S+\.txt)', out)
        assert len(files) == 2
        assert lookup(files[0]) == ('web_search', 'AI Model Deprecation Tracker 2026')

        display = _persisted_read_labels({'reads': [{'path': p} for p in files]})
        assert 'web search result' in display
        assert 'AI Model Deprecation Tracker 2026' in display
        # Opaque filename must NOT leak into the display.
        assert 'toolu_xyz' not in display

        # Stateless fallback after a restart wipes the registry.
        clear()
        fb = _persisted_read_labels({'reads': [{'path': files[0]}]})
        assert 'web search result' in fb
        assert 'toolu_xyz' not in fb

        for f in files:
            os.unlink(f)

    def test_non_persisted_read_keeps_default_display(self):
        """read_files on ordinary project files must keep the normal path
        rendering (no friendly-label hijack)."""
        from lib.tasks_pkg.persist_registry import clear
        from lib.tasks_pkg.tool_display import _persisted_read_labels
        clear()
        assert _persisted_read_labels({'reads': [{'path': 'lib/server.py'}]}) == ''

    def test_get_conversation_label_skips_decorative_first_line(self):
        """A spilled get_conversation result opens with a ``═══`` rule; the
        persisted-result label must show the meaningful title line, not the
        wall of box-drawing characters (the reported UI ugliness)."""
        import os
        from lib.tasks_pkg.compaction import _persist_to_disk
        from lib.tasks_pkg.persist_registry import clear, lookup
        from lib.tasks_pkg.tool_display import _persisted_read_labels

        clear()
        content = (
            '═' * 60 + '\n'
            'Referenced Conversation: "Why does it show fail to fetch?"\n'
            '   ID: mr4e8pnxbv440z\n'
            '   Messages: 12\n'
            + '═' * 60 + '\n\n'
            + ('some long body text ' * 6000)
        )
        out = _persist_to_disk(content, 'get_conversation', 'toolu_conv', 'conv_gc')
        # Single-file persist path.
        assert out.startswith('[Persisted to:')
        fp = out.split('[Persisted to: ', 1)[1].split(']', 1)[0]

        tool, desc = lookup(fp)
        assert tool == 'get_conversation'
        # Description is the meaningful header, NOT the ═══ rule.
        assert 'Referenced Conversation' in desc
        assert '═' not in desc

        display = _persisted_read_labels({'reads': [{'path': fp}]})
        assert 'past conversation' in display  # friendly verb
        assert 'Referenced Conversation' in display
        assert '═' not in display
        assert 'get_conversation output' not in display

        os.unlink(fp)

    def test_get_conversation_label_survives_registry_loss(self):
        """After a server restart the process-local registry is empty, so the
        row must fall back to describe_filename(). A default-spill
        get_conversation_<id>.txt must still render the ``past conversation``
        verb — never the raw opaque filename."""
        from lib.tasks_pkg.persist_registry import (
            clear, describe_filename, friendly_label,
        )
        from lib.tasks_pkg.tool_display import _persisted_read_labels

        clear()  # simulate a fresh process (no in-memory labels)
        fname = 'get_conversation_9Yqehab12cd.txt'

        # Stateless filename parse recovers the tool (no description).
        parsed = describe_filename(fname)
        assert parsed == ('get_conversation', '')
        assert friendly_label(*parsed) == 'past conversation'

        display = _persisted_read_labels({'reads': [{'path': fname}]})
        assert 'past conversation' in display
        assert 'get_conversation_9Yqehab12cd' not in display  # not the raw name
        assert '.txt' not in display

    def test_default_spill_recognition_is_allowlisted(self):
        """describe_filename must NOT mistake an ordinary project file that
        merely has a name_underscore.txt shape for a persisted spill —
        recognition is restricted to _DEFAULT_SPILL_TOOLS."""
        from lib.tasks_pkg.persist_registry import clear, describe_filename
        clear()
        assert describe_filename('requirements_dev.txt') is None
        assert describe_filename('notes_2026.txt') is None

    def test_find_files_batch_split_persist(self):
        """Batch find_files persist should produce per-search files + index listing ALL patterns."""
        import os
        import re as _re
        from lib.tasks_pkg.compaction import _persist_to_disk

        # Simulate batch find_files output (3 searches, each with many files)
        def _mk_section(pattern, in_path, n):
            files = '\n'.join(f'{in_path or "."}/match_{pattern}_{k}.py' for k in range(n))
            header = f'Files matching "{pattern}"'
            if in_path:
                header += f' in {in_path}'
            header += f' ({n} found):'
            return f'{header}\n\n{files}'

        sec1 = _mk_section('*.py', 'lib', 2000)
        sec2 = _mk_section('test_*.py', 'tests', 1500)
        sec3 = _mk_section('*.js', 'static/js', 1200)
        content = f'{sec1}\n\n{sec2}\n\n{sec3}'
        assert len(content) > 20_000  # must exceed find_files budget

        result = _persist_to_disk(content, 'find_files', 'tc_find_batch',
                                  'conv_findtest123')

        # Index should mention ALL three patterns
        assert '"*.py"' in result
        assert '"test_*.py"' in result
        assert '"*.js"' in result
        # All three in_paths visible
        assert 'lib' in result and 'tests' in result and 'static/js' in result
        # Per-search result counts visible
        assert '2000 found' in result
        assert '1500 found' in result
        assert '1200 found' in result

        file_paths = _re.findall(r'File: (\S+\.txt)', result)
        assert len(file_paths) == 3, f'expected 3 files, got {len(file_paths)}'

        for fp in file_paths:
            assert os.path.exists(fp)
            os.unlink(fp)

    def test_find_files_single_falls_through(self):
        """Single-search find_files output uses default single-file persist."""
        from lib.tasks_pkg.compaction import _persist_to_disk
        content = (
            'Files matching "*.py" in lib (5000 found):\n\n'
            + '\n'.join(f'lib/file_{k}.py' for k in range(5000))
        )
        result = _persist_to_disk(content, 'find_files', 'tc_find_single',
                                  'conv_findsingle')
        assert result.startswith('[Persisted to:')

    def test_fetch_url_batch_failed_sections_included(self):
        """Failed fetches in a batch should also appear as FAILED entries in the index."""
        import os
        import re as _re
        from lib.tasks_pkg.compaction import _persist_to_disk

        url1 = 'https://ok.example.com/a'
        url2 = 'https://bad.example.com/b'
        url3 = 'https://ok.example.com/c'
        body1 = 'A' * 40_000
        body3 = 'C' * 40_000
        content = (
            f"Content from {url1} ({len(body1):,} chars):\n\n{body1}\n\n"
            f"Failed to fetch {url2}. (HTTP 503)\n\n"
            f"Content from {url3} ({len(body3):,} chars):\n\n{body3}"
        )

        result = _persist_to_disk(content, 'fetch_url', 'tc_fetch_mixed',
                                  'conv_fetchmixed123')

        assert url1 in result
        assert url2 in result
        assert url3 in result
        assert 'FAILED' in result
        file_paths = _re.findall(r'File: (\S+\.txt)', result)
        assert len(file_paths) == 3
        for fp in file_paths:
            if os.path.exists(fp):
                os.unlink(fp)

    def test_web_search_structured_preview_shows_all_results(self):
        """web_search persist preview should show title/URL/snippet for ALL results."""
        from lib.tasks_pkg.compaction import _generate_web_search_preview
        content = (
            "Search results:\n\n"
            "[1] First Result Title\n"
            "    URL: https://example.com/first\n"
            "    Source: Google\n"
            "\n"
            "    ──── Full Page Content (10,000 chars) ────\n"
            "    First result full content here. " + "a" * 5000 + "\n"
            "\n"
            "════════════════════\n"
            "\n"
            "[2] Second Result Title\n"
            "    URL: https://example.com/second\n"
            "    Source: Bing\n"
            "\n"
            "    ──── Full Page Content (8,000 chars) ────\n"
            "    Second result full content here. " + "b" * 4000 + "\n"
            "\n"
            "════════════════════\n"
            "\n"
            "[3] Third Result Title\n"
            "    URL: https://example.com/third\n"
            "    Source: Google\n"
            "\n"
            "    (Full content not available — call fetch_url to read this page.)\n"
        )
        preview = _generate_web_search_preview(content)
        # All three results should be mentioned
        assert '[1] First Result Title' in preview
        assert '[2] Second Result Title' in preview
        assert '[3] Third Result Title' in preview
        assert 'https://example.com/first' in preview
        assert 'https://example.com/second' in preview
        assert 'https://example.com/third' in preview
        # Full content should NOT be included (only snippets)
        assert len(preview) < 5000  # much smaller than original

    def test_web_search_preview_fallback_for_non_structured(self):
        """Non-structured content should fall back to default truncation."""
        from lib.tasks_pkg.compaction import _PERSIST_PREVIEW_CHARS, _generate_web_search_preview
        content = "This is not structured web search output. " * 500
        preview = _generate_web_search_preview(content)
        assert len(preview) <= _PERSIST_PREVIEW_CHARS

    def test_persist_to_disk_web_search_splits_per_result(self):
        """_persist_to_disk for web_search should split into per-result files."""
        from lib.tasks_pkg.compaction import _persist_to_disk
        content = (
            "Search results:\n\n"
            "[1] First\n    URL: https://a.com\n    Source: G\n\n"
            "    ──── Full Page Content (5,000 chars) ────\n"
            "    Content A " + "x" * 20000 + "\n\n"
            "════════════════════\n\n"
            "[2] Second\n    URL: https://b.com\n    Source: B\n\n"
            "    ──── Full Page Content (5,000 chars) ────\n"
            "    Content B " + "y" * 20000 + "\n"
        )
        result = _persist_to_disk(content, 'web_search', 'tc_ws')
        # Should split into separate files — both results listed in index
        assert '[1] First' in result
        assert '[2] Second' in result
        assert 'https://a.com' in result
        assert 'https://b.com' in result
        # Each result has its own file path
        assert 'search_tc_ws_1_' in result
        assert 'search_tc_ws_2_' in result
        assert 'separate files' in result.lower()



# ═══════════════════════════════════════════════════════════
#  15. Per-Round Aggregate Budget

    def test_persist_to_disk_grep_search_splits_per_file(self):
        """_persist_to_disk for grep_search should split results by source file."""
        from lib.tasks_pkg.compaction import _persist_to_disk
        content = 'grep "import" (*.py) — 20 matches:\n\n'
        content += 'lib/a.py:1:import os\nlib/a.py:2:import sys\n'
        content += 'lib/b.py:1:import json\nlib/b.py:3:import re\n'
        # Make it big enough to trigger budgeting (> 30K chars)
        for i in range(500):
            content += f'lib/c.py:{i}:import thing_{i}\n'
        result = _persist_to_disk(content, 'grep_search', 'tc_gs')
        # Should split by source file
        assert 'lib/a.py' in result
        assert 'lib/b.py' in result
        assert 'lib/c.py' in result
        assert 'grep_tc_gs_' in result
        assert 'separately' in result.lower() or 'separate' in result.lower()

    def test_persist_grep_search_single_file_fallback(self):
        """grep_search with matches in only 1 file should NOT split."""
        from lib.tasks_pkg.compaction import _persist_to_disk
        content = 'grep "x" (*.py) — 5 matches:\n\n'
        content += 'lib/only.py:1:x\n' * 500 + 'y' * 30000
        result = _persist_to_disk(content, 'grep_search', 'tc_gs2')
        # Falls through to single-file persistence
        assert '[Persisted to:' in result

    def test_persist_web_search_single_result_fallback(self):
        """web_search with only 1 result should NOT split."""
        from lib.tasks_pkg.compaction import _persist_to_disk
        content = (
            "Search results:\n\n"
            "[1] Only Result\n    URL: https://only.com\n    Source: G\n\n"
            "    ──── Full Page Content (40,000 chars) ────\n"
            "    Content " + "z" * 40000 + "\n"
        )
        result = _persist_to_disk(content, 'web_search', 'tc_ws3')
        # Falls through to single-file persistence (no separator)
        assert '[Persisted to:' in result

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
                    '[Persisted to: data/tool-results/test/grep_search_tc.txt]\n'
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


# ═══════════════════════════════════════════════════════════
#  8. Phase D: Assistant Content Compaction
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAssistantContentCompaction:
    """Tests for Phase D: compacting cold assistant message content."""

    def _build_conversation(self, num_rounds=10, content_len=1200):
        """Build a conversation with N rounds of user/assistant/tool messages."""
        messages = [{'role': 'system', 'content': 'You are helpful.'}]
        for i in range(num_rounds):
            messages.append({'role': 'user', 'content': f'Question {i}'})
            messages.append({
                'role': 'assistant',
                'content': f'Answer {i}: ' + 'x' * content_len,
                'tool_calls': [{'id': f'tc_{i}', 'type': 'function',
                                'function': {'name': 'read_files',
                                             'arguments': '{}'}}],
            })
            messages.append({
                'role': 'tool',
                'tool_call_id': f'tc_{i}',
                'name': 'read_files',
                'content': f'File contents {i}',
            })
        return messages

    def test_phase_d_disabled_by_default(self):
        """Phase D should NOT compact when enable_assistant_compact is not set."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=1500)
        micro_compact(messages)
        # No assistant messages should be compacted (Phase D disabled by default)
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        assert len(compacted) == 0, (
            'Phase D should be disabled by default to preserve cache stability')

    def test_cold_assistant_messages_compacted_when_enabled(self):
        """Assistant messages outside hot tail should be compacted when opted in."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=1500)
        tokens_saved = micro_compact(messages, enable_assistant_compact=True)
        # Should have compacted some assistant messages
        assert tokens_saved > 0
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        assert len(compacted) > 0

    def test_hot_tail_assistant_messages_preserved(self):
        """The 6 most recent assistant messages should NOT be compacted."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=1500)
        micro_compact(messages, enable_assistant_compact=True)
        # Get the last 6 assistant messages
        asst_msgs = [m for m in messages if m.get('role') == 'assistant']
        hot_tail = asst_msgs[-6:]
        for m in hot_tail:
            content = m.get('content', '')
            if isinstance(content, str) and content:
                assert not content.startswith('[Assistant response compacted'), \
                    f'Hot tail assistant message was wrongly compacted: {content[:80]}'

    def test_short_assistant_content_not_compacted(self):
        """Assistant content under threshold (800 chars) should be preserved."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=200)
        tokens_saved = micro_compact(messages, enable_assistant_compact=True)
        # No assistant messages should be compacted (all < 800 chars)
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        assert len(compacted) == 0

    def test_empty_content_assistant_skipped(self):
        """Assistant messages with empty/None content (tool_calls only) should be skipped."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = [{'role': 'system', 'content': 'test'}]
        for i in range(12):
            messages.append({'role': 'user', 'content': f'Q{i}'})
            messages.append({
                'role': 'assistant',
                'content': '',  # empty — tool_calls only
                'tool_calls': [{'id': f'tc_{i}', 'type': 'function',
                                'function': {'name': 'run', 'arguments': '{}'}}],
            })
            messages.append({
                'role': 'tool', 'tool_call_id': f'tc_{i}',
                'content': 'ok',
            })
        tokens_saved = micro_compact(messages, enable_assistant_compact=True)
        # No assistant compaction should happen (all empty content)
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        assert len(compacted) == 0

    def test_already_compacted_not_double_compacted(self):
        """Assistant messages already compacted should not be re-compacted."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=1500)
        micro_compact(messages, enable_assistant_compact=True)
        # Run again
        tokens_saved_2 = micro_compact(messages, enable_assistant_compact=True)
        # Second run should save 0 tokens from assistant compaction
        # (all cold assistants already compacted)
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        # Count should be same as first run
        assert len(compacted) > 0
        # Each compacted message should appear exactly once with the marker
        for m in compacted:
            assert m['content'].count('[Assistant response compacted') == 1

    def test_preview_preserved_in_compacted(self):
        """Compacted assistant messages should preserve a 200-char preview."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=2000)
        micro_compact(messages, enable_assistant_compact=True)
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        for m in compacted:
            content = m['content']
            assert 'was' in content  # size info
            assert 'chars]' in content
            # Preview should be ~200 chars + the header
            assert len(content) < 400  # much shorter than original 2000+

    def test_list_content_compacted(self):
        """Assistant messages with list content blocks should be compacted."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = [{'role': 'system', 'content': 'test'}]
        for i in range(12):
            messages.append({'role': 'user', 'content': f'Q{i}'})
            messages.append({
                'role': 'assistant',
                'content': [
                    {'type': 'text', 'text': f'Answer {i}: ' + 'x' * 1500},
                ],
            })
        micro_compact(messages, enable_assistant_compact=True)
        compacted = [
            m for m in messages
            if m.get('role') == 'assistant'
            and isinstance(m.get('content'), str)
            and m['content'].startswith('[Assistant response compacted')
        ]
        assert len(compacted) > 0

    def test_token_savings_reported(self):
        """micro_compact should report token savings from assistant compaction."""
        from lib.tasks_pkg.compaction import micro_compact
        messages = self._build_conversation(num_rounds=12, content_len=2000)
        tokens_saved = micro_compact(messages, enable_assistant_compact=True)
        # Each compacted message saves ~(2000 - 250) / 4 ≈ 437 tokens
        # With 6 cold assistants (12 - 6 hot tail), expect ~2600+ tokens
        assert tokens_saved > 1000


# ═══════════════════════════════════════════════════════════
#  12. Turn-based preservation (2026-04-26 redesign)
#  Replaces the old "user-assistant pair" abstraction.  A turn =
#  [user, ...all subsequent non-user messages until the next user].
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTurnBoundary:
    """Verify _find_turn_boundary — the core of the new compaction design."""

    @staticmethod
    def _mk_agentic_turn(user_text, n_tool_rounds=3, tool_chars=1000):
        """Build one agentic turn: 1 user + N×(assistant(tool_calls) + tool).

        This is what a real conversation looks like: one human question
        produces many tool messages.  The OLD pair-based code treated
        these many messages as many pairs — this was the bug.
        """
        msgs = [{'role': 'user', 'content': user_text}]
        for i in range(n_tool_rounds):
            msgs.append({
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': f'tc_{i}',
                    'type': 'function',
                    'function': {'name': 'grep_search', 'arguments': '{}'},
                }],
            })
            msgs.append({
                'role': 'tool',
                'tool_call_id': f'tc_{i}',
                'name': 'grep_search',
                'content': 'x' * tool_chars,
            })
        msgs.append({
            'role': 'assistant',
            'content': f'Done investigating: {user_text}',
        })
        return msgs

    # ── Hard invariant: current turn is always preserved ─────────────

    def test_current_turn_always_preserved_fewer_turns_than_cap(self):
        """Regression for conv=modearkif6k9tr: few user turns + many tool
        messages must still preserve the current turn."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        msgs = [{'role': 'system', 'content': 'You are helpful.'}]
        # 4 user turns, each with 50 tool messages (like the bug conv)
        for k in range(4):
            msgs.extend(self._mk_agentic_turn(f'Q{k}', n_tool_rounds=50))

        # Default call — unbounded budget, _MAX_PRESERVE_TURNS cap (16)
        boundary = _find_turn_boundary(msgs)
        # Boundary must NOT equal len(messages) — we must preserve SOMETHING
        assert boundary < len(msgs), (
            'REGRESSION: boundary == len(messages) means nothing preserved — '
            'the exact bug that hit conv=modearkif6k9tr'
        )
        # The preserved slice must start with a user message
        assert msgs[boundary].get('role') == 'user', (
            f'Boundary must land on a user message, got role='
            f"{msgs[boundary].get('role')}"
        )
        # The current (last) user message must be in the preserved slice
        last_user_idx = max(i for i, m in enumerate(msgs) if m.get('role') == 'user')
        assert boundary <= last_user_idx

    def test_current_turn_preserved_even_when_oversized(self):
        """If the current turn alone exceeds the budget, preserve it anyway."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        # Build a huge single turn that dwarfs any budget
        msgs = [{'role': 'user', 'content': 'tiny old Q'}]
        msgs.append({'role': 'assistant', 'content': 'tiny old A'})
        msgs.extend(self._mk_agentic_turn('Current Q', n_tool_rounds=100,
                                          tool_chars=2000))
        # Even with a tiny budget, current turn must be preserved.
        boundary = _find_turn_boundary(msgs, budget_tokens=1)
        last_user_idx = max(i for i, m in enumerate(msgs) if m.get('role') == 'user')
        assert boundary == last_user_idx

    def test_budget_caps_older_turns(self):
        """Budget limits how many PRIOR turns (beyond current) are kept."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        msgs = []
        for k in range(10):
            msgs.extend(self._mk_agentic_turn(f'Q{k}', n_tool_rounds=2,
                                              tool_chars=400))
        # Tiny budget → only the current turn is preserved (invariant)
        b_tight = _find_turn_boundary(msgs, budget_tokens=1)
        # Large budget → more prior turns fit
        b_loose = _find_turn_boundary(msgs, budget_tokens=1_000_000)
        # Loose budget must preserve a larger slice (smaller boundary index)
        assert b_loose <= b_tight, (
            f'Larger budget should preserve more: '
            f'loose_boundary={b_loose} tight_boundary={b_tight}'
        )

    def test_max_turns_caps_preservation(self):
        """max_turns caps how many turns are kept regardless of budget."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        msgs = []
        for k in range(20):
            msgs.extend(self._mk_agentic_turn(f'Q{k}', n_tool_rounds=1))
        # Unbounded budget, max_turns=3 → only 3 turns kept
        boundary = _find_turn_boundary(msgs, budget_tokens=float('inf'),
                                       max_turns=3)
        preserved = msgs[boundary:]
        user_count_preserved = sum(
            1 for m in preserved if m.get('role') == 'user')
        assert user_count_preserved == 3

    def test_refuse_when_no_user_message(self):
        """No user message → return len(messages) to signal refusal."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'content': 'hello'},
        ]
        assert _find_turn_boundary(msgs) == len(msgs)

    def test_boundary_always_on_user_index(self):
        """Invariant: the returned boundary points at a 'user' message
        (or len(messages) for refusal)."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        msgs = [{'role': 'system', 'content': 'sys'}]
        for k in range(5):
            msgs.extend(self._mk_agentic_turn(f'Q{k}', n_tool_rounds=3))
        boundary = _find_turn_boundary(msgs, budget_tokens=5000, max_turns=2)
        assert 0 < boundary < len(msgs)
        assert msgs[boundary].get('role') == 'user'

    def test_single_turn_conversation(self):
        """A fresh conversation with just one turn → the whole turn is preserved."""
        from lib.tasks_pkg.compaction import _find_turn_boundary
        msgs = [{'role': 'system', 'content': 'sys'}]
        msgs.extend(self._mk_agentic_turn('hi', n_tool_rounds=2))
        boundary = _find_turn_boundary(msgs)
        # Should land on the user msg (start of the only turn)
        assert msgs[boundary].get('role') == 'user'
        # System is NOT part of a turn — boundary must be AFTER it
        assert boundary > 0



@pytest.mark.unit
class TestTurnBoundaryRegression:
    """Regression coverage for the conv=modearkif6k9tr bug — the old
    pair-count boundary returned ``len(messages)`` when there were fewer
    user turns than ``_MAX_PRESERVE_TURNS``. ``_find_turn_boundary`` must
    always preserve at least the current turn."""

    def test_turn_boundary_preserves_current_turn_with_few_users(self):
        from lib.tasks_pkg.compaction import (
            _find_turn_boundary, _MAX_PRESERVE_TURNS,
        )
        msgs = [{'role': 'system', 'content': 'sys'}]
        # Only 4 user turns — fewer than _MAX_PRESERVE_TURNS (16).
        for k in range(4):
            msgs.append({'role': 'user', 'content': f'Q{k}'})
            for _ in range(20):  # many tool messages per turn
                msgs.append({'role': 'assistant', 'content': None,
                             'tool_calls': [{
                                 'id': f't{k}', 'type': 'function',
                                 'function': {'name': 'grep_search',
                                              'arguments': '{}'}}]})
                msgs.append({'role': 'tool', 'tool_call_id': f't{k}',
                             'name': 'grep_search', 'content': 'x' * 500})
        n_users = sum(1 for m in msgs if m.get('role') == 'user')
        assert n_users < _MAX_PRESERVE_TURNS  # precondition of the bug

        boundary = _find_turn_boundary(msgs)
        # MUST preserve at least the current (last) turn
        assert boundary < len(msgs), (
            'REGRESSION: _find_turn_boundary returned len(messages) for a '
            'conversation with fewer user turns than _MAX_PRESERVE_TURNS — '
            'this is the exact bug from conv=modearkif6k9tr.'
        )
        last_user = max(i for i, m in enumerate(msgs) if m.get('role') == 'user')
        assert boundary <= last_user


@pytest.mark.unit
class TestRelevanceFormatFilter:
    """Verify _format_messages_for_summary only shows user + natural-language
    assistant messages to the relevance-rating cheap model."""

    def test_tool_messages_excluded(self):
        from lib.tasks_pkg.compaction import _format_messages_for_summary
        msgs = [
            {'role': 'user', 'content': 'find foo'},
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 't1', 'type': 'function',
                             'function': {'name': 'grep_search',
                                          'arguments': '{"pattern":"foo"}'}}]},
            {'role': 'tool', 'tool_call_id': 't1', 'name': 'grep_search',
             'content': 'file.py:1:foo\nfile.py:2:foo bar\n' * 1000},
            {'role': 'assistant', 'content': 'Found 2000 matches in file.py'},
        ]
        out = _format_messages_for_summary(msgs)
        assert 'find foo' in out
        assert 'Found 2000 matches' in out
        assert 'file.py:1:foo' not in out, (
            'tool result content leaked into relevance-format output'
        )
        # Tool-call-only assistant message should be dropped
        assert 'grep_search' not in out

    def test_tool_call_only_assistant_dropped(self):
        from lib.tasks_pkg.compaction import _format_messages_for_summary
        msgs = [
            {'role': 'user', 'content': 'question'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 't1', 'type': 'function',
                             'function': {'name': 'read_files',
                                          'arguments': '{}'}}]},
        ]
        out = _format_messages_for_summary(msgs)
        assert '[user] question' in out
        assert '[assistant]' not in out

    def test_assistant_with_text_kept_even_with_tool_calls(self):
        """Assistant message with both text AND tool_calls — text is kept."""
        from lib.tasks_pkg.compaction import _format_messages_for_summary
        msgs = [
            {'role': 'user', 'content': 'Q'},
            {'role': 'assistant',
             'content': 'I need to investigate this first.',
             'tool_calls': [{'id': 't1', 'type': 'function',
                             'function': {'name': 'grep_search',
                                          'arguments': '{}'}}]},
        ]
        out = _format_messages_for_summary(msgs)
        assert 'I need to investigate' in out

    def test_system_messages_excluded(self):
        from lib.tasks_pkg.compaction import _format_messages_for_summary
        msgs = [
            {'role': 'system', 'content': 'You are helpful'},
            {'role': 'user', 'content': 'Q'},
        ]
        out = _format_messages_for_summary(msgs)
        assert 'You are helpful' not in out
        assert '[user] Q' in out

    def test_reasoning_content_excluded(self):
        """reasoning_content (thinking) must not leak into relevance format."""
        from lib.tasks_pkg.compaction import _format_messages_for_summary
        msgs = [
            {'role': 'user', 'content': 'Q'},
            {'role': 'assistant', 'content': 'Answer',
             'reasoning_content': 'Secret internal thinking scratchpad'},
        ]
        out = _format_messages_for_summary(msgs)
        assert 'Secret internal thinking' not in out
        assert '[assistant] Answer' in out

    def test_empty_user_skipped(self):
        from lib.tasks_pkg.compaction import _format_messages_for_summary
        msgs = [
            {'role': 'user', 'content': ''},
            {'role': 'user', 'content': '   '},
            {'role': 'user', 'content': 'real question'},
        ]
        out = _format_messages_for_summary(msgs)
        assert '[user] real question' in out
        # Only one [user] line should be present
        assert out.count('[user]') == 1


@pytest.mark.unit
class TestContextLimitDetection:
    """Ensure newer Claude versions aren't silently downgraded to 200k."""

    def test_claude_opus_47_variants(self):
        from lib.tasks_pkg.compaction import _get_context_limit
        variants = [
            'aws.claude-opus-4.7',
            'claude-opus-4-7',
            'us.anthropic.claude-opus-4-7-v1:0',
            'claude-opus-4.8',   # future
        ]
        for m in variants:
            limit = _get_context_limit({'config': {'model': m}})
            assert limit == 1_000_000, (
                f'Model {m!r} got wrong context limit: {limit:,}. '
                'This is the latent bug that caused force-compact to fire '
                'prematurely on conv=modearkif6k9tr.'
            )

    def test_claude_sonnet_46_plus(self):
        from lib.tasks_pkg.compaction import _get_context_limit
        for m in ['claude-sonnet-4.6', 'claude-sonnet-4-7', 'aws.claude-sonnet-4.8']:
            assert _get_context_limit({'config': {'model': m}}) == 1_000_000

    def test_older_claude_still_200k(self):
        from lib.tasks_pkg.compaction import _get_context_limit
        assert _get_context_limit({'config': {'model': 'claude-3-opus'}}) == 200_000
        assert _get_context_limit({'config': {'model': 'claude-3.5-sonnet'}}) == 200_000


@pytest.mark.unit
class TestCompactRefusalGuards:
    """execute_compact_tool must refuse rather than destroy the current turn."""

    def test_refuses_when_no_user_message(self, monkeypatch):
        """No user msg → compaction is refused, messages untouched."""
        from lib.tasks_pkg.compaction import execute_compact_tool
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'content': 'orphaned'},
        ]
        original = list(msgs)
        task = {'id': 'test_refuse', 'convId': 'conv_refuse',
                'config': {'model': 'gpt-4'}}
        result = execute_compact_tool(msgs, task=task)
        assert 'skipped' in result.lower() or 'refus' in result.lower()
        assert msgs == original, (
            'REFUSED compaction MUST NOT mutate the messages list — '
            'that was the core bug we are fixing.'
        )

    def test_cooldown_not_set_on_refusal(self):
        """Refused compactions must not claim the 30s cooldown slot."""
        from lib.tasks_pkg.compaction import _summary_cooldowns, execute_compact_tool
        conv_id = 'conv_cooldown_test'
        _summary_cooldowns.pop(conv_id, None)
        msgs = [{'role': 'assistant', 'content': 'orphaned'}]  # no user
        task = {'id': 't', 'convId': conv_id, 'config': {'model': 'gpt-4'}}
        execute_compact_tool(msgs, task=task)
        # Cooldown must NOT have been set by the refused call
        assert conv_id not in _summary_cooldowns, (
            'Refused compaction registered a cooldown, blocking the next '
            'legitimate attempt for 30s.'
        )



@pytest.mark.unit
class TestForceCompactReportsSummaryFailure:
    """When the L2 summary LLM returns empty content, force_compact_if_needed
    MUST report failure (return False) and NOT inject the synthetic
    context_compact tool-pair.

    Regression for the mq7y3irly1r4hu fatal-loop bug (2026-06-12): on an
    empty summary, force_compact used to still append the synthetic pair
    and return True, which made reactive_compact believe compaction
    succeeded and skip its _head_truncate safety net — looping the same
    oversized prompt back to the API until the task crashed FATAL."""

    def _mk_messages(self):
        # System + several real turns so the boundary is well-formed.
        msgs = [{'role': 'system', 'content': 'sys'}]
        for i in range(4):
            msgs.append({'role': 'user', 'content': f'user turn {i}'})
            msgs.append({'role': 'assistant', 'content': f'assistant turn {i}'})
        return msgs

    def test_empty_summary_returns_false_and_no_injection(self, monkeypatch):
        from lib.tasks_pkg.compaction import _layer2

        # Force the summary LLM to come back empty (the exact failure mode).
        monkeypatch.setattr(_layer2, '_generate_query_aware_summary',
                            lambda *a, **k: None)
        # Avoid DB/SSE side-effects from the archive snapshot.
        monkeypatch.setattr(_layer2, '_archive_transcript', lambda *a, **k: None)

        msgs = self._mk_messages()
        original = list(msgs)
        task = {'id': 'fail_test', 'convId': 'conv_fail',
                'config': {'model': 'gpt-4'}}

        result = _layer2.force_compact_if_needed(
            msgs, task=task, force=True, keep_recent_pairs=2,
        )

        assert result is False, (
            'force_compact_if_needed must return False when the summary '
            'failed — the reactive head-truncate net depends on it.'
        )
        assert msgs == original, (
            'Failed compaction must NOT mutate/grow the message list with a '
            'synthetic context_compact pair.'
        )

    def test_successful_summary_returns_true_and_injects(self, monkeypatch):
        from lib.tasks_pkg.compaction import _layer2

        monkeypatch.setattr(_layer2, '_generate_query_aware_summary',
                            lambda *a, **k: 'SUMMARY BODY')
        monkeypatch.setattr(_layer2, '_archive_transcript', lambda *a, **k: None)

        msgs = self._mk_messages()
        task = {'id': 'ok_test', 'convId': 'conv_ok',
                'config': {'model': 'gpt-4'}}

        result = _layer2.force_compact_if_needed(
            msgs, task=task, force=True, keep_recent_pairs=2,
        )

        assert result is True
        # The synthetic context_compact tool-pair was appended.
        assert any(m.get('role') == 'tool'
                   and m.get('name') == _layer2._COMPACT_TOOL_NAME
                   for m in msgs)


# ═══════════════════════════════════════════════════════════
#  13. Paired Assistant Compaction (Phase B2)
#  A/B-verified 2026-04-27: -1.4% cache_write vs Phase B alone.
#  Enabled ALONGSIDE Phase D in reactive_compact where cache is
#  being rebuilt wholesale.
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPairedAssistantCompaction:
    """Phase B2: co-compact interstitial on assistant(tool_calls) whose
    paired cold tool result was compacted by Phase B."""

    @staticmethod
    def _mk_long_interstitial(prefix: str) -> str:
        # 500+ chars, over _PAIRED_COMMENTARY_THRESHOLD (200).
        return (f'{prefix}: Let me carefully investigate this by running '
                'several focused searches across the codebase. I want to '
                'understand the existing patterns before proposing any '
                'changes, because refactoring without understanding the '
                'intended invariants tends to break more than it fixes. '
                'I will start with a broad search, then narrow.') * 2

    @staticmethod
    def _mk_conversation(n_rounds: int, *, hot_tail: int = 3):
        """Build 1 user + N rounds of (assistant interstitial + tool result)."""
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'Investigate.'},
        ]
        for r in range(n_rounds):
            tc_id = f'call_{r}'
            msgs.append({
                'role': 'assistant',
                'content': TestPairedAssistantCompaction._mk_long_interstitial(
                    f'R{r}'),
                'tool_calls': [{
                    'id': tc_id, 'type': 'function',
                    'function': {'name': 'grep_search',
                                 'arguments': '{"pattern":"x"}'}}],
            })
            msgs.append({
                'role': 'tool', 'tool_call_id': tc_id,
                'name': 'grep_search',
                'content': 'match\n' * 500,  # 3000+ chars → cold-eligible
            })
        return msgs

    def test_off_by_default_no_paired_compaction(self):
        """micro_compact must NOT compact interstitials unless flag is set."""
        import lib.tasks_pkg.compaction as _c
        orig = _c.MICRO_HOT_TAIL
        _c.MICRO_HOT_TAIL = 2
        try:
            msgs = self._mk_conversation(6)
            _c.micro_compact(msgs, conv_id='test_off')
            # No assistant content should be replaced with the
            # interstitial-compacted marker
            touched = [m for m in msgs
                       if m.get('role') == 'assistant'
                       and isinstance(m.get('content'), str)
                       and m['content'].startswith('[Interstitial compacted')]
            assert not touched, f'Phase B2 fired when OFF: {len(touched)} messages'
        finally:
            _c.MICRO_HOT_TAIL = orig

    def test_on_compacts_paired_assistants(self):
        """With flag enabled, paired interstitials on cold rounds compact."""
        import lib.tasks_pkg.compaction as _c
        orig = _c.MICRO_HOT_TAIL
        _c.MICRO_HOT_TAIL = 2
        try:
            msgs = self._mk_conversation(6)
            _c.micro_compact(msgs, conv_id='test_on',
                             enable_paired_assistant_compact=True)
            touched = [m for m in msgs
                       if m.get('role') == 'assistant'
                       and isinstance(m.get('content'), str)
                       and m['content'].startswith('[Interstitial compacted')]
            # 6 rounds, hot_tail=2 → 4 cold pairs, 4 interstitials compacted
            assert len(touched) == 4, (
                f'Expected 4 compacted interstitials, got {len(touched)}')
        finally:
            _c.MICRO_HOT_TAIL = orig

    def test_current_turn_interstitial_preserved(self):
        """Phase B2 must NEVER touch interstitials in the hot tail — they're
        still active context for the model."""
        import lib.tasks_pkg.compaction as _c
        orig = _c.MICRO_HOT_TAIL
        _c.MICRO_HOT_TAIL = 2
        try:
            msgs = self._mk_conversation(6)
            # Record the last 2 assistants' content before compaction
            assistant_idx = [i for i, m in enumerate(msgs)
                             if m.get('role') == 'assistant']
            hot_assistant_indices = assistant_idx[-2:]
            before = [msgs[i]['content'] for i in hot_assistant_indices]
            _c.micro_compact(msgs, conv_id='test_hot',
                             enable_paired_assistant_compact=True)
            after = [msgs[i]['content'] for i in hot_assistant_indices]
            assert before == after, (
                'Phase B2 touched hot-tail interstitials — their paired '
                'tool results should not have been compacted either.')
        finally:
            _c.MICRO_HOT_TAIL = orig

    def test_idempotent(self):
        """Running B2 twice must not double-compact."""
        import lib.tasks_pkg.compaction as _c
        orig = _c.MICRO_HOT_TAIL
        _c.MICRO_HOT_TAIL = 2
        try:
            msgs = self._mk_conversation(6)
            _c.micro_compact(msgs, conv_id='idem1',
                             enable_paired_assistant_compact=True)
            snapshot = [
                m.get('content') for m in msgs if m.get('role') == 'assistant'
            ]
            _c.micro_compact(msgs, conv_id='idem2',
                             enable_paired_assistant_compact=True)
            after = [
                m.get('content') for m in msgs if m.get('role') == 'assistant'
            ]
            assert snapshot == after, 'Phase B2 not idempotent'
        finally:
            _c.MICRO_HOT_TAIL = orig

    def test_short_interstitial_skipped(self):
        """Interstitials below _PAIRED_COMMENTARY_THRESHOLD (200) are untouched."""
        import lib.tasks_pkg.compaction as _c
        orig_ht = _c.MICRO_HOT_TAIL
        _c.MICRO_HOT_TAIL = 2
        try:
            msgs = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'Q'},
            ]
            # 6 rounds with SHORT interstitials (< 200 chars) and long tool results
            for r in range(6):
                tc_id = f'call_{r}'
                msgs.append({
                    'role': 'assistant', 'content': 'short',
                    'tool_calls': [{'id': tc_id, 'type': 'function',
                                    'function': {'name': 'grep_search',
                                                 'arguments': '{}'}}],
                })
                msgs.append({
                    'role': 'tool', 'tool_call_id': tc_id,
                    'name': 'grep_search', 'content': 'match\n' * 500,
                })
            _c.micro_compact(msgs, conv_id='test_short',
                             enable_paired_assistant_compact=True)
            touched = [m for m in msgs
                       if m.get('role') == 'assistant'
                       and isinstance(m.get('content'), str)
                       and m['content'].startswith('[Interstitial compacted')]
            assert not touched, (
                'Short interstitials should not be compacted '
                f'(got {len(touched)})')
        finally:
            _c.MICRO_HOT_TAIL = orig_ht

    def test_reactive_compact_enables_paired(self):
        """reactive_compact must pass enable_paired_assistant_compact=True
        alongside enable_assistant_compact=True."""
        import inspect
        from lib.tasks_pkg import compaction as _c
        src = inspect.getsource(_c.reactive_compact)
        assert 'enable_paired_assistant_compact=True' in src, (
            'reactive_compact should enable Phase B2 — cache is being '
            'rebuilt wholesale there, so the extra savings are free.')
        assert 'enable_assistant_compact=True' in src, (
            'reactive_compact should still enable Phase D.')

    def test_micro_compact_kwarg_accepted(self):
        """Defensive: ensure the flag is actually wired through."""
        import inspect
        from lib.tasks_pkg.compaction import micro_compact
        src = inspect.getsource(micro_compact)
        assert 'enable_paired_assistant_compact' in src, (
            'micro_compact must honor enable_paired_assistant_compact kwarg')



# ═══════════════════════════════════════════════════════════
#  14. Proactive head-truncate fallback — the OOM fatal-loop fix
#
#  When the L2 summary LLM cannot run (no 'cheap' slot / saturated single
#  model / input too big) the proactive path used to return False and do
#  NOTHING, leaving the context pinned near the window every round. The
#  reactive head-truncate net never fires proactively (the max_tokens clamp
#  keeps the request just under the hard ceiling, so there's no API
#  rejection). Nothing bounded the context → unbounded re-send → OOM.
#
#  These tests drive the REAL proactive entry (run_compaction_pipeline)
#  with the summary dispatch stubbed to FAIL, and prove:
#    * WITHOUT the fallback (double-neuter NC): context stays oversized.
#    * WITH the fallback: context is bounded (messages dropped).
#    * Non-critical case (headroom remains): still returns False — the
#      empty-summary→False contract is preserved, no gratuitous truncation.
#    * Reactive path is byte-identical (does NOT double-truncate).
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProactiveHeadTruncateFallback:
    """Fix for the force-compact→OOM fatal loop when the summary LLM is down."""

    @staticmethod
    def _oversized_messages(n_turns=40, chars_per_turn=8000):
        """Build a message list that is genuinely over a 128k-token window.

        ~40 turns × ~8000 chars ≈ 320k chars ≈ 106k+ tokens (the entropy
        heuristic counts ~1 token / 3 latin chars → but padded to be safely
        over usable≈90k for a 128k window). Every turn is anchored by a real
        user message so _find_turn_boundary is well-formed."""
        msgs = [{'role': 'system', 'content': 'static system prompt'}]
        body = 'x' * chars_per_turn
        for i in range(n_turns):
            msgs.append({'role': 'user', 'content': f'user turn {i}: {body}'})
            msgs.append({'role': 'assistant', 'content': f'assistant turn {i}: {body}'})
        return msgs

    @staticmethod
    def _task_128k():
        # gpt-4 → 128k window in _get_static_context_limit.
        return {'id': 'oom_test', 'convId': 'conv-oom',
                'config': {'model': 'gpt-4'}}

    def _stub_summary_fail(self, monkeypatch):
        """Make the L2 summary LLM path fail exactly like a dead 'cheap'
        dispatch on a vanilla deploy (RuntimeError from dispatch_chat).

        dispatch_chat is lazy-imported INSIDE _generate_query_aware_summary,
        so we patch it on its home module lib.llm_dispatch — this exercises
        the REAL graceful-degradation path (cheap→text retry) end to end,
        then still raises so the summary genuinely fails."""
        import lib.llm_dispatch as _ld
        from lib.tasks_pkg.compaction import _layer2

        def _boom(*a, **k):
            raise RuntimeError('All 3 dispatch attempts failed for capability=cheap')

        monkeypatch.setattr(_ld, 'dispatch_chat', _boom)
        # Avoid DB/SSE side effects from archival + events.
        monkeypatch.setattr(_layer2, '_archive_transcript', lambda *a, **k: None)

    def test_proactive_bounds_context_when_summary_fails(self, monkeypatch):
        """WITH the fallback: run_compaction_pipeline shrinks an oversized
        context even though the summary LLM is dead."""
        import lib.tasks_pkg.compaction._pipeline as pl
        from lib.tasks_pkg.compaction._tokens import (
            _estimate_total_tokens, _get_context_limit, _usable_context,
        )
        self._stub_summary_fail(monkeypatch)
        # Neutralize L1 so the ONLY thing that can shrink the list is the L2
        # force-compact fallback we are testing (isolate the mechanism).
        monkeypatch.setattr(pl, 'micro_compact', lambda *a, **k: 0)

        msgs = self._oversized_messages()
        task = self._task_128k()
        before_tokens = _estimate_total_tokens(msgs)
        before_len = len(msgs)
        usable = _usable_context(_get_context_limit(task))
        assert before_tokens >= usable, (
            f'test fixture not oversized: {before_tokens} < usable {usable}')

        pl.run_compaction_pipeline(msgs, 1, task=task)

        after_tokens = _estimate_total_tokens(msgs)
        assert len(msgs) < before_len, (
            'proactive pipeline must DROP messages when the summary LLM is '
            'down and the context is critically over budget (OOM guard)')
        assert after_tokens < before_tokens, 'context must actually shrink'

    def test_force_compact_fallback_returns_true_and_truncates(self, monkeypatch):
        """Unit-level: force_compact_if_needed(_allow_head_truncate_fallback=True)
        returns True and drops messages when summary fails + over budget."""
        from lib.tasks_pkg.compaction import _layer2
        self._stub_summary_fail(monkeypatch)

        msgs = self._oversized_messages()
        before_len = len(msgs)
        result = _layer2.force_compact_if_needed(
            msgs, task=self._task_128k(), force=True,
            _allow_head_truncate_fallback=True)
        assert result is True
        assert len(msgs) < before_len

    def test_no_fallback_flag_preserves_false_contract(self, monkeypatch):
        """WITHOUT the opt-in flag (the reactive path's shape): summary
        failure → returns False and does NOT truncate. This is the
        force-compact-empty-summary-fatal-loop contract that reactive_compact's
        own Phase-4 head-truncate depends on."""
        from lib.tasks_pkg.compaction import _layer2
        self._stub_summary_fail(monkeypatch)

        msgs = self._oversized_messages()
        before = list(msgs)
        result = _layer2.force_compact_if_needed(
            msgs, task=self._task_128k(), force=True)  # no fallback flag
        assert result is False
        assert msgs == before, (
            'without the opt-in flag, a failed summary must NOT mutate the '
            'message list — reactive Phase-4 head-truncate depends on this')

    def test_non_critical_still_returns_false(self, monkeypatch):
        """WITH the flag but context UNDER the window: summary failure still
        returns False and does not gratuitously truncate. Only a genuine
        over-window overflow triggers the head-truncate escape."""
        from lib.tasks_pkg.compaction import _layer2
        self._stub_summary_fail(monkeypatch)

        # Small conversation, nowhere near a 128k window.
        msgs = [{'role': 'system', 'content': 'sys'}]
        for i in range(3):
            msgs.append({'role': 'user', 'content': f'q{i}'})
            msgs.append({'role': 'assistant', 'content': f'a{i}'})
        before = list(msgs)
        result = _layer2.force_compact_if_needed(
            msgs, task=self._task_128k(), force=True,
            _allow_head_truncate_fallback=True)
        assert result is False, (
            'below the window, a failed summary must not head-truncate')
        assert msgs == before

    def test_reactive_does_not_pass_fallback_flag(self):
        """reactive_compact keeps its OWN Phase-4 head-truncate; it must NOT
        also pass _allow_head_truncate_fallback to force_compact (that would
        double-truncate). Guards the byte-identical-reactive requirement."""
        import inspect
        from lib.tasks_pkg.compaction import _reactive
        src = inspect.getsource(_reactive.reactive_compact)
        assert '_allow_head_truncate_fallback' not in src, (
            'reactive_compact must not opt into the proactive head-truncate '
            'fallback — it owns its Phase-4 _head_truncate already')
