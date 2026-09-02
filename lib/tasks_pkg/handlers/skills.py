"""Runtime handlers for bounded skill discovery, loading, and installation."""

from __future__ import annotations

from lib.log import get_logger
from lib.skills import SKILL_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tool_rejection import stamp_tool_rejection

logger = get_logger(__name__)


def _extra_project_paths(cfg: dict | None, primary: str | None) -> list[str]:
    return [
        path for path in ((cfg or {}).get('projectPaths') or [])
        if path and path != primary
    ]


def _result_meta(fn_name: str, title: str, content: str, *, ok: bool) -> dict:
    success_badge = '📦 loaded' if fn_name == 'load_skill' else '📦 ready'
    return _build_simple_meta(
        fn_name,
        content,
        source='Skill',
        title=title,
        snippet=content.split('\n', 1)[0][:160],
        badge=success_badge if ok else '❌ blocked',
    )


@tool_registry.tool_set(
    SKILL_TOOL_NAMES,
    category='skills',
    description='Discover and progressively disclose user-owned skills')
def _handle_skill_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                       cfg, project_path, project_enabled, all_tools=None):
    """Execute one skill operation under the task's explicit owner."""
    owner_user_id = task.get('_userId')
    primary = project_path if project_enabled else None
    extras = _extra_project_paths(cfg, primary)
    title = str(
        fn_args.get('skill_id') or fn_args.get('catalog_id')
        or fn_args.get('query') or '')
    status = 'done'

    try:
        from lib.identity import require_user_id
        owner_user_id = require_user_id(
            owner_user_id, context='skill tool execution')
        if fn_name == 'search_skills':
            from lib.skills.discovery import (
                render_skill_search,
                search_skills,
            )
            query = str(fn_args.get('query') or '')
            search_result = search_skills(
                query,
                limit=fn_args.get('limit', 5),
                online=bool(fn_args.get('online', True)),
                project_path=primary,
                extra_paths=extras,
                owner_user_id=owner_user_id,
            )
            content = render_skill_search(
                query,
                list(search_result.get('matches') or ()),
                online_status=dict(search_result.get('online') or {}),
            )

        elif fn_name == 'load_skill':
            from lib.skills import load_skill
            content = load_skill(
                str(fn_args.get('skill_id') or ''),
                project_path=primary,
                extra_paths=extras,
                owner_user_id=owner_user_id,
            )

        elif fn_name == 'read_skill_resource':
            from lib.skills import read_skill_resource
            content = read_skill_resource(
                str(fn_args.get('skill_id') or ''),
                str(fn_args.get('resource') or ''),
                cursor=fn_args.get('cursor', 0),
                max_chars=fn_args.get('max_chars', 6000),
                project_path=primary,
                extra_paths=extras,
                owner_user_id=owner_user_id,
            )

        elif fn_name == 'request_skill_install':
            from lib.skills.catalog_install import CatalogInstallError
            from lib.tasks_pkg.tool_dispatch._approval import (
                consume_approval_receipt,
            )
            if not consume_approval_receipt(
                    task, fn_name, tc_id, fn_args):
                content = (
                    'Skill installation blocked: this exact call has no '
                    'unconsumed human-approval receipt.')
                status = 'rejected'
                stamp_tool_rejection(
                    round_entry,
                    {'kind': 'approval_receipt_missing', 'tool': fn_name},
                    reason=content, retryable=False,
                )
            else:
                from lib.skills.catalog_install import install_catalog_skill
                try:
                    result = install_catalog_skill(
                        str(fn_args.get('catalog_id') or ''),
                        owner_user_id=owner_user_id,
                        source_revision=(
                            str(fn_args.get('source_revision') or '') or None),
                        project_path=primary,
                        scope=str(fn_args.get('scope') or 'global'),
                        overwrite=bool(fn_args.get('overwrite', False)),
                    )
                except CatalogInstallError as exc:
                    content = (
                        f'Skill installation failed ({exc.code}): {exc}')
                    status = 'error'
                else:
                    memory = result['memory']
                    content = '\n'.join([
                        'Skill installed and verified.',
                        f'id: {memory["id"]}',
                        f'name: {memory.get("name") or memory["id"]}',
                        f'scope: {memory.get("scope") or "global"}',
                        f'catalog_id: {result["catalog_id"]}',
                        f'source_revision: {result["source_revision"]}',
                        f'source_registry: '
                        f'{result.get("source_registry") or "curated"}',
                        f'content_sha256: {result["content_sha256"]}',
                        'bundled_scripts_executed: false',
                        f'Next: call load_skill with skill_id={memory["id"]!r}.',
                    ])
        else:
            content = f'Unknown skill tool: {fn_name}'
            status = 'error'
    except Exception as exc:
        logger.warning('[Skills] %s failed: %s', fn_name, exc, exc_info=True)
        content = 'Skill operation failed safely; no unverified change was made.'
        status = 'error'

    ok = status == 'done' and not content.startswith((
        'Skill not found:', 'Skill **', 'Invalid skill',
        'Failed to read skill', 'Failed to stat skill resource:',
        'Failed to read skill resource:', 'Skill resource not found:',
        'Skill resource is too large', 'Skill resource is binary',
        'Skill operation failed:',
    ))
    meta = _result_meta(fn_name, title, content, ok=ok)
    if status != 'done':
        meta['writeOk'] = False
    _finalize_tool_round(
        task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
