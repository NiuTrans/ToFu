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


# ═══════════════════════════════════════════════════════
#  list_dir
# ═══════════════════════════════════════════════════════

def tool_list_dir(base, rel_path='.'):
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
            if name not in IGNORE_DIRS and not name.startswith('.'):
                try:
                    cc = sum(1 for e in os.scandir(entry.path)
                             if not e.name.startswith('.') and e.name not in IGNORE_DIRS)
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

def tool_read_file(base, rel_path, start_line=None, end_line=None):
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
    """Merge overlapping/adjacent ranges for the same file."""
    GAP_THRESHOLD = 40
    from collections import OrderedDict
    grouped = OrderedDict()
    for spec in reads:
        if not isinstance(spec, dict) or 'path' not in spec:
            grouped.setdefault(None, []).append(spec)
            continue
        p = spec['path']
        sl = spec.get('start_line')
        el = spec.get('end_line')
        grouped.setdefault(p, []).append((sl, el))

    merged = []
    for p, ranges in grouped.items():
        if p is None:
            for spec in ranges:
                merged.append(spec)
            continue
        full_file = any(sl is None and el is None for sl, el in ranges)
        if full_file:
            merged.append({'path': p})
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
            if s is not None:
                entry['start_line'] = s
            if e is not None:
                entry['end_line'] = e
            merged.append(entry)
    return merged



def tool_read_files(base, reads):
    """Batch-read multiple files (or file ranges) in one call."""
    if not reads or not isinstance(reads, list):
        return 'Error: "reads" must be a non-empty array of {path, start_line?, end_line?} objects.'
    MAX_BATCH = 20
    if len(reads) > MAX_BATCH:
        reads = reads[:MAX_BATCH]

    reads = _merge_same_file_ranges(reads)

    parts = []
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

        if sl is not None or el is not None:
            try:
                target = _safe_path(base, rel_path)
                if os.path.isfile(target):
                    file_sz = os.path.getsize(target)
                    if file_sz <= WHOLE_FILE_THRESHOLD:
                        sl, el = None, None
            except (ValueError, OSError) as e:
                logger.debug('[Tools] read_files range check failed for %s: %s', rel_path, e, exc_info=True)
                pass

        result = tool_read_file(base, rel_path, sl, el)
        if total_chars + len(result) > BATCH_CHAR_BUDGET:
            remaining = BATCH_CHAR_BUDGET - total_chars
            if remaining > 200:
                result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
            else:
                parts.append(f'[{i+1}] … [{len(reads) - i} more files skipped — batch budget exceeded]')
                break
        total_chars += len(result)
        parts.append(result)
    return '\n\n'.join(parts)


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


def _build_rg_cmd(target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Build ripgrep command with equivalent behavior to our grep usage."""
    if count_only:
        cmd = ['rg', '-ci', '--color=never', '--no-heading']
    else:
        cmd = ['rg', '-ni', '--color=never', '--no-heading']
    # Skip our standard ignore dirs (rg also auto-respects .gitignore)
    for d in list(IGNORE_DIRS)[:20]:
        cmd.extend(['-g', f'!{d}/'])
    if include:
        cmd.extend(['-g', include])
    if ctx_n > 0 and not count_only:
        cmd.extend(['-C', str(ctx_n)])
    if not count_only:
        cmd.extend(['-m', str(cap)])
    cmd.extend(['--', pattern, target])
    return cmd


def _build_grep_cmd(target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Build GNU grep command."""
    if count_only:
        cmd = ['grep', '-rci', '--color=never', '-I']
    else:
        cmd = ['grep', '-rni', '--color=never', '-I']
    for d in list(IGNORE_DIRS)[:20]:
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


def _run_rg(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Run ripgrep. Returns formatted string on success, None on binary-not-found."""
    cmd = _build_rg_cmd(target, pattern, include, ctx_n, cap, count_only)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30, cwd=base, errors='replace')
        return _format_grep_output(base, result.stdout, pattern, include, ctx_n, cap, count_only)
    except subprocess.TimeoutExpired:
        logger.warning('[Tools] rg timed out: pattern=%s target=%s', pattern[:40], target)
        return 'Grep timed out. Try a more specific pattern or path.'
    except FileNotFoundError:
        logger.warning('[Tools] rg binary not found despite detection at startup')
        return None
    except Exception as e:
        logger.warning('[Tools] rg failed: pattern=%s target=%s: %s', pattern[:40], target, e, exc_info=True)
        return None


def _run_gnu_grep(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Run GNU grep. Returns formatted string on success, None on binary-not-found."""
    cmd = _build_grep_cmd(target, pattern, include, ctx_n, cap, count_only)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30, cwd=base, errors='replace')
        return _format_grep_output(base, result.stdout, pattern, include, ctx_n, cap, count_only)
    except subprocess.TimeoutExpired:
        logger.warning('[Tools] grep timed out: pattern=%s target=%s', pattern[:40], target)
        return 'Grep timed out. Try a more specific pattern or path.'
    except FileNotFoundError:
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
    match_count = 0
    matches = []
    deadline = time.time() + 20
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            if include and not fnmatch.fnmatch(fname, include):
                continue
            if _should_ignore(fname):
                continue
            fp = os.path.join(root, fname)
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
                matches.append('\u23f0 (grep timed out \u2014 try a more specific path or pattern)')
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


def tool_find_files(base, pattern, rel_path=None, max_results=None):
    """Find files by name glob pattern.

    Args:
        max_results: Cap on number of files returned. Default 100.
    """
    try:
        target = _safe_path(base, rel_path or '.')
    except ValueError as e:
        logger.debug('[Tools] find_files safe_path rejected %s: %s', rel_path, e, exc_info=True)
        return str(e)
    cap = max(1, min(int(max_results), 500)) if max_results else 100
    matches = []
    deadline = time.time() + 15
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in sorted(dirs)
                   if d not in IGNORE_DIRS and not d.startswith('.')]
        for fname in sorted(files):
            if fnmatch.fnmatch(fname.lower(), pattern.lower()):
                rel = os.path.relpath(os.path.join(root, fname), base)
                try:
                    sz = os.path.getsize(os.path.join(root, fname))
                except Exception as e:
                    logger.debug('[Tools] getsize failed for %s: %s', fname, e, exc_info=True)
                    sz = 0
                matches.append(f'  {rel} ({_fmt_size(sz)})')
                if len(matches) >= cap:
                    break
        if len(matches) >= cap:
            break
        if time.time() > deadline:
            matches.append('  \u23f0 (search timed out after 15s \u2014 try a more specific path)')
            break
    if not matches:
        return f'No files matching: {pattern}'
    hdr = f'Files matching "{pattern}"'
    if rel_path:
        hdr += f' in {rel_path}'
    return hdr + f' ({len(matches)} found):\n\n' + '\n'.join(matches)
