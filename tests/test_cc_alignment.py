"""Tests for Claude Code alignment improvements — Phase 2.

Tests cover:
  1. System prompt sections (FRC, summarize guidance, tool usage, output efficiency)
  2. Ultrathink/effort keyword detection
  3. System prompt role validation (only system/user/assistant/tool)

Run:  pytest tests/test_cc_alignment.py -m unit -v
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib as _lib


@pytest.fixture(autouse=True)
def _disable_extended_ttl():
    """Disable extended TTL to keep cache_control assertions stable."""
    original = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = False
    yield
    _lib.CACHE_EXTENDED_TTL = original


# ═══════════════════════════════════════════════════════════
#  1. System Prompt Sections
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSystemPromptSections:
    """Verify Claude Code-inspired prompt sections exist and are well-formed."""

    # The system_context._FUNCTION_RESULT_CLEARING_SECTION /
    # _SUMMARIZE_TOOL_RESULTS_SECTION / _TOOL_USAGE_GUIDANCE /
    # _OUTPUT_EFFICIENCY_GUIDANCE constants were removed when Layout B
    # (the legacy chatui guidance block) was retired on 2026-05-07.
    # The same content now lives in `system_prompt_cc.section_*` and
    # is exercised by the integration tests below.

    def test_frc_section_exists(self):
        from lib.tasks_pkg.system_prompt_cc import section_function_result_clearing
        section = section_function_result_clearing()
        assert 'Function Result Clearing' in section
        assert 'cleared' in section.lower()
        assert 'recent' in section.lower()

    def test_frc_section_references_hot_tail_count(self):
        from lib.tasks_pkg.compaction import MICRO_HOT_TAIL
        from lib.tasks_pkg.system_prompt_cc import section_function_result_clearing
        assert str(MICRO_HOT_TAIL) in section_function_result_clearing()

    def test_summarize_section_exists(self):
        from lib.tasks_pkg.system_prompt_cc import section_summarize_tool_results
        section = section_summarize_tool_results()
        assert 'important information' in section.lower()
        assert 'write down' in section.lower()

    def test_tool_usage_guidance_exists(self):
        from lib.tasks_pkg.system_prompt_cc import section_using_tools
        section = section_using_tools()
        assert 'parallel' in section.lower()
        assert 'read_files' in section or 'grep_search' in section
        assert 'apply_diff' in section

    def test_tool_usage_guidance_parallel_calls(self):
        """Ensures the guidance tells the model about independent parallel calls."""
        from lib.tasks_pkg.system_prompt_cc import section_using_tools
        section = section_using_tools()
        assert 'independent' in section.lower()
        assert 'parallel' in section.lower()

    def test_output_efficiency_guidance_exists(self):
        from lib.tasks_pkg.system_prompt_cc import section_output_efficiency
        section = section_output_efficiency()
        assert 'concise' in section.lower()
        assert 'brief' in section.lower()

    def test_sections_injected_when_tools_present(self):
        """Verify sections are appended to system message when has_real_tools=True."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'You are a helpful assistant.'}]
        _inject_system_contexts(
            messages,
            project_path='/tmp/test',
            project_enabled=False,
            memory_enabled=False,
            search_enabled=False,
            swarm_enabled=False,
            has_real_tools=True,
        )
        system_content = messages[0]['content']
        # Content may now be a list of text blocks (static/dynamic split)
        if isinstance(system_content, list):
            full_text = '\n\n'.join(b['text'] for b in system_content if isinstance(b, dict))
        else:
            full_text = system_content
        assert 'Function Result Clearing' in full_text
        assert 'important information' in full_text
        assert 'parallel' in full_text.lower()
        assert 'concise' in full_text.lower()

    def test_sections_not_injected_when_no_tools(self):
        """Without tools, the FRC/summarize sections should NOT be injected."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'You are a helpful assistant.'}]
        _inject_system_contexts(
            messages,
            project_path='/tmp/test',
            project_enabled=False,
            memory_enabled=False,
            search_enabled=False,
            swarm_enabled=False,
            has_real_tools=False,
        )
        system_content = messages[0]['content']
        if isinstance(system_content, list):
            full_text = '\n\n'.join(b['text'] for b in system_content if isinstance(b, dict))
        else:
            full_text = system_content
        assert 'Function Result Clearing' not in full_text


# ═══════════════════════════════════════════════════════════
#  2. Ultrathink / Effort Keyword Detection
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUltrathinkDetection:
    """Verify ultrathink keyword detection works correctly."""

    def test_ultrathink_detected_lowercase(self):
        from lib.tasks_pkg.model_config import _has_ultrathink_keyword
        assert _has_ultrathink_keyword('please ultrathink about this')

    def test_ultrathink_detected_uppercase(self):
        from lib.tasks_pkg.model_config import _has_ultrathink_keyword
        assert _has_ultrathink_keyword('ULTRATHINK and solve this')

    def test_ultrathink_detected_mixed_case(self):
        from lib.tasks_pkg.model_config import _has_ultrathink_keyword
        assert _has_ultrathink_keyword('UltraThink carefully')

    def test_ultrathink_not_detected_in_substring(self):
        from lib.tasks_pkg.model_config import _has_ultrathink_keyword
        # Should NOT match as substring (e.g., "ultrathinking")
        assert not _has_ultrathink_keyword('ultrathinking is not a word')

    def test_ultrathink_not_detected_when_absent(self):
        from lib.tasks_pkg.model_config import _has_ultrathink_keyword
        assert not _has_ultrathink_keyword('please think deeply about this')

    def test_ultrathink_at_word_boundary(self):
        from lib.tasks_pkg.model_config import _has_ultrathink_keyword
        assert _has_ultrathink_keyword('please, ultrathink!')
        assert _has_ultrathink_keyword('(ultrathink)')

    def test_extract_latest_user_text_string(self):
        from lib.tasks_pkg.model_config import _extract_latest_user_text
        cfg = {
            'messages': [
                {'role': 'user', 'content': 'first message'},
                {'role': 'assistant', 'content': 'ok'},
                {'role': 'user', 'content': 'second message ultrathink'},
            ]
        }
        result = _extract_latest_user_text(cfg)
        assert result == 'second message ultrathink'

    def test_extract_latest_user_text_multimodal(self):
        from lib.tasks_pkg.model_config import _extract_latest_user_text
        cfg = {
            'messages': [
                {'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,...'}},
                    {'type': 'text', 'text': 'analyze this ultrathink'},
                ]},
            ]
        }
        result = _extract_latest_user_text(cfg)
        assert 'ultrathink' in result

    def test_extract_latest_user_text_empty(self):
        from lib.tasks_pkg.model_config import _extract_latest_user_text
        cfg = {'messages': []}
        assert _extract_latest_user_text(cfg) == ''

    def test_extract_latest_user_text_no_user_messages(self):
        from lib.tasks_pkg.model_config import _extract_latest_user_text
        cfg = {'messages': [{'role': 'system', 'content': 'You are helpful.'}]}
        assert _extract_latest_user_text(cfg) == ''


# ═══════════════════════════════════════════════════════════
#  3. (removed) Tool Deferral System — deferral subsystem deleted
# ═══════════════════════════════════════════════════════════

# Tool deferral subsystem (TestToolDeferral / TestToolSearchHandler) was
# removed when the deferral framework itself was deleted — frontend toggles
# already gate per-feature tool inclusion, so partition/search machinery had
# no remaining role.


# ═══════════════════════════════════════════════════════════
#  5. Message Role Validation
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMessageRoles:
    """Verify only standard OpenAI roles are used in messages."""

    VALID_ROLES = {'system', 'user', 'assistant', 'tool', 'developer'}

    def test_system_context_uses_valid_roles(self):
        """_inject_system_contexts should only produce messages with valid roles."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'Base system prompt.'}]
        _inject_system_contexts(
            messages,
            project_path='/tmp/test',
            project_enabled=False,
            memory_enabled=False,
            search_enabled=False,
            swarm_enabled=False,
            has_real_tools=True,
        )
        for msg in messages:
            assert msg['role'] in self.VALID_ROLES, (
                f"Invalid role '{msg['role']}' in message: {msg.get('content', '')[:100]}"
            )

    def test_compaction_summary_uses_valid_roles(self):
        """Force-compact messages should only use valid roles."""
        # The compaction summary injects a synthetic user + tool pair
        from lib.tasks_pkg.compaction import micro_compact
        messages = [
            {'role': 'system', 'content': 'System prompt'},
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
            {'role': 'tool', 'tool_call_id': 'tc1', 'content': 'result'},
        ]
        micro_compact(messages)
        for msg in messages:
            assert msg['role'] in self.VALID_ROLES


# ═══════════════════════════════════════════════════════════
#  6. Assemble Tool List Return Value
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveModelConfigResponseFormat:
    """Verify _resolve_model_config surfaces responseFormat into the mcfg dict."""

    def test_response_format_surfaced(self):
        from lib.tasks_pkg.model_config import _resolve_model_config
        rf = {'type': 'json_object'}
        mcfg = _resolve_model_config({'model': 'gpt-x', 'responseFormat': rf}, 'tid12345')
        assert mcfg['response_format'] == rf

    def test_response_format_none_by_default(self):
        from lib.tasks_pkg.model_config import _resolve_model_config
        mcfg = _resolve_model_config({'model': 'gpt-x'}, 'tid12345')
        assert mcfg['response_format'] is None


@pytest.mark.unit
class TestAssembleToolListReturnValue:
    """Verify _assemble_tool_list returns the expected 3-tuple."""

    def test_returns_three_values(self):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        result = _assemble_tool_list(
            cfg={'messages': []},
            project_path=None,
            project_enabled=False,
            task_id='test',
            search_mode='off',
            search_enabled=False,
            fetch_enabled=False,
            code_exec_enabled=False,
            browser_enabled=False,
            desktop_enabled=False,
            swarm_enabled=False,
            scheduler_enabled=False,
            messages=None,
        )
        assert len(result) == 3, f'Expected 3-tuple, got {len(result)}-tuple'
        tool_list, has_real_tools, max_tool_rounds = result
        assert tool_list is not None

    def test_default_tool_list_includes_read_files(self):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        tool_list, has_real_tools, max_tool_rounds = _assemble_tool_list(
            cfg={'messages': []},
            project_path=None,
            project_enabled=False,
            task_id='test',
            search_mode='off',
            search_enabled=False,
            fetch_enabled=False,
            code_exec_enabled=False,
            browser_enabled=False,
            desktop_enabled=False,
            swarm_enabled=False,
            scheduler_enabled=False,
            messages=None,
        )
        # read_files is always-on (handles absolute local paths like
        # PDFs/images/Office docs regardless of project mode).
        assert tool_list is not None
        names = {t['function']['name'] for t in tool_list}
        assert 'read_files' in names
        assert has_real_tools is True


# ═══════════════════════════════════════════════════════════
#  7. Integration: Full Pipeline
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFullPipelineIntegration:
    """End-to-end integration tests."""

    def test_ultrathink_escalation_in_config(self):
        """Verify ultrathink in user message escalates thinking depth."""
        from lib.tasks_pkg.model_config import _extract_latest_user_text, _has_ultrathink_keyword
        cfg = {
            'messages': [
                {'role': 'user', 'content': 'ultrathink about the architecture of this project'},
            ]
        }
        user_text = _extract_latest_user_text(cfg)
        assert _has_ultrathink_keyword(user_text)

    def test_system_prompt_complete_structure(self):
        """Verify the complete system prompt structure after injection.

        Under the Claude-Code-style layout the static block from
        ``system_prompt_cc.build_static_prompt`` orders sections as:

            intro -> # System -> # Doing tasks -> # Executing actions
            with care -> # Using your tools -> # Tone and style
            -> # Output efficiency -> # Function Result Clearing
            -> SUMMARIZE -> system-reminder note -> # Environment
            -> Notes -> Current date.

        Assert the relevant CC sections appear, in the right relative
        order — not Layout B's old ordering.
        """
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'Pre-existing.'}]
        _inject_system_contexts(
            messages,
            project_path='/tmp/test',
            project_enabled=False,
            memory_enabled=False,
            search_enabled=False,
            swarm_enabled=False,
            has_real_tools=True,
        )
        content = messages[0]['content']
        if isinstance(content, list):
            full = '\n\n'.join(b['text'] for b in content if isinstance(b, dict))
        else:
            full = content
        tool_pos = full.find('Using your tools')
        output_pos = full.find('Output efficiency')
        frc_pos = full.find('Function Result Clearing')
        summarize_pos = full.find('important information')
        for label, pos in [('Using your tools', tool_pos),
                            ('Output efficiency', output_pos),
                            ('Function Result Clearing', frc_pos),
                            ('important information (summarize)', summarize_pos)]:
            assert pos >= 0, f'Missing section: {label}'
        # Section order matches build_static_prompt()'s assembly
        assert tool_pos < output_pos < frc_pos < summarize_pos


# ═══════════════════════════════════════════════════════════════════════════════
#  Test: System-reminder wrapping & multi-block cache segmentation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSystemReminderAndBlocks:
    """Verify system-reminder wrapping and multi-block system message."""

    def test_wrap_system_reminder(self):
        """_wrap_system_reminder produces correct tags."""
        from lib.tasks_pkg.system_context import _wrap_system_reminder
        result = _wrap_system_reminder('Hello world')
        assert result == '<system-reminder>\nHello world\n</system-reminder>'

    def test_project_context_wrapped(self):
        """Project context (CLAUDE.md) is now placed in a USER message with
        ``_isMeta: True`` and wrapped in <system-reminder> tags. Mirrors
        Claude Code's ``prependUserContext`` (utils/api.ts:449). The static
        prompt itself stays in the system message."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'Base'},
                    {'role': 'user', 'content': 'hi'}]
        with patch('lib.project_mod.get_context_for_prompt',
                   return_value='Project files: a.py, b.py'):
            _inject_system_contexts(
                messages, '/tmp', True, False, False, False,
                has_real_tools=True,
            )
        # Find the prepended user _isMeta message
        meta_msg = next(
            (m for m in messages
             if m.get('role') == 'user' and m.get('_isMeta')), None)
        assert meta_msg is not None, \
            f"Expected a user _isMeta message; got {[m.get('role') for m in messages]}"
        body = meta_msg['content']
        if isinstance(body, list):
            body = '\n\n'.join(b.get('text', '') for b in body if isinstance(b, dict))
        assert '<system-reminder>' in body
        assert 'Project files: a.py, b.py' in body
        assert '</system-reminder>' in body

        # Static prompt remains in the system message; CLAUDE.md is NOT in it
        sys_content = messages[0]['content']
        if isinstance(sys_content, list):
            sys_text = '\n\n'.join(b.get('text', '') for b in sys_content
                                    if isinstance(b, dict))
        else:
            sys_text = sys_content
        assert 'Project files: a.py, b.py' not in sys_text

    def test_skills_context_in_system_message(self):
        """Memory count hint is injected into the system message.

        Both compact memory instructions and the dynamic count hint go
        into the system message. No listing is injected into the user
        message (on-demand via search_memories tool + memory-prefetch).
        """
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [
            {'role': 'system', 'content': 'Base'},
            {'role': 'user', 'content': 'Hello world'},
        ]
        # has_real_tools=False means no memory injection (no tools = no memory)
        _inject_system_contexts(
            messages, '/tmp', False, True, False, False,
            has_real_tools=False,
        )
        content = messages[0]['content']
        if isinstance(content, list):
            full = '\n\n'.join(b['text'] for b in content if isinstance(b, dict))
        else:
            full = content
        assert '<available_memories>' not in full
        assert '<available_skills>' not in full

        # With has_real_tools=True, the count hint should appear in system msg
        messages2 = [
            {'role': 'system', 'content': 'Base'},
            {'role': 'user', 'content': 'Hello world'},
        ]
        with patch('lib.memory.build_memory_context',
                   return_value='You have 10 accumulated memories from previous sessions. Use search_memories(query) to find relevant past experience.'):
            _inject_system_contexts(
                messages2, '/tmp', False, True, False, False,
                has_real_tools=True,
            )
        content2 = messages2[0]['content']
        if isinstance(content2, list):
            full2 = '\n\n'.join(b['text'] for b in content2 if isinstance(b, dict))
        else:
            full2 = content2
        assert '10 accumulated memories' in full2
        assert 'memory_accumulation' in full2

    def test_static_guidance_as_separate_block(self):
        """Static guidance sections are part of the cache-segmented system
        message. After the Layout A consolidation, the CC static block
        contains FRC + tool usage + output efficiency, and the memory
        block (when has_real_tools=True AND memory_enabled=True) gets its
        OWN separate cache block so a memory CRUD doesn't invalidate the
        FRC/tools/static prefix."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'Pre-existing prompt'}]
        # memory_enabled=True so the memory_accumulation block is injected
        _inject_system_contexts(
            messages, '/tmp', False, True, False, False,
            has_real_tools=True,
        )
        content = messages[0]['content']
        assert isinstance(content, list), f"Expected list, got {type(content)}"
        assert len(content) >= 2, f"Expected at least 2 blocks, got {len(content)}"
        full = '\n\n'.join(b.get('text', '') for b in content
                            if isinstance(b, dict))
        # Static CC content lives somewhere in the system message blocks
        assert 'Function Result Clearing' in full
        assert 'concise' in full.lower()
        # Memory accumulation reminder gets its own segmented block (last)
        last_block_text = content[-1].get('text', '')
        assert '<memory_accumulation>' in last_block_text or \
               'accumulated memories' in last_block_text

    def test_cache_breakpoints_per_block(self):
        """add_cache_breakpoints should cache each text block independently."""
        from lib.llm import add_cache_breakpoints
        body = {
            'model': 'claude-sonnet-4-20250514',
            'messages': [{
                'role': 'system',
                'content': [
                    {'type': 'text', 'text': 'Dynamic context block'},
                    {'type': 'text', 'text': 'Static guidance block'},
                ],
            }],
        }
        add_cache_breakpoints(body)
        blocks = body['messages'][0]['content']
        # Both blocks should have cache_control
        assert 'cache_control' in blocks[0], "Block 0 should have cache_control"
        assert 'cache_control' in blocks[1], "Block 1 should have cache_control"
        assert blocks[0]['cache_control'] == {'type': 'ephemeral'}
        assert blocks[1]['cache_control'] == {'type': 'ephemeral'}
