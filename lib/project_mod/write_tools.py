"""Project write tools — write_file, apply_diff, apply_diffs, create_project.

Extracted from tools.py for modularity. Re-exported via tools.py for backward compat.
"""

import os
from difflib import SequenceMatcher

from lib.log import audit_log, get_logger
from lib.project_mod.config import _lock, _roots, _state
from lib.project_mod.modifications import _record_modification
from lib.project_mod.scanner import _fmt_size, _safe_path, add_project_root

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  create_project — bootstrap a new workspace root
# ═══════════════════════════════════════════════════════

# System paths where a user-facing project MUST NOT be created.  These are
# either OS-owned directories (where writing files would likely corrupt the
# system) or special filesystems (/proc, /sys, /dev) where creating a
# directory is meaningless or actively harmful.
#
# Note: we block both exact matches AND any path under these system roots
# (e.g. '/etc/myproj' is rejected).  Windows equivalents are included for
# cross-platform safety, though the dominant deployment is Linux/macOS.
_FORBIDDEN_CREATE_ROOTS = (
    '/', '/etc', '/usr', '/bin', '/sbin', '/boot',
    '/sys', '/proc', '/dev', '/var', '/lib', '/lib32', '/lib64', '/root',
    'C:\\', 'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)',
)


def _is_forbidden_create_path(abs_path):
    """Return True if *abs_path* must not host a new project.

    Rejects:
      - the filesystem root itself ('/' or 'C:\\')
      - exact match with any entry in ``_FORBIDDEN_CREATE_ROOTS``
      - any descendant of the Unix-style system roots
      - the user's ``$HOME`` itself (a project at ~ would shadow many files)
    """
    if not abs_path:
        return True
    p = os.path.normpath(abs_path)
    # Strip trailing separator for comparison, but preserve '/' and 'C:\\'.
    p_cmp = p.rstrip(os.sep) if len(p) > 1 and not (len(p) == 3 and p[1] == ':') else p

    for forb in _FORBIDDEN_CREATE_ROOTS:
        forb_cmp = forb.rstrip(os.sep) if len(forb) > 1 and not (len(forb) == 3 and forb[1] == ':') else forb
        if p_cmp == forb_cmp:
            return True
        # Descendant check — only for Unix-style system roots where
        # every child is system-managed (not '/home', '/opt', '/tmp', etc.).
        if forb in ('/etc', '/usr', '/bin', '/sbin', '/boot',
                    '/sys', '/proc', '/dev', '/lib', '/lib32', '/lib64'):
            if p_cmp.startswith(forb + os.sep):
                return True

    # Reject '$HOME' itself (but allow children like '~/projects/foo').
    try:
        home = os.path.expanduser('~')
        if home and home != '~':
            home_cmp = home.rstrip(os.sep) or home
            if p_cmp == home_cmp:
                return True
    except (OSError, KeyError):
        # Can't determine HOME — skip this check rather than blocking.
        pass

    return False


def tool_create_project(path, name=None, overwrite=False, conv_id=None, task_id=None):
    """Create a new project directory and register it as an extra workspace root.

    After this call, the new path can be addressed with the ``<name>:<rel>``
    prefix in any path-taking tool (``write_file``, ``apply_diff``,
    ``read_files``, ``run_command``, …), or by the absolute path directly.

    Args:
        path: Target directory (may start with ``~``).  Created if missing.
        name: Short root name used as the ``name:`` prefix.  Defaults to
            the directory basename; collisions get a numeric suffix.
        overwrite: If True, accept a non-empty existing directory (files are
            NOT deleted — only the "non-empty" guard is bypassed so the root
            can still be registered).
        conv_id: Conversation ID (for audit log only).
        task_id: Task ID (for audit log only).

    Returns:
        dict with keys: ok, action, path, rootName, created, message, error.
    """
    if not path or not isinstance(path, str):
        return {'ok': False, 'error': 'path is required (non-empty string)',
                'action': 'create_project', 'path': path}

    # Normalise & expand.  abspath(expanduser(...)) handles '~/foo', relative
    # paths resolved against CWD, and trailing separators.
    try:
        abs_path = os.path.abspath(os.path.expanduser(path.strip()))
    except Exception as e:
        logger.warning('[Project] create_project: invalid path %r: %s', path, e)
        return {'ok': False, 'action': 'create_project', 'path': path,
                'error': f'Invalid path: {e}'}

    # ── Safety gate: forbid system paths ──
    if _is_forbidden_create_path(abs_path):
        msg = (f'Refusing to create a project at system path: {abs_path}. '
               f'Choose a user-writable location (e.g. under ~/projects or '
               f'a sibling of the current project).')
        logger.warning('[Project] create_project blocked (system path): %s', abs_path)
        return {'ok': False, 'action': 'create_project', 'path': abs_path, 'error': msg}

    # ── Require an active project session (for audit context & session dir) ──
    with _lock:
        primary = _state.get('path')
    if not primary:
        return {'ok': False, 'action': 'create_project', 'path': abs_path,
                'error': 'No primary project is set. Open a project before calling create_project.'}

    # ── Create or verify directory ──
    already_existed = os.path.exists(abs_path)
    if already_existed:
        if not os.path.isdir(abs_path):
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': f'Path exists but is not a directory: {abs_path}'}
        try:
            has_entries = any(True for _ in os.scandir(abs_path))
        except OSError as e:
            logger.warning('[Project] create_project scandir failed %s: %s', abs_path, e)
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': f'Cannot inspect directory: {e}'}
        if has_entries and not overwrite:
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': (f'Directory exists and is not empty: {abs_path}. '
                              f'Set overwrite=true to register it as a workspace root anyway '
                              f'(existing files are NOT deleted).')}
        created = False
    else:
        try:
            os.makedirs(abs_path, exist_ok=True)
        except OSError as e:
            logger.error('[Project] create_project makedirs failed for %s: %s',
                         abs_path, e, exc_info=True)
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': f'Cannot create directory: {e}'}
        created = True

    # ── Register as extra root (never replace primary) ──
    # add_project_root auto-handles name collisions by appending a suffix
    # and is a no-op if an existing root already maps to the same path.
    try:
        add_project_root(abs_path, name=name)
    except Exception as e:
        logger.error('[Project] create_project: add_project_root failed for %s: %s',
                     abs_path, e, exc_info=True)
        # Don't try to rm_rf the directory we just made — user may want it.
        return {'ok': False, 'action': 'create_project', 'path': abs_path,
                'error': f'Directory ready but failed to register as workspace root: {e}'}

    # Look up the actually-assigned root name (may differ from `name` on collision).
    root_name = None
    with _lock:
        for rn, rs in _roots.items():
            if rs['path'] == abs_path:
                root_name = rn
                break
    if not root_name:
        # Shouldn't happen — add_project_root always adds or finds the entry.
        root_name = (name or os.path.basename(abs_path) or 'root')
        logger.warning('[Project] create_project: root lookup fell through for %s, '
                       'using fallback name %s', abs_path, root_name)

    audit_log('project_create',
              path=abs_path, root_name=root_name,
              created=created, overwrite=bool(overwrite),
              conv_id=conv_id, task_id=task_id)
    logger.info('[Project] create_project: path=%s root=%s created=%s overwrite=%s',
                abs_path, root_name, created, bool(overwrite))

    hint = (f'Use path prefix "{root_name}:<rel>" (e.g. '
            f'write_file(path=\'{root_name}:README.md\', ...)) or absolute paths '
            f'under {abs_path} for subsequent write operations.')
    msg = (f'{"Created" if created else "Registered existing directory"} "{abs_path}" '
           f'as workspace root "{root_name}". {hint}')

    return {
        'ok': True,
        'action': 'create_project',
        'path': abs_path,
        'rootName': root_name,
        'created': created,
        'overwrite': bool(overwrite),
        'message': msg,
    }


# ═══════════════════════════════════════════════════════
#  Fuzzy match helper
# ═══════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════
#  Absolute-path write safety
# ═══════════════════════════════════════════════════════

def _resolve_write_path(base, rel_path):
    """Return the on-disk target for a write/edit tool, accepting either
    a project-relative path or an absolute path under a *registered* root.

    Symmetrically mirrors ``read_files`` which also accepts absolute paths.
    The key safety constraint: an absolute path must resolve INSIDE some
    entry of ``_roots`` (the set of paths the user has explicitly opened
    or created via ``create_project``).  Absolute paths outside every
    registered root are rejected — preventing the model from silently
    writing to ``/etc/passwd`` or similar just because it ignored the
    ``rootname:`` convention.

    Raises ``ValueError`` on rejection so callers can surface the error
    consistently with the existing ``_safe_path`` code path.
    """
    if rel_path and (rel_path.startswith('/') or rel_path.startswith('~')):
        abs_path = os.path.abspath(os.path.expanduser(rel_path))
        # Check containment against every registered root.
        with _lock:
            roots_snapshot = [rs['path'] for rs in _roots.values()]
        for root_path in roots_snapshot:
            norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
            if abs_path == norm_root or abs_path.startswith(norm_root + os.sep):
                return abs_path
        raise ValueError(
            f'Absolute path {abs_path} is outside all registered workspace roots. '
            f'Call create_project(path=...) first, or use a "rootname:relative" prefix.'
        )
    return _safe_path(base, rel_path)


def _find_closest_match(content, search, threshold=0.6):
    """Find the most similar block in content to the search string."""
    search_lines = search.split('\n')
    n = len(search_lines)
    if n == 0 or not content.strip():
        return None

    content_lines = content.split('\n')
    if len(content_lines) < n:
        return None

    best_ratio = 0.0
    best_start = 0

    search_first_stripped = search_lines[0].strip()[:40]
    search_last_stripped = search_lines[-1].strip()[:40] if n > 1 else search_first_stripped
    candidate_starts = set()
    for i, line in enumerate(content_lines):
        ls = line.strip()
        if (search_first_stripped and search_first_stripped in ls) or \
           (search_last_stripped and search_last_stripped in ls):
            for offset in range(max(0, i - n + 1), min(len(content_lines) - n + 1, i + 1)):
                candidate_starts.add(offset)

    if not candidate_starts:
        candidate_starts = set(range(0, len(content_lines) - n + 1, max(1, (len(content_lines) - n) // 500 + 1)))

    for start in candidate_starts:
        window = '\n'.join(content_lines[start:start + n])
        ratio = SequenceMatcher(None, search, window, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_ratio >= threshold:
        best_text = '\n'.join(content_lines[best_start:best_start + n])
        if len(best_text) > 600:
            best_text = best_text[:600] + '\n… (truncated)'
        return {
            'text': best_text,
            'line': best_start + 1,
            'similarity': best_ratio,
        }
    return None


# ═══════════════════════════════════════════════════════
#  VS Code file-watcher nudge
# ═══════════════════════════════════════════════════════

def _touch_for_vscode(filepath):
    """Bump mtime to ensure VS Code's file watcher picks up external writes."""
    try:
        st = os.stat(filepath)
        new_mtime = st.st_mtime + 0.000001
        os.utime(filepath, (st.st_atime, new_mtime))
    except OSError as e:
        logger.debug('[WriteTools] Failed to bump mtime for VS Code watcher on %s: %s', filepath, e)


# ═══════════════════════════════════════════════════════
#  write_file
# ═══════════════════════════════════════════════════════

def tool_write_file(base, rel_path, content, description='', conv_id=None, task_id=None):
    """Write full content to a file. Creates parent dirs if needed.

    Accepts:
      * project-relative paths (sandboxed to *base*), and
      * absolute paths that resolve under a registered workspace root —
        useful for writing into directories created by ``create_project``.
    """
    try:
        target = _resolve_write_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] write_file path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'write_file', 'path': rel_path}

    existed = os.path.isfile(target)
    old_lines = 0
    old_content = None
    if existed:
        try:
            with open(target, errors='replace') as f:
                old_content = f.read()
                old_lines = old_content.count('\n') + 1
        except Exception as e:
            logger.debug('[Tools] write_file old content read failed for %s: %s', rel_path, e, exc_info=True)

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning('[Tools] makedirs failed for parent of %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'error': f'Cannot create directory: {e}',
                    'action': 'write_file', 'path': rel_path}

    original_content = old_content if existed else None

    try:
        with open(target, 'w', newline='') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        new_lines = content.count('\n') + 1
        sz = len(content.encode('utf-8'))

        _record_modification(base, 'write_file', rel_path, original_content,
                             conv_id=conv_id, task_id=task_id)

        result = {
            'ok': True, 'action': 'write_file', 'path': rel_path,
            'created': not existed, 'bytesWritten': sz,
            'lines': new_lines, 'oldLines': old_lines if existed else None,
            'description': description,
        }
        logger.info('write_file: %s (%dL, %s) %s', rel_path, new_lines, _fmt_size(sz),
              '[created]' if not existed else '[updated from %dL]' % old_lines)
        return result
    except Exception as e:
        logger.error('[Tools] write_file failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'write_file', 'path': rel_path}


# ═══════════════════════════════════════════════════════
#  apply_diff / apply_diffs
# ═══════════════════════════════════════════════════════

def _apply_one_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None):
    """Apply a single search-and-replace to a file.

    Accepts project-relative paths and absolute paths under registered roots.
    """
    try:
        target = _resolve_write_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] apply_diff path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path}

    if not os.path.isfile(target):
        return {'ok': False, 'error': f'File not found: {rel_path}',
                'action': 'apply_diff', 'path': rel_path}

    try:
        with open(target, errors='replace') as f:
            content = f.read()
    except Exception as e:
        logger.warning('[Tools] apply_diff read failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': f'Cannot read file: {e}',
                'action': 'apply_diff', 'path': rel_path}

    _tw_replaced = False
    count = content.count(search)
    if count == 0:
        norm_content = content.replace('\r\n', '\n')
        norm_search = search.replace('\r\n', '\n')
        count = norm_content.count(norm_search)
        if count == 0:
            def _rstrip_lines(s):
                return '\n'.join(l.rstrip() for l in s.split('\n'))

            tw_content = _rstrip_lines(norm_content)
            tw_search = _rstrip_lines(norm_search)
            tw_count = tw_content.count(tw_search)

            if tw_count >= 1:
                if tw_count > 1 and not replace_all:
                    return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                            'error': f'Search text matches {tw_count} locations (after trailing-whitespace normalization). '
                                     f'Make it more specific, or set replace_all=true to replace all occurrences.'}
                tw_lines = tw_content.split('\n')
                search_lines = tw_search.split('\n')
                n_sl = len(search_lines)
                content_lines = norm_content.split('\n')

                matched_starts = []
                for i in range(len(tw_lines) - n_sl + 1):
                    if tw_lines[i:i + n_sl] == search_lines:
                        matched_starts.append(i)

                if matched_starts:
                    replace_norm = replace.replace('\r\n', '\n')
                    replace_lines = replace_norm.split('\n')
                    for start_idx in reversed(matched_starts):
                        content_lines[start_idx:start_idx + n_sl] = replace_lines
                        if not replace_all:
                            break
                    content = '\n'.join(content_lines)
                    search = norm_search
                    count = tw_count
                    _tw_replaced = True
                    logger.debug('apply_diff: trailing-WS normalized match in %s '
                                 '(%d locations)', rel_path, tw_count)
                else:
                    tw_count = 0

            if tw_count == 0:
                hint = _find_closest_match(norm_content, norm_search)
                error_msg = (f'Search text not found in {rel_path}. '
                             f'File has {content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += f'\n\n💡 Most similar block (line {hint["line"]}, {hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```'
                return {
                    'ok': False, 'action': 'apply_diff', 'path': rel_path,
                    'error': error_msg,
                    'searchLen': len(search),
                }
        else:
            content = norm_content
            search = norm_search

    if count > 1 and not replace_all:
        return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                'error': f'Search text matches {count} locations. Make it more specific, or set replace_all=true to replace all occurrences.'}

    if _tw_replaced:
        new_content = content
        _orig_line_count = norm_content.count('\n') + 1
    else:
        new_content = content.replace(search, replace) if replace_all else content.replace(search, replace, 1)
        _orig_line_count = content.count('\n') + 1

    reverse_patch = {'search': replace, 'replace': search}
    if replace_all and count > 1:
        reverse_patch['replace_all'] = True

    try:
        with open(target, 'w', newline='') as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        old_lines = _orig_line_count
        new_lines = new_content.count('\n') + 1
        diff_lines = len(search.split('\n'))

        _record_modification(base, 'apply_diff', rel_path, reverse_patch=reverse_patch,
                             conv_id=conv_id, task_id=task_id)

        result = {
            'ok': True, 'action': 'apply_diff', 'path': rel_path,
            'linesChanged': diff_lines,
            'oldLines': old_lines, 'newLines': new_lines,
            'description': description,
        }
        if replace_all and count > 1:
            result['replacedCount'] = count
        logger.info('apply_diff: %s (%d lines changed, %dL → %dL%s)',
              rel_path, diff_lines, old_lines, new_lines,
              f', {count} replacements' if (replace_all and count > 1) else '')
        return result
    except Exception as e:
        logger.error('[Tools] apply_diff write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path}


def tool_apply_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None):
    """Apply a single search-and-replace edit (backward-compatible entry point)."""
    return _apply_one_diff(base, rel_path, search, replace, description, conv_id, replace_all=replace_all, task_id=task_id)


def tool_apply_diffs(base_path, edits, conv_id=None, task_id=None):
    """Apply multiple search-and-replace edits in one batch."""
    if not edits:
        return 'No edits provided.'

    MAX_EDITS = 30
    if len(edits) > MAX_EDITS:
        edits = edits[:MAX_EDITS]

    # Import _resolve_base here (from tools.py) to avoid circular import
    from lib.project_mod.tools import _resolve_base

    results = []
    ok_count = 0
    fail_count = 0

    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            results.append(f'[{i}] ❌ Invalid edit entry')
            fail_count += 1
            continue

        rp = edit.get('path', '')
        search = edit.get('search', '')
        replace = edit.get('replace', '')
        desc = edit.get('description', '')

        if not rp or not search:
            results.append(f'[{i}] ❌ Missing required field (path or search)')
            fail_count += 1
            continue

        ra = bool(edit.get('replace_all', False))

        bp, resolved_rp = _resolve_base(base_path, rp)
        result = _apply_one_diff(bp, resolved_rp, search, replace, desc, conv_id, replace_all=ra, task_id=task_id)

        if result['ok']:
            ok_count += 1
            extra = ''
            if result.get('replacedCount'):
                extra = f' [{result["replacedCount"]} occurrences]'
            results.append(
                f'[{i}] ✅ {result["path"]}: {result["linesChanged"]} lines changed '
                f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] ❌ {rp}: {result["error"]}')

    header = f'Applied {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)


# ═══════════════════════════════════════════════════════
#  insert_content
# ═══════════════════════════════════════════════════════

def _insert_one(base, rel_path, anchor, content, position='after', description='', conv_id=None, task_id=None):
    """Insert content before or after an anchor string in a file.

    Args:
        base: Project base path.
        rel_path: Relative file path.
        anchor: Literal string to locate the insertion point.
        content: New content to insert.
        position: 'before' or 'after' the anchor.
        description: Optional description.
        conv_id: Conversation ID for undo tracking.
        task_id: Task ID for undo tracking.

    Returns:
        dict with ok, action, path, error (on failure), or ok + line info (on success).
    """
    try:
        target = _resolve_write_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] insert_content path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path}

    if not os.path.isfile(target):
        return {'ok': False, 'error': f'File not found: {rel_path}',
                'action': 'insert_content', 'path': rel_path}

    try:
        with open(target, errors='replace') as f:
            file_content = f.read()
    except Exception as e:
        logger.warning('[Tools] insert_content read failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': f'Cannot read file: {e}',
                'action': 'insert_content', 'path': rel_path}

    # ── Locate anchor (same normalization strategy as apply_diff) ──
    norm_content = file_content
    norm_anchor = anchor
    _normalized = False

    count = file_content.count(anchor)
    if count == 0:
        # Try CRLF → LF normalization
        norm_content = file_content.replace('\r\n', '\n')
        norm_anchor = anchor.replace('\r\n', '\n')
        count = norm_content.count(norm_anchor)
        if count > 0:
            _normalized = True
        else:
            # Try trailing-whitespace normalization
            def _rstrip_lines(s):
                return '\n'.join(l.rstrip() for l in s.split('\n'))

            tw_content = _rstrip_lines(norm_content)
            tw_anchor = _rstrip_lines(norm_anchor)
            tw_count = tw_content.count(tw_anchor)

            if tw_count == 0:
                hint = _find_closest_match(norm_content, norm_anchor)
                error_msg = (f'Anchor text not found in {rel_path}. '
                             f'File has {file_content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += (f'\n\n💡 Most similar block (line {hint["line"]}, '
                                  f'{hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```')
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg, 'anchorLen': len(anchor)}

            if tw_count > 1:
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': f'Anchor text matches {tw_count} locations '
                                 f'(after trailing-whitespace normalization). '
                                 f'Make it more specific.'}

            # Single match after TW normalization — find the real position
            # by matching line-by-line in the original content
            tw_lines = tw_content.split('\n')
            anchor_lines = tw_anchor.split('\n')
            n_al = len(anchor_lines)
            content_lines = norm_content.split('\n')

            match_start = None
            for i in range(len(tw_lines) - n_al + 1):
                if tw_lines[i:i + n_al] == anchor_lines:
                    match_start = i
                    break

            if match_start is not None:
                # Reconstruct the original anchor text from the file
                orig_anchor_lines = content_lines[match_start:match_start + n_al]
                norm_anchor = '\n'.join(orig_anchor_lines)
                norm_content = norm_content  # already LF-normalized
                count = 1
                _normalized = True
                logger.debug('insert_content: trailing-WS normalized match in %s', rel_path)
            else:
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': 'Anchor matched after normalization but line mapping failed. '
                                 'Please use read_files to get the exact content.'}

    if _normalized:
        file_content = norm_content
        anchor = norm_anchor

    if count > 1:
        return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                'error': f'Anchor text matches {count} locations. '
                         f'Make it more specific to identify a unique position.'}

    # ── Build new content ──
    anchor_idx = file_content.index(anchor)

    if position == 'before':
        # Insert content before the anchor
        # Ensure proper newline separation
        insert_text = content
        if not insert_text.endswith('\n'):
            insert_text += '\n'
        new_content = file_content[:anchor_idx] + insert_text + file_content[anchor_idx:]
    else:  # 'after'
        # Insert content after the anchor
        after_idx = anchor_idx + len(anchor)
        insert_text = content
        # Ensure a newline between anchor and inserted content
        if after_idx < len(file_content) and file_content[after_idx] != '\n':
            insert_text = '\n' + insert_text
        elif after_idx < len(file_content):
            # anchor ends, next char is \n — insert after that newline
            after_idx += 1
        if not insert_text.endswith('\n'):
            insert_text += '\n'
        new_content = file_content[:after_idx] + insert_text + file_content[after_idx:]

    # ── Build reverse patch for undo ──
    # For undo, we just need to remove the inserted content.
    # We can do this as an apply_diff-style reverse patch:
    # search = anchor + inserted content (or inserted content + anchor)
    # replace = anchor
    if position == 'before':
        reverse_patch = {'search': insert_text + anchor, 'replace': anchor}
    else:
        chunk_start = anchor_idx
        chunk_end = anchor_idx + len(anchor) + len(insert_text)
        # Adjust if we consumed the trailing newline of anchor
        if file_content[anchor_idx + len(anchor):anchor_idx + len(anchor) + 1] == '\n':
            chunk_end = anchor_idx + len(anchor) + 1 + len(insert_text)
        inserted_block = new_content[chunk_start:chunk_end]
        reverse_patch = {'search': inserted_block, 'replace': file_content[anchor_idx:anchor_idx + len(anchor) + (1 if file_content[anchor_idx + len(anchor):anchor_idx + len(anchor) + 1] == '\n' else 0)]}

    # ── Write ──
    try:
        with open(target, 'w', newline='') as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        old_lines = file_content.count('\n') + 1
        new_lines = new_content.count('\n') + 1
        inserted_lines = content.count('\n') + 1

        _record_modification(base, 'apply_diff', rel_path, reverse_patch=reverse_patch,
                             conv_id=conv_id, task_id=task_id)

        # Calculate which line the insertion happened at
        anchor_line = file_content[:anchor_idx].count('\n') + 1

        result = {
            'ok': True, 'action': 'insert_content', 'path': rel_path,
            'position': position,
            'anchorLine': anchor_line,
            'linesInserted': inserted_lines,
            'oldLines': old_lines, 'newLines': new_lines,
            'description': description,
        }
        logger.info('insert_content: %s (%d lines inserted %s anchor at L%d, %dL → %dL)',
                     rel_path, inserted_lines, position, anchor_line,
                     old_lines, new_lines)
        return result
    except Exception as e:
        logger.error('[Tools] insert_content write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path}


def tool_insert_content(base, rel_path, anchor, content, position='after', description='', conv_id=None, task_id=None):
    """Insert content before or after an anchor string (single edit entry point)."""
    return _insert_one(base, rel_path, anchor, content, position, description, conv_id, task_id=task_id)


def tool_insert_contents(base_path, edits, conv_id=None, task_id=None):
    """Apply multiple insert_content edits in one batch."""
    if not edits:
        return 'No edits provided.'

    MAX_EDITS = 30
    if len(edits) > MAX_EDITS:
        edits = edits[:MAX_EDITS]

    from lib.project_mod.tools import _resolve_base

    results = []
    ok_count = 0
    fail_count = 0

    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            results.append(f'[{i}] ❌ Invalid edit entry')
            fail_count += 1
            continue

        rp = edit.get('path', '')
        anchor = edit.get('anchor', '')
        content = edit.get('content', '')
        position = edit.get('position', 'after')
        desc = edit.get('description', '')

        if not rp or not anchor:
            results.append(f'[{i}] ❌ Missing required field (path or anchor)')
            fail_count += 1
            continue

        if position not in ('before', 'after'):
            results.append(f'[{i}] ❌ Invalid position: {position} (must be "before" or "after")')
            fail_count += 1
            continue

        bp, resolved_rp = _resolve_base(base_path, rp)
        result = _insert_one(bp, resolved_rp, anchor, content, position, desc, conv_id, task_id=task_id)

        if result['ok']:
            ok_count += 1
            results.append(
                f'[{i}] ✅ {result["path"]}: {result["linesInserted"]} lines inserted '
                f'{result["position"]} anchor at L{result["anchorLine"]} '
                f'({result["oldLines"]}L → {result["newLines"]}L)'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] ❌ {rp}: {result["error"]}')

    header = f'Inserted {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)
