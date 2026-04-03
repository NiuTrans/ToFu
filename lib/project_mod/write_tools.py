"""Project write tools — write_file, apply_diff, apply_diffs.

Extracted from tools.py for modularity. Re-exported via tools.py for backward compat.
"""

import os
from difflib import SequenceMatcher

from lib.log import get_logger
from lib.project_mod.config import MAX_FILE_SIZE
from lib.project_mod.modifications import _record_modification, _schedule_index_update
from lib.project_mod.scanner import _fmt_size, _safe_path

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Fuzzy match helper
# ═══════════════════════════════════════════════════════

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
    except OSError:
        pass


# ═══════════════════════════════════════════════════════
#  write_file
# ═══════════════════════════════════════════════════════

def tool_write_file(base, rel_path, content, description='', conv_id=None, task_id=None):
    """Write full content to a file. Creates parent dirs if needed."""
    try:
        target = _safe_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] write_file safe_path rejected %s: %s', rel_path, e, exc_info=True)
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
        _schedule_index_update(base, rel_path)

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
    """Apply a single search-and-replace to a file."""
    try:
        target = _safe_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] apply_diff safe_path rejected %s: %s', rel_path, e, exc_info=True)
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
        _schedule_index_update(base, rel_path)

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
