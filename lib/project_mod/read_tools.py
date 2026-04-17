"""Project read-only tools — list_dir, read_file(s), grep, find_files.

Extracted from tools.py for modularity. Re-exported via tools.py for backward compat.
"""

import fnmatch
import os
import re
import shutil
import subprocess
import time

from lib.log import get_logger
from lib.project_mod.config import (
    BINARY_EXTENSIONS,
    IGNORE_DIRS,
    MAX_DATA_FILE_PREVIEW,
    MAX_FILE_SIZE,
    MAX_GREP_RESULTS,
    MAX_READ_CHARS,
)
from lib.project_mod.config import (
    _state as _project_state,
)
from lib.project_mod.scanner import (
    _estimate_lines,
    _fmt_size,
    _is_data_file,
    _is_likely_data_content,
    _safe_path,
    _should_ignore,
)

logger = get_logger(__name__)

# Detect ripgrep at module load time (5x faster than GNU grep on our codebase)
_HAS_RG = shutil.which('rg') is not None
if _HAS_RG:
    logger.info('[Tools] ripgrep detected — using rg as primary grep engine')
else:
    logger.info('[Tools] ripgrep not found — using GNU grep')

# Detect fd-find at module load time (3-4x faster than GNU find / Python os.walk)
_FD_BIN = shutil.which('fd') or shutil.which('fdfind')  # Debian names it fdfind
if _FD_BIN:
    logger.info('[Tools] fd-find detected at %s — using fd as primary find engine', _FD_BIN)
else:
    logger.info('[Tools] fd-find not found — using Python os.walk for find_files')


# ═══════════════════════════════════════════════════════
#  list_dir
# ═══════════════════════════════════════════════════════

def tool_list_dir(base, rel_path='.'):
    ignore = IGNORE_DIRS
    try:
        target = _safe_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] list_dir safe_path rejected %s: %s', rel_path, e, exc_info=True)
        return str(e)
    if not os.path.isdir(target):
        return f'Not a directory: {rel_path}'
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name)
    except PermissionError:
        logger.debug('[Tools] list_dir permission denied for %s', rel_path, exc_info=True)
        return f'Permission denied: {rel_path}'
    dirs_out, files_out = [], []
    for entry in entries:
        name = entry.name
        try:
            is_d = entry.is_dir(follow_symlinks=False)
        except OSError:
            logger.debug('[Tools] is_dir check failed for entry %s', name)
            continue
        if is_d:
            if name not in ignore and not name.startswith('.'):
                try:
                    cc = sum(1 for e in os.scandir(entry.path)
                             if not e.name.startswith('.') and e.name not in ignore)
                except Exception as e:
                    logger.debug('[Tools] child count scan failed for dir %s: %s', name, e, exc_info=True)
                    cc = '?'
                dirs_out.append(f'  📁 {name}/ ({cc} items)')
        else:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
                sz = st.st_size
            except OSError:
                logger.debug('[Tools] stat failed for file entry %s', name)
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in BINARY_EXTENSIONS and sz > 0:
                lc = _estimate_lines(sz, ext)
                files_out.append(f'  📄 {name} ({lc}L, {_fmt_size(sz)})')
            else:
                files_out.append(f'  📄 {name} ({_fmt_size(sz)})')
    result = f'Directory: {rel_path or "."}\n\n'
    if dirs_out:
        result += 'Directories:\n' + '\n'.join(dirs_out) + '\n\n'
    if files_out:
        result += 'Files:\n' + '\n'.join(files_out)
    if not dirs_out and not files_out:
        result += '(empty or all files ignored)'
    # ── Project Summary when listing root ──
    if rel_path in ('.', '', None) and target == base:
        try:
            fc = _project_state.get('fileCount', 0)
            dc = _project_state.get('dirCount', 0)
            ts = _project_state.get('totalSize', 0)
            langs = _project_state.get('languages', {})
            if fc > 0:
                result += '\n\n── Project Summary ──\n'
                result += f'Total: {fc} files, {dc} dirs, {_fmt_size(ts)}\n'
                if langs:
                    lang_parts = [f'{ext}: {c}' for ext, c in sorted(langs.items(), key=lambda x: -x[1])[:8]]
                    result += f'Languages: {", ".join(lang_parts)}\n'
        except Exception as e:
            logger.debug('[ProjectTools] Non-critical: project summary unavailable for list_dir: %s', e, exc_info=True)
    return result


# ═══════════════════════════════════════════════════════
#  Symbol extraction for code files
# ═══════════════════════════════════════════════════════

_SYMBOL_RE = re.compile(
    r'^(?:'
    r'(?:def|class|async\s+def)\s+(\w+)'
    r'|([A-Z][A-Z_0-9]{2,})\s*='
    r'|(?:function|const|let|var)\s+(\w+)'
    r'|(?:export\s+(?:default\s+)?(?:function|class|const))\s+(\w+)'
    r')',
    re.MULTILINE,
)


def _extract_symbols(text, ext, max_symbols=20):
    """Extract top-level symbol names (def/class/CONSTANT) from source code."""
    if ext not in ('.py', '.js', '.ts', '.jsx', '.tsx', '.mjs'):
        return ''
    symbols = []
    for m in _SYMBOL_RE.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        if name and name not in symbols:
            symbols.append(name)
            if len(symbols) >= max_symbols:
                break
    if not symbols:
        return ''
    sym_str = ', '.join(symbols)
    if len(symbols) >= max_symbols:
        sym_str += ', …'
    return f'  Symbols: {sym_str}\n'


# ═══════════════════════════════════════════════════════
#  read_file / read_files
# ═══════════════════════════════════════════════════════

def _is_absolute_path(path: str) -> bool:
    """Check if a path is absolute (starts with / or ~) rather than project-relative."""
    if not path:
        return False
    return path.startswith('/') or path.startswith('~')


def _read_absolute_file(path: str, start_line=None, end_line=None):
    """Read a file by absolute path, supporting images, PDFs, Office docs, and text.

    Delegates to ``lib.file_reader.read_local_file`` for binary format detection
    and encoding handling.  Adds line-range support on top for text results.

    Args:
        path: Absolute file path (may start with ~ for home expansion).
        start_line: Optional start line (1-based).
        end_line: Optional end line (inclusive).

    Returns:
        For images: dict with ``__screenshot__`` protocol.
        For all other files: str with extracted text content.
    """
    from lib.file_reader import read_local_file as _read_local
    result = _read_local(path)

    # Images return a dict — line ranges don't apply
    if isinstance(result, dict) and result.get('__screenshot__'):
        return result

    # For text results, apply line range if requested
    if isinstance(result, str) and (start_line or end_line) and not result.startswith('❌'):
        lines = result.split('\n')
        total = len(lines)
        s = max(1, start_line or 1) - 1
        e = min(total, end_line or total)
        sliced = '\n'.join(lines[s:e])
        expanded_path = os.path.expanduser(path)
        expanded_path = os.path.abspath(expanded_path)
        filename = os.path.basename(expanded_path)
        header = f'File: {filename} (lines {s + 1}-{e} of {total})\n'
        return header + '─' * 40 + '\n' + sliced

    return result


def _read_project_file(base, rel_path, start_line=None, end_line=None):
    """Read a single project-relative file.  Internal helper for tool_read_files.

    Handles safe-path validation, data-file detection, symbol TOC extraction,
    and truncation.  Absolute paths are NOT handled here — the caller
    (tool_read_files) routes those to _read_absolute_file.
    """
    try:
        target = _safe_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] read_file safe_path rejected %s: %s', rel_path, e, exc_info=True)
        return str(e)
    if not os.path.isfile(target):
        return f'File not found: {rel_path}'
    sz = os.path.getsize(target)
    if sz > MAX_FILE_SIZE:
        return f'File too large ({_fmt_size(sz)}). Use grep_search to find specific content.'

    filename = os.path.basename(rel_path)
    is_data = _is_data_file(filename, sz)

    try:
        with open(target, errors='replace') as f:
            if start_line or end_line:
                all_lines = f.readlines()
                total = len(all_lines)
                s = max(1, start_line or 1) - 1
                e = min(total, end_line or total)
                text = ''.join(all_lines[s:e])
                header = f'File: {rel_path} (lines {s + 1}-{e} of {total})\n'
            else:
                text = f.read()
                total = text.count('\n') + 1

                if is_data or (sz > 20_000 and _is_likely_data_content(text)):
                    preview = text[:MAX_DATA_FILE_PREVIEW]
                    nl = preview.rfind('\n')
                    if nl > 0:
                        preview = preview[:nl]
                    preview_lines = preview.count('\n') + 1
                    header = (f'File: {rel_path} ({total} lines, {_fmt_size(sz)}) '
                              f'[DATA FILE — showing first {preview_lines} lines]\n')
                    text = preview + (
                        f'\n\n… [{total - preview_lines} more lines not shown. '
                        f'This appears to be a data file. Use grep_search to find specific content, '
                        f'or read_file with start_line/end_line for a specific range.]')
                else:
                    if len(text) > MAX_READ_CHARS:
                        text = text[:MAX_READ_CHARS] + f'\n\n… [truncated at {MAX_READ_CHARS:,} chars]'
                    header = f'File: {rel_path} ({total} lines, {_fmt_size(sz)})\n'
                    ext = os.path.splitext(rel_path)[1].lower()
                    sym_toc = _extract_symbols(text, ext)
                    if sym_toc:
                        header += sym_toc
        return header + '─' * 40 + '\n' + text
    except Exception as e:
        logger.warning('[Tools] read_file failed for %s: %s', rel_path, e, exc_info=True)
        return f'Error reading {rel_path}: {e}'


def _merge_same_file_ranges(reads):
    """Merge overlapping/adjacent ranges for the same file.

    Preserves ``_base`` (per-spec base override for multi-root) through
    the merge — the first occurrence's ``_base`` wins for each path group.
    """
    GAP_THRESHOLD = 40
    from collections import OrderedDict
    grouped = OrderedDict()  # path → list[(sl, el)]
    base_map = {}            # path → _base (first seen)
    for spec in reads:
        if not isinstance(spec, dict) or 'path' not in spec:
            grouped.setdefault(None, []).append(spec)
            continue
        p = spec['path']
        sl = spec.get('start_line')
        el = spec.get('end_line')
        grouped.setdefault(p, []).append((sl, el))
        if p not in base_map and '_base' in spec:
            base_map[p] = spec['_base']

    merged = []
    for p, ranges in grouped.items():
        if p is None:
            for spec in ranges:
                merged.append(spec)
            continue
        full_file = any(sl is None and el is None for sl, el in ranges)
        if full_file:
            entry = {'path': p}
            if p in base_map:
                entry['_base'] = base_map[p]
            merged.append(entry)
            continue
        sorted_ranges = sorted(ranges, key=lambda r: (r[0] or 1, r[1] or float('inf')))
        combined = []
        for sl, el in sorted_ranges:
            if not combined:
                combined.append([sl, el])
            else:
                prev_s, prev_e = combined[-1]
                if sl is not None and prev_e is not None and sl <= prev_e + GAP_THRESHOLD:
                    combined[-1][1] = max(prev_e, el) if el is not None else prev_e
                else:
                    combined.append([sl, el])
        for s, e in combined:
            entry = {'path': p}
            if p in base_map:
                entry['_base'] = base_map[p]
            if s is not None:
                entry['start_line'] = s
            if e is not None:
                entry['end_line'] = e
            merged.append(entry)
    return merged



def tool_read_files(base, reads):
    """Batch-read multiple files (or file ranges) in one call.

    Each spec in *reads* is ``{path, start_line?, end_line?}``.
    Multi-root callers may attach ``_base`` per-spec to override the
    default *base* for that particular file.

    Absolute paths (starting with ``/`` or ``~``) are routed to
    ``_read_absolute_file`` and bypass the project sandbox.
    """
    if not reads or not isinstance(reads, list):
        return 'Error: "reads" must be a non-empty array of {path, start_line?, end_line?} objects.'
    MAX_BATCH = 20
    if len(reads) > MAX_BATCH:
        reads = reads[:MAX_BATCH]

    reads = _merge_same_file_ranges(reads)

    parts = []
    image_results = {}  # index → dict for __screenshot__ results
    total_chars = 0
    BATCH_CHAR_BUDGET = 200_000
    WHOLE_FILE_THRESHOLD = 40_000
    for i, spec in enumerate(reads):
        if not isinstance(spec, dict) or 'path' not in spec:
            parts.append(f'[{i+1}] Error: each entry must have a "path" field')
            continue
        rel_path = spec['path']
        sl = spec.get('start_line')
        el = spec.get('end_line')
        spec_base = spec.get('_base', base)  # per-spec base override (multi-root)

        # Route: absolute paths → _read_absolute_file (images, PDFs, Office, text)
        if _is_absolute_path(rel_path):
            result = _read_absolute_file(rel_path, sl, el)
            # Image results are dicts — track separately
            if isinstance(result, dict) and result.get('__screenshot__'):
                text_fallback = result.get('_text_fallback', 'Image loaded.')
                image_results[i] = result
                parts.append(text_fallback)
                total_chars += len(text_fallback)
                continue
            # Text/PDF/Office result — budget as normal string
            if isinstance(result, str):
                if total_chars + len(result) > BATCH_CHAR_BUDGET:
                    remaining = BATCH_CHAR_BUDGET - total_chars
                    if remaining > 200:
                        result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
                    else:
                        parts.append(f'[{i+1}] … [{len(reads) - i} more files skipped — batch budget exceeded]')
                        break
                total_chars += len(result)
                parts.append(result)
                continue
            parts.append(str(result))
            continue

        # Project-relative path — auto-expand small files to whole-file
        if sl is not None or el is not None:
            try:
                target = _safe_path(spec_base, rel_path)
                if os.path.isfile(target):
                    file_sz = os.path.getsize(target)
                    if file_sz <= WHOLE_FILE_THRESHOLD:
                        sl, el = None, None
            except (ValueError, OSError) as e:
                logger.debug('[Tools] read_files range check failed for %s: %s', rel_path, e, exc_info=True)

        result = _read_project_file(spec_base, rel_path, sl, el)
        if total_chars + len(result) > BATCH_CHAR_BUDGET:
            remaining = BATCH_CHAR_BUDGET - total_chars
            if remaining > 200:
                result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
            else:
                parts.append(f'[{i+1}] … [{len(reads) - i} more files skipped — batch budget exceeded]')
                break
        total_chars += len(result)
        parts.append(result)

    text_result = '\n\n'.join(parts)

    # If any image results, return a mixed result with __batch_images__
    if image_results:
        return {
            '__batch_images__': image_results,
            '_text_content': text_result,
        }
    return text_result


# ═══════════════════════════════════════════════════════
#  grep / find_files
# ═══════════════════════════════════════════════════════

def tool_grep(base, pattern, rel_path=None, include=None, context_lines=None,
              max_results=None, count_only=False):
    """Search for a pattern across project files using ripgrep (preferred) or grep.

    Falls back through: rg → grep → pure-Python grep.

    Args:
        max_results: Cap on matching lines returned (like head -n). Default MAX_GREP_RESULTS.
        count_only: If True, return only the match count (like grep -c), not the lines.
    """
    try:
        target = _safe_path(base, rel_path or '.')
    except ValueError as e:
        logger.debug('[Tools] grep safe_path rejected %s: %s', rel_path, e, exc_info=True)
        return str(e)
    ctx_n = max(0, min(10, int(context_lines))) if context_lines else 0
    cap = max(1, min(int(max_results), 500)) if max_results else MAX_GREP_RESULTS

    if _HAS_RG:
        result = _run_rg(base, target, pattern, include, ctx_n, cap, count_only)
        if result is not None:
            return result
        # rg binary vanished or failed — fall through to grep
        logger.warning('[Tools] ripgrep failed, falling back to grep')

    result = _run_gnu_grep(base, target, pattern, include, ctx_n, cap, count_only)
    if result is not None:
        return result

    # Both binaries failed
    logger.info('[Tools] grep binary not found, falling back to Python grep')
    return _python_grep(base, target, pattern, include, cap, count_only)


def _build_rg_cmd(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Build ripgrep command with equivalent behavior to our grep usage."""
    if count_only:
        cmd = ['rg', '-ci', '--color=never', '--no-heading']
    else:
        cmd = ['rg', '-ni', '--color=never', '--no-heading']
    # Skip ignored dirs
    for d in list(IGNORE_DIRS)[:30]:
        cmd.extend(['-g', f'!{d}/'])
    # ★ rg auto-respects .gitignore only inside a git repo (.git/ present).
    #   When there's no .git/ (e.g. exported projects, workdir copies),
    #   explicitly point rg at the .gitignore file so it still honors it.
    #   This is critical — without it, rg crawls into huge ignored dirs
    #   (swebench_workdir, conda_envs, etc.) and times out.
    if base:
        gitignore = os.path.join(base, '.gitignore')
        if os.path.isfile(gitignore) and not os.path.isdir(os.path.join(base, '.git')):
            cmd.extend(['--ignore-file', gitignore])
    if include:
        cmd.extend(['-g', include])
    if ctx_n > 0 and not count_only:
        cmd.extend(['-C', str(ctx_n)])
    if not count_only:
        cmd.extend(['-m', str(cap)])
    # Safety caps: skip huge files and limit search depth
    cmd.extend(['--max-filesize', _RG_MAX_FILESIZE])
    cmd.extend(['--max-depth', str(_TOOL_MAX_DEPTH)])
    cmd.extend(['--', pattern, target])
    return cmd


def _build_grep_cmd(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Build GNU grep command."""
    if count_only:
        cmd = ['grep', '-rci', '--color=never', '-I']
    else:
        cmd = ['grep', '-rni', '--color=never', '-I']
    for d in list(IGNORE_DIRS)[:30]:
        cmd.extend(['--exclude-dir', d])
    # ★ GNU grep doesn't have --ignore-file, so parse .gitignore manually
    #   and add --exclude-dir for directory patterns found there.
    if base:
        gitignore = os.path.join(base, '.gitignore')
        if os.path.isfile(gitignore):
            gi_dirs = _load_gitignore_dirs(gitignore)
            for d in gi_dirs:
                cmd.extend(['--exclude-dir', d])
    if include:
        cmd.extend(['--include', include])
    if ctx_n > 0 and not count_only:
        cmd.extend(['-C', str(ctx_n)])
    if not count_only:
        cmd.extend(['-m', str(cap)])
    cmd.extend(['--', pattern, target])
    return cmd


def _format_grep_output(base, raw_output, pattern, include, ctx_n,
                        cap=MAX_GREP_RESULTS, count_only=False):
    """Format grep/rg output into user-facing result string."""
    output = raw_output.strip()
    if not output:
        hint = f'No matches found for: {pattern}'
        if include:
            hint += f' in {include}'
        if '\\' in pattern or '.*' in pattern or '|' in pattern:
            hint += '\nHint: pattern looks like complex regex. Try a simpler literal substring instead.'
        else:
            hint += '\nHint: try a shorter/broader substring, or check spelling. Search is case-insensitive.'
        return hint

    # count_only mode: sum per-file counts from grep -c / rg -c output
    if count_only:
        total = 0
        for line in output.split('\n'):
            # rg -c / grep -c output: "file:count" or just "count"
            parts = line.rsplit(':', 1)
            try:
                total += int(parts[-1])
            except (ValueError, IndexError):
                continue
        hdr = f'grep "{pattern}"'
        if include:
            hdr += f' ({include})'
        return f'{hdr} \u2014 {total} matches (count only)'

    lines = output.split('\n')
    rel_lines = []
    truncated = False
    total_chars = 0
    max_line_len = 300
    max_total_chars = 20000 if ctx_n > 0 else 12000
    bp = base + '/'
    for line in lines[:cap]:
        if line.startswith(bp):
            line = line[len(bp):]
        if len(line) > max_line_len:
            line = line[:max_line_len] + '  \u2026(truncated)'
        total_chars += len(line) + 1
        if total_chars > max_total_chars:
            truncated = True
            break
        rel_lines.append(line)
    match_count = len(rel_lines)
    if truncated:
        rel_lines.append(f'\u2026 (output truncated at {max_total_chars} chars, {len(lines)} total matches)')
    hdr = f'grep "{pattern}"'
    if include:
        hdr += f' ({include})'
    hdr += f' \u2014 {match_count} matches:\n\n'
    return hdr + '\n'.join(rel_lines)


# Max filesize that rg should bother searching (skip huge data/binary files)
_RG_MAX_FILESIZE = '2M'

# Max depth for rg/fd to search (safety cap)
_TOOL_MAX_DEPTH = 30


def _get_io_timeout(base, default=60):
    """Get adjusted I/O timeout for the given base path (cross-DC aware).

    The base default is 60s (increased from 30s to accommodate large
    projects on FUSE filesystems).
    """
    try:
        from lib.cross_dc import get_timeout_multiplier
        return int(default * get_timeout_multiplier(base))
    except Exception as e:
        logger.debug('[Tools] cross_dc timeout multiplier unavailable: %s', e)
        return default



def _load_gitignore_dirs(gitignore_path):
    """Extract directory names from a .gitignore file for GNU grep --exclude-dir.

    Only returns simple directory entries (e.g. 'swebench_workdir/' or
    'swebench_workdir') — NOT glob patterns, negations, or complex paths.
    This is a lightweight parser for the specific case of feeding exclude
    dirs to GNU grep, which doesn't support --ignore-file natively.
    """
    dirs = []
    try:
        with open(gitignore_path, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('!'):
                    continue
                # Strip leading / (anchored to repo root)
                line = line.lstrip('/')
                # Skip glob patterns and complex entries
                if '*' in line or '?' in line or '**' in line:
                    continue
                # Directory entries: either explicit 'dir/' or bare 'dir' name
                name = line.rstrip('/')
                # Only take simple names (no nested paths like 'a/b')
                if '/' in name:
                    continue
                if name and name not in IGNORE_DIRS:
                    dirs.append(name)
    except OSError as e:
        logger.debug('[Tools] Failed to read .gitignore dirs: %s', e)
    return dirs


def _load_gitignore_patterns(base):
    """Load .gitignore patterns from project root for Python fallback walkers.

    Returns a list of compiled (pattern, is_negation) tuples, or empty list
    if no .gitignore exists.  Supports basic glob patterns and directory
    markers (trailing /).  Does NOT support full gitignore spec (nested
    .gitignore, ** globstar, etc.) — this is a best-effort optimization
    for the rare case when rg/fd are unavailable.
    """
    gi_path = os.path.join(base, '.gitignore')
    if not os.path.isfile(gi_path):
        return []
    patterns = []
    try:
        with open(gi_path, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                negated = line.startswith('!')
                if negated:
                    line = line[1:]
                # Normalize: strip leading / (anchored patterns → treat as relative)
                line = line.lstrip('/')
                patterns.append((line, negated))
    except OSError as e:
        logger.debug('[Tools] Failed to read .gitignore: %s', e)
    return patterns


def _gitignore_match(rel_path, is_dir, patterns):
    """Check if rel_path matches any .gitignore pattern.

    Returns True if the path should be ignored.
    """
    if not patterns:
        return False
    ignored = False
    for pat, negated in patterns:
        # Directory-only pattern (trailing /)
        dir_only = pat.endswith('/')
        if dir_only:
            if not is_dir:
                continue
            pat = pat.rstrip('/')
        # Match against both the full relative path and just the basename
        basename = os.path.basename(rel_path)
        matched = (fnmatch.fnmatch(rel_path, pat)
                   or fnmatch.fnmatch(basename, pat)
                   or fnmatch.fnmatch(rel_path, f'**/{pat}'))
        if matched:
            ignored = not negated
    return ignored


def _run_rg(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Run ripgrep. Returns formatted string on success, None on binary-not-found."""
    cmd = _build_rg_cmd(base, target, pattern, include, ctx_n, cap, count_only)
    io_timeout = _get_io_timeout(base)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=io_timeout, cwd=base, errors='replace')
        return _format_grep_output(base, result.stdout, pattern, include, ctx_n, cap, count_only)
    except subprocess.TimeoutExpired:
        logger.warning('[Tools] rg timed out after %ds: pattern=%s target=%s',
                       io_timeout, pattern[:60], target)
        rel_target = os.path.relpath(target, base) if target != base else '.'
        return (f'Grep timed out after {io_timeout}s searching "{rel_target}". '
                f'Try a more specific subdirectory path or narrower file glob (include parameter).')
    except FileNotFoundError:
        logger.warning('[Tools] rg binary not found despite detection at startup')
        return None
    except Exception as e:
        logger.warning('[Tools] rg failed: pattern=%s target=%s: %s', pattern[:40], target, e, exc_info=True)
        return None


def _run_gnu_grep(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Run GNU grep. Returns formatted string on success, None on binary-not-found."""
    cmd = _build_grep_cmd(base, target, pattern, include, ctx_n, cap, count_only)
    io_timeout = _get_io_timeout(base)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=io_timeout, cwd=base, errors='replace')
        return _format_grep_output(base, result.stdout, pattern, include, ctx_n, cap, count_only)
    except subprocess.TimeoutExpired:
        logger.warning('[Tools] grep timed out after %ds: pattern=%s target=%s',
                       io_timeout, pattern[:60], target)
        rel_target = os.path.relpath(target, base) if target != base else '.'
        return (f'Grep timed out after {io_timeout}s searching "{rel_target}". '
                f'Try a more specific subdirectory path or narrower file glob (include parameter).')
    except FileNotFoundError:
        logger.debug('[Tools] GNU grep binary not found, will try fallback')
        return None
    except Exception as e:
        logger.warning('[Tools] grep failed for pattern=%s target=%s: %s', pattern[:40], target, e, exc_info=True)
        return f'Grep error: {e}'


def _python_grep(base, target, pattern, include=None, cap=MAX_GREP_RESULTS, count_only=False):
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.debug('[Tools] python_grep invalid regex pattern: %s', e, exc_info=True)
        return f'Invalid pattern: {e}'
    gi_patterns = _load_gitignore_patterns(base)
    match_count = 0
    matches = []
    timeout_val = _get_io_timeout(base, default=40)
    deadline = time.time() + timeout_val
    for root, dirs, files in os.walk(target):
        # Prune ignored, hidden, and .gitignore-matched directories
        pruned = []
        for d in dirs:
            if d in IGNORE_DIRS or d.startswith('.'):
                continue
            if gi_patterns:
                d_rel = os.path.relpath(os.path.join(root, d), base)
                if _gitignore_match(d_rel, True, gi_patterns):
                    continue
            pruned.append(d)
        dirs[:] = pruned
        for fname in files:
            if include and not fnmatch.fnmatch(fname, include):
                continue
            if _should_ignore(fname):
                continue
            fp = os.path.join(root, fname)
            # .gitignore check for files
            if gi_patterns:
                f_rel = os.path.relpath(fp, base)
                if _gitignore_match(f_rel, False, gi_patterns):
                    continue
            try:
                if os.path.getsize(fp) > MAX_FILE_SIZE:
                    continue
                with open(fp, errors='replace') as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            match_count += 1
                            if not count_only:
                                rel = os.path.relpath(fp, base)
                                matches.append(f'{rel}:{i}:{line.rstrip()}')
                            if not count_only and len(matches) >= cap:
                                break
            except Exception as e:
                logger.debug('[Tools] grep file read failed for %s: %s', fp, e, exc_info=True)
                continue
            if not count_only and len(matches) >= cap:
                break
        if not count_only and len(matches) >= cap:
            break
        if time.time() > deadline:
            if not count_only:
                matches.append(f'\u23f0 (grep timed out after {timeout_val}s \u2014 '
                               f'try a more specific path or pattern)')
            logger.warning('[Tools] python_grep timed out after %ds', timeout_val)
            break

    if count_only:
        hdr = f'grep "{pattern}"'
        if include:
            hdr += f' ({include})'
        return f'{hdr} \u2014 {match_count} matches (count only)'

    if not matches:
        return f'No matches found for: {pattern}'
    max_line_len = 300
    max_total_chars = 12000
    truncated = []
    total = 0
    for m in matches:
        if len(m) > max_line_len:
            m = m[:max_line_len] + '  \u2026(truncated)'
        total += len(m) + 1
        if total > max_total_chars:
            truncated.append(f'\u2026 (output truncated at {max_total_chars} chars)')
            break
        truncated.append(m)
    return f'grep results ({len(matches)} matches):\n\n' + '\n'.join(truncated)


def _fd_find(target, base, pattern, cap):
    """Find files using fd-find (3-4x faster than os.walk on large dirs).

    Returns list of formatted match strings, or None if fd fails.
    """
    io_timeout = _get_io_timeout(target, default=30)
    cmd = [_FD_BIN, '-g', pattern, target,
           '--type', 'f',
           '--max-results', str(cap),
           '--max-depth', str(_TOOL_MAX_DEPTH)]
    # Exclude ignored dirs + hidden dirs
    for d in IGNORE_DIRS:
        cmd.extend(['--exclude', d])
    # ★ fd auto-respects .gitignore only inside a git repo (.git/ present).
    #   Explicitly point fd at .gitignore when there's no .git/ dir.
    if base:
        gitignore = os.path.join(base, '.gitignore')
        if os.path.isfile(gitignore) and not os.path.isdir(os.path.join(base, '.git')):
            cmd.extend(['--ignore-file', gitignore])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=io_timeout,
        )
        if result.returncode not in (0, 1):  # 1 = no matches (normal)
            logger.debug('[Tools] fd returned code %d: %s', result.returncode, result.stderr[:200])
            return None
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        matches = []
        for line in lines[:cap]:
            full = line if os.path.isabs(line) else os.path.join(target, line)
            rel = os.path.relpath(full, base)
            try:
                sz = os.path.getsize(full)
            except Exception as e:
                logger.debug('[Tools] getsize failed for %s: %s', rel, e, exc_info=True)
                sz = 0
            matches.append(f'  {rel} ({_fmt_size(sz)})')
        return matches
    except subprocess.TimeoutExpired:
        logger.warning('[Tools] fd timed out after %ds for pattern=%s in %s',
                       io_timeout, pattern, os.path.relpath(target, base))
        return None
    except Exception as e:
        logger.warning('[Tools] fd failed: %s', e)
        return None


def _python_find(target, base, pattern, cap):
    """Find files using Python os.walk + fnmatch (fallback).

    Enhanced with .gitignore awareness, heavy directory detection,
    depth limiting, and extra directory pruning.
    """
    gi_patterns = _load_gitignore_patterns(base)
    matches = []
    timeout_val = _get_io_timeout(base, default=30)
    deadline = time.time() + timeout_val
    for root, dirs, files in os.walk(target):
        # Prune directories
        pruned = []
        for d in sorted(dirs):
            if d in IGNORE_DIRS or d.startswith('.'):
                continue
            if gi_patterns:
                d_rel = os.path.relpath(os.path.join(root, d), base)
                if _gitignore_match(d_rel, True, gi_patterns):
                    continue
            pruned.append(d)
        dirs[:] = pruned
        for fname in sorted(files):
            if fnmatch.fnmatch(fname.lower(), pattern.lower()):
                fp = os.path.join(root, fname)
                rel = os.path.relpath(fp, base)
                # .gitignore check
                if gi_patterns and _gitignore_match(rel, False, gi_patterns):
                    continue
                try:
                    sz = os.path.getsize(fp)
                except Exception as e:
                    logger.debug('[Tools] getsize failed for %s: %s', fname, e, exc_info=True)
                    sz = 0
                matches.append(f'  {rel} ({_fmt_size(sz)})')
                if len(matches) >= cap:
                    return matches
        if len(matches) >= cap:
            return matches
        if time.time() > deadline:
            matches.append(f'  \u23f0 (search timed out after {timeout_val}s \u2014 '
                           f'try a more specific path)')
            return matches
    return matches



def tool_grep_batch(base, searches):
    """Batch grep: run multiple searches in one call.

    Each spec in *searches* is ``{pattern, path?, include?, context_lines?, max_results?, count_only?}``.
    Returns combined results separated by headers.

    Args:
        base: Project root directory.
        searches: List of search spec dicts.
    """
    if not searches or not isinstance(searches, list):
        return 'Error: "searches" must be a non-empty array of {pattern, ...} objects.'
    MAX_BATCH = 20
    if len(searches) > MAX_BATCH:
        searches = searches[:MAX_BATCH]

    parts = []
    total_chars = 0
    BATCH_CHAR_BUDGET = 100_000
    for i, spec in enumerate(searches):
        if not isinstance(spec, dict) or 'pattern' not in spec:
            parts.append(f'[{i+1}] Error: each entry must have a "pattern" field')
            continue
        result = tool_grep(
            base,
            spec['pattern'],
            rel_path=spec.get('path'),
            include=spec.get('include'),
            context_lines=spec.get('context_lines'),
            max_results=spec.get('max_results'),
            count_only=bool(spec.get('count_only', False)),
        )
        if total_chars + len(result) > BATCH_CHAR_BUDGET:
            remaining = BATCH_CHAR_BUDGET - total_chars
            if remaining > 200:
                result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
            else:
                parts.append(f'[{i+1}] … [{len(searches) - i} more searches skipped — batch budget exceeded]')
                break
        total_chars += len(result)
        parts.append(result)

    return '\n\n'.join(parts)


def tool_find_files_batch(base, searches):
    """Batch find: run multiple find operations in one call.

    Each spec in *searches* is ``{pattern, path?, max_results?}``.
    Returns combined results separated by headers.

    Args:
        base: Project root directory.
        searches: List of find spec dicts.
    """
    if not searches or not isinstance(searches, list):
        return 'Error: "searches" must be a non-empty array of {pattern, ...} objects.'
    MAX_BATCH = 20
    if len(searches) > MAX_BATCH:
        searches = searches[:MAX_BATCH]

    parts = []
    total_chars = 0
    BATCH_CHAR_BUDGET = 100_000
    for i, spec in enumerate(searches):
        if not isinstance(spec, dict) or 'pattern' not in spec:
            parts.append(f'[{i+1}] Error: each entry must have a "pattern" field')
            continue
        result = tool_find_files(
            base,
            spec['pattern'],
            rel_path=spec.get('path'),
            max_results=spec.get('max_results'),
        )
        if total_chars + len(result) > BATCH_CHAR_BUDGET:
            remaining = BATCH_CHAR_BUDGET - total_chars
            if remaining > 200:
                result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
            else:
                parts.append(f'[{i+1}] … [{len(searches) - i} more finds skipped — batch budget exceeded]')
                break
        total_chars += len(result)
        parts.append(result)

    return '\n\n'.join(parts)


def tool_find_files(base, pattern, rel_path=None, max_results=None):
    """Find files by name glob pattern.

    Uses fd-find when available (3-4x faster on large dirs), falls back to
    Python os.walk + fnmatch.

    Args:
        base: Project root directory.
        pattern: Glob pattern (e.g. '*.py', 'test_*.js').
        rel_path: Subdirectory to search in (relative to base).
        max_results: Cap on number of files returned. Default 100.
    """
    try:
        target = _safe_path(base, rel_path or '.')
    except ValueError as e:
        logger.debug('[Tools] find_files safe_path rejected %s: %s', rel_path, e, exc_info=True)
        return str(e)
    cap = max(1, min(int(max_results), 500)) if max_results else 100

    matches = None
    if _FD_BIN:
        t0 = time.perf_counter()
        matches = _fd_find(target, base, pattern, cap)
        if matches is not None:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.debug('[Tools] fd found %d files in %.1fms', len(matches), elapsed)

    if matches is None:
        t0 = time.perf_counter()
        matches = _python_find(target, base, pattern, cap)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.debug('[Tools] os.walk found %d files in %.1fms', len(matches), elapsed)

    if not matches:
        return f'No files matching: {pattern}'
    hdr = f'Files matching "{pattern}"'
    if rel_path:
        hdr += f' in {rel_path}'
    return hdr + f' ({len(matches)} found):\n\n' + '\n'.join(matches)
