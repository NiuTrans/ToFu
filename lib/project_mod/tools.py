"""Project tool implementations — dispatch façade.

Individual tool groups have been extracted into sibling modules:
  read_tools.py   — list_dir, read_file, read_files, grep, find_files
  write_tools.py  — write_file, apply_diff, apply_diffs, _find_closest_match
  run_command.py  — run_command + its command-execution machinery
                    (_clean_command_output, destructive-guards, snapshots, …)

This file retains:
  - browse_directory
  - Tool dispatch (execute_tool, project_tool_display, _resolve_base)
  - Re-exports of all symbols from read_tools / write_tools / run_command
    for backward compat
"""
import os
import shutil
import time

from lib.log import audit_log, get_logger
from lib.project_mod.config import (
    CODE_EXTENSIONS,
)
from lib.project_mod.scanner import (
    _fmt_size,
)

logger = get_logger(__name__)

# ── Re-export from run_command (backward compat) ──
from lib.project_mod.run_command import (  # noqa: E402,F401
    _clean_command_output,
    _collect_descendants,
    _diff_snapshots,
    _extract_device_ids,
    _extract_progress_label,
    _extract_progress_pct,
    _extract_write_targets,
    _filter_changes_by_targets,
    _format_cuda_device_range,
    _format_run_output,
    _get_cmd_env,
    _has_unquoted_shell_metachars,
    _is_any_child_reading_stdin,
    _is_catastrophic_delete,
    _is_destructive_command,
    _kill_process_tree,
    _line_fingerprint,
    _maybe_harden_grep_command,
    _maybe_wrap_rm_with_trash,
    _record_run_command_changes,
    _run_command_interactive,
    _run_command_simple,
    _safe_on_chunk,
    _snapshot_project_files,
    _split_pipeline,
    tool_run_command,
)

# ── Re-export from read_tools (backward compat) ──
from lib.project_mod.read_tools import (  # noqa: E402,F401
    _extract_symbols,
    _merge_same_file_ranges,
    _python_grep,
    tool_find_files,
    tool_find_files_batch,
    tool_grep,
    tool_grep_batch,
    tool_inspect_image,
    tool_list_dir,
    tool_read_files,
)

# ── Re-export from write_tools (backward compat) ──
from lib.project_mod.write_tools import (  # noqa: E402,F401
    _apply_one_diff,
    _find_closest_match,
    _insert_one,
    _resolve_write_path,
    _touch_for_vscode,
    tool_apply_diff,
    tool_apply_diffs,
    tool_create_project,
    tool_insert_content,
    tool_insert_contents,
    tool_write_file,
)


# ═══════════════════════════════════════════════════════
#  ★ Directory Browser — NEW
# ═══════════════════════════════════════════════════════

def browse_directory(path_str=None, show_hidden=False):
    """List subdirectories at a given path for folder browser UI."""
    if not path_str or path_str == '~':
        path_str = os.path.expanduser('~')
    abs_path = os.path.abspath(os.path.expanduser(path_str))

    if not os.path.isdir(abs_path):
        return {'error': f'Not a directory: {abs_path}', 'path': abs_path}

    parent = os.path.dirname(abs_path)
    dirs = []
    files_count = 0
    try:
        for entry in sorted(os.scandir(abs_path), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not show_hidden and entry.name.startswith('.'):
                        continue
                    # Check if it looks like a project (has code files)
                    has_code = False
                    item_count = 0
                    try:
                        for sub in os.scandir(entry.path):
                            item_count += 1
                            if item_count > 100:
                                break
                            ext = os.path.splitext(sub.name)[1].lower()
                            if ext in CODE_EXTENSIONS:
                                has_code = True
                    except (PermissionError, OSError) as e:
                        logger.debug('[Tools] dir scan failed for %s: %s', entry.name, e, exc_info=True)
                    dirs.append({
                        'name': entry.name,
                        'path': entry.path,
                        'itemCount': item_count,
                        'hasCode': has_code,
                        'hidden': entry.name.startswith('.'),
                    })
                elif entry.is_file(follow_symlinks=False):
                    files_count += 1
            except (PermissionError, OSError) as e:
                logger.debug('[Tools] entry processing failed for entry: %s', e, exc_info=True)
                continue
    except PermissionError:
        logger.debug('[Tools] permission denied scanning %s', abs_path, exc_info=True)
        return {'error': f'Permission denied: {abs_path}', 'path': abs_path}

    return {
        'path': abs_path,
        'parent': parent if parent != abs_path else None,
        'dirs': dirs,
        'filesCount': files_count,
        'showHidden': show_hidden,
    }


def create_directory(parent_str, name):
    """Create a new sub-directory under *parent_str* for the folder browser UI.

    Backs the "New folder" action of the project path panel so a user can
    scaffold a project directory without leaving the app.

    Args:
        parent_str: Absolute path of the parent directory (``~`` is expanded).
            Must already exist and be a directory.
        name: New folder name. A single path segment only — separators and
            ``..`` are rejected so a create can never escape *parent_str*.

    Returns:
        dict with ``path`` (the created dir) on success, or ``error`` on
        failure. Never raises for the expected cases (invalid name, missing
        parent, permission denied, already exists).
    """
    from lib.project_mod.write_tools import _is_forbidden_create_path

    if not parent_str:
        return {'error': 'No parent directory provided'}
    abs_parent = os.path.abspath(os.path.expanduser(parent_str))
    if not os.path.isdir(abs_parent):
        return {'error': f'Not a directory: {abs_parent}'}

    clean = (name or '').strip()
    # A folder name must be a single, non-navigating path segment. Reject any
    # separator or parent-ref so the create is confined to abs_parent.
    if (not clean or clean in ('.', '..')
            or '/' in clean or '\\' in clean or os.sep in clean
            or (os.altsep and os.altsep in clean)):
        return {'error': 'Invalid folder name — use a single name without slashes'}

    abs_path = os.path.join(abs_parent, clean)
    # Defense in depth: the resolved path must still live directly under parent.
    if os.path.dirname(abs_path) != abs_parent:
        return {'error': 'Invalid folder name'}
    if _is_forbidden_create_path(abs_path):
        return {'error': f'Refusing to create a folder at a system path: {abs_path}'}
    if os.path.exists(abs_path):
        return {'error': f'Already exists: {clean}'}

    try:
        os.mkdir(abs_path)
    except PermissionError:
        logger.warning('[Tools] create_directory permission denied: %s', abs_path)
        return {'error': f'Permission denied: {abs_parent}'}
    except OSError as e:
        logger.warning('[Tools] create_directory failed for %s: %s', abs_path, e)
        return {'error': f'Cannot create folder: {e}'}

    audit_log('project_folder_create', path=abs_path)
    logger.info('[Tools] create_directory: %s', abs_path)
    return {'ok': True, 'path': abs_path, 'name': clean, 'parent': abs_parent}


def delete_directory(path_str):
    """Move a directory to the recoverable trash bin for the folder browser UI.

    Backs the "Delete folder" action of the project path panel. Rather than an
    irreversible ``rmtree``, the target is MOVED into a ``.tofu_trash`` bin
    (same recoverable-delete convention as the ``rm`` shim in run_command), so
    a mis-click can be undone from the filesystem.

    Args:
        path_str: Absolute path of the directory to delete (``~`` expanded).

    Returns:
        dict with ``trashed`` (the trash location) on success, or ``error``.
        Refuses forbidden system paths, registered workspace roots, and any
        path that is not an existing directory.
    """
    from lib.agent_artifacts import TRASH_DIR
    from lib.project_mod.config import get_roots
    from lib.project_mod.write_tools import _is_forbidden_create_path

    if not path_str:
        return {'error': 'No path provided'}
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        return {'error': f'Not a directory: {abs_path}'}
    if os.path.islink(abs_path):
        return {'error': 'Refusing to delete a symlink'}
    if _is_forbidden_create_path(abs_path):
        return {'error': f'Refusing to delete a system path: {abs_path}'}

    # Never delete a directory that is currently a registered workspace root —
    # remove it from the workspace first. Guards against nuking the open project.
    try:
        root_paths = {os.path.realpath(rs['path']) for rs in get_roots().values()}
    except Exception as e:
        logger.debug('[Tools] delete_directory root snapshot failed: %s', e)
        root_paths = set()
    if os.path.realpath(abs_path) in root_paths:
        return {'error': 'This folder is an active workspace root — '
                         'remove it from the workspace before deleting.'}

    # Move into a timestamped trash bin beside the parent so it stays on the
    # same filesystem (a rename, not a cross-device copy) and is recoverable.
    parent = os.path.dirname(abs_path)
    trash_root = os.path.join(parent, TRASH_DIR)
    dest_dir = os.path.join(trash_root, str(int(time.time() * 1000)))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(abs_path))
        shutil.move(abs_path, dest)
    except PermissionError:
        logger.warning('[Tools] delete_directory permission denied: %s', abs_path)
        return {'error': f'Permission denied: {abs_path}'}
    except OSError as e:
        logger.warning('[Tools] delete_directory failed for %s: %s', abs_path, e)
        return {'error': f'Cannot delete folder: {e}'}

    audit_log('project_folder_delete', path=abs_path, trashed=dest)
    logger.info('[Tools] delete_directory: %s → %s', abs_path, dest)
    return {'ok': True, 'path': abs_path, 'trashed': dest, 'parent': parent}


# ═══════════════════════════════════════════════════════
#  Tool Dispatch
# ═══════════════════════════════════════════════════════

def _resolve_base(base_path, rel_path, conv_id=None):
    """Resolve base_path + rel_path, supporting multi-root 'name:path' syntax.

    If rel_path contains ':', treat the part before ':' as a root name.
    Otherwise fall back to the provided base_path.

    ★ Cross-root safety: when multiple roots are configured, checks if
    the requested relative path exists under the primary root.  If it
    does NOT exist there but DOES exist under exactly one other root,
    auto-routes to that root and logs a warning.  This prevents the
    common model mistake of writing files intended for root B into root A.

    ★ conv_id scoping (2026-05-05): when the caller knows which
    conversation's root registry should authoritatively answer this
    resolution, pass the full conv_id.  resolve_namespaced_path will
    check that conv's registry first so concurrent tasks cannot
    clobber each other's root namespaces.  Falls back to the shared
    global _roots when no conv-specific match is found.

    Self-healing fallback: if no conv-specific registry answers AND
    ``base_path`` is provided AND its basename matches the root name
    used in ``rel_path``, resolve to ``base_path`` + rel.  This covers
    the concurrent-clobber case where a task's global _roots entry was
    overwritten by another task after the system prompt was built but
    before the tool call executed.

    Returns (effective_base, effective_rel).
    """
    if rel_path and ':' in rel_path and not os.path.isabs(rel_path):
        # Check it's not a Windows drive letter like C:\...
        colon_idx = rel_path.index(':')
        # Reject prefixes that can't be a workspace root name: JSON/array
        # punctuation or whitespace before the colon means rel_path is a
        # serialized blob (e.g. a stringified reads array '[{"path": ...]'),
        # not 'rootname:path'.  Treating it as a root produced misleading
        # "Unknown workspace root '[{\"path\"'" errors.
        _looks_like_root = not any(c in rel_path[:colon_idx] for c in '[]{}"\'\t\n ')
        if colon_idx > 0 and colon_idx < 40 and _looks_like_root:  # reasonable name length
            from lib.project_mod.config import resolve_namespaced_path
            try:
                return resolve_namespaced_path(rel_path, conv_id=conv_id)
            except ValueError as _ve:
                _name, _, _rest = rel_path.partition(':')
                # ── Self-heal: base_path's basename matches the requested
                #    root name → this is almost certainly the concurrent-
                #    clobber case (we *are* in the task whose root that is,
                #    but some other task wiped the global registry).  Resolve
                #    to the provided base_path.  Safe because the name and
                #    path agree by construction.
                if base_path:
                    bp_basename = os.path.basename(os.path.abspath(base_path))
                    if bp_basename == _name or bp_basename.lower() == _name.lower():
                        logger.info('[Tools] Self-heal namespaced path %r: '
                                    'base_path basename matches unknown root — '
                                    'resolving to base_path (conv-state race workaround). '
                                    'conv_id=%s',
                                    rel_path, conv_id[:12] if conv_id else '?')
                        return base_path, (_rest or '.')
                # ★ DO NOT silently strip the 'name:' prefix. Stripping
                #   it converts a model typo ('CDP:foo' when meant 'cdp:foo',
                #   or a stale root that was cleared by set_project) into a
                #   DATA-LOSS bug: the write tools fall back to the primary
                #   root and silently overwrite whatever file with the same
                #   relative name exists there.  See the
                #   create_project_frontend_sync_bug memo.
                #
                #   Instead, raise a sentinel that path-taking tools surface
                #   as an explicit error to the model.  The only legitimate
                #   case for a colon in a path is a Windows drive letter
                #   ('C:\...'), which is already excluded by isabs() above.
                # Log ONCE here with full context. Task-executor layers
                # that re-raise should NOT re-log this as WARNING — they
                # check isinstance(e, UnknownWorkspaceRootError) and log
                # at INFO (recoverable, LLM-facing error).
                logger.warning('[Tools] namespaced path %r: unknown root %r — '
                               'refusing to fall through to primary '
                               '(would risk silent clobber). %s',
                               rel_path, _name, _ve)
                from lib.project_mod.config import UnknownWorkspaceRootError
                raise UnknownWorkspaceRootError(
                    f'Unknown workspace root "{_name}" in path "{rel_path}". '
                    f'Either (1) call create_project(path=...) first to register '
                    f'"{_name}" as a root, (2) use a known root name (see the '
                    f'multi-root table shown at session start), or (3) use a '
                    f'plain relative path without any colon prefix (will resolve '
                    f'under the primary root).'
                ) from _ve

    # ── Multi-root cross-check for path-misrouting ──
    # When the model forgets the 'rootname:' prefix in a multi-root
    # workspace, the path silently resolves under the primary root.
    # If the file/dir does NOT exist under primary but DOES exist under
    # exactly one other root, auto-route there.  This is a safety net,
    # not a substitute for proper 'rootname:' prefix usage.
    if base_path and rel_path and rel_path not in ('.', '', '/'):
        from lib.project_mod.config import _lock as _cfg_lock
        from lib.project_mod.config import get_conv_roots
        with _cfg_lock:
            # ★ Source roots from the SAME conv-scoped registry the namespaced
            #   resolver uses (get_conv_roots falls back to global _roots when
            #   the conv has none).  Reading the global _roots here would let a
            #   concurrent conversation's root leak in and misroute a write —
            #   the same clobber-risk class as the prompt root-table leak.
            roots_view = get_conv_roots(conv_id)
            if len(roots_view) > 1:
                primary_target = os.path.join(base_path, rel_path)
                if not os.path.exists(primary_target):
                    # File doesn't exist under primary — check other roots
                    candidate_roots = []
                    for rn, rs in roots_view.items():
                        if rs['path'] == base_path:
                            continue
                        other_target = os.path.join(rs['path'], rel_path)
                        if os.path.exists(other_target):
                            candidate_roots.append((rn, rs['path']))
                    if len(candidate_roots) == 1:
                        rn, rp = candidate_roots[0]
                        # Successful single-candidate resolution is NOT an error
                        # — log at INFO so it stays out of error.log.
                        logger.info(
                            '[Tools] ★ Cross-root auto-route: %s not found under primary %s '
                            'but exists under [%s] %s — routing there. '
                            'Model should use \'%s:%s\' prefix to be explicit.',
                            rel_path, base_path, rn, rp, rn, rel_path)
                        return rp, rel_path
                    elif len(candidate_roots) > 1:
                        names = ', '.join(f'{rn}' for rn, _ in candidate_roots)
                        logger.warning(
                            '[Tools] ★ Ambiguous multi-root path: %s not found under primary '
                            'but exists in multiple roots (%s). Using primary as fallback. '
                            'Model should use explicit root prefix.',
                            rel_path, names)

    return base_path, rel_path




def _resolve_base_safe(base_path, rel_path, conv_id=None):
    """Same as _resolve_base but returns (None, error_string) on ValueError.

    Used by execute_tool for tools that must surface the error as a tool
    result to the model, rather than bubbling as an exception.
    """
    try:
        return _resolve_base(base_path, rel_path, conv_id=conv_id), None
    except ValueError as e:
        logger.debug('[Tools] _resolve_base_safe rejected %r: %s', rel_path, e)
        return None, str(e)

def _edits_not_array_msg(tool_name, edits, shape):
    """Actionable error when a batch tool's ``edits`` isn't a JSON array.

    The recurring failure mode is a model emitting the whole array as one
    escaped JSON *string* (frequently with unescaped inner quotes, so the
    harness can't auto-parse it). Naming that case explicitly stops the
    model retrying the same broken shape — see conv mpyv4vq9qod3dr.
    """
    if isinstance(edits, str):
        return (f'{tool_name}: "edits" arrived as a string, not an array. '
                f'Send it as a real JSON array of objects (each {shape}) — '
                f'do NOT stringify the array into one blob, and make sure '
                f'inner quotes are escaped.')
    return f'{tool_name}: "edits" array is required (each entry {shape}).'


# ══════════════════════════════════════════════════════════
#  execute_tool — per-tool handlers + dispatch registry
# ══════════════════════════════════════════════════════════
# Each handler: _exec_<tool>(fn_args, base_path, conv_id, task_id, kwargs).
# Wired into _EXEC_HANDLERS; execute_tool is a thin dispatcher.


def _exec_list_dir(fn_args, base_path, conv_id, task_id, kwargs):
    bp, rp = _resolve_base(base_path, fn_args.get('path', '.'), conv_id=conv_id)
    return tool_list_dir(bp, rp)


def _exec_read_files(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # ★ Compatibility shim: some models (e.g. DeepSeek) flatten the
    #   "reads" array into top-level {"path": "..."} instead of
    #   {"reads": [{"path": "..."}]}.  Detect and auto-wrap.
    reads = fn_args.get('reads')
    if reads is None:
        # Model passed top-level scalar params — wrap into reads array
        spec = {}
        for key in ('path', 'start_line', 'end_line'):
            if key in fn_args:
                spec[key] = fn_args[key]
        if 'path' in spec:
            reads = [spec]
            logger.info('[Tools] read_files: auto-wrapped flat args into reads array '
                        '(path=%s) — model likely missing "reads" wrapper',
                        str(spec['path'])[:120])
        else:
            reads = []
    # ★ Recovery: some models serialize the WHOLE reads array as a JSON
    #   string into a scalar 'path' (e.g. path='[{"path": "a.py", ...}]').
    #   The stringified blob then looks like a 'rootname:path' spec and
    #   produced misleading "Unknown workspace root" errors.  Detect a
    #   path that is actually a JSON array/object and parse it back.
    if isinstance(reads, list):
        for _idx, _spec in enumerate(reads):
            _p = _spec.get('path') if isinstance(_spec, dict) else _spec
            if isinstance(_p, str) and _p.lstrip()[:1] in ('[', '{'):
                import json as _json
                try:
                    _parsed = _json.loads(_p)
                except (ValueError, TypeError) as _je:
                    logger.debug('[Tools] read_files: path looks like JSON but '
                                 'failed to parse (%s): %.120s', _je, _p)
                    continue
                if isinstance(_parsed, dict):
                    _parsed = [_parsed]
                if isinstance(_parsed, list) and _parsed:
                    logger.warning('[Tools] read_files: recovered stringified reads '
                                   'array from scalar path (%d spec(s)) — model '
                                   'serialized the array into "path"', len(_parsed))
                    reads = _parsed
                    break
    if not isinstance(reads, list):
        return (
            'Error: read_files expects "reads" to be an array of '
            '{"path": "...", "start_line"?: int, "end_line"?: int} objects. '
            f'Got type={type(reads).__name__}. '
            'Correct usage: {"reads": [{"path": "file.py"}]}'
        )
    # Resolve multi-root 'rootname:path' and normalise bare-string specs.
    # Each spec gets a '_base' key so tool_read_files can use the correct
    # base per file (important for multi-root workspaces).
    resolved = []
    invalid_specs = []  # (index, preview) pairs for error reporting
    for i, spec in enumerate(reads):
        if isinstance(spec, dict) and 'path' in spec:
            bp2, rp2 = _rb(base_path, spec['path'])
            resolved.append({'path': rp2, 'start_line': spec.get('start_line'),
                             'end_line': spec.get('end_line'), '_base': bp2})
        elif isinstance(spec, str) and spec.strip():
            bp2, rp2 = _rb(base_path, spec.strip())
            resolved.append({'path': rp2, '_base': bp2})
            logger.debug('[Tools] read_files: normalised bare string spec %r → dict', spec[:80])
        else:
            invalid_specs.append((i, type(spec).__name__, str(spec)[:120]))
            logger.warning('[Tools] read_files: invalid spec at index %d type=%s val=%r',
                           i, type(spec).__name__, str(spec)[:120])
    # If ALL specs were invalid, return a clear error so the model can retry
    if not resolved and invalid_specs:
        details = '; '.join(f'index {i}: {t} {v!r}' for i, t, v in invalid_specs[:5])
        return (
            f'Error: read_files received {len(invalid_specs)} invalid spec(s) '
            f'and no valid ones. Each entry in "reads" must be '
            f'{{"path": "...", "start_line"?: int, "end_line"?: int}} — '
            f'a bare path string is also accepted as a shorthand. '
            f'Invalid entries: {details}. Retry with correct schema.'
        )
    # If SOME specs were invalid, prepend a warning but still read the valid ones
    result = tool_read_files(base_path, resolved)
    if invalid_specs:
        details = '; '.join(f'index {i}: {t} {v!r}' for i, t, v in invalid_specs[:5])
        warn = (
            f'[Note] read_files: {len(invalid_specs)} invalid spec(s) skipped — '
            f'{details}. Each entry must be {{"path": "..."}} or a bare path string.\n\n'
        )
        if isinstance(result, str):
            return warn + result
        if isinstance(result, dict):
            # Batch-image result — prepend warn to the text portion
            result = dict(result)
            result['_text_content'] = warn + result.get('_text_content', '')
            return result
    return result


def _exec_inspect_image(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    raw_path = fn_args.get('path')
    if not raw_path or not isinstance(raw_path, str):
        return ('Error: inspect_image requires a "path" string pointing to '
                'an image file, or an attachment reference (e.g. /api/images/<file>).')

    # ── Uploaded-attachment reference: resolve via the centralized resolver ──
    # A chat-uploaded image has no filesystem path — it is referenced by
    # /api/images/<f> (or a data:/http(s) ref). Pass the raw ref straight
    # through (do NOT run it through project-root resolution, which would
    # mangle it into a bogus path) plus the task's messages so text refs can
    # be located.
    from lib.attachments import is_attachment_ref
    if is_attachment_ref(raw_path):
        task = kwargs.get('task') or {}
        messages = task.get('messages') if isinstance(task, dict) else None
        return tool_inspect_image(
            base_path, raw_path,
            crop=fn_args.get('crop'),
            rotate=fn_args.get('rotate', 0),
            zoom=fn_args.get('zoom'),
            grid=bool(fn_args.get('grid', False)),
            messages=messages,
        )

    bp, rp = _rb(base_path, raw_path)
    return tool_inspect_image(
        bp, rp,
        crop=fn_args.get('crop'),
        rotate=fn_args.get('rotate', 0),
        zoom=fn_args.get('zoom'),
        grid=bool(fn_args.get('grid', False)),
    )


def _exec_grep_search(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # ★ Batch mode: if 'searches' array is present, run all searches
    searches = fn_args.get('searches')
    if searches and isinstance(searches, list):
        # Resolve paths in each search spec for multi-root
        resolved = []
        for spec in searches:
            if not isinstance(spec, dict):
                continue
            sp = spec.get('path')
            if sp:
                bp2, rp2 = _rb(base_path, sp)
                spec = dict(spec, path=rp2, _base=bp2)
            else:
                spec = dict(spec, _base=base_path)
            resolved.append(spec)
        # Group by base and run batch per base
        from collections import OrderedDict
        by_base = OrderedDict()
        for spec in resolved:
            bp2 = spec.pop('_base', base_path)
            by_base.setdefault(bp2, []).append(spec)
        parts = []
        for bp2, specs in by_base.items():
            parts.append(tool_grep_batch(bp2, specs))
        return '\n\n'.join(parts)
    search_path = fn_args.get('path')
    bp = base_path
    if search_path:
        bp, search_path = _rb(base_path, search_path)
    return tool_grep(bp, fn_args.get('pattern', ''),
                     search_path, fn_args.get('include'),
                     fn_args.get('context_lines'),
                     max_results=fn_args.get('max_results'),
                     count_only=bool(fn_args.get('count_only', False)))


def _exec_find_files(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # ★ Batch mode: if 'searches' array is present, run all finds
    searches = fn_args.get('searches')
    if searches and isinstance(searches, list):
        resolved = []
        for spec in searches:
            if not isinstance(spec, dict):
                continue
            sp = spec.get('path')
            if sp:
                bp2, rp2 = _rb(base_path, sp)
                spec = dict(spec, path=rp2, _base=bp2)
            else:
                spec = dict(spec, _base=base_path)
            resolved.append(spec)
        from collections import OrderedDict
        by_base = OrderedDict()
        for spec in resolved:
            bp2 = spec.pop('_base', base_path)
            by_base.setdefault(bp2, []).append(spec)
        parts = []
        for bp2, specs in by_base.items():
            parts.append(tool_find_files_batch(bp2, specs))
        return '\n\n'.join(parts)
    search_path = fn_args.get('path')
    bp = base_path
    if search_path:
        bp, search_path = _rb(base_path, search_path)
    return tool_find_files(bp, fn_args.get('pattern', ''),
                           search_path,
                           max_results=fn_args.get('max_results'))
# ★ create_project — bootstrap a new workspace root


def _exec_create_project(fn_args, base_path, conv_id, task_id, kwargs):
    result = tool_create_project(
        fn_args.get('path', ''),
        name=fn_args.get('name'),
        overwrite=bool(fn_args.get('overwrite', False)),
        conv_id=conv_id, task_id=task_id,
    )
    if result.get('ok'):
        return (f"{result['message']}")
    return f"create_project failed: {result.get('error', 'unknown error')}"
# ★ Write tools — pass conv_id + task_id for per-round undo


def _exec_write_file(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    try:
        bp, rp = _rb(base_path, fn_args.get('path', ''))
    except ValueError as _rve:
        logger.debug('[tools] execute_tool caught %s: %s', type(_rve).__name__, _rve)
        return f"write_file: {_rve}"
    result = tool_write_file(bp, rp,
                             fn_args.get('content', ''),
                             fn_args.get('description', ''),
                             conv_id=conv_id, task_id=task_id)
    if result['ok']:
        return (f"File {'created' if result.get('created') else 'updated'}: {result['path']} "
                f"({result['lines']} lines, {_fmt_size(result['bytesWritten'])})")
    else:
        return f"Write failed: {result['error']}"


def _exec_apply_diff(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    try:
        bp, rp = _rb(base_path, fn_args.get('path', ''))
    except ValueError as _rve:
        logger.debug('[tools] execute_tool caught %s: %s', type(_rve).__name__, _rve)
        return f"apply_diff: {_rve}"
    result = tool_apply_diff(bp, rp,
                             fn_args.get('search', ''),
                             fn_args.get('replace', ''),
                             fn_args.get('description', ''),
                             conv_id=conv_id, task_id=task_id,
                             replace_all=bool(fn_args.get('replace_all', False)))
    if result['ok']:
        msg = (f"Applied diff to {result['path']}: "
               f"{result['linesChanged']} lines changed "
               f"({result['oldLines']}L → {result['newLines']}L)")
        if result.get('replacedCount'):
            msg += f" [{result['replacedCount']} occurrences replaced]"
        return msg
    else:
        return f"Diff failed: {result['error']}"


def _exec_apply_diffs(fn_args, base_path, conv_id, task_id, kwargs):
    edits = fn_args.get('edits')
    if not edits or not isinstance(edits, list):
        return _edits_not_array_msg('apply_diffs', edits, '{path, search, replace}')
    return tool_apply_diffs(base_path, edits, conv_id=conv_id, task_id=task_id)


def _exec_insert_content(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    try:
        bp, rp = _rb(base_path, fn_args.get('path', ''))
    except ValueError as _rve:
        logger.debug('[tools] execute_tool caught %s: %s', type(_rve).__name__, _rve)
        return f"insert_content: {_rve}"
    result = tool_insert_content(bp, rp,
                                 fn_args.get('anchor', ''),
                                 fn_args.get('content', ''),
                                 fn_args.get('position', 'after'),
                                 fn_args.get('description', ''),
                                 conv_id=conv_id, task_id=task_id)
    if result['ok']:
        return (f"Inserted {result['linesInserted']} lines "
                f"{result['position']} anchor at L{result['anchorLine']} "
                f"in {result['path']} "
                f"({result['oldLines']}L → {result['newLines']}L)")
    else:
        return f"Insert failed: {result['error']}"


def _exec_insert_contents(fn_args, base_path, conv_id, task_id, kwargs):
    edits = fn_args.get('edits')
    if not edits or not isinstance(edits, list):
        return _edits_not_array_msg('insert_contents', edits, '{path, anchor, content}')
    return tool_insert_contents(base_path, edits, conv_id=conv_id, task_id=task_id)


def _sticky_cwd_enabled():
    """True unless the operator disabled sticky cwd via TOFU_STICKY_CWD=0."""
    return os.environ.get('TOFU_STICKY_CWD', '').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _exec_run_command(fn_args, base_path, conv_id, task_id, kwargs):
    from lib.project_mod.config import get_conv_cwd, set_conv_cwd

    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # ★ Multi-root: resolve working_dir if model specifies one
    cwd = base_path
    working_dir = fn_args.get('working_dir', '')
    sticky_on = bool(conv_id) and _sticky_cwd_enabled()
    if working_dir:
        cwd_bp, _ = _rb(base_path, working_dir)
        cwd = os.path.join(cwd_bp, _) if _ and _ != '.' else cwd_bp
        # ★ Sticky cwd: remember where the model explicitly navigated so its
        #   next run_command with no working_dir resumes here (kills repeated
        #   `cd <project>`). Validated/gated inside set_conv_cwd (containment).
        if sticky_on:
            set_conv_cwd(conv_id, cwd)
    elif sticky_on:
        # ★ No working_dir → resume from this conversation's last cwd, if any.
        #   Stateless derived affinity: no persistent shell, env still re-derived
        #   per call by _get_cmd_env. Safe-degrades to base_path when absent.
        sticky = get_conv_cwd(conv_id)
        if sticky:
            cwd = sticky

    # ★ cd-capture sink: after the command runs, remember the shell's final cwd
    #   (so a trailing `cd subdir` inside the command also sticks). Captured via
    #   a dedicated temp FILE, never stdout — so it cannot be spoofed by command
    #   output. set_conv_cwd re-validates containment; a hop outside the conv's
    #   roots is silently ignored (isolation preserved).
    cwd_sink = (lambda captured: set_conv_cwd(conv_id, captured)) if sticky_on else None

    command_str = fn_args.get('command', '')
    destructive = _is_destructive_command(command_str)

    # ★ Read-only root guard: refuse a destructive command whose working
    #   directory lives inside a root marked read-only. Read-only commands
    #   (grep/ls/cat/…) still run — only writes are blocked. This mirrors
    #   the write-tool guard so every mutation path honours RO uniformly.
    if destructive:
        from lib.project_mod.config import is_readonly_path
        if is_readonly_path(cwd, conv_id=conv_id):
            return (
                f"run_command refused: the working directory '{cwd}' is "
                f"inside a READ-ONLY workspace root. Commands that could "
                f"modify files are not allowed there. Run it in a writable "
                f"root, or use read-only commands only.")

    # ★ Pre-compute write targets to decide if snapshotting is useful.
    # If we can't determine specific targets (opaque commands like
    # python3, make, npm …), DON'T snapshot — the diff would include
    # every file that changed autonomously (log files, DB WAL, etc.)
    # and we'd report false positives.  Only snapshot when we know
    # exactly which files the command WRITES to.
    write_targets = _extract_write_targets(command_str, cwd) if destructive else set()
    # write_targets: set = specific files;  None = opaque;  empty set = read-only
    can_track = destructive and write_targets is not None and len(write_targets) > 0

    snap_before = None
    _saved_contents = {}
    if can_track:
        # ★ Take filesystem snapshot before command to detect changes
        snap_before = _snapshot_project_files(cwd)
        # Save content of existing files so we can undo deletions/modifications
        # Cap per-file at 100KB, total at 20MB to avoid memory explosion
        _total_saved = 0
        _MAX_FILE_SAVE = 100 * 1024
        _MAX_TOTAL_SAVE = 20 * 1024 * 1024
        for rel, mtime in snap_before.items():
            if _total_saved >= _MAX_TOTAL_SAVE:
                break
            abs_p = os.path.join(cwd, rel)
            try:
                fsize = os.path.getsize(abs_p)
                if fsize > _MAX_FILE_SAVE:
                    continue
                with open(abs_p, 'rb') as f:
                    raw = f.read(_MAX_FILE_SAVE)
                _saved_contents[rel] = raw
                _total_saved += len(raw)
            except OSError as e:
                logger.debug('[run_command] Snapshot read failed for %s: %s', rel, e)
        logger.debug('[run_command] Snapshot taken (%d files), write_targets=%s: %.200s',
                     len(snap_before), write_targets, command_str)
    elif destructive:
        logger.debug('[run_command] Opaque command, skipping snapshot (no deterministic write targets): %.200s',
                     command_str)
    else:
        logger.debug('[run_command] Read-only command, skipping snapshot: %.200s', command_str)

    result = tool_run_command(cwd,
                              command_str,
                              fn_args.get('timeout', None),
                              stdin_callback=kwargs.get('stdin_callback'),
                              task=kwargs.get('task'),
                              on_chunk=kwargs.get('on_chunk'),
                              cwd_sink=cwd_sink)

    # ★ Diff snapshot after command (only if we took one)
    if snap_before is not None:
        snap_after = _snapshot_project_files(cwd)
        changes = _diff_snapshots(cwd, snap_before, snap_after)
        if changes:
            # ★ Filter changes to only include files the command
            # could plausibly write to.  write_targets was already
            # computed above and is guaranteed to be a non-empty set
            # (not None) since we only snapshot when can_track=True.
            changes = _filter_changes_by_targets(changes, write_targets, cwd)
            if changes:
                logger.debug('[run_command] Write targets=%s, filtered to %d change(s)',
                             write_targets, len(changes))
            # Enrich deleted/modified entries with original content for undo
            for ch in changes:
                rel = ch['rel_path']
                if ch['change_type'] in ('deleted', 'modified'):
                    raw = _saved_contents.get(rel)
                    if raw is not None:
                        # Try to decode as text; keep as bytes if binary
                        try:
                            ch['original_content'] = raw.decode('utf-8')
                        except (UnicodeDecodeError, ValueError) as _e_audit:
                            logger.debug('[tools] execute_tool caught %s: %s', type(_e_audit).__name__, _e_audit)
                            ch['original_content'] = raw
            recorded = _record_run_command_changes(
                cwd, changes, conv_id=conv_id, task_id=task_id)
            if recorded:
                logger.info('[run_command] Detected %d file change(s): %s',
                            len(recorded),
                            ', '.join(f"{r['path']}({r['action']})" for r in recorded[:10]))
    return result


_EXEC_HANDLERS = {
    'list_dir': _exec_list_dir,
    'read_files': _exec_read_files,
    'inspect_image': _exec_inspect_image,
    'grep_search': _exec_grep_search,
    'find_files': _exec_find_files,
    'create_project': _exec_create_project,
    'write_file': _exec_write_file,
    'apply_diff': _exec_apply_diff,
    'apply_diffs': _exec_apply_diffs,
    'insert_content': _exec_insert_content,
    'insert_contents': _exec_insert_contents,
    'run_command': _exec_run_command,
}


def execute_tool(fn_name, fn_args, base_path, conv_id=None, task_id=None, **kwargs):
    """Dispatch a project tool call to its handler (see _EXEC_HANDLERS)."""
    handler = _EXEC_HANDLERS.get(fn_name)
    if handler is None:
        return f'Unknown project tool: {fn_name}'
    return handler(fn_args, base_path, conv_id, task_id, kwargs)


# Note: tool_project_history / tool_project_diff / tool_project_blame were
# retired in the Tier-3 file-history redesign (2026-05-08).  See
# lib/file_history/__init__.py for the rationale.


def execute_standalone_command(fn_name, fn_args, working_dir=None, stdin_callback=None,
                               on_chunk=None):
    """Execute run_command without requiring a project path."""
    if fn_name == 'run_command':
        return tool_run_command(working_dir,
                                fn_args.get('command', ''),
                                fn_args.get('timeout', None),
                                stdin_callback=stdin_callback,
                                on_chunk=on_chunk)
    return f'Unknown tool: {fn_name}'


def project_tool_display(fn_name, fn_args):
    """Return a concise display string for a project tool call (no emoji prefix — added by frontend)."""
    if not isinstance(fn_args, dict):
        return f'{fn_name}({fn_args})'
    if fn_name == 'read_files':
        reads = fn_args.get('reads')
        # Defensive: some models emit ``reads`` as a JSON *string*
        # ('[{"path": ...}]') instead of an array. Without coercion the
        # loop below iterates the string character-by-character and renders
        # a garbled "Read 24 files: ; < p a +20 more" line. The execution
        # path is already protected (validate_then_repair coerces it), but
        # the streaming early-announce path builds this display BEFORE that
        # repair runs, so coerce here too.
        if isinstance(reads, str):
            import json
            try:
                reads = json.loads(reads)
            except (ValueError, TypeError) as e:
                logger.debug('[ToolDisplay] read_files reads=str not JSON: %s', e)
                reads = None
        if reads is None and 'path' in fn_args:
            # Flat-args compat (same shim as execute_tool)
            reads = [fn_args]
        if not reads:
            return 'Read files (empty)'
        # Group by unique path, collect line ranges per file
        from collections import OrderedDict
        grouped = OrderedDict()
        for r in reads:
            # LLM sometimes produces ["path1", "path2"] instead of [{path: "path1"}, ...]
            if isinstance(r, str):
                grouped.setdefault(r, [])
                continue
            if not isinstance(r, dict):
                continue
            p = r.get('path', '?')
            sl, el = r.get('start_line'), r.get('end_line')
            grouped.setdefault(p, [])
            if sl is not None and el is not None:
                grouped[p].append(f'L{sl}-{el}')
            elif sl is not None:
                grouped[p].append(f'L{sl}+')
        n_files = len(grouped)
        # Split each path into (rootname_prefix, bare_path) so the
        # rootname is preserved on display in multi-root workspaces —
        # otherwise two roots' files with the same basename look identical.
        # Rootname prefix = "name:" where name has no '/' or '\' and isn't
        # a Windows drive letter (drive letters are single chars, so the
        # heuristic ``len > 1 or non-ascii`` distinguishes them).
        def _split_rootname(path_str):
            if ':' not in path_str:
                return '', path_str
            head, _, rest = path_str.partition(':')
            if not head or '/' in head or '\\' in head:
                return '', path_str
            # Windows drive letter heuristic: single ASCII letter before ':'
            if len(head) == 1 and head.isalpha():
                return '', path_str
            return head + ':', rest
        # Disambiguate duplicate basenames (rootname-aware)
        from collections import Counter
        bare_basenames = [_split_rootname(p)[1].rsplit('/', 1)[-1] for p in grouped]
        dup = {b for b, c in Counter(bare_basenames).items() if c > 1}
        parts = []
        for p, ranges in list(grouped.items())[:4]:
            prefix, bare = _split_rootname(p)
            base = bare.rsplit('/', 1)[-1]
            name = '/'.join(bare.rsplit('/', 2)[-2:]) if base in dup else base
            display_name = f'{prefix}{name}'
            if ranges:
                parts.append(f'{display_name} {", ".join(ranges)}')
            else:
                parts.append(display_name)
        suffix = f' +{n_files - 4} more' if n_files > 4 else ''
        return f'Read {n_files} file{"s" if n_files != 1 else ""}: {"; ".join(parts)}{suffix}'
    elif fn_name == 'grep_search':
        # ★ Batch mode
        searches = fn_args.get('searches')
        if searches and isinstance(searches, list):
            n = len(searches)
            pats = []
            for s in searches[:4]:
                if isinstance(s, dict):
                    pats.append(s.get('pattern', '?')[:30])
            suffix = f' +{n - 4} more' if n > 4 else ''
            return f'grep {n} patterns: /{"; /".join(pats)}/{suffix}'
        pat = fn_args.get('pattern', '?')[:40]
        inc = fn_args.get('include', '')
        search_path = fn_args.get('path', '')
        suffix = ''
        if inc and search_path:
            suffix = f' in {inc} ({search_path})'
        elif inc:
            suffix = f' in {inc}'
        elif search_path:
            suffix = f' in {search_path}'
        return f'grep /{pat}/' + suffix
    elif fn_name == 'list_dir':
        return f'List {fn_args.get("path", ".")}'
    elif fn_name == 'find_files':
        # ★ Batch mode
        searches = fn_args.get('searches')
        if searches and isinstance(searches, list):
            n = len(searches)
            pats = []
            for s in searches[:4]:
                if isinstance(s, dict):
                    pats.append(s.get('pattern', '?'))
            suffix = f' +{n - 4} more' if n > 4 else ''
            return f'Find {n} patterns: {", ".join(pats)}{suffix}'
        search_path = fn_args.get('path', '')
        return f'Find {fn_args.get("pattern", "?")}' + (f' in {search_path}' if search_path else '')
    elif fn_name == 'create_project':
        p = fn_args.get('path', '?')
        nm = fn_args.get('name')
        return f'Create project {p}' + (f' (name={nm})' if nm else '')
    elif fn_name == 'write_file':
        p = fn_args.get('path', '?')
        desc = fn_args.get('description', '')
        return f'Write {p}' + (f' — {desc}' if desc else '')
    elif fn_name == 'apply_diff':
        p = fn_args.get('path', '?')
        desc = fn_args.get('description', '')
        return f'Patch {p}' + (f' — {desc}' if desc else '')
    elif fn_name == 'apply_diffs':
        edits = fn_args.get('edits')
        if edits and isinstance(edits, list):
            paths = list(dict.fromkeys(e.get('path', '?') for e in edits if isinstance(e, dict)))
            n = len(edits)
            desc = fn_args.get('description', '')
            if len(paths) == 1:
                label = f'Patch {paths[0]} ({n} edits)'
            elif len(paths) <= 3:
                label = f'Patch {", ".join(paths)} ({n} edits)'
            else:
                label = f'Patch {len(paths)} files ({n} edits)'
            return label + (f' — {desc}' if desc else '')
        return 'Patch (empty)'
    elif fn_name == 'insert_content':
        p = fn_args.get('path', '?')
        desc = fn_args.get('description', '')
        pos = fn_args.get('position', 'after')
        return f'Insert into {p} ({pos})' + (f' — {desc}' if desc else '')
    elif fn_name == 'insert_contents':
        edits = fn_args.get('edits')
        if edits and isinstance(edits, list):
            paths = list(dict.fromkeys(e.get('path', '?') for e in edits if isinstance(e, dict)))
            n = len(edits)
            desc = fn_args.get('description', '')
            if len(paths) == 1:
                label = f'Insert into {paths[0]} ({n} insertions)'
            elif len(paths) <= 3:
                label = f'Insert into {", ".join(paths)} ({n} insertions)'
            else:
                label = f'Insert into {len(paths)} files ({n} insertions)'
            return label + (f' — {desc}' if desc else '')
        return 'Insert (empty)'
    elif fn_name == 'run_command':
        cmd = fn_args.get('command', '?')
        return cmd  # Full command without $ prefix — frontend adds it
    return fn_name

