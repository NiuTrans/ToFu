"""Tofu built-in static system prompt sections.

This package builds the platform's built-in system prompt: one builder
function per prose section, each returning either a string or None.
Nothing in this package reads runtime state — sections that depend on
environment info accept explicit arguments, so the assembled prompt stays
cache-stable across turns.

The ``is_code_context`` flag (== project_enabled at the caller) gates the
SWE-shaped content (code-hygiene bullets in ``# Doing tasks``, the
git/CI examples in ``# Executing actions with care``, and the SWE framing
in the intro). When False, the prompt becomes a generic-assistant prompt:
the model isn't biased toward "I'm doing code now" framing for translation,
paper Q&A, daily-report or trading turns.

Prose references tools by Tofu's own names only: read_files,
apply_diff / insert_content, write_file, find_files, grep_search,
run_command, todo_write, ask_human (via human_guidance),
spawn_agents (async swarm).

This module is a pure re-export facade — the implementations live in the
sibling sub-modules (``_sections``, ``_environment``, ``_build``).
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Prose sections (re-exported from ._sections)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.system_prompt_base._sections import (  # noqa: E402,F401
    _with_heading,
    section_intro,
    section_system,
    section_doing_tasks,
    section_actions,
    section_using_tools,
    section_tone_and_style,
    section_output_efficiency,
    section_system_reminders,
    section_function_result_clearing,
    section_summarize_tool_results,
    _DOING_TASKS_GENERAL,
    _DOING_TASKS_CODE_ONLY,
    _ACTIONS_PRINCIPLE,
    _ACTIONS_EXAMPLES_CODE,
    _ACTIONS_EXAMPLES_GENERIC,
    _ACTIONS_OBSTACLE,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Environment + date sections (re-exported from ._environment)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.system_prompt_base._environment import (  # noqa: E402,F401
    _short_os_version,
    section_environment,
    section_current_date,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Assembler + user-context reminder (re-exported from ._build)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.system_prompt_base._build import (  # noqa: E402,F401
    BLOCK_META,
    build_static_blocks,
    build_static_prompt,
    build_user_context_reminder,
    resolve_static_prompt_profile,
)


__all__ = [
    # prose sections
    'section_intro',
    'section_system',
    'section_doing_tasks',
    'section_actions',
    'section_using_tools',
    'section_tone_and_style',
    'section_output_efficiency',
    'section_system_reminders',
    'section_function_result_clearing',
    'section_summarize_tool_results',
    # environment
    'section_environment',
    'section_current_date',
    # assembler
    'BLOCK_META',
    'build_static_blocks',
    'build_static_prompt',
    'build_user_context_reminder',
    'resolve_static_prompt_profile',
    # helper
    '_with_heading',
]
