"""Workspace-root name resolution for filesystem tool-call display pills.

In a multi-root workspace, filesystem tool calls carry a ``_toolRoot`` name
so the frontend can render a ``rootname:`` prefix pill on the tool-call
line.  This module resolves that name from either an explicit
``rootname:rel/path`` prefix or a longest-prefix match against the
registered roots.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)


# ── Filesystem tools whose display lines should carry a workspace-root pill
#    in multi-root workspaces.  Mirrors PROJECT_TOOL_NAMES + read_files
#    (which is global, not in PROJECT_TOOL_NAMES).  Non-filesystem tools
#    (web_search, fetch_url, browser_*, mcp__*, …) are intentionally excluded
#    — the rootname pill is only meaningful for paths the user could
#    distinguish between project roots.
_FS_TOOLS_FOR_ROOT_PILL = frozenset({
    'read_files', 'inspect_image', 'list_dir', 'grep_search', 'find_files',
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'run_command',
})


def _split_rootname_prefix(path_str):
    """Parse ``rootname:rel/path`` → (rootname, rest).  Returns ('', path)
    when no prefix is present.  Mirrors the rules used by
    ``resolve_namespaced_path`` and the frontend's ``_splitRoot`` helper:
    skips absolute paths, Windows drive letters (single ASCII char), and
    anything where a ``/`` precedes the ``:``.
    """
    if not path_str or not isinstance(path_str, str):
        return '', path_str or ''
    if path_str.startswith('/') or path_str.startswith('~'):
        return '', path_str
    if os.path.isabs(path_str):
        return '', path_str
    ci = path_str.find(':')
    if ci <= 0 or ci >= 40:
        return '', path_str
    si = path_str.find('/')
    if si != -1 and si < ci:
        return '', path_str
    head = path_str[:ci]
    # Windows drive letter heuristic
    if len(head) == 1 and head.isalpha():
        return '', path_str
    if not head or '\\' in head:
        return '', path_str
    return head, path_str[ci + 1:] or '.'


def _extract_first_path_arg(fn_name, fn_args):
    """Return the first path-like value from fn_args for a filesystem tool,
    or '' when none is present.  Handles batch shapes (``reads`` /
    ``edits`` / ``searches``) by inspecting the first entry that has a
    ``path``.  ``run_command`` is keyed off ``working_dir`` (the cwd).
    """
    if not isinstance(fn_args, dict):
        return ''
    if fn_name == 'run_command':
        return fn_args.get('working_dir') or ''
    if fn_name == 'read_files':
        reads = fn_args.get('reads')
        if isinstance(reads, list):
            for r in reads:
                if isinstance(r, dict) and r.get('path'):
                    return r['path']
                if isinstance(r, str) and r:
                    return r
        if fn_args.get('path'):
            return fn_args['path']
        return ''
    if fn_name in ('apply_diff', 'insert_content', 'inspect_image'):
        return fn_args.get('path') or ''
    if fn_name in ('edit_file', 'apply_diffs', 'insert_contents'):
        edits = fn_args.get('edits')
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, dict) and e.get('path'):
                    return e['path']
        return ''
    if fn_name == 'grep_search' or fn_name == 'find_files':
        searches = fn_args.get('searches')
        if isinstance(searches, list):
            for s in searches:
                if isinstance(s, dict) and s.get('path'):
                    return s['path']
        return fn_args.get('path') or ''
    return fn_args.get('path') or ''


def _resolve_tool_root_name(fn_name, fn_args, conv_id=None):
    """Determine the workspace-root name a filesystem tool call targets.

    Returns the rootname string, or '' when:
      - the tool is not a filesystem tool we want to label,
      - no roots are registered (single-root setup pre-init),
      - or only a single root exists (single-root workspace — rendering
        is unchanged in that case).

    Uses ``rootname:`` prefix when present; otherwise resolves to the
    primary root for the conversation (or the global primary).
    """
    if fn_name not in _FS_TOOLS_FOR_ROOT_PILL:
        return ''
    try:
        from lib.project_mod.config import (
            _conv_primary,
            _conv_roots,
            _lock,
            _roots,
            _state,
        )
    except Exception as e:
        logger.debug('[ToolDisplay] root-name lookup unavailable: %s', e)
        return ''

    # Pick the registry that scopes this conv (falls back to global).
    #
    # Multi-root detection must consider BOTH registries. The conv-scoped
    #   map can lag the global one (e.g. user opens a new conv while the
    #   global registry already has 4 roots; conv_map starts empty or
    #   single-entry). Suppressing the prefix when EITHER registry alone
    #   is single-entry produced the visible inconsistency on this conv —
    #   identical paths got the prefix on some rounds, lost it on others,
    #   purely based on which side of the registry was checked. Treat the
    #   workspace as multi-root if EITHER registry says so.
    with _lock:
        conv_map = _conv_roots.get(conv_id) if conv_id else None
        global_count = len(_roots)
        conv_count = len(conv_map) if conv_map else 0
        if max(global_count, conv_count) <= 1:
            return ''
        # Prefer conv-scoped registry for resolution; fall back to global
        # if conv-scoped is empty or has only the primary in it.
        registry = conv_map if (conv_map and conv_count > 1) else _roots
        # Snapshot what we need under the lock.
        registry_items = [(rn, rs.get('path', '')) for rn, rs in registry.items()]
        primary_path = ''
        if conv_id:
            primary_path = _conv_primary.get(conv_id, '') or ''
        if not primary_path:
            primary_path = _state.get('path') or ''

    raw_path = _extract_first_path_arg(fn_name, fn_args)
    head, _rest = _split_rootname_prefix(raw_path)
    if head:
        # Match registry name (case-insensitive fallback).
        names = [rn for rn, _ in registry_items]
        if head in names:
            return head
        for rn in names:
            if rn.lower() == head.lower():
                return rn
        # Unknown root prefix — surface what the model wrote so the user
        # can see the typo / stale name in the UI.
        return head

    # No explicit ``rootname:`` prefix. When the path is ABSOLUTE, attribute
    # it to whichever registered root contains it (longest-prefix match).
    # Without this, an absolute path under a NON-primary root (e.g. the model
    # reading ``/abs/to/FDP/hope/op2_train.sh`` while ``chatui`` is primary)
    # would skip the prefix branch above and fall through to the primary
    # fallback below — mislabeled as the primary root, or unlabeled. The
    # longest-prefix match disambiguates nested roots correctly.
    if raw_path and (raw_path.startswith('/') or raw_path.startswith('~')
                     or os.path.isabs(raw_path)):
        try:
            abs_path = os.path.abspath(os.path.expanduser(raw_path))
        except (OSError, ValueError) as e:
            logger.debug('[ToolDisplay] abspath(%r) failed: %s', raw_path, e)
            abs_path = ''
        if abs_path:
            best_name, best_len = '', -1
            for rn, rp in registry_items:
                if not rp:
                    continue
                try:
                    abs_root = os.path.abspath(os.path.expanduser(rp))
                except (OSError, ValueError) as e:
                    logger.debug('[ToolDisplay] abspath(%r) failed: %s', rp, e)
                    continue
                if abs_path == abs_root or abs_path.startswith(abs_root.rstrip('/') + '/'):
                    if len(abs_root) > best_len:
                        best_name, best_len = rn, len(abs_root)
            if best_name:
                return best_name

    # No prefix — fall back to the primary root's name.
    if primary_path:
        try:
            abs_primary = os.path.abspath(primary_path)
        except (OSError, ValueError) as e:
            logger.debug('[ToolDisplay] abspath(%r) failed: %s', primary_path, e)
            abs_primary = primary_path
        for rn, rp in registry_items:
            try:
                if os.path.abspath(rp) == abs_primary:
                    return rn
            except (OSError, ValueError) as e:
                logger.debug('[ToolDisplay] abspath(%r) failed: %s', rp, e)
                if rp == primary_path:
                    return rn
    return ''
