# HOT_PATH
"""Memory management tool handlers: create, update, delete, merge memories."""

from __future__ import annotations

from lib.log import get_logger
from lib.memory.tools import MEMORY_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)


# ── Memory operation handlers (registry pattern) ────────────────────

def _memory_create(fn_args, project_path, extra_paths=None):
    # New memories are always written to the PRIMARY project_path;
    # extra_paths is accepted for a uniform handler signature but ignored.
    from lib.memory.storage import create_memory
    mem = create_memory(
        name=fn_args.get('name', 'Untitled Memory'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=(fn_args.get('scope')
               or ('project' if project_path else 'global')),
        project_path=project_path,
    )
    content = (
        f"Memory created: **{mem['name']}** "
        f"(id: {mem['id']}, scope: {mem['scope']})\n\n"
        f"This memory will be available in future conversations when Memory mode is enabled."
    )
    return content, '💡 saved', f"💡 Memory: {fn_args.get('name', '?')}"


def _memory_update(fn_args, project_path, extra_paths=None):
    from lib.memory.storage import update_memory
    sid = fn_args.get('memory_id', '')
    mem = update_memory(
        memory_id=sid,
        updates={k: v for k, v in fn_args.items() if k not in ('memory_id',) and v is not None},
        project_path=project_path,
        extra_paths=extra_paths,
    )
    if mem is None:
        logger.warning('[Memory] update_memory returned None for memory_id=%s', sid)
        return f"Memory not found: {sid}", '❌ not found', f"✏️ Memory: {sid}"
    return f"Memory updated: **{mem['name']}** (id: {mem['id']})", '✏️ updated', f"✏️ Memory: {mem['name']}"


def _memory_delete(fn_args, project_path, extra_paths=None):
    from lib.memory.storage import delete_memory
    sid = fn_args.get('memory_id', '')
    deleted = delete_memory(memory_id=sid, project_path=project_path,
                            extra_paths=extra_paths)
    if deleted:
        return f"Memory deleted: {sid}", '🗑️ deleted', f"🗑️ Memory: {sid}"
    return f"Memory not found: {sid}", '❌ not found', f"🗑️ Memory: {sid}"


def _memory_merge(fn_args, project_path, extra_paths=None):
    from lib.memory.storage import merge_memories
    result = merge_memories(
        memory_ids=fn_args.get('memory_ids', []),
        name=fn_args.get('name', 'Merged Memory'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=(fn_args.get('scope')
               or ('project' if project_path else 'global')),
        project_path=project_path,
        extra_paths=extra_paths,
    )
    merged = result['merged_memory']
    n_del = len(result['deleted_ids'])
    failed = result.get('failed_ids') or []
    content = (
        f"Merged {n_del} memories → **{merged['name']}** (id: {merged['id']}, scope: {merged['scope']})"
    )
    if failed:
        content += (
            f"\n\n⚠️ {len(failed)} source memory(ies) could NOT be deleted and "
            f"remain as duplicates of the merged memory: {', '.join(failed)}. "
            f"Delete them manually if the merge should replace them."
        )
    return (
        content,
        f'🔀 merged {n_del}' + (f' ({len(failed)} kept)' if failed else ''),
        f"🔀 Merge → {fn_args.get('name', '?')}",
    )


def _memory_search(fn_args, project_path, extra_paths=None):
    from lib.memory.relevance import search_memories
    query = fn_args.get('query', '')
    top_k = fn_args.get('top_k', 30)
    result = search_memories(
        query=query,
        project_path=project_path,
        top_k=top_k,
        extra_paths=extra_paths,
    )
    snippet = query[:80] if query else '(empty)'
    return result, '🔍 searched', f'🔍 Memory search: {snippet}'


# Module-level dispatch table — maps memory fn_name → handler.
_MEMORY_OP_DISPATCH = {
    'create_memory': _memory_create,
    'update_memory': _memory_update,
    'delete_memory': _memory_delete,
    'merge_memories': _memory_merge,
    'search_memories': _memory_search,
}


@tool_registry.tool_set(MEMORY_TOOL_NAMES, category='memory',
                        description='Create, update, delete, or merge memories')
def _handle_memory_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    _proj = project_path if project_enabled else None
    # Multi-root: read/locate memories across the primary + extra roots
    # (projectPaths[1:]). New memories are still written to the primary.
    _extra_paths = []
    if _proj:
        _all_paths = (cfg or {}).get('projectPaths') or []
        _extra_paths = [p for p in _all_paths[1:]
                        if p and p != _proj] if len(_all_paths) > 1 else []
    memory_ok = False
    try:
        handler = _MEMORY_OP_DISPATCH.get(fn_name)
        if handler is None:
            raise ValueError(f'Unknown memory operation: {fn_name}')
        tool_content, badge_ok, title = handler(fn_args, _proj, _extra_paths)
        memory_ok = True
    except ValueError as e:
        # Invalid model arguments are a settled tool result, not an executor
        # crash. Keep one actionable warning without a misleading traceback;
        # unexpected implementation/storage failures below retain exc_info.
        logger.warning('[Executor] memory operation %s rejected: %s',
                       fn_name, e)
        tool_content = f"Failed to {fn_name.replace('_', ' ')}: {e}"
        badge_ok = '❌ failed'
        title = f"❌ {fn_name}: error"
    except Exception as e:
        logger.warning('[Executor] memory operation %s failed: %s',
                       fn_name, e, exc_info=True)
        tool_content = f"Failed to {fn_name.replace('_', ' ')}: {e}"
        badge_ok = '❌ failed'
        title = f"❌ {fn_name}: error"

    meta = _build_simple_meta(
        fn_name, tool_content, source='Memory',
        title=title,
        snippet=(fn_args.get('description', '') or fn_args.get('memory_id', ''))[:120],
        badge=badge_ok if memory_ok else '❌ failed',
        extra={'memoryOk': memory_ok},
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
