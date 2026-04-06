"""Tests for Claude Code alignment improvements — Phase 2.

Tests cover:
  1. System prompt sections (FRC, summarize guidance, tool usage, output efficiency)
  2. Ultrathink/effort keyword detection
  3. Tool deferral system (partition, search, format)
  4. Tool search handler integration
  5. System prompt role validation (only system/user/assistant/tool)

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

    def test_frc_section_exists(self):
        from lib.tasks_pkg.system_context import _FUNCTION_RESULT_CLEARING_SECTION
        assert 'Function Result Clearing' in _FUNCTION_RESULT_CLEARING_SECTION
        assert 'cleared' in _FUNCTION_RESULT_CLEARING_SECTION.lower()
        assert 'recent' in _FUNCTION_RESULT_CLEARING_SECTION.lower()

    def test_frc_section_references_hot_tail_count(self):
        from lib.tasks_pkg.compaction import MICRO_HOT_TAIL
        from lib.tasks_pkg.system_context import _FUNCTION_RESULT_CLEARING_SECTION
        assert str(MICRO_HOT_TAIL) in _FUNCTION_RESULT_CLEARING_SECTION

    def test_summarize_section_exists(self):
        from lib.tasks_pkg.system_context import _SUMMARIZE_TOOL_RESULTS_SECTION
        assert 'important information' in _SUMMARIZE_TOOL_RESULTS_SECTION.lower()
        assert 'write down' in _SUMMARIZE_TOOL_RESULTS_SECTION.lower()

    def test_tool_usage_guidance_exists(self):
        from lib.tasks_pkg.system_context import _TOOL_USAGE_GUIDANCE
        assert 'parallel' in _TOOL_USAGE_GUIDANCE.lower()
        assert 'read_files' in _TOOL_USAGE_GUIDANCE or 'grep_search' in _TOOL_USAGE_GUIDANCE
        assert 'apply_diff' in _TOOL_USAGE_GUIDANCE

    def test_tool_usage_guidance_parallel_calls(self):
        """Ensures the guidance tells the model about independent parallel calls."""
        from lib.tasks_pkg.system_context import _TOOL_USAGE_GUIDANCE
        assert 'independent' in _TOOL_USAGE_GUIDANCE.lower()
        assert 'parallel' in _TOOL_USAGE_GUIDANCE.lower()

    def test_output_efficiency_guidance_exists(self):
        from lib.tasks_pkg.system_context import _OUTPUT_EFFICIENCY_GUIDANCE
        assert 'concise' in _OUTPUT_EFFICIENCY_GUIDANCE.lower()
        assert 'brief' in _OUTPUT_EFFICIENCY_GUIDANCE.lower()

    def test_sections_injected_when_tools_present(self):
        """Verify sections are appended to system message when has_real_tools=True."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'You are a helpful assistant.'}]
        _inject_system_contexts(
            messages,
            project_path='/tmp/test',
            project_enabled=False,
            skills_enabled=False,
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
            skills_enabled=False,
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
#  3. Tool Deferral System
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolDeferral:
    """Verify tool partitioning, search, and formatting."""

    def _make_tool(self, name):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Tool {name}",
                "parameters": {"type": "object", "properties": {}}
            }
        }

    def test_partition_core_tools_stay(self):
        from lib.tools.deferral import partition_tools
        tools = [self._make_tool('read_files'), self._make_tool('grep_search')]
        core, deferred = partition_tools(tools)
        assert len(core) == 2
        assert len(deferred) == 0

    def test_partition_no_static_deferral(self):
        """Phase 1 static deferral removed — formerly-deferred tools stay in core."""
        from lib.tools.deferral import partition_tools
        tools = [
            self._make_tool('read_files'),
            self._make_tool('browser_type'),
            self._make_tool('browser_scroll'),
        ]
        core, deferred = partition_tools(tools)
        core_names = {t['function']['name'] for t in core}
        assert 'read_files' in core_names
        assert 'browser_type' in core_names
        assert 'browser_scroll' in core_names
        assert len(deferred) == 0

    def test_partition_no_tool_search_when_no_deferred(self):
        """When there are NO deferred tools, tool_search is NOT added."""
        from lib.tools.deferral import partition_tools
        tools = [self._make_tool('read_files')]
        core, deferred = partition_tools(tools)
        core_names = {t['function']['name'] for t in core}
        assert 'tool_search' not in core_names

    def test_partition_empty_list(self):
        from lib.tools.deferral import partition_tools
        core, deferred = partition_tools([])
        assert core == []
        assert deferred == []

    def test_search_finds_matching_tools(self):
        from lib.tools.deferral import search_deferred_tools
        deferred = [
            self._make_tool('browser_type'),
            self._make_tool('browser_scroll'),
            self._make_tool('generate_image'),
        ]
        matched = search_deferred_tools('browser', deferred)
        matched_names = {t['function']['name'] for t in matched}
        assert 'browser_type' in matched_names
        assert 'browser_scroll' in matched_names
        assert 'generate_image' not in matched_names

    def test_search_finds_by_hint_keywords(self):
        from lib.tools.deferral import search_deferred_tools
        deferred = [self._make_tool('generate_image')]
        matched = search_deferred_tools('image create picture', deferred)
        assert len(matched) == 1
        assert matched[0]['function']['name'] == 'generate_image'

    def test_search_no_match(self):
        from lib.tools.deferral import search_deferred_tools
        deferred = [self._make_tool('browser_type')]
        matched = search_deferred_tools('database sql', deferred)
        assert len(matched) == 0

    def test_search_empty_query(self):
        from lib.tools.deferral import search_deferred_tools
        deferred = [self._make_tool('browser_type')]
        matched = search_deferred_tools('', deferred)
        assert len(matched) == 0

    def test_search_empty_deferred(self):
        from lib.tools.deferral import search_deferred_tools
        matched = search_deferred_tools('browser', [])
        assert len(matched) == 0

    def test_format_results_with_matches(self):
        from lib.tools.deferral import format_search_results
        tools = [self._make_tool('browser_type')]
        result = format_search_results(tools)
        assert 'browser_type' in result
        assert 'Found 1' in result
        assert 'available' in result.lower()

    def test_format_results_no_matches(self):
        from lib.tools.deferral import format_search_results
        result = format_search_results([])
        assert 'No matching tools' in result

    def test_no_static_deferral(self):
        """Phase 1 static deferral is removed — all user-selected tools stay core.

        Tools previously in DEFERRED_TOOL_HINTS should NOT be auto-deferred.
        Only Phase 2 dynamic threshold deferral can move tools.
        """
        from lib.tools.deferral import partition_tools
        tools = [
            self._make_tool('read_files'),
            self._make_tool('create_scheduled_task'),
            self._make_tool('list_scheduled_tasks'),
            self._make_tool('browser_type'),
            self._make_tool('generate_image'),
            self._make_tool('desktop_screenshot'),
        ]
        core, deferred = partition_tools(tools)
        core_names = {t['function']['name'] for t in core}
        # All tools should remain in core — no static deferral
        assert 'create_scheduled_task' in core_names
        assert 'list_scheduled_tasks' in core_names
        assert 'browser_type' in core_names
        assert 'generate_image' in core_names
        assert 'desktop_screenshot' in core_names
        assert len(deferred) == 0


# ═══════════════════════════════════════════════════════════
#  4. Tool Search Handler Integration
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolSearchHandler:
    """Verify tool_search handler behavior."""

    def _make_tool(self, name):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Tool {name}",
                "parameters": {"type": "object", "properties": {}}
            }
        }

    def test_handler_registered(self):
        """Verify tool_search is registered in the tool registry."""
        from lib.tasks_pkg.executor import tool_registry
        handler = tool_registry.lookup('tool_search')
        assert handler is not None

    def test_handler_no_deferred_tools(self):
        """When task has no deferred tools, handler returns appropriate message."""
        from lib.tasks_pkg.executor import tool_registry
        handler = tool_registry.lookup('tool_search')
        import threading
        task = {'id': 'test123', '_deferred_tools': [], 'events': [], 'events_lock': threading.Lock()}
        result = handler(
            task=task, tc=None, fn_name='tool_search', tc_id='tc1',
            fn_args={'query': 'browser'}, rn=1, round_entry=None,
            cfg={}, project_path='/tmp', project_enabled=False,
            all_tools=[],
        )
        # Handler returns (tc_id, tool_content, is_search) tuple
        assert isinstance(result, tuple) and len(result) == 3
        ret_tc_id, tool_content, is_search = result
        assert ret_tc_id == 'tc1'
        assert 'No deferred tools' in tool_content or 'already loaded' in tool_content
        assert is_search is False

    def test_handler_discovers_and_activates_tools(self):
        """tool_search should discover deferred tools and add them to all_tools."""
        from lib.tasks_pkg.executor import tool_registry
        handler = tool_registry.lookup('tool_search')

        import threading
        deferred = [self._make_tool('browser_type'), self._make_tool('generate_image')]
        all_tools = [self._make_tool('read_files')]
        task = {'id': 'test123', '_deferred_tools': deferred.copy(), 'events': [], 'events_lock': threading.Lock()}

        result = handler(
            task=task, tc=None, fn_name='tool_search', tc_id='tc1',
            fn_args={'query': 'browser'}, rn=1, round_entry=None,
            cfg={}, project_path='/tmp', project_enabled=False,
            all_tools=all_tools,
        )

        # Handler returns (tc_id, tool_content, is_search) tuple
        assert isinstance(result, tuple) and len(result) == 3
        ret_tc_id, tool_content, is_search = result
        assert ret_tc_id == 'tc1'
        assert is_search is False

        # browser_type should be activated (added to all_tools)
        active_names = {t['function']['name'] for t in all_tools}
        assert 'browser_type' in active_names
        # generate_image should NOT be activated (didn't match query)
        assert 'generate_image' not in active_names
        # browser_type should be removed from deferred
        remaining = {t['function']['name'] for t in task['_deferred_tools']}
        assert 'browser_type' not in remaining
        assert 'generate_image' in remaining


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
            skills_enabled=False,
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
class TestAssembleToolListReturnValue:
    """Verify _assemble_tool_list returns 4-tuple with deferred_tools."""

    def test_returns_four_values(self):
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
        assert len(result) == 4, f'Expected 4-tuple, got {len(result)}-tuple'
        tool_list, deferred_tools, has_real_tools, max_tool_rounds = result
        assert isinstance(deferred_tools, list)

    def test_no_tools_returns_empty_deferred(self):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        tool_list, deferred_tools, has_real_tools, max_tool_rounds = _assemble_tool_list(
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
        assert deferred_tools == []
        # Always-on tools (skills, emit_to_user, read_local_file) are always present
        # even when all explicit tool categories are disabled
        assert isinstance(tool_list, list)


# ═══════════════════════════════════════════════════════════
#  7. Integration: Full Pipeline
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFullPipelineIntegration:
    """End-to-end integration tests."""

    def test_deferral_partition_real_tools(self):
        """Test partitioning with realistic tool definitions — no static deferral.

        With Phase 1 removed, all user-selected tools stay in core.
        Only Phase 2 dynamic threshold can defer tools.
        """
        from lib.tools.deferral import partition_tools

        tools = []
        for name in ['read_files', 'list_dir', 'grep_search', 'find_files',
                      'write_file', 'apply_diff', 'run_command',
                      'web_search', 'fetch_url',
                      'create_skill', 'update_skill',
                      'check_error_logs', 'resolve_error',
                      'emit_to_user', 'ask_human',
                      'browser_type', 'browser_scroll',
                      'browser_select_option', 'generate_image',
                      'create_scheduled_task']:
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Tool {name}",
                    "parameters": {"type": "object", "properties": {}}
                }
            })

        core, deferred = partition_tools(tools)
        core_names = {t['function']['name'] for t in core}

        # ALL user-selected tools stay in core (no static deferral)
        assert 'read_files' in core_names
        assert 'web_search' in core_names
        assert 'emit_to_user' in core_names
        assert 'browser_type' in core_names
        assert 'generate_image' in core_names
        assert 'create_scheduled_task' in core_names
        # No deferred tools → no tool_search added
        assert len(deferred) == 0
        assert 'tool_search' not in core_names
        assert len(core) == len(tools)

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
        """Verify the complete system prompt structure after injection."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'You are a helpful assistant.'}]
        _inject_system_contexts(
            messages,
            project_path='/tmp/test',
            project_enabled=False,
            skills_enabled=False,
            search_enabled=False,
            swarm_enabled=False,
            has_real_tools=True,
        )
        content = messages[0]['content']
        # Content may now be list of blocks; extract full text
        if isinstance(content, list):
            full = '\n\n'.join(b['text'] for b in content if isinstance(b, dict))
        else:
            full = content
        # Should contain all 4 sections in order
        frc_pos = full.find('Function Result Clearing')
        summarize_pos = full.find('important information')
        tool_pos = full.find('Using your tools')
        output_pos = full.find('Output efficiency')
        assert frc_pos > 0
        assert summarize_pos > frc_pos
        assert tool_pos > summarize_pos
        assert output_pos > tool_pos


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
        """Project context injection wraps in system-reminder tags."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'Base'}]
        with patch('lib.project_mod.get_context_for_prompt',
                   return_value='Project files: a.py, b.py'), \
             patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp', True, False, False, False,
                has_real_tools=True,
            )
        content = messages[0]['content']
        # Content should be list (multi-block) or string with system-reminder
        if isinstance(content, list):
            full = '\n\n'.join(b['text'] for b in content if isinstance(b, dict))
        else:
            full = content
        assert '<system-reminder>' in full
        assert 'Project files: a.py, b.py' in full
        assert '</system-reminder>' in full

    def test_skills_context_in_user_message(self):
        """Skills listing is injected into the user message, not the system message.

        After the refactor, _inject_system_contexts no longer injects the full
        skills listing. Instead, inject_skills_to_user() handles it.
        """
        from lib.tasks_pkg.system_context import _inject_system_contexts, inject_skills_to_user
        messages = [
            {'role': 'system', 'content': 'Base'},
            {'role': 'user', 'content': 'Hello world'},
        ]
        with patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp', False, True, False, False,
                has_real_tools=False,
            )
        # System message should NOT contain skills listing
        content = messages[0]['content']
        if isinstance(content, list):
            full = '\n\n'.join(b['text'] for b in content if isinstance(b, dict))
        else:
            full = content
        assert '<available_skills>' not in full

        # Now inject skills into user message
        with patch('lib.skills.build_skills_context',
                   return_value='<available_skills>\nSkill 1: python\n</available_skills>'):
            inject_skills_to_user(
                messages, skills_enabled=True, has_real_tools=False)

        # User message should contain skills listing
        user_msg = messages[-1]
        assert user_msg['role'] == 'user'
        user_text = user_msg.get('content', '')
        if isinstance(user_text, list):
            user_text = '\n'.join(b.get('text', '') for b in user_text if isinstance(b, dict))
        assert '<available_skills>' in user_text

    def test_static_guidance_as_separate_block(self):
        """Static guidance sections are injected as a separate text block."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        messages = [{'role': 'system', 'content': 'User prompt'}]
        with patch('lib.tasks_pkg.system_context._build_search_addendum',
                   return_value=''):
            _inject_system_contexts(
                messages, '/tmp', False, False, False, False,
                has_real_tools=True,
            )
        content = messages[0]['content']
        # Should be a list of blocks: [dynamic, static_guidance]
        assert isinstance(content, list), f"Expected list, got {type(content)}"
        assert len(content) >= 2, f"Expected at least 2 blocks, got {len(content)}"
        # First block: user prompt + skills instructions
        # Last block: static guidance (FRC + tool usage + output efficiency)
        last_block_text = content[-1]['text']
        assert 'Function Result Clearing' in last_block_text
        assert 'concise' in last_block_text.lower()

    def test_cache_breakpoints_per_block(self):
        """add_cache_breakpoints should cache each text block independently."""
        from lib.llm_client import add_cache_breakpoints
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
