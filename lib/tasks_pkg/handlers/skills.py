"""lib/tasks_pkg/handlers/skills.py — Skills tool handlers (activate_skill).

The skills channel is READ-ONLY for the model: it can activate a
user-installed skill (load its guide + file manifest) but has NO skill CRUD —
install / uninstall / enable-toggle are user-only actions in the Settings UI.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.skills import SKILL_TOOL_NAMES


@tool_registry.tool_set(
    SKILL_TOOL_NAMES,
    category='skills',
    description='Load an installed skill package (progressive disclosure)')
def _handle_skill_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                       cfg, project_path, project_enabled, all_tools=None):
    """Handle activate_skill — load a skill's full guide + file manifest.

    Returns:
        tuple: (tool_call_id, result_content, is_search_flag)
    """
    from lib.skills import activate_skill

    _proj = project_path if project_enabled else None
    # Multi-root: pass the non-primary workspace roots so skills installed
    # under any attached root are activatable.
    _extra_paths = []
    if isinstance(cfg, dict):
        _extra_paths = [
            p for p in (cfg.get('projectPaths') or [])
            if p and p != _proj
        ]

    try:
        tool_content = activate_skill(
            fn_args.get('skill', ''),
            project_path=_proj,
            extra_paths=_extra_paths,
        )
        ok = tool_content.startswith('Skill activated:')
        meta = _build_simple_meta(
            fn_name, tool_content,
            source='Skill',
            title=fn_args.get('skill', ''),
            snippet=tool_content.split('\n', 1)[0][:120],
            badge='📦 loaded' if ok else '❌ not loaded',
        )
    except Exception as e:
        logger.error('[Skills] activate_skill failed: %s', e, exc_info=True)
        tool_content = f'Failed to activate skill: {str(e)}'
        meta = _build_simple_meta(
            fn_name, tool_content,
            source='Skill',
            title=fn_args.get('skill', ''),
            snippet=tool_content[:120],
            badge='❌ failed',
        )

    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
