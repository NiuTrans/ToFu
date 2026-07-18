"""lib/project_mod/write_tools/_ops.py — the write/edit OPERATIONS.

Extracted verbatim from the former flat ``write_tools.py``: the public tool
entry points (``tool_write_file`` / ``save_uploaded_file`` / ``tool_apply_diff``
/ ``tool_apply_diffs`` / ``tool_insert_content`` / ``tool_insert_contents``) plus
their single-edit cores (``_apply_one_diff`` / ``_insert_one``). Every operation
resolves + attributes through the ``_paths`` core and matches through the
``_text`` helpers.
"""

import os

from lib.log import get_logger
from lib.project_mod.modifications import _record_modification
from lib.project_mod.scanner import _fmt_size

from ._paths import (
    _enforce_not_readonly,
    _mod_attribution,
    _resolve_write_path,
    _should_record_modification,
)
from ._text import (
    _decode_unicode_escapes,
    _describe_duplicate_matches,
    _find_closest_match,
    _touch_for_vscode,
)

logger = get_logger(__name__)


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
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
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

        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'write_file', _mod_rel, original_content,
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
#  save_uploaded_file — binary-safe drag-and-drop into a project folder
# ═══════════════════════════════════════════════════════
# Backs POST /api/v1/project/upload. Unlike tool_write_file (text `content`),
# this writes RAW BYTES so images / PDFs / archives dropped onto the folder
# browser land on disk intact. It deliberately does NOT auto-register a new
# workspace root the way an absolute-path agent write does: a UI file-drop
# into a directory the user has not attached is a mistake we want to surface,
# not silently expand the workspace. The destination must therefore already
# resolve INSIDE a registered root (any form: primary, extra, or a subdir of
# one). Read-only roots are refused, name collisions auto-rename (never
# clobber), and the write is recorded so it appears in the file-changes bar
# and is undoable exactly like an agent write.

def _dedupe_target(target):
    """Return *target*, or a ``name (n).ext`` sibling if it already exists.

    Preserves the extension and inserts a `` (n)`` counter before it, mirroring
    the OS "copy" convention. Bounded to avoid an unbounded loop on a pathologic
    directory; falls back to a timestamp suffix past the cap.
    """
    if not os.path.exists(target):
        return target
    root, ext = os.path.splitext(target)
    for n in range(1, 1000):
        candidate = f'{root} ({n}){ext}'
        if not os.path.exists(candidate):
            return candidate
    import time as _t
    return f'{root} ({int(_t.time() * 1000)}){ext}'


def save_uploaded_file(base, rel_path, data, description='', conv_id=None,
                       task_id=None, on_conflict='rename'):
    """Save raw *data* bytes to a project file dropped via the folder browser.

    Args:
        base: The active project root (absolute path).
        rel_path: Destination path — a project-relative path OR an absolute
            path that MUST resolve inside an already-registered workspace root.
        data: The file bytes.
        description: Optional note (unused by the model; kept for symmetry).
        conv_id / task_id: Attribution for the undo journal.
        on_conflict: ``'rename'`` (default — auto-suffix ``name (1).ext``) or
            ``'overwrite'`` (replace in place, recording the pre-image so undo
            restores it).

    Returns:
        dict: ``{ok, action, path, created, renamed, bytesWritten}`` on success,
        or ``{ok: False, error, ...}`` on rejection/failure. Never raises for
        the expected cases (read-only root, unregistered path, IO error).
    """
    if not isinstance(data, (bytes, bytearray)):
        return {'ok': False, 'error': 'save_uploaded_file expects bytes',
                'action': 'upload_file', 'path': rel_path}

    # Resolve WITHOUT the auto-register branch: a UI drop must target an
    # already-attached root. We reuse _resolve_write_path only for the
    # relative-path + read-only enforcement; for an absolute path we first
    # verify it lives under a registered root ourselves so a stray drop can't
    # invent a workspace.
    is_abs = bool(rel_path) and (rel_path.startswith('/') or rel_path.startswith('~'))
    if is_abs:
        abs_path = os.path.abspath(os.path.expanduser(rel_path))
        from lib.project_mod.config import _lock, _roots
        with _lock:
            roots_snapshot = [rs['path'] for rs in _roots.values()]
        inside_root = False
        for root_path in roots_snapshot:
            norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
            if abs_path == norm_root or abs_path.startswith(norm_root + os.sep):
                inside_root = True
                break
        if not inside_root:
            return {'ok': False, 'action': 'upload_file', 'path': rel_path,
                    'error': ('Destination is not inside any attached workspace '
                              'folder. Add it as a project folder first, then drop.')}
        try:
            _enforce_not_readonly(abs_path, conv_id=conv_id)
        except ValueError as e:
            logger.debug('[Tools] upload rejected (readonly) %s: %s', rel_path, e)
            return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}
        target = abs_path
    else:
        try:
            target = _resolve_write_path(base, rel_path, conv_id=conv_id)
        except ValueError as e:
            logger.debug('[Tools] upload path rejected %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning('[Tools] upload makedirs failed for parent of %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'error': f'Cannot create directory: {e}',
                    'action': 'upload_file', 'path': rel_path}

    renamed = False
    original_content = None
    existed = os.path.isfile(target)
    if existed and on_conflict == 'rename':
        new_target = _dedupe_target(target)
        renamed = new_target != target
        target = new_target
        existed = os.path.isfile(target)
    if existed:  # overwrite path — capture pre-image so undo restores bytes
        try:
            with open(target, 'rb') as f:
                original_content = f.read()
        except Exception as e:
            logger.debug('[Tools] upload pre-image read failed for %s: %s', target, e)

    try:
        with open(target, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        sz = len(data)

        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, target if is_abs else rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'write_file', _mod_rel, original_content,
                                 conv_id=conv_id, task_id=task_id)

        logger.info('upload_file: %s (%s) %s', target, _fmt_size(sz),
                    '[created]' if not (existed and original_content is not None) else '[overwrote]')
        return {
            'ok': True, 'action': 'upload_file', 'path': target,
            'name': os.path.basename(target),
            'created': original_content is None,
            'renamed': renamed, 'bytesWritten': sz,
            'description': description,
        }
    except Exception as e:
        logger.error('[Tools] upload_file failed for %s: %s', target, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}


# ═══════════════════════════════════════════════════════
#  apply_diff / apply_diffs
# ═══════════════════════════════════════════════════════

def _apply_one_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None):
    """Apply a single search-and-replace to a file.

    Accepts project-relative paths and absolute paths under registered roots.
    """
    try:
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
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
                    locs = _describe_duplicate_matches(tw_content, tw_search)
                    error_msg = (f'Search text matches {tw_count} locations (after trailing-whitespace '
                                 f'normalization). Make it more specific (add surrounding lines so it '
                                 f'matches exactly once), or set replace_all=true to replace all occurrences.')
                    if locs:
                        error_msg += f'\n\n{locs}'
                    return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                            'error': error_msg}
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

            # ── Tier 4: unicode-escape normalization ──
            # The model often emits a real glyph (⏰, em-dash …) where the file
            # holds the literal escape sequence (\u23f0, \u2014), or vice-versa.
            # Decode \uXXXX / \UXXXXXXXX / \xXX on BOTH sides for the comparison
            # only, then splice the model's verbatim replacement into the real
            # file lines.
            if tw_count == 0:
                esc_content_lines = [_decode_unicode_escapes(l).rstrip()
                                     for l in norm_content.split('\n')]
                esc_search_lines = [_decode_unicode_escapes(l).rstrip()
                                    for l in norm_search.split('\n')]
                n_el = len(esc_search_lines)
                esc_starts = [i for i in range(len(esc_content_lines) - n_el + 1)
                              if esc_content_lines[i:i + n_el] == esc_search_lines]
                if esc_starts:
                    if len(esc_starts) > 1 and not replace_all:
                        esc_content = '\n'.join(esc_content_lines)
                        esc_search = '\n'.join(esc_search_lines)
                        locs = _describe_duplicate_matches(esc_content, esc_search)
                        error_msg = (f'Search text matches {len(esc_starts)} locations (after unicode-escape '
                                     f'normalization). Make it more specific (add surrounding lines so it '
                                     f'matches exactly once), or set replace_all=true to replace all occurrences.')
                        if locs:
                            error_msg += f'\n\n{locs}'
                        return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                                'error': error_msg}
                    content_lines = norm_content.split('\n')
                    replace_lines = replace.replace('\r\n', '\n').split('\n')
                    for start_idx in reversed(esc_starts):
                        content_lines[start_idx:start_idx + n_el] = replace_lines
                        if not replace_all:
                            break
                    content = '\n'.join(content_lines)
                    search = norm_search
                    count = len(esc_starts)
                    _tw_replaced = True
                    logger.debug('apply_diff: unicode-escape normalized match in %s '
                                 '(%d locations)', rel_path, count)

            if tw_count == 0 and not _tw_replaced:
                hint = _find_closest_match(norm_content, norm_search)
                error_msg = (f'Search text not found in {rel_path}. '
                             f'File has {content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += f'\n\nMost similar block (line {hint["line"]}, {hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```'
                return {
                    'ok': False, 'action': 'apply_diff', 'path': rel_path,
                    'error': error_msg,
                    'searchLen': len(search),
                }
        else:
            content = norm_content
            search = norm_search

    if count > 1 and not replace_all:
        locs = _describe_duplicate_matches(content, search)
        error_msg = (f'Search text matches {count} locations. Make it more specific (add surrounding '
                     f'lines so it matches exactly once), or set replace_all=true to replace all occurrences.')
        if locs:
            error_msg += f'\n\n{locs}'
        return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                'error': error_msg}

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

        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=content,
                                 reverse_patch=reverse_patch,
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


def _invalid_edit_entry_msg(i, edit):
    """Build an actionable FAIL line for a batch edit that isn't an object.

    The common cause is a model emitting the whole ``edits`` array as one
    escaped JSON *string* (often with unescaped inner quotes, so it can't be
    auto-parsed) — the harness then wraps that string into a single-element
    list, and each element is a str, not a dict. Tell the model exactly that
    so it re-emits a real array of objects instead of retrying blind.
    """
    if isinstance(edit, str):
        return (f'[{i}] FAIL Invalid edit entry: got a string, expected an '
                f'object with {{path, search, replace}}. The "edits" array '
                f'must be real JSON objects, not a single stringified-JSON '
                f'blob — re-send each edit as its own object.')
    return (f'[{i}] FAIL Invalid edit entry: expected an object with '
            f'{{path, search, replace}}, got {type(edit).__name__}.')


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
            results.append(_invalid_edit_entry_msg(i, edit))
            fail_count += 1
            continue

        rp = edit.get('path', '')
        search = edit.get('search', '')
        replace = edit.get('replace', '')
        desc = edit.get('description', '')

        if not rp or not search:
            results.append(f'[{i}] FAIL Missing required field (path or search)')
            fail_count += 1
            continue

        ra = bool(edit.get('replace_all', False))

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_apply_diffs caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {_rve}')
            continue
        result = _apply_one_diff(bp, resolved_rp, search, replace, desc, conv_id, replace_all=ra, task_id=task_id)

        if result['ok']:
            ok_count += 1
            extra = ''
            if result.get('replacedCount'):
                extra = f' [{result["replacedCount"]} occurrences]'
            results.append(
                f'[{i}] OK {result["path"]}: {result["linesChanged"]} lines changed '
                f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {result["error"]}')

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
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
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

            # ── Tier 4: unicode-escape normalization ──
            # Glyph-vs-literal-escape drift (anchor "⏰" vs file "\u23f0").
            # Decode \uXXXX / \UXXXXXXXX / \xXX on both sides for matching,
            # then reconstruct the real anchor text from the file lines.
            if tw_count == 0:
                esc_content_lines = [_decode_unicode_escapes(l).rstrip()
                                     for l in norm_content.split('\n')]
                esc_anchor_lines = [_decode_unicode_escapes(l).rstrip()
                                    for l in norm_anchor.split('\n')]
                n_el = len(esc_anchor_lines)
                esc_starts = [i for i in range(len(esc_content_lines) - n_el + 1)
                              if esc_content_lines[i:i + n_el] == esc_anchor_lines]
                if len(esc_starts) == 1:
                    real_lines = norm_content.split('\n')[esc_starts[0]:esc_starts[0] + n_el]
                    norm_anchor = '\n'.join(real_lines)
                    count = 1
                    _normalized = True
                    logger.debug('insert_content: unicode-escape normalized match in %s', rel_path)
                elif len(esc_starts) > 1:
                    esc_content = '\n'.join(esc_content_lines)
                    esc_anchor = '\n'.join(esc_anchor_lines)
                    locs = _describe_duplicate_matches(esc_content, esc_anchor)
                    error_msg = (f'Anchor text matches {len(esc_starts)} locations (after unicode-escape '
                                 f'normalization). Make it more specific by adding surrounding lines so it '
                                 f'matches exactly once.')
                    if locs:
                        error_msg += f'\n\n{locs}'
                    return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                            'error': error_msg}

            if tw_count == 0 and not _normalized:
                hint = _find_closest_match(norm_content, norm_anchor)
                error_msg = (f'Anchor text not found in {rel_path}. '
                             f'File has {file_content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += (f'\n\nMost similar block (line {hint["line"]}, '
                                  f'{hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```')
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg, 'anchorLen': len(anchor)}

            if tw_count > 1:
                locs = _describe_duplicate_matches(tw_content, tw_anchor)
                error_msg = (f'Anchor text matches {tw_count} locations (after trailing-whitespace '
                             f'normalization). Make it more specific by adding surrounding lines so it '
                             f'matches exactly once.')
                if locs:
                    error_msg += f'\n\n{locs}'
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg}

            # Single match after TW normalization — find the real position
            # by matching line-by-line in the original content.
            # Skipped when tier-4 escape normalization already resolved a match.
            if tw_count == 1 and not _normalized:
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
        locs = _describe_duplicate_matches(file_content, anchor)
        error_msg = (f'Anchor text matches {count} locations. Make it more specific to identify a '
                     f'unique position by adding surrounding lines.')
        if locs:
            error_msg += f'\n\n{locs}'
        return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                'error': error_msg}

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

        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=file_content,
                                 reverse_patch=reverse_patch,
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
            results.append(_invalid_edit_entry_msg(i, edit))
            fail_count += 1
            continue

        rp = edit.get('path', '')
        anchor = edit.get('anchor', '')
        content = edit.get('content', '')
        position = edit.get('position', 'after')
        desc = edit.get('description', '')

        if not rp or not anchor:
            results.append(f'[{i}] FAIL Missing required field (path or anchor)')
            fail_count += 1
            continue

        if position not in ('before', 'after'):
            results.append(f'[{i}] FAIL Invalid position: {position} (must be "before" or "after")')
            fail_count += 1
            continue

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_insert_contents caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {_rve}')
            continue
        result = _insert_one(bp, resolved_rp, anchor, content, position, desc, conv_id, task_id=task_id)

        if result['ok']:
            ok_count += 1
            results.append(
                f'[{i}] OK {result["path"]}: {result["linesInserted"]} lines inserted '
                f'{result["position"]} anchor at L{result["anchorLine"]} '
                f'({result["oldLines"]}L → {result["newLines"]}L)'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {result["error"]}')

    header = f'Inserted {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)
