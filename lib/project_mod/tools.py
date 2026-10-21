"""Project tool implementations — dispatch façade.

Individual tool groups have been extracted into sibling modules:
  read_tools.py   — list_dir, read_file, read_files, grep, find_files
  write_tools.py  — write_file, apply_diff, apply_diffs, _find_closest_match
  run_command.py  — run_command + its command-execution machinery
                    (_clean_command_output, destructive-guards, snapshots, …)

This file retains:
  - browse_directory
  - Tool dispatch (execute_tool, project_tool_display)
  - Re-exports of all symbols from read_tools / write_tools / run_command
    plus _resolve_base from path_resolution, for backward compat
"""
import os
from pathlib import Path
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

# Folder-picker metadata is cosmetic (code badge + approximate child count),
# but the repository can live on FUSE.  Never turn one shallow browse into
# ``number_of_children × 100`` network metadata reads.  The current directory,
# nested details, and response size each have an explicit hard ceiling.
_PROJECT_BROWSE_MAX_ENTRIES = 1024
_PROJECT_BROWSE_MAX_DIRS = 512
_PROJECT_BROWSE_RESPONSE_BYTES = 96 * 1024
# Initial navigation performs exactly one ``scandir``. Child counts/code badges
# are cosmetic and a single nested FUSE lookup can otherwise hold the whole
# modal open. The implementation remains available behind these internal
# budgets for targeted tests/future progressive enrichment, but zero is the
# latency-safe product default.
_PROJECT_BROWSE_DETAIL_DIRS = 0
_PROJECT_BROWSE_DETAIL_ENTRIES = 0

# ── Re-export from run_command (backward compat) ──
import re
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
    parse_safe_directory_listing_command,
    parse_safe_file_find_command,
    _record_run_command_changes,
    _run_command_interactive,
    _run_command_simple,
    _safe_on_chunk,
    _snapshot_project_files,
    _snapshot_scope_rel_targets,
    _snapshot_targets,
    _split_pipeline,
    _with_command_writer,
    tool_run_command,
)

# ── Re-export from read_tools (backward compat) ──
from lib.project_mod.read_tools import (  # noqa: E402,F401
    _extract_symbols,
    _merge_same_file_ranges,
    _normalize_line_range,
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
    _fail_tag,
    _find_closest_match,
    _insert_one,
    _resolve_write_path,
    tool_apply_diff,
    tool_apply_diffs,
    tool_edit_file,
    tool_insert_content,
    tool_insert_contents,
    tool_write_file,
)

# ── Re-export from path_resolution (backward compat) ──
from lib.project_mod.path_resolution import _resolve_base  # noqa: E402,F401


# ═══════════════════════════════════════════════════════
#  Directory Browser — NEW
# ═══════════════════════════════════════════════════════

def browse_directory(path_str=None, show_hidden=False):
    """List one directory with bounded, best-effort child metadata."""
    if not path_str or path_str == '~':
        path_str = os.path.expanduser('~')
    abs_path = os.path.abspath(os.path.expanduser(path_str))

    if not os.path.isdir(abs_path):
        return {'error': f'Not a directory: {abs_path}', 'path': abs_path}

    parent = os.path.dirname(abs_path)
    dirs = []
    files_count = 0
    truncated = False
    scanned_entries = 0
    response_bytes = len(abs_path.encode('utf-8')) + 256
    try:
        with os.scandir(abs_path) as entries:
            for entry in entries:
                if scanned_entries >= _PROJECT_BROWSE_MAX_ENTRIES:
                    truncated = True
                    break
                scanned_entries += 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not show_hidden and entry.name.startswith('.'):
                            continue
                        if len(dirs) >= _PROJECT_BROWSE_MAX_DIRS:
                            truncated = True
                            break
                        entry_bytes = (
                            len(entry.name.encode('utf-8'))
                            + len(entry.path.encode('utf-8'))
                            + 128
                        )
                        if response_bytes + entry_bytes \
                                > _PROJECT_BROWSE_RESPONSE_BYTES:
                            truncated = True
                            break
                        dirs.append({
                            'name': entry.name,
                            'path': entry.path,
                            'itemCount': 0,
                            'hasCode': False,
                            'hidden': entry.name.startswith('.'),
                            'detailsDeferred': True,
                        })
                        response_bytes += entry_bytes
                    elif entry.is_file(follow_symlinks=False):
                        files_count += 1
                except (PermissionError, OSError) as e:
                    logger.debug(
                        '[Tools] entry processing failed for entry: %s',
                        e,
                        exc_info=True,
                    )
                    continue
    except (PermissionError, OSError):
        logger.debug('[Tools] permission denied scanning %s', abs_path, exc_info=True)
        return {'error': f'Unable to browse: {abs_path}', 'path': abs_path}

    dirs.sort(key=lambda item: item['name'].lower())
    detail_entries_left = _PROJECT_BROWSE_DETAIL_ENTRIES
    for index, item in enumerate(dirs):
        if index >= _PROJECT_BROWSE_DETAIL_DIRS or detail_entries_left <= 0:
            break
        item_count = 0
        has_code = False
        details_deferred = False
        per_directory_limit = min(101, detail_entries_left)
        try:
            with os.scandir(item['path']) as children:
                for child in children:
                    if item_count >= per_directory_limit:
                        details_deferred = True
                        break
                    item_count += 1
                    ext = os.path.splitext(child.name)[1].lower()
                    if ext in CODE_EXTENSIONS:
                        has_code = True
        except (PermissionError, OSError) as e:
            logger.debug(
                '[Tools] bounded dir detail scan failed for %s: %s',
                item['name'],
                e,
                exc_info=True,
            )
            details_deferred = True
        detail_entries_left -= item_count
        item['itemCount'] = item_count
        item['hasCode'] = has_code
        item['detailsDeferred'] = details_deferred

    return {
        'path': abs_path,
        'parent': parent if parent != abs_path else None,
        'dirs': dirs,
        'filesCount': files_count,
        'showHidden': show_hidden,
        'truncated': truncated,
        'scannedEntries': scanned_entries,
        'countsExact': not truncated,
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
    from lib.agent_loop.agent_artifacts import TRASH_DIR
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
    from lib.project_mod.config import UnknownWorkspaceRootError
    result_projection_items = kwargs.get('result_projection_items')

    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # Compatibility shim: some models (e.g. DeepSeek) flatten the
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
    # Recovery: some models serialize the WHOLE reads array as a JSON
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
            try:
                bp2, rp2 = _rb(base_path, spec['path'])
            except UnknownWorkspaceRootError as error:
                resolved.append({'path': spec['path'], '_path_error': str(error)})
                continue
            resolved.append({'path': rp2, 'start_line': spec.get('start_line'),
                             'end_line': spec.get('end_line'), '_base': bp2,
                             '_display_path': str(spec['path'])})
        elif isinstance(spec, str) and spec.strip():
            try:
                bp2, rp2 = _rb(base_path, spec.strip())
            except UnknownWorkspaceRootError as error:
                resolved.append({'path': spec.strip(), '_path_error': str(error)})
                continue
            resolved.append({'path': rp2, '_base': bp2,
                             '_display_path': spec.strip()})
            logger.debug('[Tools] read_files: normalised bare string spec %r → dict', spec[:80])
        else:
            invalid_specs.append((i, type(spec).__name__, str(spec)[:120]))
            logger.warning('[Tools] read_files: invalid spec at index %d type=%s val=%r',
                           i, type(spec).__name__, str(spec)[:120])
    if isinstance(result_projection_items, list):
        from lib.tools.result_projection import file_read_result_projection_item
        for index, value_type, preview in invalid_specs:
            result_projection_items.append(file_read_result_projection_item(
                index=index + 1,
                path=f'<invalid read item {index + 1}>',
                result=(f'Invalid read_files item: {value_type} {preview!r}.'),
                status='error'))
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
    capture = kwargs.get('file_source_capture')
    from lib.project_mod.config import get_conv_roots
    source_roots = tuple(os.path.abspath(state['path']) for state in
                         get_conv_roots(conv_id).values()) if conv_id else ()
    result = tool_read_files(
        base_path, resolved, result_items=result_projection_items,
        task=kwargs.get('task'), source_capture=capture, source_roots=source_roots,
        read_observations=kwargs.get('read_observations'))
    if fn_args.get('bind_sources') is True and isinstance(result, str):
        result += ('\n[Source binding coverage: only reads carrying a File source descriptor '
                   'are bound. Attachments, converters, binary files, unverified roots and '
                   'reads without a durable task cannot issue file sources.]')
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
            user_id=task.get('_userId') if isinstance(task, dict) else None,
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

    task = kwargs.get('task') or {}
    owner_user_id = task.get('_userId') if isinstance(task, dict) else None

    # Batch mode: if 'searches' array is present, run all searches
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
            parts.append(tool_grep_batch(
                bp2, specs, owner_user_id=owner_user_id))
        return '\n\n'.join(parts)
    search_path = fn_args.get('path')
    bp = base_path
    if search_path:
        bp, search_path = _rb(base_path, search_path)
    return tool_grep(bp, fn_args.get('pattern', ''),
                     search_path, fn_args.get('include'),
                     fn_args.get('context_lines'),
                     max_results=fn_args.get('max_results'),
                     count_only=bool(fn_args.get('count_only', False)),
                     owner_user_id=owner_user_id,
                     from_line=fn_args.get('from_line'))


def _exec_find_files(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # Batch mode: if 'searches' array is present, run all finds
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
# Write tools — pass conv_id + task_id for per-round undo


def _exec_write_file(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    try:
        bp, rp = _rb(base_path, fn_args.get('path', ''))
    except ValueError as _rve:
        logger.debug('[tools] execute_tool caught %s: %s', type(_rve).__name__, _rve)
        # Keep the 'Write failed' prefix — _read_gate._result_indicates_success
        # and tools/meta.py pattern-match it to tell failure from success.
        return f"Write failed: {_rve}"
    result = tool_write_file(bp, rp,
                             fn_args.get('content', ''),
                             fn_args.get('description', ''),
                             conv_id=conv_id, task_id=task_id,
                             allow_outside=bool(fn_args.get('allow_outside_workspace')),
                             append=bool(fn_args.get('append')))
    if result['ok']:
        msg = (f"File {'created' if result.get('created') else 'updated'}: {result['path']} "
               f"({result['lines']} lines, {_fmt_size(result['bytesWritten'])})")
        absolute = result.get('absolutePath')
        if absolute:
            from lib.project_mod.config import get_conv_roots
            roots = get_conv_roots(conv_id) if conv_id else {}
            owners = [(name, state['path']) for name, state in roots.items()
                      if Path(absolute).is_relative_to(state['path'])]
            label = max(owners, key=lambda item: len(item[1]))[0] if owners else '(unregistered)'
            msg += f"\nResolved file: {absolute!r}; workspace root: {label}."
        if result.get('appended'):
            msg += f" [appended {_fmt_size(result['appendedBytes'])}]"
        if result.get('syntaxWarning'):
            msg += ' ' + result['syntaxWarning']
        return msg
    else:
        return f"Write failed: {result['error']}"


def _exec_apply_diff(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    try:
        bp, rp = _rb(base_path, fn_args.get('path', ''))
    except ValueError as _rve:
        logger.debug('[tools] execute_tool caught %s: %s', type(_rve).__name__, _rve)
        # 'Diff failed' prefix — read-gate contract; tag categorizes the failure.
        return f"Diff failed [path-invalid]: {_rve}"
    result = tool_apply_diff(bp, rp,
                             fn_args.get('search', ''),
                             fn_args.get('replace', ''),
                             fn_args.get('description', ''),
                             conv_id=conv_id, task_id=task_id,
                             replace_all=bool(fn_args.get('replace_all', False)),
                             allow_outside=bool(fn_args.get('allow_outside_workspace')))
    if result['ok']:
        msg = (f"Applied diff to {result['path']}: "
               f"{result['linesChanged']} lines changed "
               f"({result['oldLines']}L → {result['newLines']}L)")
        if result.get('replacedCount'):
            msg += f" [{result['replacedCount']} occurrences replaced]"
        if result.get('syntaxWarning'):
            msg += ' ' + result['syntaxWarning']
        return msg
    else:
        return f"Diff failed{_fail_tag(result)}: {result['error']}"


def _exec_apply_diffs(fn_args, base_path, conv_id, task_id, kwargs):
    edits = fn_args.get('edits')
    if not edits or not isinstance(edits, list):
        return _edits_not_array_msg('apply_diffs', edits, '{path, search, replace}')
    return tool_apply_diffs(base_path, edits, conv_id=conv_id, task_id=task_id,
                            allow_outside=bool(fn_args.get('allow_outside_workspace')),
                            atomic=bool(fn_args.get('atomic')))


def _exec_edit_file(fn_args, base_path, conv_id, task_id, kwargs):
    edits = fn_args.get('edits')
    if not edits or not isinstance(edits, list):
        return _edits_not_array_msg(
            'edit_file', edits, '{path, operation, anchor, content}')
    batch_path = fn_args.get('path')
    return tool_edit_file(
        base_path, edits, conv_id=conv_id, task_id=task_id,
        allow_outside=bool(fn_args.get('allow_outside_workspace')),
        atomic=fn_args.get('atomic', True),
        batch_path=batch_path if isinstance(batch_path, str) else None)


def _exec_insert_content(fn_args, base_path, conv_id, task_id, kwargs):
    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    try:
        bp, rp = _rb(base_path, fn_args.get('path', ''))
    except ValueError as _rve:
        logger.debug('[tools] execute_tool caught %s: %s', type(_rve).__name__, _rve)
        # 'Insert failed' prefix — read-gate contract; tag categorizes the failure.
        return f"Insert failed [path-invalid]: {_rve}"
    result = tool_insert_content(bp, rp,
                                 fn_args.get('anchor', ''),
                                 fn_args.get('content', ''),
                                 fn_args.get('position', 'after'),
                                 fn_args.get('description', ''),
                                 conv_id=conv_id, task_id=task_id,
                                 allow_outside=bool(fn_args.get('allow_outside_workspace')))
    if result['ok']:
        msg = (f"Inserted {result['linesInserted']} lines "
               f"{result['position']} anchor at L{result['anchorLine']} "
               f"in {result['path']} "
               f"({result['oldLines']}L → {result['newLines']}L)")
        for _note in result.get('echoRepairs') or []:
            msg += f' [{_note}]'
        if result.get('syntaxWarning'):
            msg += ' ' + result['syntaxWarning']
        return msg
    else:
        return f"Insert failed{_fail_tag(result)}: {result['error']}"


def _exec_insert_contents(fn_args, base_path, conv_id, task_id, kwargs):
    edits = fn_args.get('edits')
    if not edits or not isinstance(edits, list):
        return _edits_not_array_msg('insert_contents', edits, '{path, anchor, content}')
    return tool_insert_contents(base_path, edits, conv_id=conv_id, task_id=task_id,
                                allow_outside=bool(fn_args.get('allow_outside_workspace')))


def _sticky_cwd_enabled():
    """True unless the operator disabled sticky cwd via TOFU_STICKY_CWD=0."""
    return os.environ.get('TOFU_STICKY_CWD', '').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _bounded_directory_fast_path_available(base, rel_path):
    """Return whether a reader can safely replace a directory shell command.

    ``ls file`` and ``find file`` have valid semantics that the bounded
    directory readers do not emulate. Symlink escapes likewise stay with the
    ordinary runner and its existing sandbox/authority policy.
    """
    if not base:
        return False
    try:
        base_real = os.path.realpath(base)
        target_real = os.path.realpath(os.path.join(base_real, rel_path))
        return (
            os.path.commonpath([base_real, target_real]) == base_real
            and os.path.isdir(target_real)
            # isdir() alone passes on permission-denied directories; the
            # bounded reader would then return its plain-text failure where
            # the real shell reports a truthful non-zero exit + stderr.
            # Decline and keep the shell for unreadable targets.
            and os.access(target_real, os.R_OK | os.X_OK)
        )
    except (OSError, TypeError, ValueError):
        return False


# Failure texts the bounded directory readers RETURN instead of raising
# (read_tools.py tool_list_dir / tool_find_files). Anchored on prefixes — the
# same structural signal meta.py::_EXEC_ERROR_PREFIXES relies on. The
# availability gate pre-validates containment, type and readability, so these
# only surface on a post-gate race or a partial-walk notice (e.g. a
# permission-denied SUBdirectory during find) — real ls/find also exit
# non-zero for both.
_FAST_PATH_READER_FAILURE_PREFIXES = (
    'Not a directory:', 'Unable to list directory:', 'find: ')


def _fast_path_command_result(command, output):
    """Wrap a bounded-reader fast-path result in the run_command result contract.

    Every real run_command result settles as ``$ cmd\\n<output>\\n\\n[exit code: N]``
    (run_command.py), and every downstream classifier — the task handler
    (handlers/code_exec.py), lib/tools/meta.py and the frontend's
    historical-round heuristic — parses THAT marker. A fast-path result
    returned without it is indistinguishable from a pre-execution refusal:
    a successful listing was rendered as an amber "not run" card with the
    full text pinned open as the refusal reason.
    """
    text = (output or '').strip()
    exit_code = 1 if text.startswith(_FAST_PATH_READER_FAILURE_PREFIXES) else 0
    result = f'$ {command}\n'
    if text:
        result += f'{text}\n'
    return result + f'\n[exit code: {exit_code}]'


def _execute_find_fast_path(cwd, file_find):
    """Run a parsed safe-find via the bounded GNU-order walker.

    Returns the rendered output text, or None when the exact-total modes
    (``| wc -l``, ``| sort``) could not complete within the walk budget —
    the caller then retains the native find segment. Capped modes
    (``| head -n K``, the default 500-row page) tolerate an early stop:
    downstream ``head`` would have SIGPIPEd the native find at the same
    point.
    """
    from lib.project_mod.shell_find import shell_find

    tail = file_find['tail'] or []
    cap = file_find['max_results']
    # A sort anywhere upstream of a head consumes the whole stream; so does
    # wc -l without a preceding head. Only a leading head bounds the walk.
    needs_full_walk = False
    walk_limit = cap + 1
    if tail:
        first = tail[0]
        if first[0] == 'head':
            walk_limit = max(0, first[1])
        else:
            needs_full_walk = True
            walk_limit = None
    outcome = shell_find(
        cwd, file_find['path'], file_find['patterns'],
        case_sensitive=file_find['case_sensitive'],
        entry_type=file_find['entry_type'],
        max_depth=file_find['max_depth'],
        min_depth=file_find['min_depth'],
        limit=walk_limit)
    if outcome is None:
        return None
    entries = outcome['entries']
    if needs_full_walk and outcome['exhausted']:
        return None                       # inexact total — keep native find

    if not tail:
        lines = entries[:cap]
        if len(entries) > cap:
            lines.append(
                f'find: [results capped at {cap}; narrow the pattern or '
                'path]')
        elif outcome['exhausted']:
            lines.append(
                'find: [search stopped by the walk budget - results are '
                'partial; try a more specific path]')
        return '\n'.join(lines)

    count_mode = False
    for op in tail:
        if op[0] == 'head':
            entries = entries[:op[1]]
        elif op[0] == 'sort':
            entries = sorted(entries)
        elif op[0] == 'wc':
            count_mode = True
    if count_mode:
        return str(len(entries))
    return '\n'.join(entries)


def _plan_in_process_cp(command_str):
    """Parse a LONE cp invocation for in-process parallel execution.

    The whole command must be one static cp segment — no pipes, chains,
    redirections, or substitutions (those keep the shell, which splices the
    helper per segment). Returns a CopyPlan or None.
    """
    from lib.project_mod.run_command import _has_dynamic_shell_syntax
    from lib.project_mod.command_analysis import (
        _shell_words, _split_pipeline_spans)
    spans = _split_pipeline_spans(command_str)
    if len(spans) != 1:
        return None
    segment = command_str[spans[0][0]:spans[0][1]]
    if _has_dynamic_shell_syntax(segment):
        return None
    words = _shell_words(segment)
    if not words or words[0].text not in ('cp', '/bin/cp', '/usr/bin/cp'):
        return None
    from lib.project_mod import copy_engine
    return copy_engine.parse_cp_argv([w.text for w in words[1:]])


def _run_cp_in_process(command_str, cp_plan, cwd, timeout_arg, task,
                       on_chunk):
    """Execute a lone cp via the parallel engine with run_command's seams.

    The engine polls the same abort flag and explicit-timeout deadline the
    subprocess runner would, so a FUSE-hung copy stays interruptible; -v
    lines stream to stdout chunks exactly like native cp output would.
    """
    import time as _time
    from lib.project_mod import copy_engine

    timeout = timeout_arg
    if not isinstance(timeout, (int, float)) or int(timeout) <= 0:
        timeout = None
    else:
        timeout = max(1, int(timeout))
        try:
            from lib.project_mod.config import MAX_COMMAND_TIMEOUT
            if MAX_COMMAND_TIMEOUT is not None:
                timeout = min(timeout, MAX_COMMAND_TIMEOUT)
        except Exception as e:
            logger.debug('[cp] timeout clamp skipped: %s', e)
        try:
            from lib.cross_dc import get_timeout_multiplier
            multiplier = get_timeout_multiplier(cwd)
            if multiplier > 1.0:
                timeout = int(timeout * multiplier)
        except Exception as e:
            logger.debug('[cp] cross-DC timeout probe skipped: %s', e)
    deadline = (_time.monotonic() + timeout) if timeout else None

    verbose_lines = []
    error_lines = []

    def _stream(stream, line):
        if stream == 'stdout':
            verbose_lines.append(line)
        else:
            error_lines.append(line)
        if on_chunk is not None:
            try:
                on_chunk(stream, line + '\n')
            except Exception as e:
                logger.debug('[cp] on_chunk raised: %s', e)

    logger.info('[run_command] executing lone cp in-process via the '
                'parallel copy engine (cwd=%s): %.200s', cwd, command_str)
    result = copy_engine.run_copy_plan(
        cp_plan, cwd,
        on_verbose=lambda line: _stream('stdout', line),
        on_error=lambda line: _stream('stderr', line),
        stop_check=(lambda: bool(task.get('aborted'))) if task else None,
        deadline=deadline)
    return _format_run_output(
        command_str,
        '\n'.join(verbose_lines),
        '\n'.join(error_lines),
        result.exit_code,
        timed_out=result.timed_out,
        aborted=result.stopped)


def _command_environment_root(cwd, base_path, conv_id):
    """Select the most specific authorized root containing the final cwd."""
    from lib.project_mod.config import get_conv_roots
    current = os.path.realpath(cwd)
    candidates = [state['path'] for state in get_conv_roots(conv_id).values()]
    if base_path:
        candidates.append(base_path)
    roots = []
    for candidate in candidates:
        root = os.path.realpath(candidate)
        try:
            if os.path.commonpath([current, root]) == root:
                roots.append(root)
        except ValueError:
            continue  # Different Windows drives cannot share an environment.
    return max(roots, key=len) if roots else current


def _exec_run_command(fn_args, base_path, conv_id, task_id, kwargs):
    from lib.project_mod.config import get_conv_cwd, set_conv_cwd

    def _rb(bp_arg, rp_arg):
        return _resolve_base(bp_arg, rp_arg, conv_id=conv_id)

    # Multi-root: resolve working_dir if model specifies one
    cwd = base_path
    working_dir = fn_args.get('working_dir', '')
    sticky_on = bool(conv_id) and _sticky_cwd_enabled()
    if working_dir:
        cwd_bp, _ = _rb(base_path, working_dir)
        cwd = os.path.join(cwd_bp, _) if _ and _ != '.' else cwd_bp
        # Sticky cwd: remember where the model explicitly navigated so its
        #   next run_command with no working_dir resumes here (kills repeated
        #   `cd <project>`). Validated/gated inside set_conv_cwd (containment).
        if sticky_on:
            set_conv_cwd(conv_id, cwd)
    elif sticky_on:
        # No working_dir → resume from this conversation's last cwd, if any.
        #   Stateless derived affinity: no persistent shell, env still re-derived
        #   per call by _get_cmd_env. Safe-degrades to base_path when absent.
        sticky = get_conv_cwd(conv_id)
        if sticky:
            cwd = sticky

    # Validate before fast-path readers, snapshots, and subprocess setup.
    from lib.project_mod.run_command import validate_command_cwd
    from lib.project_mod.config import get_conv_roots
    cwd_error = validate_command_cwd(cwd, fn_args.get('command', ''),
                                     roots=get_conv_roots(conv_id) if conv_id else None)
    if cwd_error:
        return cwd_error

    # cd-capture sink: after the command runs, remember the shell's final cwd
    #   (so a trailing `cd subdir` inside the command also sticks). Captured via
    #   a dedicated temp FILE, never stdout — so it cannot be spoofed by command
    #   output. set_conv_cwd re-validates containment; a hop outside the conv's
    #   roots is silently ignored (isolation preserved).
    cwd_sink = (lambda captured: set_conv_cwd(conv_id, captured)) if sticky_on else None

    command_str = fn_args.get('command', '')
    if 'frozen_input_paths' in fn_args and not isinstance(fn_args['frozen_input_paths'], dict):
        return 'Error: frozen_input_paths must be an explicit scope-field/path map; command was not started.'

    from lib.tasks_pkg.workspace_delivery import command_workspace_refusal
    from lib.project_mod.tool_admission import recorded_command_source
    # Only the exact server-generated durable wrapper can borrow its original
    # admission source. Its stream/sentinel writes are runtime bookkeeping,
    # not model write targets; unbound or modified wrappers get no exemption.
    original = recorded_command_source(command_str)
    admission_str = command_str if original is None else original
    refusal = command_workspace_refusal(kwargs.get('task') or {}, admission_str, cwd)
    if refusal:
        return refusal
    directory_listing = (
        parse_safe_directory_listing_command(command_str)
        if os.environ.get('TOFU_RUN_LS_FASTPATH', '1') != '0' else None)
    if directory_listing is not None \
            and _bounded_directory_fast_path_available(
                cwd, directory_listing['path']):
        logger.info(
            '[run_command] routed plain ls through bounded directory reader: %s',
            directory_listing['path'],
        )
        return _fast_path_command_result(
            command_str,
            tool_list_dir(
                cwd,
                directory_listing['path'],
                show_hidden=directory_listing['show_hidden'],
                shell_compatible=True,
            ))
    file_find = (
        parse_safe_file_find_command(command_str)
        if os.environ.get('TOFU_RUN_FIND_FASTPATH', '1') != '0' else None)
    if file_find is not None \
            and not os.path.islink(os.path.join(cwd, file_find['path'])) \
            and _bounded_directory_fast_path_available(cwd, file_find['path']):
        _find_outcome = _execute_find_fast_path(cwd, file_find)
        if _find_outcome is not None:
            logger.info(
                '[run_command] routed find through bounded shell walker: '
                '%s %s tail=%s', file_find['path'], file_find['patterns'],
                file_find['tail'])
            return _fast_path_command_result(command_str, _find_outcome)
        # Exact-total modes (wc -l / sort) exhausted the walk budget: fall
        # through to the native find segment with its own timeout budget.
    destructive = _is_destructive_command(admission_str)

    # Read-only root guard: refuse a destructive command whose working
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

    return _exec_run_command_with_writer(
        cwd, command_str, fn_args=fn_args, base_path=base_path,
        conv_id=conv_id, task_id=task_id, kwargs=kwargs, cwd_sink=cwd_sink)


@_with_command_writer
def _exec_run_command_with_writer(cwd, command_str, *, fn_args, base_path,
                                  conv_id, task_id, kwargs, cwd_sink):
    """Hold the command's checkout leases from preimage through undo history.

    Reuse the runner's target resolution and busy/detach refusals. The nested
    subprocess runner reenters the same thread-owned leases; in-process cp
    cannot bypass them. Read-only commands still run without writer exclusion.
    """
    from lib.project_mod.tool_admission import recorded_command_source
    original = recorded_command_source(command_str)
    admission_str = command_str if original is None else original
    destructive = _is_destructive_command(admission_str)

    # Pre-compute write targets to decide if snapshotting is useful.
    # If we can't determine specific targets (opaque commands like
    # python3, make, npm …), DON'T snapshot — the diff would include
    # every file that changed autonomously (log files, DB WAL, etc.)
    # and we'd report false positives.  Only snapshot when we know
    # exactly which files the command WRITES to.
    write_targets = _extract_write_targets(admission_str, cwd) if destructive else set()
    # write_targets: set = specific files;  None = opaque;  empty set = read-only
    can_track = destructive and write_targets is not None and len(write_targets) > 0

    snap_before = None
    snapshot_targets = _snapshot_scope_rel_targets(write_targets) if can_track else None
    _saved_contents = {}
    if can_track:
        # Take filesystem snapshot before command to detect changes
        snap_before = (_snapshot_targets(cwd, snapshot_targets) if snapshot_targets is not None
                       else _snapshot_project_files(cwd))
        # Save content of existing files so we can undo deletions/modifications
        # Cap per-file at 100KB, total at 20MB to avoid memory explosion
        _total_saved = 0
        _MAX_FILE_SAVE = 100 * 1024
        from lib.project_mod.snapshot_content import UNDO_BATCH_CONTENT_BYTES
        _MAX_TOTAL_SAVE = UNDO_BATCH_CONTENT_BYTES
        from lib.project_mod.snapshot_content import read_snapshot_content
        for rel, (mtime, fsize) in snap_before.items():
            if _total_saved >= _MAX_TOTAL_SAVE:
                break
            if fsize > _MAX_FILE_SAVE:
                continue
            raw = read_snapshot_content(
                cwd, rel, snap_before, min(_MAX_FILE_SAVE, _MAX_TOTAL_SAVE - _total_saved))
            if raw is not None:
                _saved_contents[rel] = raw
                _total_saved += len(raw)
        logger.debug('[run_command] Snapshot taken (%d files), write_targets=%s: %.200s',
                     len(snap_before), write_targets, command_str)
    elif destructive:
        logger.debug('[run_command] Opaque command, skipping snapshot (no deterministic write targets): %.200s',
                     command_str)
    else:
        logger.debug('[run_command] Read-only command, skipping snapshot: %.200s', command_str)

    result = None
    # Lone cp → in-process parallel copy engine (zero spawn; measured 2.9x
    #   on FUSE tree copies). Sits AFTER the destructive-command snapshot so
    #   undo evidence and index invalidation below behave exactly as for a
    #   subprocess cp. Compound commands keep the shell + per-segment helper
    #   splice inside tool_run_command. Disable with TOFU_RUN_CP_FASTPATH=0.
    if 'frozen_input_paths' not in fn_args and not kwargs.get('stdin_callback') \
            and os.environ.get('TOFU_RUN_CP_FASTPATH', '1') != '0':
        try:
            _cp_plan = _plan_in_process_cp(command_str)
        except Exception as _cp_plan_e:
            logger.debug('[run_command] in-process cp planning failed: %s',
                         _cp_plan_e)
            _cp_plan = None
        if _cp_plan is not None:
            result = _run_cp_in_process(
                command_str, _cp_plan, cwd,
                # Model-facing timeout removed: commands run to completion;
                # stopping is the model's job via background + `kill`.
                None,
                kwargs.get('task'), kwargs.get('on_chunk'))

    if result is None:
        result = tool_run_command(cwd,
                                  command_str,
                                  # No model-facing timeout by default (commands
                                  # run to completion; stop via background+kill),
                                  # but an explicit timeout IS honored — it
                                  # drives the deadlineTs countdown the spawn
                                  # lifecycle stamps (the owner's caught bug:
                                  # dropping it here killed the countdown).
                                  fn_args.get('timeout'),
                                  stdin_callback=kwargs.get('stdin_callback'),
                                  task=kwargs.get('task'),
                                  on_chunk=kwargs.get('on_chunk'),
                                  on_spawn=kwargs.get('on_spawn'),
                                  on_grep_intercept=kwargs.get('on_grep_intercept'),

                                  runtime_context=kwargs.get('runtime_context'),
                                  cwd_sink=cwd_sink,
                                  credentials=fn_args.get('credentials'),
                                  **({'frozen_input_paths':fn_args['frozen_input_paths']} if 'frozen_input_paths' in fn_args else {}),
                                  environment_root=_command_environment_root(cwd, base_path, conv_id))

    if destructive:
        # Opaque shell commands can mutate files without going through the
        # write-tool freshness hook. Drop every known project index containing
        # this cwd so a later indexed find/grep cannot serve a stale snapshot.
        from lib.project_mod import tree_index
        from lib.project_mod.config import get_conv_roots
        candidate_roots = {os.path.abspath(base_path)} if base_path else set()
        candidate_roots.update(
            os.path.abspath(state['path'])
            for state in get_conv_roots(conv_id).values()
            if isinstance(state, dict) and state.get('path')
        )
        cwd_abs = os.path.abspath(cwd)
        for root in candidate_roots:
            try:
                within_root = os.path.commonpath([root, cwd_abs]) == root
            except ValueError:
                within_root = False
            if within_root:
                try:
                    tree_index.invalidate(root)
                except Exception as exc:
                    logger.debug(
                        '[run_command] index invalidation failed for %s: %s',
                        root, exc)

    # Diff snapshot after command (only if we took one)
    if snap_before is not None:
        snap_after = (_snapshot_targets(cwd, snapshot_targets) if snapshot_targets is not None
                      else _snapshot_project_files(cwd))
        changes = _diff_snapshots(cwd, snap_before, snap_after)
        if not all(getattr(snap, 'complete', True) for snap in (snap_before, snap_after)):
            # Insert BEFORE the terminal ``[exit code: N]`` marker. Every
            # downstream classifier (tools/meta.py, handlers/code_exec.py)
            # end-anchors on that marker to distinguish "ran" from
            # "refused/blocked"; appending after it made a successful
            # command look like a not-run refusal (conv mubjnnu5k9jhfx),
            # which also tripped the intent-stall nudge.
            _warn = '\n[File tracking incomplete: scan budget or filesystem access limited coverage.]'
            _m = re.search(r'\n?\[exit code: -?\d+\]\s*$', result)
            if _m:
                result = result[:_m.start()] + _warn + result[_m.start():]
            else:
                result += _warn
        if changes:
            # Filter changes to only include files the command
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
            if any(ch['change_type'] in ('deleted', 'modified')
                   and 'original_content' not in ch for ch in changes):
                result += '\n[Undo unavailable for some changed files: original content was not captured.]'
            recorded = _record_run_command_changes(
                cwd, changes, conv_id=conv_id, task_id=task_id)
            if len(recorded) < len(changes):
                result += '\n[File history unavailable: some changes could not be saved for undo.]'
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
    'write_file': _exec_write_file,
    'edit_file': _exec_edit_file,
    'apply_diff': _exec_apply_diff,
    'apply_diffs': _exec_apply_diffs,
    'insert_content': _exec_insert_content,
    'insert_contents': _exec_insert_contents,
    'run_command': _exec_run_command,
}


def _scoped_publish(scope, path, data, original, fingerprint, conv_id, task_id):
    """Retain prepare/journal ordering without running ungranted recovery I/O.

    Pending intents require the trusted recovery owner, not a file-only worker.
    The new intent is derived from the pinned stage descriptor; no path-based
    preimage, realpath target rewrite or generic post-edit subprocess is used.
    """
    import base64
    import uuid
    from lib.project_mod import modifications as mods
    from lib.project_mod.session_lock import locked_session
    from lib.project_mod.write_intents import locked_intents, _validate, _digest, IntentError
    from lib.project_mod.write_recovery import post_image
    from lib.project_mod.write_tools._ops import _record_modification_checked, _AppliedWriteFailure, _UndoRecordingFailed
    from lib.project_mod.tool_admission import journal_conv_id
    session = mods._get_session_dir(scope.root)
    with locked_session(session) as state, locked_intents(session) as store:
        if state.get('writing') or state.get('recovering'):
            raise IntentError('Nested scoped journal write is not allowed')
        if store.read():
            raise IntentError('Pending write recovery requires the trusted recovery owner; no scoped write ran')
        state['preparedJournal'] = mods._assert_journal_ready(session)
        state['writing'] = True
        record = None
        published = False

        def prepare(parent, stage):
            nonlocal record
            metadata = os.stat(stage, dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
            record = _validate({
                'version': 1, 'state': 'prepared', 'changeId': uuid.uuid4().hex,
                'basePath': scope.root, 'rootPath': scope.root,
                'path': path, 'stageName': stage, 'rootIdentity': list(scope.root_identity),
                'stagedIdentity': [metadata.st_dev, metadata.st_ino],
                'original': None if original is None else base64.b64encode(original).decode('ascii'),
                'postBytes': len(data), 'postDigest': _digest(data),
                'convId': journal_conv_id(conv_id), 'taskId': task_id,
            })
            mods._atomic_json_write(os.path.join(store.directory, record['changeId'] + '.json'), record)

        def applied():
            nonlocal published
            published = True

        try:
            scope.publish(path, data, fingerprint, preimage=original, before_replace=prepare, after_replace=applied)
            _record_modification_checked(scope.root, 'write_file', path,
                original_content=original, conv_id=journal_conv_id(conv_id), task_id=task_id,
                change_id=record['changeId'], post_image=post_image(record))
            try:
                store.acknowledge(record)
            except OSError:
                logger.warning('Scoped write committed; prepared intent cleanup remains pending', exc_info=True)
        except Exception as error:
            if not published and record is not None:
                # Stage cleanup is already anchored in publish(). Do not call
                # generic recovery, which could open a newly substituted root.
                try:
                    store.acknowledge(record)
                except OSError:
                    logger.warning('Unpublished scoped intent retained for trusted recovery', exc_info=True)
            if published and not isinstance(error, _AppliedWriteFailure):
                raise _UndoRecordingFailed() from error
            raise
        finally:
            state.pop('writing', None)
            state.pop('preparedJournal', None)


def _scoped_freshness(scope, path, data, conv_id, *, require_read=False, record=False):
    """Use bytes already read through the fence, never a path-based re-read."""
    from lib import write_freshness as freshness
    if not conv_id or not freshness._gate_enabled():
        return
    target = os.path.join(scope.root, path)
    if record:
        freshness.record_written(conv_id, target, data)
        return
    with freshness._lock:
        previous = freshness._tokens.get((conv_id, target))
    if require_read and previous is None:
        raise ValueError('Read this scoped file before editing it')
    if data is not None and previous is not None:
        current = freshness.fingerprint_for_written_bytes(target, data, previous[0])
        if (current != previous[0]
                or (previous[0][0] == 's' and previous[0][3:6] != (scope.root, *scope.root_identity))):
            raise ValueError('Scoped file changed since your read; re-read and recompute the edit')


def _execute_scoped_files(scope, fn_name, fn_args, conv_id, task_id, source_capture=None):
    """Actual first-party file-only handler; no generic path/converter fallbacks."""
    from contextlib import ExitStack
    from lib.project_mod.post_commit import defer_publication
    from lib.project_mod import workspace_lease as leases
    from lib.project_mod.tool_admission import admission_timeout, cancellation
    from lib.project_mod.write_tools._ops import (
        _compute_diff, _compute_insert, _fail_tag, _AppliedWriteFailure)
    if fn_name == 'read_files':
        import hashlib
        import json
        from lib import write_freshness as freshness
        outputs = []
        observations = []
        for read in fn_args.get('reads', [fn_args]):
            path = read['path']
            data, _ = scope.read(path)
            text = data.decode('utf-8')
            if '\x00' in text:
                raise ValueError('Scoped reads support UTF-8 text only, not opaque converters')

            observation = freshness.ReadObservation(os.path.join(scope.root, path), (
                's', len(data), hashlib.sha256(data).hexdigest(), scope.root, *scope.root_identity))
            lines = text.splitlines(keepends=True)
            start = read.get('start_line', 1)
            end = min(read.get('end_line', len(lines)), len(lines))

            has_range = ('start_line' in read or 'end_line' in read)
            if has_range and not lines[start - 1:end]:
                raise ValueError(f'Requested line range for {path} is beyond EOF')
            rendered = f'File: {path} (lines {start}-{end} of {len(lines)})\n' + ''.join(lines[start - 1:end])
            if source_capture is not None:
                from lib.artifacts.file_snapshot import read_project_snapshot
                def check_source():
                    source_capture.check()
                    scope.check(path)
                snapshot = read_project_snapshot(path, [scope.root], check=check_source,
                    expected_root_identity=scope.root_identity)
                import hashlib
                if snapshot.sha256 != hashlib.sha256(data).hexdigest():
                    raise ValueError('Scoped file changed before source binding')
                source = source_capture.bind(snapshot, roots=[scope.root])
                rendered += '\n[File source: ' + json.dumps(source, ensure_ascii=False) + ']'
            outputs.append(rendered)
            observations.append(observation)
        # A later item/source-binding failure returns no batch content. Do not
        # certify earlier, undisplayed items before the whole scoped read succeeds.
        for observation in observations:
            freshness.record_observation(conv_id, observation)
        return '\n\n'.join(outputs)
    paths = list(dict.fromkeys(scope.paths))
    with defer_publication(), ExitStack() as stack:
        # Discover trusted checkout metadata only from the owned root, never
        # from realpath(model_target). The lower lease primitive accepts that
        # fixed identity and does not re-resolve a raced leaf/ancestor alias.
        from lib.project_mod.snapshot_content import anchored_directory
        scope.check(paths[0], write=True)
        with anchored_directory(scope.root, '.', scope.root_identity):
            identity = leases._checkout_identity(scope.root)
        if identity is not None:
            root, common = identity
            if root != scope.root:
                raise ValueError('Scoped checkout lease resolved a different owned root')
            stack.enter_context(leases._lease(root, leases._FILE_BARRIER,
                admission_timeout(), cancellation(), _identity=identity))
            for path in sorted(paths):
                target = os.path.join(scope.root, path)
                stack.enter_context(leases._lease(target, leases._WRITE,
                    admission_timeout(), cancellation(), _identity=(target, common)))
        if fn_name == 'write_file':
            path = paths[0]
            original, fingerprint = scope.read(path, missing=True, write=True)
            _scoped_freshness(scope, path, original, conv_id)
            data = fn_args['content'].encode('utf-8')
            if fn_args.get('append') and original is not None:
                data = original + data
            _scoped_publish(scope, path, data, original, fingerprint, conv_id, task_id)

            _scoped_freshness(scope, path, data, conv_id, record=True)
            return (f"File {'created' if original is None else 'updated'}: {path} "
                    f"({len(data.splitlines())} lines, {_fmt_size(len(data))})")
        # File grants share one root/journal. Hold its existing reentrant
        # admission over the complete plan and publication, not once per file.
        # Scoped workers never replay pending intents on ungranted paths.
        from lib.project_mod import modifications as mods
        from lib.project_mod.session_lock import locked_session
        from lib.project_mod.write_intents import locked_intents, IntentError
        session = mods._get_session_dir(scope.root)
        state = stack.enter_context(locked_session(session))
        store = stack.enter_context(locked_intents(session))
        if state.get('writing') or state.get('recovering'):
            raise IntentError('Nested scoped journal write is not allowed')
        if store.read():
            raise IntentError('Pending write recovery requires the trusted recovery owner; no scoped write ran')
        mods._cache_get(session)
        strings = {}
        plans = []
        for path in paths:
            ops = [(i, edit) for i, edit in enumerate(fn_args['edits'], 1) if edit['path'] == path]
            try:
                original, fingerprint = scope.read(path, write=True)
                _scoped_freshness(scope, path, original, conv_id, require_read=True)
                current = original.decode('utf-8')
                successes = []
                for i, edit in ops:
                    operation = edit['operation']
                    if operation == 'replace':
                        comp = _compute_diff(current, path, edit['anchor'], edit['content'],
                                             replace_all=edit.get('replace_all', False))
                    else:
                        comp = _compute_insert(current, path, edit['anchor'], edit['content'],
                            position='after' if operation == 'insert_after' else 'before', syntax_guard=False)
                    if comp['ok']:
                        current = comp['new_content']
                        successes.append(i)
                    else:
                        strings[i] = f"[{i}] FAIL {path} [{operation}]{_fail_tag(comp)}: {comp['error']}"
                if successes:
                    plans.append((path, original, fingerprint, current.encode('utf-8'), successes))
            except (OSError, ValueError) as error:
                for i, _ in ops:
                    strings[i] = f'[{i}] FAIL {path}: {error}'

        rejected = fn_args.get('atomic', True) and bool(strings)
        ok = skipped = 0
        for path, original, fingerprint, data, successes in plans:
            if rejected:
                for i in successes:
                    strings[i] = (f'[{i}] SKIP {path}: '
                                  'Not applied because atomic preflight failed.')
                    skipped += 1
                continue
            try:
                _scoped_publish(scope, path, data, original, fingerprint, conv_id, task_id)
                _scoped_freshness(scope, path, data, conv_id, record=True)
                for i in successes:
                    strings[i] = f'[{i}] OK {path}'
                    ok += 1
            except _AppliedWriteFailure:
                # A mutation with uncertain durability must never masquerade
                # as a preflight refusal that is safe to retry.
                raise
            except (OSError, ValueError) as error:
                for i in successes:
                    strings[i] = f'[{i}] FAIL {path}: {error}'
        total = len(fn_args['edits'])
        header = f'Applied {ok}/{total} edits ({total - ok - skipped} failed)'
        if rejected:
            header += f'; atomic preflight failed; no files written; {skipped} skipped'
        return header + '\n' + '\n'.join(strings[i] for i in sorted(strings))


def execute_tool(fn_name, fn_args, base_path, conv_id=None, task_id=None, **kwargs):
    """Dispatch a project tool call to its handler (see _EXEC_HANDLERS)."""

    # Scope admission precedes root rebinding and every generic I/O adapter.
    from lib.project_dispatch.scope import tool_scope
    with tool_scope(kwargs.get('task'), fn_name, fn_args, base_path) as scope:
        if scope is not None:
            from lib.project_mod.tool_admission import tool_admission_scope, cancellation
            control = cancellation()
            runtime = kwargs.get('runtime_context') or (control.runtime_context if control is not None else None)
            with tool_admission_scope(scope.task, runtime, tool_name=fn_name):
                return _execute_scoped_files(scope, fn_name, fn_args, conv_id, task_id,
                                             kwargs.get('file_source_capture'))
    handler = _EXEC_HANDLERS.get(fn_name)
    if handler is None:
        return f'Unknown project tool: {fn_name}'
    task = kwargs.get('task')
    from contextlib import nullcontext
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots, execution_root_key
    from lib.project_mod.config import execution_root_scope
    policy = ensure_execution_roots(task) if task else None
    if policy is not None:
        conv_id = execution_root_key(task)
    root_scope = execution_root_scope(conv_id, policy) if policy is not None else nullcontext()
    from lib.project_mod.abs_path_guard import registered_root_scope
    from lib.project_mod.tool_admission import tool_admission_scope
    with root_scope, registered_root_scope(conv_id), tool_admission_scope(
            task, kwargs.get('runtime_context'), tool_name=fn_name,
            command_source=kwargs.get('recorded_command_source')):
        result = handler(fn_args, base_path, conv_id, task_id, kwargs)
    return result


# Note: tool_project_history / tool_project_diff / tool_project_blame were
# retired in the Tier-3 file-history redesign (2026-05-08).  See
# lib/file_history/__init__.py for the rationale.


def execute_standalone_command(fn_name, fn_args, working_dir=None, stdin_callback=None,
                               on_chunk=None, on_spawn=None, task=None,
                               on_grep_intercept=None, runtime_context=None):
    """Execute run_command without requiring a project path.

    ``task`` is the SAME cooperative-control seam the project path already
    had (): when provided, the runner registers
    ``_subprocess_pid`` on it (so the stuck-task reaper can INTERRUPT the
    command instead of force-failing the whole turn, and the interrupt
    endpoint can target it) and polls ``task['aborted']`` (so Stop kills
    the process tree). Callers without a live task (headless sandbox)
    simply omit it — behaviour is then identical to before.
    """
    if fn_name == 'run_command':
        command = fn_args.get('command', '')
        if 'frozen_input_paths' in fn_args and not isinstance(fn_args['frozen_input_paths'], dict):
            return 'Error: frozen_input_paths must be an explicit scope-field/path map; command was not started.'
        directory_listing = (
            parse_safe_directory_listing_command(command)
            if os.environ.get('TOFU_RUN_LS_FASTPATH', '1') != '0' else None)
        from lib.project_mod.run_command import validate_command_cwd
        cwd_error = validate_command_cwd(working_dir, command)
        if cwd_error:
            return cwd_error
        if 'frozen_input_paths' not in fn_args and directory_listing is not None \
                and _bounded_directory_fast_path_available(
                    working_dir, directory_listing['path']):
            return _fast_path_command_result(
                command,
                tool_list_dir(
                    working_dir,
                    directory_listing['path'],
                    show_hidden=directory_listing['show_hidden'],
                    shell_compatible=True,
                ))
        file_find = (
            parse_safe_file_find_command(command)
            if os.environ.get('TOFU_RUN_FIND_FASTPATH', '1') != '0' else None)
        if 'frozen_input_paths' not in fn_args and file_find is not None \
                and _bounded_directory_fast_path_available(
                    working_dir, file_find['path']):
            return _fast_path_command_result(
                command,
                tool_find_files(
                    working_dir,
                    file_find['pattern'],
                    rel_path=file_find['path'],
                    max_results=file_find['max_results'],
                    case_sensitive=file_find['case_sensitive'],
                    shell_output=True,
                    respect_project_ignores=False,
                ))
        result = tool_run_command(working_dir,
                                  command,
                                  # Model-facing timeout removed (see above).
                                  None,
                                  stdin_callback=stdin_callback,
                                  on_chunk=on_chunk,
                                  on_spawn=on_spawn,
                                  on_grep_intercept=on_grep_intercept,
                                  task=task,

                                  runtime_context=runtime_context,
                                  credentials=fn_args.get('credentials'),
                                  **({'frozen_input_paths':fn_args['frozen_input_paths']} if 'frozen_input_paths' in fn_args else {}))
        if working_dir and _is_destructive_command(command):
            from lib.project_mod import tree_index
            try:
                tree_index.invalidate(working_dir)
            except Exception as exc:
                logger.debug(
                    '[run_command] standalone index invalidation failed: %s',
                    exc)
        return result
    return f'Unknown tool: {fn_name}'


#: Tool names ``project_tool_display`` has a dedicated branch for. Kept adjacent
#: to that if/elif chain so a new branch added there is added here in the same
#: edit — ``format_tool_args_brief`` relies on this membership set.
#: ``create_project`` is display-only legacy: the tool was retired, but old
#: tool rounds still need a readable label instead of a generic fallback.
_PROJECT_DISPLAY_TOOLS = frozenset({
    'list_dir', 'read_files', 'grep_search', 'find_files', 'create_project',
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents', 'run_command',
})


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
            # Mirror the reversed-range repair execute_tool applies, so the
            # chip shows the range actually read rather than the raw one.
            sl, el, _ = _normalize_line_range(sl, el)
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
        from lib.attachments import attachment_display_name

        def _attachment_label(path_str):
            # Uploaded-attachment refs render as the user's exact original
            # filename, clearly marked — never as the raw att_media_/att_txt_
            # ref. The registry is populated by model projection before the
            # model can ever pass the ref to read_files.
            if not path_str.startswith(('att_media_', 'att_txt_')):
                return None
            exact = attachment_display_name(path_str)
            return (f'uploaded attachment "{exact}"' if exact
                    else f'uploaded attachment {path_str}')

        bare_basenames = [_split_rootname(p)[1].rsplit('/', 1)[-1] for p in grouped]
        dup = {b for b, c in Counter(bare_basenames).items() if c > 1}
        parts = []
        for p, ranges in list(grouped.items())[:4]:
            attachment_label = _attachment_label(p)
            if attachment_label is not None:
                display_name = attachment_label
            else:
                prefix, bare = _split_rootname(p)
                base = bare.rsplit('/', 1)[-1]
                name = ('/'.join(bare.rsplit('/', 2)[-2:])
                        if base in dup else base)
                display_name = f'{prefix}{name}'
            if ranges:
                parts.append(f'{display_name} {", ".join(ranges)}')
            else:
                parts.append(display_name)
        suffix = f' +{n_files - 4} more' if n_files > 4 else ''
        return f'Read {n_files} file{"s" if n_files != 1 else ""}: {"; ".join(parts)}{suffix}'
    elif fn_name == 'grep_search':
        # Batch mode
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
        # Batch mode
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
    elif fn_name == 'edit_file':
        edits = fn_args.get('edits')
        if edits and isinstance(edits, list):
            batch_path = fn_args.get('path')
            paths = list(dict.fromkeys(
                (e.get('path') or batch_path or '?')
                for e in edits if isinstance(e, dict)))
            # Headline the author's pre-edit intent (schema-capped at 120
            # chars); the per-operation list ("replace, replace, …") carried
            # no information — every batch rendered the same words.
            desc = str(fn_args.get('description') or '').strip()
            # No "(N edits)" count — the expanded round already lists every
            # edit with its own status, so the header count duplicated it.
            label = (f'Edit {paths[0]}' if len(paths) == 1
                     else f'Edit {len(paths)} files')
            return label + (f' — {desc}' if desc else '')
        return 'Edit (empty)'
    elif fn_name == 'apply_diff':
        p = fn_args.get('path', '?')
        desc = fn_args.get('description', '')
        return f'Patch {p}' + (f' — {desc}' if desc else '')
    elif fn_name == 'apply_diffs':
        edits = fn_args.get('edits')
        if edits and isinstance(edits, list):
            paths = list(dict.fromkeys(e.get('path', '?') for e in edits if isinstance(e, dict)))
            desc = fn_args.get('description', '')
            if len(paths) == 1:
                label = f'Patch {paths[0]}'
            elif len(paths) <= 3:
                label = f'Patch {", ".join(paths)}'
            else:
                label = f'Patch {len(paths)} files'
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
            desc = fn_args.get('description', '')
            if len(paths) == 1:
                label = f'Insert into {paths[0]}'
            elif len(paths) <= 3:
                label = f'Insert into {", ".join(paths)}'
            else:
                label = f'Insert into {len(paths)} files'
            return label + (f' — {desc}' if desc else '')
        return 'Insert (empty)'
    elif fn_name == 'run_command':
        cmd = fn_args.get('command', '?')
        return cmd  # Full command without $ prefix — frontend adds it
    return fn_name


# ─────────────────────────────────────────────────────────────────────────────
#  Compact one-line args brief (swarm sub-agent timeline, timer poll trace)
# ─────────────────────────────────────────────────────────────────────────────

def _clip_brief(text, max_len):
    if len(text) > max_len:
        return text[:max_len - 1] + '…'
    return text


def _batch_terms_brief(items, key, singular_header):
    """Extract ``key`` from a batch list of dicts/strings into a one-line brief."""
    parts = []
    for it in items:
        if isinstance(it, dict):
            parts.append(str(it.get(key) or '?'))
        else:
            parts.append(str(it))
    if len(parts) == 1:
        return parts[0]
    return f"{len(parts)} {singular_header}: {'; '.join(parts)}"


def format_tool_args_brief(fn_name, fn_args, max_len=200):
    """Return a one-line human brief of a tool call's salient args.

    Backs the compact per-call lines where the full
    ``lib.tasks_pkg.tool_display`` round-entry rendering is too heavy: the
    swarm panel's sub-agent tool timeline (live SSE + ``tool_log`` → reload
    recovery) and the timer poll's toolTrace.

    Extraction is name-keyed, so it is immune to LLM argument ORDERING — the
    pre-fix ``str(fn_args)[:N]`` showed whatever key the model happened to
    emit first (for ``write_file``/``apply_diff`` usually ``content``/
    ``search``), hiding the path entirely.

    Args:
        fn_name: Canonical tool name.
        fn_args: Argument dict, or a raw JSON string (coerced when possible).
        max_len: Hard cap on the result (ellipsis marks truncation).

    Returns:
        e.g. ``Write lib/foo.py — add retry``, ``Read 3 files: a.py; b.py; c.py``,
        the query for web_search, the URL for fetch_url, the command for
        run_command, or a truncated repr for unknown tools.
    """
    if isinstance(fn_args, str):
        import json
        try:
            parsed = json.loads(fn_args)
        except (ValueError, TypeError) as e:
            logger.debug('[ToolBrief] args string not JSON for %s: %s', fn_name, e)
            parsed = None
        if isinstance(parsed, dict):
            fn_args = parsed
        else:
            return _clip_brief(fn_args, max_len)
    if not isinstance(fn_args, dict):
        return _clip_brief(str(fn_args), max_len)
    if fn_name in _PROJECT_DISPLAY_TOOLS:
        return _clip_brief(project_tool_display(fn_name, fn_args), max_len)
    if fn_name == 'web_search':
        queries = fn_args.get('queries')
        if isinstance(queries, list) and queries:
            return _clip_brief(_batch_terms_brief(queries, 'query', 'searches'), max_len)
        return _clip_brief(str(fn_args.get('query') or 'web_search'), max_len)
    if fn_name == 'fetch_url':
        urls = fn_args.get('urls')
        if isinstance(urls, list) and urls:
            return _clip_brief(_batch_terms_brief(urls, 'url', 'URLs'), max_len)
        return _clip_brief(str(fn_args.get('url') or 'fetch_url'), max_len)
    # Everything else: reuse the full round-label renderer (the same dispatch
    # table the chat timeline uses) instead of dumping the raw args dict — a
    # repr of whatever key the model emitted first is not human information.
    # Lazy import: lib.tasks_pkg.tool_display._renderers imports THIS module.
    try:
        from lib.tasks_pkg.tool_display import tool_round_label
        label = tool_round_label(fn_name, fn_args)
    except Exception as e:
        logger.debug('[ToolBrief] round-label delegation failed for %s: %s',
                     fn_name, e)
        label = ''
    if label and label != fn_name:
        return _clip_brief(' · '.join(label.splitlines()), max_len)
    return _clip_brief(str(fn_args), max_len)
