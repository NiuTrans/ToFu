# HOT_PATH
"""Write-approval gating — metadata enrichers + the ``_handle_approval`` gate.

Registry pattern: each tool type that needs approval has a dedicated
enricher function that augments the base ``approval_meta`` dict.  The
``_handle_approval`` gate emits the ``write_approval_request`` event and
blocks waiting for the user's decision — it NEVER executes the tool.
"""

from __future__ import annotations

import uuid
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.approval import request_write_approval
from lib.tasks_pkg.executor import _finalize_tool_round
from lib.tasks_pkg.manager import append_event
from lib.tools import build_project_tool_meta

logger = get_logger(__name__)


# ── Approval metadata enrichers ────────────────────────────────────────
# Registry pattern: each tool type that needs approval has a dedicated
# function that enriches the base ``approval_meta`` dict.

def _approval_meta_run_command(approval_meta, fn_args):
    """Enrich approval metadata for ``run_command``."""
    approval_meta['command'] = fn_args.get('command', '')
    approval_meta['description'] = fn_args.get('description', '')
    approval_meta['path'] = fn_args.get('working_dir', '') or ''


def _approval_meta_write_file(approval_meta, fn_args):
    """Enrich approval metadata for ``write_file``."""
    content = fn_args.get('content', '')
    approval_meta['contentPreview'] = content[:500] + ('…' if len(content) > 500 else '')
    approval_meta['contentLines'] = content.count('\n') + 1
    approval_meta['contentChars'] = len(content)


def _approval_meta_apply_diff(approval_meta, fn_args):
    """Enrich approval metadata for ``apply_diff`` (single edit)."""
    search_text = fn_args.get('search', '')
    replace_text = fn_args.get('replace', '')
    approval_meta['search'] = search_text[:2000] + ('…' if len(search_text) > 2000 else '')
    approval_meta['replace'] = replace_text[:2000] + ('…' if len(replace_text) > 2000 else '')
    approval_meta['searchLines'] = search_text.count('\n') + 1
    approval_meta['searchChars'] = len(search_text)
    approval_meta['replaceLines'] = replace_text.count('\n') + 1
    approval_meta['replaceChars'] = len(replace_text)
    if fn_args.get('replace_all'):
        approval_meta['replaceAll'] = True


def _approval_meta_apply_diffs(approval_meta, fn_args):
    """Enrich approval metadata for ``apply_diffs`` (batch)."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = f'Batch: {len(edits)} edits across {len(paths)} file(s)'
    edit_summaries = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        s_text = e.get('search', '')
        r_text = e.get('replace', '')
        edit_summaries.append({
            'path': e.get('path', '?'),
            'description': e.get('description', ''),
            'search': s_text[:500] + ('…' if len(s_text) > 500 else ''),
            'replace': r_text[:500] + ('…' if len(r_text) > 500 else ''),
            'searchLines': s_text.count('\n') + 1,
            'replaceLines': r_text.count('\n') + 1,
        })
    approval_meta['editSummaries'] = edit_summaries


def _approval_meta_insert_content(approval_meta, fn_args):
    """Enrich approval metadata for ``insert_content`` (single insertion)."""
    anchor_text = fn_args.get('anchor', '')
    content_text = fn_args.get('content', '')
    pos = fn_args.get('position', 'after')
    approval_meta['search'] = anchor_text[:2000] + ('…' if len(anchor_text) > 2000 else '')
    approval_meta['replace'] = content_text[:2000] + ('…' if len(content_text) > 2000 else '')
    approval_meta['searchLines'] = anchor_text.count('\n') + 1
    approval_meta['searchChars'] = len(anchor_text)
    approval_meta['replaceLines'] = content_text.count('\n') + 1
    approval_meta['replaceChars'] = len(content_text)
    approval_meta['description'] = approval_meta.get('description', '') or f'Insert {pos} anchor'


def _approval_meta_insert_contents(approval_meta, fn_args):
    """Enrich approval metadata for ``insert_contents`` (batch)."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = f'Batch: {len(edits)} insertions across {len(paths)} file(s)'
    edit_summaries = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        anchor_text = e.get('anchor', '')
        content_text = e.get('content', '')
        pos = e.get('position', 'after')
        edit_summaries.append({
            'path': e.get('path', '?'),
            'description': e.get('description', f'Insert {pos} anchor'),
            'search': anchor_text[:500] + ('…' if len(anchor_text) > 500 else ''),
            'replace': content_text[:500] + ('…' if len(content_text) > 500 else ''),
            'searchLines': anchor_text.count('\n') + 1,
            'replaceLines': content_text.count('\n') + 1,
        })
    approval_meta['editSummaries'] = edit_summaries


def _approval_meta_create_project(approval_meta, fn_args):
    """Enrich approval metadata for ``create_project``."""
    target_path = fn_args.get('path', '')
    root_name = fn_args.get('name', '')
    overwrite = bool(fn_args.get('overwrite', False))
    approval_meta['path'] = target_path
    approval_meta['rootName'] = root_name
    approval_meta['overwrite'] = overwrite
    if overwrite:
        approval_meta['description'] = (
            f'Create / register workspace root at {target_path} '
            f'(overwrite=true — existing non-empty dir will be registered as-is)'
        )
    else:
        approval_meta['description'] = f'Create new workspace root at {target_path}'


# Module-level dispatch table — maps tool name → approval meta enricher.
# Only tools that need special approval metadata are listed; tools not in
# this dict get the base metadata only (path + description).
_APPROVAL_META_ENRICHERS = {
    'run_command':      _approval_meta_run_command,
    'write_file':       _approval_meta_write_file,
    'apply_diff':       _approval_meta_apply_diff,
    'apply_diffs':      _approval_meta_apply_diffs,
    'insert_content':   _approval_meta_insert_content,
    'insert_contents':  _approval_meta_insert_contents,
    'create_project':   _approval_meta_create_project,
}


def _handle_approval(
    task: dict[str, Any],
    fn_name: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    project_path: str | None,
    round_num: int,
    model: str,
) -> tuple[bool, str | None]:
    """Gate a write operation on manual user approval (no execution).

    Emits a ``write_approval_request`` event and blocks waiting for the user
    response. This function ONLY decides — it never executes the tool. On
    approval the caller lets the tool fall through to the normal serial
    write-tool dispatch, so a single execution path serves project writes,
    run_command, memory, MCP, and custom write tools.

    Uses the :data:`_APPROVAL_META_ENRICHERS` dispatch table to build
    tool-specific approval metadata.

    Returns
    -------
    tuple[bool, str | None]
        ``(approved, reject_content)``. When approved, ``(True, None)`` and
        ``round_entry`` is left ``pending_approval`` for the executor to
        finalize. When rejected, ``(False, message)`` and the round has
        already been finalized with a ``rejected`` badge.
    """
    tid = task['id'][:8]
    approval_id = f'{task["id"]}_{uuid.uuid4().hex[:8]}'
    approval_meta = {
        'approvalId': approval_id,
        'toolName': fn_name,
        'path': fn_args.get('path', ''),
        'description': fn_args.get('description', ''),
    }

    # Dispatch to tool-specific enricher (if one exists)
    enricher = _APPROVAL_META_ENRICHERS.get(fn_name)
    if enricher is not None:
        enricher(approval_meta, fn_args)

    round_entry['status'] = 'pending_approval'
    round_entry['approvalId'] = approval_id
    round_entry['approvalMeta'] = approval_meta
    append_event(task, build_event(
        EventType.WRITE_APPROVAL_REQUEST,
        roundNum=rn,
        toolCallId=round_entry.get('toolCallId', ''),
        approvalId=approval_id,
        meta=approval_meta,
    ))
    logger.debug(
        '[Task %s] Waiting for write approval: tool=%s path=%s round=%d model=%s',
        tid, fn_name, fn_args.get('path', ''), round_num, model,
    )

    approved = request_write_approval(approval_id, timeout=120)

    if not approved:
        tool_content = f'⚠️ User rejected this {fn_name} operation on {fn_args.get("path", "")}.'
        meta = build_project_tool_meta(fn_name, fn_args, tool_content)
        meta['badge'] = 'rejected'
        meta['writeOk'] = False
        _finalize_tool_round(task, rn, round_entry, [meta])
        return False, tool_content

    # Approved — do NOT execute here. The caller lets the item fall through to
    # the normal serial write-tool dispatch (_execute_tool_one), so a single
    # execution path serves project writes, run_command, memory, MCP, and custom
    # write tools uniformly. round_entry stays 'pending_approval' until that
    # execution finalizes it.
    logger.info('[Task %s] Write approved: tool=%s — dispatching via normal path',
                tid, fn_name)
    return True, None
