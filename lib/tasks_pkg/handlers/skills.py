# HOT_PATH
"""Skill management tool handlers: create, update, delete, merge skills."""

from __future__ import annotations

from lib.log import get_logger
from lib.skills import SKILL_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)


# ── Skill operation handlers (registry pattern) ────────────────────

def _skill_create(fn_args, project_path):
    from lib.skills import create_skill
    skill = create_skill(
        name=fn_args.get('name', 'Untitled Skill'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=fn_args.get('scope', 'project'),
        project_path=project_path,
    )
    content = (
        f"✅ Skill created: **{skill['name']}** "
        f"(id: {skill['id']}, scope: {skill['scope']})\n\n"
        f"This skill will be available in future conversations when Skills mode is enabled."
    )
    return content, '💡 saved', f"💡 Skill: {fn_args.get('name', '?')}"


def _skill_update(fn_args, project_path):
    from lib.skills import update_skill
    sid = fn_args.get('skill_id', '')
    skill = update_skill(
        skill_id=sid,
        updates={k: v for k, v in fn_args.items() if k not in ('skill_id',) and v is not None},
        project_path=project_path,
    )
    if skill is None:
        logger.warning('[Skill] update_skill returned None for skill_id=%s', sid)
        return f"❌ Skill not found: {sid}", '❌ not found', f"✏️ Skill: {sid}"
    return f"✅ Skill updated: **{skill['name']}** (id: {skill['id']})", '✏️ updated', f"✏️ Skill: {skill['name']}"


def _skill_delete(fn_args, project_path):
    from lib.skills import delete_skill
    sid = fn_args.get('skill_id', '')
    deleted = delete_skill(skill_id=sid, project_path=project_path)
    if deleted:
        return f"✅ Skill deleted: {sid}", '🗑️ deleted', f"🗑️ Skill: {sid}"
    return f"❌ Skill not found: {sid}", '❌ not found', f"🗑️ Skill: {sid}"


def _skill_merge(fn_args, project_path):
    from lib.skills import merge_skills
    result = merge_skills(
        skill_ids=fn_args.get('skill_ids', []),
        name=fn_args.get('name', 'Merged Skill'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=fn_args.get('scope', 'project'),
        project_path=project_path,
    )
    merged = result['merged_skill']
    n_del = len(result['deleted_ids'])
    return (
        f"✅ Merged {n_del} skills → **{merged['name']}** (id: {merged['id']}, scope: {merged['scope']})",
        f'🔀 merged {n_del}',
        f"🔀 Merge → {fn_args.get('name', '?')}",
    )


# Module-level dispatch table — maps skill fn_name → handler.
_SKILL_OP_DISPATCH = {
    'create_skill': _skill_create,
    'update_skill': _skill_update,
    'delete_skill': _skill_delete,
    'merge_skills': _skill_merge,
}


@tool_registry.tool_set(SKILL_TOOL_NAMES, category='skills',
                        description='Create, update, delete, or merge skills')
def _handle_skill_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    _proj = project_path if project_enabled else None
    skill_ok = False
    try:
        handler = _SKILL_OP_DISPATCH.get(fn_name)
        if handler is None:
            raise ValueError(f'Unknown skill operation: {fn_name}')
        tool_content, badge_ok, title = handler(fn_args, _proj)
        skill_ok = True
    except Exception as e:
        logger.warning('[Executor] skill operation %s failed: %s', fn_name, e, exc_info=True)
        tool_content = f"❌ Failed to {fn_name.replace('_', ' ')}: {e}"
        badge_ok = '❌ failed'
        title = f"❌ {fn_name}: error"

    meta = _build_simple_meta(
        fn_name, tool_content, source='Skills',
        title=title,
        snippet=(fn_args.get('description', '') or fn_args.get('skill_id', ''))[:120],
        badge=badge_ok if skill_ok else '❌ failed',
        extra={'skillOk': skill_ok},
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
