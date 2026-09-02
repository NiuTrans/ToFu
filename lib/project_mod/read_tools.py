"""Project read-only tools — list_dir, read_file(s), grep, find_files.

Extracted from tools.py for modularity. Re-exported via tools.py for backward compat.
"""

import base64
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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
from lib.project_mod.grep_engine import SearchRequest, run_search
from lib.project_mod import tree_index

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


# ── SVG inline-render signal ──────────────────────────────────────────
# An SVG file is read as TEXT (its XML source enters the model stream
# unchanged — the model should see the markup). But the frontend can ALSO
# render that markup visually, like an image. read_files returns a plain
# str for text, so there is no room to attach a render descriptor to the
# result itself. We bridge it out-of-band via a per-thread collector — the
# same race-free seam write_tools uses for workspace-root signals: the
# synchronous handler→execute_tool→tool_read_files call chain runs in one
# thread, so a thread-local list is safe. The project tool handler drains
# it after the read and attaches the data URIs to the tool-round meta.

_svg_signal = threading.local()

# Cap on how large an SVG we inline-render (the base64 data URI rides the
# tool-round meta to the browser; a giant SVG would bloat the payload).
_MAX_SVG_RENDER_BYTES = 512 * 1024


def _signal_svg_render(filename, svg_source):
    """Record an SVG whose source should also be rendered inline on the frontend.

    Appends ``{filename, uri}`` to the current thread's pending list (created
    lazily), where ``uri`` is a ``data:image/svg+xml;base64,...`` URL the
    browser can drop into an ``<img src>``. Drained by the project tool
    handler via :func:`drain_svg_render_signals` after the read runs.
    """
    if not svg_source or len(svg_source.encode('utf-8', 'ignore')) > _MAX_SVG_RENDER_BYTES:
        return
    try:
        b64 = base64.b64encode(svg_source.encode('utf-8')).decode('ascii')
    except Exception as e:
        logger.debug('[Tools] svg render encode failed for %s: %s', filename, e)
        return
    pending = getattr(_svg_signal, 'pending', None)
    if pending is None:
        pending = []
        _svg_signal.pending = pending
    pending.append({
        'filename': filename,
        'format': 'svg',
        'uri': f'data:image/svg+xml;base64,{b64}',
    })


def drain_svg_render_signals():
    """Return and clear the current thread's pending SVG-render signals.

    Returns a list of ``{filename, format, uri}`` dicts (empty when none).
    Called by the project tool handler immediately after read_files runs, so
    the SVG source can be surfaced as an inline ``<img>`` in the tool round.
    """
    pending = getattr(_svg_signal, 'pending', None)
    if not pending:
        return []
    _svg_signal.pending = []
    return list(pending)


def _is_svg_path(path):
    """True when *path* names an SVG file (by extension)."""
    return isinstance(path, str) and path.lower().endswith('.svg')


def _maybe_signal_svg(target, rel_path):
    """If *target* is a readable SVG under the render cap, signal its source.

    Best-effort: reads the raw XML from disk and hands it to
    :func:`_signal_svg_render` so the frontend can render it inline. Any
    failure is logged at debug and skipped — the text read is unaffected.
    """
    if not _is_svg_path(rel_path):
        return
    try:
        if not os.path.isfile(target):
            return
        if os.path.getsize(target) > _MAX_SVG_RENDER_BYTES:
            return
        with open(target, encoding='utf-8', errors='replace') as f:
            source = f.read()
    except OSError as e:
        logger.debug('[Tools] svg render read failed for %s: %s', rel_path, e)
        return
    _signal_svg_render(os.path.basename(rel_path), source)


# ═══════════════════════════════════════════════════════
#  list_dir
# ═══════════════════════════════════════════════════════

_LIST_DIR_SCAN_LIMIT = 10_000
_LIST_DIR_ENTRY_LIMIT = 1_000
_LIST_DIR_OUTPUT_CHAR_LIMIT = 64_000
_FIND_SCAN_LIMIT = 250_000


def _scan_directory_entries(target, *, show_hidden=False,
                            include_directory_stat=False,
                            respect_project_ignores=True,
                            include_non_regular=False):
    """Return a bounded, sorted directory snapshot.

    The limits bound both hostile/FUSE directories and response growth. The
    scan still reports truncation instead of presenting a partial list as
    complete. Symlinks and special files are skipped because this backend is a
    project navigator, not an authority-expanding replacement for arbitrary
    GNU ``ls`` shapes. ``run_command`` can disable project-ignore filtering
    and retain non-regular entries for its shell-compatible listing view.
    """
    rows = []
    scanned = 0
    truncated = False
    with os.scandir(target) as iterator:
        for entry in iterator:
            if scanned >= _LIST_DIR_SCAN_LIMIT:
                truncated = True
                break
            scanned += 1
            name = entry.name
            if not show_hidden and name.startswith('.'):
                continue
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                logger.debug('[Tools] list_dir type check failed for %s', name)
                continue
            if respect_project_ignores and is_dir and name in IGNORE_DIRS:
                continue
            if not is_dir and not is_file and not include_non_regular:
                continue
            size = None
            modified = None
            if is_file or include_directory_stat or include_non_regular:
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                    size = None if is_dir else stat_result.st_size
                    modified = stat_result.st_mtime
                except OSError:
                    logger.debug('[Tools] list_dir stat failed for %s', name)
                    if is_file:
                        continue
            rows.append({
                'name': name,
                'path': entry.path,
                'is_dir': is_dir,
                'is_file': is_file,
                'is_symlink': is_symlink,
                'size': size,
                'modified': modified,
            })
    rows.sort(key=lambda row: (not row['is_dir'], row['name'].lower(),
                               row['name']))
    if len(rows) > _LIST_DIR_ENTRY_LIMIT:
        rows = rows[:_LIST_DIR_ENTRY_LIMIT]
        truncated = True
    return rows, truncated, scanned


def tool_list_dir(base, rel_path='.', *, show_hidden=False,
                  shell_compatible=False):
    try:
        target = _safe_path(base, rel_path)
    except ValueError as e:
        logger.debug('[Tools] list_dir safe_path rejected %s: %s', rel_path, e, exc_info=True)
        return str(e)
    if not os.path.isdir(target):
        return f'Not a directory: {rel_path}'
    try:
        entries, truncated, scanned = _scan_directory_entries(
            target,
            show_hidden=show_hidden,
            respect_project_ignores=not shell_compatible,
            include_non_regular=shell_compatible,
        )
    except (PermissionError, OSError):
        logger.debug('[Tools] list_dir unable to scan %s', rel_path,
                     exc_info=True)
        return f'Unable to list directory: {rel_path}'
    dirs_out, files_out = [], []
    output_chars = 0
    for entry in entries:
        name = entry['name']
        if entry['is_dir']:
            # NOTE: the per-subdir child count was dropped — it cost one
            # nested os.scandir per subdir per call (O(subdirs) FUSE
            # readdirs). The tree_index indexes files only, so listing into a
            # subdir remains the bounded way to inspect it.
            rendered = f'  {name}/'
            dirs_out.append(rendered)
        elif entry['is_file']:
            sz = int(entry['size'] or 0)
            ext = os.path.splitext(name)[1].lower()
            if ext not in BINARY_EXTENSIONS and sz > 0:
                lc = _estimate_lines(sz, ext)
                rendered = f'  {name} ({lc}L, {_fmt_size(sz)})'
            else:
                rendered = f'  {name} ({_fmt_size(sz)})'
            files_out.append(rendered)
        else:
            kind = 'symlink' if entry['is_symlink'] else 'special'
            rendered = f'  {name} [{kind}]'
            files_out.append(rendered)
        output_chars += len(rendered) + 1
        if output_chars >= _LIST_DIR_OUTPUT_CHAR_LIMIT:
            if entry['is_dir']:
                dirs_out.pop()
            else:
                files_out.pop()
            truncated = True
            break
    result = f'Directory: {rel_path or "."}\n\n'
    if dirs_out:
        result += 'Directories:\n' + '\n'.join(dirs_out) + '\n\n'
    if files_out:
        result += 'Files:\n' + '\n'.join(files_out)
    if not dirs_out and not files_out:
        result += '(empty or all files ignored)'
    if truncated:
        result += (f'\n\n… [listing truncated: scanned at most {scanned} '
                   f'entries; returned {len(dirs_out) + len(files_out)}. '
                   'Use a narrower relative path.]')
    # ── Project Summary when listing root ──
    if not shell_compatible and rel_path in ('.', '', None) and target == base:
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
    from lib.project_mod.abs_path_guard import AbsPathDenied, enforce_abs_read
    try:
        enforce_abs_read(path)
    except AbsPathDenied as e:
        logger.debug('[ReadTools] absolute read denied for %s: %s', path, e)
        return f'Error: {e}'

    from lib.file_reader import read_local_file as _read_local
    result = _read_local(path)

    # Images return a dict — line ranges don't apply
    if isinstance(result, dict) and result.get('__screenshot__'):
        return result

    # For text results, apply line range if requested
    if isinstance(result, str) and (start_line or end_line) and not result.startswith('Error:'):
        lines = result.split('\n')
        total = len(lines)
        s = max(1, start_line or 1) - 1
        e = min(total, end_line or total)
        expanded_path = os.path.expanduser(path)
        expanded_path = os.path.abspath(expanded_path)
        filename = os.path.basename(expanded_path)
        if s >= e:
            return (f'Error: requested line range {start_line or 1}-{end_line or total} '
                    f'is empty or out of bounds for {filename} ({total} lines).')
        sliced = '\n'.join(lines[s:e])
        header = f'File: {filename} (lines {s + 1}-{e} of {total})\n'
        return header + '─' * 40 + '\n' + sliced

    return result


def _read_project_file(base, rel_path, start_line=None, end_line=None, _pre_stat=None):
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
    # ONE stat for existence/type + size, reused across this read instead of
    # os.path.isfile + os.path.getsize (which each re-stat the file on FUSE).
    try:
        st = _pre_stat if _pre_stat is not None else os.stat(target)
    except OSError as e:
        logger.debug('[Tools] read_file stat failed for %s: %s', rel_path, e)
        return f'File not found: {rel_path}'
    if not stat.S_ISREG(st.st_mode):
        return f'File not found: {rel_path}'
    sz = st.st_size

    # ── Binary / image classification (mirror _read_absolute_file) ──
    # Done AFTER path resolution so it applies to project-RELATIVE paths
    # too — file type is keyed on the file, never on absolute-vs-relative.
    # Images → read_local_file returns a __screenshot__ dict so the base64
    # rides the native image_url protocol and NEVER enters the text stream.
    # PDFs / Office → text extraction.  Without this a relative-path image
    # (e.g. static/icons/x.png) was opened with errors='replace', decoded to
    # ~1 char/byte of U+FFFD garbage, and tokenised as text — conv
    # mqgfkmxy (2026-06-16): four sub-512KB PNGs → 1.7M chars → 1.36M
    # tokens → fatal HTTP 400.  Text-with-binary-extension (.svg XML,
    # .min.js) is deliberately NOT diverted here; it falls through to the
    # text reader, which applies a content-based binary sniff below.
    ext = os.path.splitext(target)[1].lower()
    from lib.file_reader import (
        IMAGE_EXTENSIONS as _IMG_EXT,
        OFFICE_EXTENSIONS as _OFF_EXT,
        PDF_EXTENSIONS as _PDF_EXT,
    )
    if ext in _IMG_EXT or ext in _PDF_EXT or ext in _OFF_EXT:
        from lib.file_reader import read_local_file
        return read_local_file(target)

    if sz > MAX_FILE_SIZE and not (start_line or end_line):
        return (f'File too large ({_fmt_size(sz)}). Use grep_search to find specific content, '
                f'or read_files with start_line/end_line for a specific range.')

    # ── Content-based binary sniff ──
    # Catches binaries whose extension is NOT image/pdf/office (e.g. .so,
    # .zip, a mislabelled blob, or an image read with the wrong suffix) and
    # stops raw bytes from being decoded as U+FFFD text.  Clean text files
    # — including .svg / minified JS — pass this fine.
    try:
        with open(target, 'rb') as _bf:
            _head = _bf.read(8192)
        if _head:
            _nontext = sum(1 for b in _head if b < 8 or (13 < b < 32 and b != 27))
            if _nontext > len(_head) * 0.30:
                return (f'[Binary file: {rel_path} ({_fmt_size(sz)}) — not shown. '
                        f'Content is not valid text. Images are read via the native '
                        f'image protocol; other binaries (archives, compiled objects) '
                        f'cannot be read as text.]')
    except OSError as e:
        logger.debug('[Tools] read_file binary sniff failed for %s: %s', rel_path, e)

    filename = os.path.basename(rel_path)
    is_data = _is_data_file(filename, sz)

    try:
        with open(target, errors='replace') as f:
            if start_line or end_line:
                all_lines = f.readlines()
                total = len(all_lines)
                s = max(1, start_line or 1) - 1
                e = min(total, end_line or total)
                if s >= e:
                    return (f'Error: requested line range {start_line or 1}-{end_line or total} '
                            f'is empty or out of bounds for {rel_path} ({total} lines).')
                text = ''.join(all_lines[s:e])
                header = f'File: {rel_path} (lines {s + 1}-{e} of {total})\n'
            else:
                text = f.read()
                total = text.count('\n') + 1
                # One-shot big read: hint its pages out of the page cache so
                # they stop counting against a shared cgroup limit (no-op
                # off-Linux). Ranged reads stay cached — likely re-read soon.
                if sz >= 8 << 20:
                    try:
                        os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    except (OSError, AttributeError) as e:
                        logger.debug('[Tools] fadvise skipped for %s: %s', rel_path, e)

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


def _normalize_line_range(start_line, end_line):
    """Return ``(start, end, swapped)`` with a reversed range corrected.

    A range whose start exceeds its end (``start_line=6171,
    end_line=6162``) is UNAMBIGUOUS — there is exactly one interval the
    caller can mean, and an inverted interval is never a legitimate
    request. Models emit these regularly (transposed digits, or naming a
    grep hit bottom-up), so we repair rather than reject.

    ONLY the reversed case is repaired. A range that is merely OUT OF
    BOUNDS (lines 20000-20100 of a 9000-line file) is a real error and is
    left untouched for the readers to report — swapping cannot rescue it,
    and silently "fixing" it would mask a genuinely wrong request.
    Single-sided (one bound ``None``) and equal bounds cannot be reversed
    and pass through unchanged, so the helper is idempotent.
    """
    if start_line is None or end_line is None or start_line <= end_line:
        return start_line, end_line, False
    return end_line, start_line, True


def _merge_same_file_ranges(reads):
    """Merge overlapping/adjacent ranges for the same file.

    Preserves ``_base`` (per-spec base override for multi-root) and the
    caller-visible ``_display_path`` through the merge — the first occurrence
    wins for each path group.
    """
    GAP_THRESHOLD = 40
    from collections import OrderedDict
    grouped = OrderedDict()  # path → list[(sl, el)]
    base_map = {}            # path → _base (first seen)
    display_path_map = {}    # path → original caller-visible path
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
        if p not in display_path_map and '_display_path' in spec:
            display_path_map[p] = spec['_display_path']

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
            if p in display_path_map:
                entry['_display_path'] = display_path_map[p]
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
            if p in display_path_map:
                entry['_display_path'] = display_path_map[p]
            if s is not None:
                entry['start_line'] = s
            if e is not None:
                entry['end_line'] = e
            merged.append(entry)
    return merged



def tool_read_files(base, reads, *, result_items=None):
    """Batch-read multiple files (or file ranges) in one call.

    Each spec in *reads* is ``{path, start_line?, end_line?}``.
    Multi-root callers may attach ``_base`` per-spec to override the
    default *base* for that particular file.

    ``result_items`` is an optional request-owned sink for bounded structured
    per-file previews.  It does not change the legacy text return; the task
    result-envelope boundary consumes and discards the sidecar items.

    Absolute paths (starting with ``/`` or ``~``) are routed to
    ``_read_absolute_file`` and bypass the project sandbox.
    """
    if not reads or not isinstance(reads, list):
        return (
            'Error: "reads" must be a non-empty array of {path, start_line?, end_line?} objects. '
            'Example: {"reads": [{"path": "src/main.py"}]}'
        )
    MAX_BATCH = 20
    if len(reads) > MAX_BATCH:
        reads = reads[:MAX_BATCH]

    # Coerce LLM-emitted string line numbers to int. The model
    # occasionally returns ``start_line: "70"`` instead of ``70`` and
    # downstream arithmetic (``sl <= prev_e + GAP_THRESHOLD``) crashes
    # with ``TypeError: can only concatenate str (not "int") to str``.
    # Treat unparseable values as None (== "no range given").
    for spec in reads:
        if not isinstance(spec, dict):
            continue
        for k in ('start_line', 'end_line'):
            v = spec.get(k)
            if v is None or isinstance(v, int):
                continue
            try:
                spec[k] = int(v)
            except (ValueError, TypeError) as e:
                logger.debug('[Tools] read_files: dropping non-numeric %s=%r (%s)', k, v, e)
                spec[k] = None

    # Repair reversed ranges (start > end) BEFORE merging. Order matters:
    #   _merge_same_file_ranges sorts by (start, end) and coalesces within
    #   GAP_THRESHOLD, so a reversed spec batched with another range for the
    #   same file is absorbed by it and vanishes — the model then receives a
    #   clean result for lines it never asked for, with NO error. Normalising
    #   here fixes both that silent case and the lone-spec case (which merely
    #   errored). Note the swap so the reader sees its call was malformed.
    swapped_paths = []
    for spec in reads:
        if not isinstance(spec, dict):
            continue
        s, e_, was_swapped = _normalize_line_range(spec.get('start_line'), spec.get('end_line'))
        if was_swapped:
            spec['start_line'], spec['end_line'] = s, e_
            swapped_paths.append(f'{spec.get("path", "?")} ({e_}→{s} read as {s}-{e_})')
            logger.info('[Tools] read_files: reversed line range %s=%s-%s corrected to %s-%s',
                        spec.get('path'), e_, s, s, e_)

    reads = _merge_same_file_ranges(reads)

    parts = []
    image_results = {}  # index → dict for __screenshot__ results
    total_chars = 0
    BATCH_CHAR_BUDGET = 50 * 1024 * 1024  # lifted; per-file size bounds are the real limit
    WHOLE_FILE_THRESHOLD = 40_000

    def _record_result(index, spec, result, *, status=None):
        if not isinstance(result_items, list):
            return
        from lib.tools.result_projection import file_read_result_projection_item
        result_items.append(file_read_result_projection_item(
            index=index + 1,
            path=spec.get('_display_path', spec.get('path', '?'))
            if isinstance(spec, dict) else '?',
            result=result,
            start_line=spec.get('start_line') if isinstance(spec, dict) else None,
            end_line=spec.get('end_line') if isinstance(spec, dict) else None,
            status=status,
        ))

    def _record_batch_skips(start_index):
        for skipped_index in range(start_index, len(reads)):
            skipped_spec = reads[skipped_index]
            _record_result(
                skipped_index, skipped_spec,
                'Read skipped because the batch output budget was exhausted.',
                status='skipped')

    for i, spec in enumerate(reads):
        if not isinstance(spec, dict) or 'path' not in spec:
            error = f'[{i+1}] Error: each entry must have a "path" field'
            parts.append(error)
            _record_result(i, spec, error, status='error')
            continue
        rel_path = spec['path']
        sl = spec.get('start_line')
        el = spec.get('end_line')
        spec_base = spec.get('_base', base)  # per-spec base override (multi-root)

        # Route: absolute paths → _read_absolute_file (images, PDFs, Office, text)
        if _is_absolute_path(rel_path):
            result = _read_absolute_file(rel_path, sl, el)
            # SVG reads as text (model sees the markup); ALSO signal its
            # source so the frontend can render it inline like an image.
            if isinstance(result, str) and not result.startswith('Error:'):
                _maybe_signal_svg(os.path.abspath(os.path.expanduser(rel_path)), rel_path)
            # Image results are dicts — track separately
            if isinstance(result, dict) and result.get('__screenshot__'):
                text_fallback = result.get('_text_fallback', 'Image loaded.')
                image_results[i] = result
                parts.append(text_fallback)
                total_chars += len(text_fallback)
                _record_result(i, spec, result)
                continue
            # Text/PDF/Office result — budget as normal string
            if isinstance(result, str):
                if total_chars + len(result) > BATCH_CHAR_BUDGET:
                    remaining = BATCH_CHAR_BUDGET - total_chars
                    if remaining > 200:
                        result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
                    else:
                        notice = f'[{i+1}] … [{len(reads) - i} more files skipped — batch budget exceeded]'
                        parts.append(notice)
                        _record_batch_skips(i)
                        break
                total_chars += len(result)
                parts.append(result)
                _record_result(i, spec, result)
                continue
            rendered = str(result)
            parts.append(rendered)
            _record_result(i, spec, rendered)
            continue

        # Project-relative path — auto-expand small files to whole-file.
        # Compute ONE stat here and hand it to _read_project_file so the
        # read path doesn't re-stat the target.
        _pre_stat = None
        if sl is not None or el is not None:
            try:
                target = _safe_path(spec_base, rel_path)
                st = os.stat(target)
                if stat.S_ISREG(st.st_mode) and st.st_size <= WHOLE_FILE_THRESHOLD:
                    sl, el = None, None
                _pre_stat = st
            except (ValueError, OSError) as e:
                logger.debug('[Tools] read_files range check failed for %s: %s', rel_path, e, exc_info=True)

        result = _read_project_file(spec_base, rel_path, sl, el, _pre_stat=_pre_stat)
        # Relative-path images/PDFs/Office now return a __screenshot__ dict
        # (parity with the absolute branch) — track separately so base64
        # never reaches the text accumulator below.
        if isinstance(result, dict) and result.get('__screenshot__'):
            result['filename'] = os.path.basename(rel_path)
            text_fallback = result.get('_text_fallback', 'Image loaded.')
            image_results[i] = result
            parts.append(text_fallback)
            total_chars += len(text_fallback)
            _record_result(i, spec, result)
            continue
        # SVG reads as text (model sees the markup); ALSO signal its source
        # so the frontend can render it inline like an image.
        if isinstance(result, str) and not result.startswith(('Error', 'File not found')):
            try:
                _svg_target = _safe_path(spec_base, rel_path)
                _maybe_signal_svg(_svg_target, rel_path)
            except ValueError as e:
                logger.debug('[Tools] svg signal safe_path rejected %s: %s', rel_path, e)
        if total_chars + len(result) > BATCH_CHAR_BUDGET:
            remaining = BATCH_CHAR_BUDGET - total_chars
            if remaining > 200:
                result = result[:remaining] + '\n… [truncated — batch budget exceeded]'
            else:
                notice = f'[{i+1}] … [{len(reads) - i} more files skipped — batch budget exceeded]'
                parts.append(notice)
                _record_batch_skips(i)
                break
        total_chars += len(result)
        parts.append(result)
        _record_result(i, spec, result)

    text_result = '\n\n'.join(parts)

    if swapped_paths:
        text_result = ('[Note] read_files: reversed line range(s) auto-corrected — '
                       + '; '.join(swapped_paths[:5])
                       + '. start_line must be <= end_line.\n\n') + text_result

    # If any image results, return a mixed result with __batch_images__
    if image_results:
        return {
            '__batch_images__': image_results,
            '_text_content': text_result,
        }
    return text_result


# ═══════════════════════════════════════════════════════
#  inspect_image
# ═══════════════════════════════════════════════════════

def tool_inspect_image(base, path, *, crop=None, rotate=0, zoom=None, grid=False,
                       messages=None):
    """Re-render a region of an image at full resolution (zoom/rotate/crop).

    Resolution order:
      1. **Uploaded-attachment reference** (``/api/images/<f>``, ``data:`` URI,
         ``http(s)://`` URL) — resolved via the centralized
         :func:`lib.attachments.resolve_attachment` to the ORIGINAL bytes,
         then transformed in memory. This is how a chat-uploaded image (which
         has NO filesystem path the model can name) is inspected — the fix for
         the model inventing a bogus ``/dev/null`` path.
      2. **Filesystem path** — absolute / ~ paths pass straight through,
         project-relative paths resolve under ``base``; delegates to
         :func:`lib.file_reader.inspect_image_file`.

    Returns a ``__screenshot__`` dict on success or an ``Error: …`` string.
    """
    from lib.attachments import is_attachment_ref, resolve_attachment
    if is_attachment_ref(path):
        resolved = resolve_attachment(path, messages=messages)
        if not resolved:
            return (f'Error: could not resolve attachment reference {path!r}. '
                    'The uploaded file may no longer be available.')
        if resolved.get('kind') != 'image':
            return (f'Error: reference {path!r} is a text attachment, not an image; '
                    'inspect_image only works on images.')
        from lib.file_reader import inspect_image_bytes
        return inspect_image_bytes(resolved['raw'], source_name=os.path.basename(path) or 'image',
                                   crop=crop, rotate=rotate, zoom=zoom, grid=grid)

    if _is_absolute_path(path):
        target = path
    else:
        try:
            target = _safe_path(base, path)
        except ValueError as e:
            logger.debug('[Tools] inspect_image safe_path rejected %s: %s', path, e)
            return str(e)
    from lib.file_reader import inspect_image_file
    return inspect_image_file(target, crop=crop, rotate=rotate, zoom=zoom, grid=grid)


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

    # ── Fast path: persistent tree index (zero directory traversal) ──
    io_timeout = _get_io_timeout(base)
    indexed = _grep_via_index(base, target, pattern, include, ctx_n, cap,
                              count_only, io_timeout)
    if indexed is not None:
        return indexed
    tree_index.warm(base)  # no index yet — build it behind the live walk

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
    # Skip ignored dirs — ALL of them, in a deterministic order.
    # (Was list(IGNORE_DIRS)[:30]: once the set grew past 30 entries the
    # per-process hash order silently dropped arbitrary exclusions, so
    # node_modules & co. leaked back into grep results ~nondeterministically.)
    for d in sorted(IGNORE_DIRS):
        cmd.extend(['-g', f'!{d}/'])
    # rg auto-respects .gitignore only inside a git repo (.git/ present).
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
    # Do not use rg -m here: it is a PER-FILE limit. The shared executor
    # enforces ``cap`` globally and terminates the backend once reached.
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
    # See _build_rg_cmd: complete, deterministic exclusions (no [:30] cap).
    for d in sorted(IGNORE_DIRS):
        cmd.extend(['--exclude-dir', d])
    # GNU grep doesn't have --ignore-file, so parse .gitignore manually
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
    # GNU grep -m is also per file. Global limiting belongs to grep_engine.
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
            except (ValueError, IndexError) as _e_audit:
                logger.debug('[read_tools] _format_grep_output caught %s: %s', type(_e_audit).__name__, _e_audit)
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
    # rg/grep with -C N emits three kinds of lines:
    #   match     "<path>:<lineno>:<content>"   (separator ':')
    #   context   "<path>-<lineno>-<content>"   (separator '-')
    #   group sep "--"
    # `cap` must apply to MATCH lines only — otherwise context inflates the
    # apparent count and prematurely truncates real results when ctx_n>0.
    match_count = 0
    for line in lines:
        stripped = line[len(bp):] if line.startswith(bp) else line
        is_match = bool(_RG_MATCH_LINE.match(stripped))
        if is_match:
            if match_count >= cap:
                # Hit the per-formatter cap on real matches; drop the rest
                # (including any trailing context that would follow).
                truncated = True
                break
            match_count += 1
        if len(stripped) > max_line_len:
            stripped = stripped[:max_line_len] + '  \u2026(truncated)'
        total_chars += len(stripped) + 1
        if total_chars > max_total_chars:
            truncated = True
            break
        rel_lines.append(stripped)
    if truncated:
        rel_lines.append(f'\u2026 (output truncated at {max_total_chars} chars or {cap} matches)')
    hdr = f'grep "{pattern}"'
    if include:
        hdr += f' ({include})'
    hdr += f' \u2014 {match_count} matches:\n\n'
    return hdr + '\n'.join(rel_lines)


# Max filesize that rg should bother searching (skip huge data/binary files)
_RG_MAX_FILESIZE = '2M'
# Numeric twin of _RG_MAX_FILESIZE for the index candidate filter (rg size
# suffixes are 1024-based).
_RG_MAX_BYTES = 2 * 1024 * 1024

# Max depth for rg/fd to search (safety cap)
_TOOL_MAX_DEPTH = 30

# Real-match line discriminator for rg/grep output. Two emitted shapes:
#   multi-file : "<path>:<lineno>:<content>"   (path prefix present)
#   single-file: "<lineno>:<content>"          (rg/grep omit the path when
#                                               searching exactly one file)
# The path prefix is therefore optional. Context lines use '-' after the
# line number ("<path>-<lineno>-..." / "<lineno>-...") and group separators
# are literal "--", so neither matches.
_RG_MATCH_LINE = re.compile(r'^(?:.+?:)?\d+:')


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


def _run_grep_subprocess(cmd, base, io_timeout, max_matches=None):
    """Run a grep-family subprocess and return ``(stdout, timed_out)``.

    On timeout, kills the process and returns whatever stdout has been
    buffered so far instead of discarding it. The trailing line is
    dropped because it may be a partial match cut by the kill.

    Returns ``(None, False)`` if the binary is missing.
    """
    binary = os.path.basename(cmd[0]) if cmd else ''
    is_rg = binary == 'rg'

    def _is_match_line(line):
        try:
            return bool(_RG_MATCH_LINE.match(line.decode('utf-8', errors='replace')))
        except Exception as exc:
            logger.debug('grep match-line classification failed: %s', exc)
            return False

    request = SearchRequest(
        cwd=base,
        rg_argv=tuple(cmd) if is_rg else (),
        gnu_argv=tuple(cmd) if not is_rg else (),
        preferred_backend='rg' if is_rg else 'gnu',
        max_results=max_matches,
        timeout=io_timeout,
        match_line=_is_match_line,
    )
    result = run_search(request)
    if result.unavailable:
        return None, False
    return result.stdout.decode('utf-8', errors='replace'), result.timed_out


def _format_grep_timeout(base, target, pattern, include, ctx_n, cap, count_only,
                         io_timeout, partial_stdout, reason):
    """Build the user-facing message for a timed-out grep run.

    If ``partial_stdout`` has any bytes, formats them as a normal result
    block and prepends a banner so the model knows to treat them as
    incomplete. If empty, falls back to the original "Grep timed out"
    hint.
    """
    rel_target = os.path.relpath(target, base) if target != base else '.'
    footer = ''
    try:
        from lib.project_mod.gitignore_suggest import format_footer, record_timeout_and_probe
        footer = format_footer(record_timeout_and_probe(base, reason=reason))
    except Exception as e:
        logger.debug('[Tools] gitignore-suggest probe failed: %s', e, exc_info=True)
    if partial_stdout and partial_stdout.strip():
        formatted = _format_grep_output(base, partial_stdout, pattern, include, ctx_n,
                                        cap, count_only)
        banner = (f'[partial results — grep timed out after {io_timeout}s '
                  f'searching "{rel_target}"; some matches may be missing. '
                  f'Narrow with a subdirectory path or include= glob to get '
                  f'complete results.]\n\n')
        return banner + formatted + footer
    return (f'Grep timed out after {io_timeout}s searching "{rel_target}". '
            f'Try a more specific subdirectory path or narrower file glob (include parameter).' + footer)


# ── Index-backed grep (zero directory traversal) ─────────────────────
# rg costs ~130ms fixed per PROCESS plus per-file opens whose latency is
# mount-dependent (≈0.005ms warm local, ≥0.5ms FUSE/cross-DC).  No static
# chunk/jobs profile wins on both: big argv-bound chunks minimize process
# overhead (fast disks), many small chunks maximize latency-hiding process
# parallelism (slow mounts).  So the runner PROBES with one 600-file chunk,
# measures ms/file, and picks the profile for the rest of the candidates
# from that measurement — self-calibrating per mount, per query.
_GREP_INDEX_PROBE_FILES = 600
# Probe wall-time below which the mount is "fast" (few big argv-bound chunks
# win).  The probe inherently pays one ~130ms process setup, so the
# comparison is on TOTAL probe seconds — fast mounts finish 600 files plus
# setup well under this bound; latency-bound mounts blow past it on opens
# alone (per-file arithmetic would wrongly blame setup on small chunks).
_GREP_INDEX_PROBE_FAST_S = float(os.environ.get('TOFU_GREP_INDEX_PROBE_FAST_S', '0.25') or 0.25)
# Candidate volume above which a (non-latency-bound) search parallelizes
# content reads across 8 processes.
_GREP_INDEX_CONTENT_BYTES = int(os.environ.get('TOFU_GREP_INDEX_CONTENT_BYTES',
                                               str(20 * 1024 * 1024)) or 20 * 1024 * 1024)

_GREP_INDEX_ARGV_BUDGET = max(64 * 1024, min(1024 * 1024,
                              int(os.environ.get('TOFU_GREP_INDEX_ARGV_BUDGET',
                                                 str(512 * 1024)) or 512 * 1024)))


def _index_chunk_env_override():
    """Explicit operator profile ``(jobs, chunk_cap)`` or None (auto-probe)."""
    if 'TOFU_GREP_INDEX_JOBS' not in os.environ and 'TOFU_GREP_INDEX_CHUNK' not in os.environ:
        return None
    jobs = max(1, min(8, int(os.environ.get('TOFU_GREP_INDEX_JOBS', '4') or 4)))
    cap = max(1, min(100_000, int(os.environ.get('TOFU_GREP_INDEX_CHUNK', '1500') or 1500)))
    return jobs, cap


def _chunk_paths(paths, byte_budget, count_budget):
    """Split *paths* into argv-safe chunks (byte AND count bounded)."""
    chunks, cur, cur_bytes = [], [], 0
    for p in paths:
        need = len(p) + 1
        if cur and (cur_bytes + need > byte_budget or len(cur) >= count_budget):
            chunks.append(cur)
            cur, cur_bytes = [], 0
        cur.append(p)
        cur_bytes += need
    if cur:
        chunks.append(cur)
    return chunks


def _run_index_chunks(base, pattern, ctx_n, cap, count_only, cands, deadline,
                      total_bytes=0):
    """Run rg over the candidate list, preserving candidate order.

    Self-calibrating on TWO axes (explicit-arg rg parallelizes weakly inside
    one process, so process count is the real lever):
      * probe wall time (first 600 candidates) → metadata latency;
      * total candidate bytes from the index snapshot → content volume.
    Fast+small trees get few big argv-bound chunks (process overhead
    dominates); content-heavy searches get 8 processes × 1500-file chunks
    (parallel content reads); latency-bound mounts get 8 × 600 (maximum
    open-latency overlap).  ``TOFU_GREP_INDEX_JOBS`` / ``TOFU_GREP_INDEX_CHUNK``
    pin the profile explicitly and skip the probe.

    Returns ``(stdout_bytes, timed_out, unavailable)``.  ``unavailable`` means
    the rg binary vanished (caller should fall through to the GNU path).
    """

    def _is_match_line(line):
        try:
            return bool(_RG_MATCH_LINE.match(line.decode('utf-8', errors='replace')))
        except Exception as exc:
            logger.debug('[Tools] index grep match-line classification failed: %s', exc)
            return False

    def _mk_cmd(chunk):
        flags = ['-ci'] if count_only else ['-ni']
        # --no-messages: a candidate deleted between index snapshot and search
        # must not leak "No such file" errors into results (NB: GNU grep's
        # -s means no-messages, but rg's -s means --case-sensitive — do NOT
        # shorten this flag).
        # -H: candidates always come from a DIRECTORY target here (explicit
        # single-file operands short-circuit to the live path earlier), and a
        # directory walk always prefixes paths — even when the candidate set
        # ends up holding exactly one file.
        cmd = ['rg'] + flags + ['--color=never', '--no-heading', '--no-messages', '-H']
        if ctx_n > 0 and not count_only:
            cmd.extend(['-C', str(ctx_n)])
        cmd.extend(['--', pattern])
        cmd.extend(chunk)
        return cmd

    def _run_one(i, chunk):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            return i, b'', True, False, 0.0
        t0 = time.perf_counter()
        req = SearchRequest(cwd=base, rg_argv=tuple(_mk_cmd(chunk)), preferred_backend='rg',
                            max_results=None if count_only else cap,
                            timeout=remaining, match_line=_is_match_line)
        res = run_search(req)
        dt = time.perf_counter() - t0
        if res.unavailable:
            return i, b'', False, True, dt
        return i, res.stdout, res.timed_out, False, dt

    # ── Probe: first chunk always runs alone, timed for the profile pick ──
    probe_size = _GREP_INDEX_PROBE_FILES
    override = _index_chunk_env_override()
    if override is not None or len(cands) <= probe_size:
        jobs, chunk_cap = override or (4, 1500)
        chunks = _chunk_paths(cands, _GREP_INDEX_ARGV_BUDGET, chunk_cap)
    else:
        i0, probe_out, probe_to, probe_un, probe_dt = _run_one(
            0, cands[:probe_size])
        content_heavy = total_bytes > _GREP_INDEX_CONTENT_BYTES
        if probe_dt >= _GREP_INDEX_PROBE_FAST_S:
            jobs, chunk_cap = 8, 600       # latency-bound mount
        elif content_heavy:
            jobs, chunk_cap = 8, 1500      # cheap opens, heavy content reads
        else:
            jobs, chunk_cap = 2, 100_000   # fast mount, little content
        logger.debug('[Tools] index grep probe: %.0fms for %d files, %.1fMB candidates '
                     '→ profile jobs=%d chunk=%d',
                     probe_dt * 1000, probe_size, total_bytes / 1048576, jobs, chunk_cap)
        chunks = [cands[:probe_size]] + _chunk_paths(
            cands[probe_size:], _GREP_INDEX_ARGV_BUDGET, chunk_cap)
        # Pre-seed the probe result so the pool only schedules the rest.
        results = [None] * len(chunks)
        results[0] = probe_out
        if probe_un:
            return b'', False, True
        if probe_to:
            return probe_out, True, False
        if not count_only and probe_out:
            got0 = sum(1 for ln in probe_out.split(b'\n')
                       if ln and _RG_MATCH_LINE.match(ln.decode('utf-8', errors='replace')))
            if got0 >= cap:
                return probe_out, False, False
        pool_chunks = chunks[1:]
        results, timed_out, unavailable = _run_pool(
            _run_one, pool_chunks, jobs, results, offset=1,
            count_only=count_only, cap=cap, match_re=_RG_MATCH_LINE)
        raw = b''.join(b for b in results if b)
        return raw, timed_out, unavailable

    results, timed_out, unavailable = _run_pool(
        _run_one, chunks, jobs, [None] * len(chunks), offset=0,
        count_only=count_only, cap=cap, match_re=_RG_MATCH_LINE)
    raw = b''.join(b for b in results if b)
    return raw, timed_out, unavailable


def _run_pool(_run_one, chunks, jobs, results, offset, count_only, cap, match_re):
    """Schedule ``_run_one(i+offset, chunk)`` over *chunks* with early exits.

    Writes into *results* (probe pre-seed honoured via *offset*); returns
    ``(results, timed_out, unavailable)``.  Stops consuming when a chunk
    times out (partial honesty) or the global match cap is already satisfied.
    """
    timed_out = False
    unavailable = False
    pool = ThreadPoolExecutor(max_workers=max(1, min(jobs, len(chunks))))
    try:
        futs = [pool.submit(_run_one, offset + i, chunk) for i, chunk in enumerate(chunks)]
        got = 0
        for i, fut in enumerate(futs):
            try:
                _i, data, to, un, _dt = fut.result()
            except Exception as e:
                logger.debug('[Tools] index grep chunk %d failed: %s', offset + i, e)
                data, to, un = b'', False, False
            if un:
                unavailable = True
            results[offset + i] = data
            if to:
                timed_out = True
                break  # later chunks unreliable — report partial
            if not count_only and data:
                got += sum(1 for ln in data.split(b'\n')
                           if ln and match_re.match(ln.decode('utf-8', errors='replace')))
                if got >= cap:
                    break  # already have a full page of matches — skip the tail
    finally:
        # Do NOT wait for abandoned chunks: pending ones are cancelled and
        # in-flight ones are bounded by their own run_search timeout.
        pool.shutdown(wait=False, cancel_futures=True)
    return results, timed_out, unavailable


def _grep_via_index(base, target, pattern, include, ctx_n, cap, count_only,
                    io_timeout):
    """Index-backed grep.  Returns a formatted result string, or None when the
    index cannot serve this query (caller falls back to the live rg walk).

    The persistent tree index yields the exact candidate file list (ignore
    rules + include glob applied in memory); rg then runs on EXPLICIT paths,
    performing zero directory traversal — the operation that blows the 60s
    timeout on FUSE/cross-DC trees.
    """
    try:
        if os.path.isfile(target):
            return None  # explicit single-file operand: rg searches even
                         # ignored/hidden files — keep the live semantics
        base_abs = os.path.abspath(base)
        target_abs = os.path.abspath(target)
        if target_abs != base_abs and not target_abs.startswith(base_abs + os.sep):
            return None  # absolute target outside this root — not indexed
        entry = tree_index.acquire(base_abs)
        if entry is None:
            return None
        rel_target = os.path.relpath(target_abs, base_abs)
        cands, total_bytes = tree_index.grep_candidates(
            entry, '' if rel_target == '.' else rel_target, include, _RG_MAX_BYTES)
        if not cands:
            return _format_grep_output(base, '', pattern, include, ctx_n, cap, count_only)
        raw, timed_out, unavailable = _run_index_chunks(
            base, pattern, ctx_n, cap, count_only, cands,
            time.monotonic() + io_timeout, total_bytes)
        if unavailable and not raw:
            return None  # rg vanished mid-flight — GNU path still available
        text = raw.decode('utf-8', errors='replace')
        if timed_out:
            return _format_grep_timeout(base, target, pattern, include, ctx_n, cap,
                                        count_only, io_timeout, text, 'rg_index_timeout')
        return _format_grep_output(base, text, pattern, include, ctx_n, cap, count_only)
    except Exception as e:
        logger.warning('[Tools] index grep failed for %s: %s — falling back to live walk',
                       pattern[:40], e, exc_info=True)
        return None


def _run_rg(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Run ripgrep. Returns formatted string on success, None on binary-not-found."""
    cmd = _build_rg_cmd(base, target, pattern, include, ctx_n, cap, count_only)
    io_timeout = _get_io_timeout(base)
    try:
        stdout, timed_out = _run_grep_subprocess(
            cmd, base, io_timeout, None if count_only else cap)
        if stdout is None:
            logger.warning('[Tools] rg binary not found despite detection at startup')
            return None
        if timed_out:
            logger.warning('[Tools] rg timed out after %ds: pattern=%s target=%s partial_bytes=%d',
                           io_timeout, pattern[:60], target, len(stdout))
            return _format_grep_timeout(base, target, pattern, include, ctx_n, cap,
                                        count_only, io_timeout, stdout, 'rg_timeout')
        return _format_grep_output(base, stdout, pattern, include, ctx_n, cap, count_only)
    except Exception as e:
        logger.warning('[Tools] rg failed: pattern=%s target=%s: %s', pattern[:40], target, e, exc_info=True)
        return None


def _run_gnu_grep(base, target, pattern, include, ctx_n, cap=MAX_GREP_RESULTS, count_only=False):
    """Run GNU grep. Returns formatted string on success, None on binary-not-found."""
    cmd = _build_grep_cmd(base, target, pattern, include, ctx_n, cap, count_only)
    io_timeout = _get_io_timeout(base)
    try:
        stdout, timed_out = _run_grep_subprocess(
            cmd, base, io_timeout, None if count_only else cap)
        if stdout is None:
            logger.debug('[Tools] GNU grep binary not found, will try fallback')
            return None
        if timed_out:
            logger.warning('[Tools] grep timed out after %ds: pattern=%s target=%s partial_bytes=%d',
                           io_timeout, pattern[:60], target, len(stdout))
            return _format_grep_timeout(base, target, pattern, include, ctx_n, cap,
                                        count_only, io_timeout, stdout, 'grep_timeout')
        return _format_grep_output(base, stdout, pattern, include, ctx_n, cap, count_only)
    except Exception as e:
        logger.warning('[Tools] grep failed for pattern=%s target=%s: %s', pattern[:40], target, e, exc_info=True)
        return f'Grep error: {e}'


def _python_grep(base, target, pattern, include=None, cap=MAX_GREP_RESULTS, count_only=False):
    capability_note = (
        '[limited Python fallback: rg and GNU grep are unavailable; supports '
        'case-insensitive Python regex, one include glob, and text files only]\n')
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.debug('[Tools] python_grep invalid regex pattern: %s', e, exc_info=True)
        return capability_note + f'Invalid pattern: {e}'
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
                matches.append(f'[grep timed out after {timeout_val}s - '
                               f'try a more specific path or pattern]')
            logger.warning('[Tools] python_grep timed out after %ds', timeout_val)
            break

    if count_only:
        hdr = f'grep "{pattern}"'
        if include:
            hdr += f' ({include})'
        return capability_note + f'{hdr} \u2014 {match_count} matches (count only)'

    if not matches:
        return capability_note + f'No matches found for: {pattern}'
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
    return (capability_note
            + f'grep results ({len(matches)} matches):\n\n'
            + '\n'.join(truncated))


def _find_via_index(base, target, pattern, cap, *, case_sensitive=False):
    """Index-backed find_files. Returns ``(relative_path, size)`` rows, or None when
    the index cannot serve this query (caller falls back to fd / os.walk).

    Pure in-memory glob over the persistent tree index — no readdir, no stat,
    no subprocess.  Sizes come from the index snapshot (display-grade
    freshness; the alternative is one FUSE stat per returned file).
    """
    try:
        base_abs = os.path.abspath(base)
        target_abs = os.path.abspath(target)
        if target_abs != base_abs and not target_abs.startswith(base_abs + os.sep):
            return None  # absolute target outside this root — not indexed
        entry = tree_index.acquire(base_abs)
        if entry is None:
            return None
        rel_target = os.path.relpath(target_abs, base_abs)
        rows = tree_index.find_matching(
            entry, '' if rel_target == '.' else rel_target, pattern, cap,
            case_sensitive=case_sensitive)
        return rows
    except Exception as e:
        logger.warning('[Tools] index find failed for %s: %s — falling back to live walk',
                       pattern[:40], e, exc_info=True)
        return None


def _fd_find(target, base, pattern, cap, *, case_sensitive=False,
             respect_project_ignores=True):
    """Find files using fd-find (3-4x faster than os.walk on large dirs).

    Returns ``(relative_path, size)`` rows, or ``None`` if fd fails.
    """
    io_timeout = _get_io_timeout(target, default=30)
    cmd = [_FD_BIN, '-g', pattern, target,
           '--type', 'f',
           '--max-results', str(cap)]
    cmd.append('--case-sensitive' if case_sensitive else '--ignore-case')
    if respect_project_ignores:
        cmd.extend(['--max-depth', str(_TOOL_MAX_DEPTH)])
        # Exclude ignored dirs + hidden dirs.
        for d in sorted(IGNORE_DIRS):
            cmd.extend(['--exclude', d])
    else:
        # A translated shell ``find`` must not silently omit hidden or ignored
        # files merely because the project navigation index does.
        cmd.extend(['--hidden', '--no-ignore'])
    # fd auto-respects .gitignore only inside a git repo (.git/ present).
    # Explicitly point fd at .gitignore when there is no .git/ dir.
    if respect_project_ignores and base:
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
            matches.append((rel, sz))
        return matches
    except subprocess.TimeoutExpired:
        logger.warning('[Tools] fd timed out after %ds for pattern=%s in %s',
                       io_timeout, pattern, os.path.relpath(target, base))
        return [(
            None,
            f'[search timed out after {io_timeout}s - '
            'try a more specific path]',
        )]
    except Exception as e:
        logger.warning('[Tools] fd failed: %s', e)
        return None


def _python_find(target, base, pattern, cap, *, case_sensitive=False,
                 respect_project_ignores=True):
    """Find files using a bounded ``scandir`` DFS + fnmatch fallback.

    Unlike ``os.walk``, this does not first materialize every name in a huge
    directory. Work, depth, notices, and matches all have explicit bounds.
    """
    gi_patterns = (
        _load_gitignore_patterns(base) if respect_project_ignores else [])
    matches = []
    scanned = 0
    timeout_val = _get_io_timeout(base, default=30)
    deadline = time.time() + timeout_val
    comparable_pattern = pattern if case_sensitive else pattern.lower()
    stack = [(target, 0)]
    error_notices = 0
    depth_truncated = False
    while stack:
        root, depth = stack.pop()
        child_directories = []
        try:
            with os.scandir(root) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > _FIND_SCAN_LIMIT:
                        matches.append((
                            None,
                            f'[search stopped after scanning '
                            f'{_FIND_SCAN_LIMIT} entries - try a more '
                            'specific path]',
                        ))
                        return matches
                    if scanned % 256 == 0 and time.time() > deadline:
                        matches.append((
                            None,
                            f'[search timed out after {timeout_val}s - '
                            'try a more specific path]',
                        ))
                        return matches
                    name = entry.name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError as exc:
                        logger.debug('[Tools] find type check failed for %s: %s',
                                     entry.path, exc)
                        continue
                    if is_dir:
                        if depth >= _TOOL_MAX_DEPTH:
                            depth_truncated = True
                            continue
                        if respect_project_ignores and (
                                name in IGNORE_DIRS or name.startswith('.')):
                            continue
                        rel = os.path.relpath(entry.path, base)
                        if gi_patterns and _gitignore_match(
                                rel, True, gi_patterns):
                            continue
                        child_directories.append((entry.path, depth + 1))
                        continue
                    if not is_file or (
                            respect_project_ignores and name.startswith('.')):
                        continue
                    comparable = name if case_sensitive else name.lower()
                    if not fnmatch.fnmatchcase(
                            comparable, comparable_pattern):
                        continue
                    rel = os.path.relpath(entry.path, base)
                    if gi_patterns and _gitignore_match(
                            rel, False, gi_patterns):
                        continue
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        logger.debug('[Tools] find stat failed for %s: %s',
                                     entry.path, exc)
                        continue
                    matches.append((rel, stat_result.st_size))
                    if len(matches) >= cap:
                        return matches
        except OSError as exc:
            logger.debug('[Tools] find scan failed for %s: %s', root, exc)
            if error_notices < 8:
                error_notices += 1
                matches.append((
                    None,
                    f'[unable to scan {os.path.relpath(root, base)}: {exc}]',
                ))
        # Reverse restores the directory encounter order under LIFO traversal.
        stack.extend(reversed(child_directories))
        if time.time() > deadline:
            matches.append((
                None,
                f'[search timed out after {timeout_val}s - '
                'try a more specific path]',
            ))
            return matches
    if depth_truncated:
        matches.append((
            None,
            f'[search depth capped at {_TOOL_MAX_DEPTH} - '
            'try a more specific path]',
        ))
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


def tool_find_files(base, pattern, rel_path=None, max_results=None, *,
                    case_sensitive=False, shell_output=False,
                    respect_project_ignores=True):
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
    if not os.path.isdir(target):
        if shell_output:
            return f"find: '{rel_path or '.'}': No such directory"
        return f'Not a directory: {rel_path or "."}'
    cap = max(1, min(int(max_results), 500)) if max_results else 100
    query_cap = cap + 1

    # ── Fast path: persistent tree index (pure in-memory glob, µs–ms) ──
    matches = None
    if respect_project_ignores:
        matches = _find_via_index(
            base, target, pattern, query_cap,
            case_sensitive=case_sensitive)

    if matches is None:
        if respect_project_ignores:
            # No index yet — build it behind the live walk. A shell-compatible
            # search deliberately skips the filtered index instead.
            tree_index.warm(base)
        if _FD_BIN:
            t0 = time.perf_counter()
            matches = _fd_find(
                target, base, pattern, query_cap,
                case_sensitive=case_sensitive,
                respect_project_ignores=respect_project_ignores)
            if matches is not None:
                elapsed = (time.perf_counter() - t0) * 1000
                logger.debug('[Tools] fd found %d files in %.1fms', len(matches), elapsed)

    if matches is None:
        t0 = time.perf_counter()
        matches = _python_find(
            target, base, pattern, query_cap,
            case_sensitive=case_sensitive,
            respect_project_ignores=respect_project_ignores)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.debug('[Tools] os.walk found %d files in %.1fms', len(matches), elapsed)

    if not matches:
        return '' if shell_output else f'No files matching: {pattern}'
    real_matches = [row for row in matches if row[0] is not None]
    notices = [row[1] for row in matches if row[0] is None]
    was_capped = len(real_matches) > cap
    real_matches = real_matches[:cap]
    capped_notice = (
        f'[results capped at {cap}; narrow the pattern or path]'
        if was_capped else '')
    if shell_output:
        requested_path = str(rel_path or '.')
        dot_prefixed = requested_path == '.' or requested_path.startswith('./')
        lines = [
            (f'./{rel}' if dot_prefixed and not str(rel).startswith('./')
             else str(rel))
            for rel, _size in real_matches
        ]
        lines.extend(f'find: {notice}' for notice in notices)
        if capped_notice:
            lines.append(f'find: {capped_notice}')
        return '\n'.join(lines)
    hdr = f'Files matching "{pattern}"'
    if rel_path:
        hdr += f' in {rel_path}'
    rendered = [f'  {rel} ({_fmt_size(size)})'
                for rel, size in real_matches]
    rendered.extend(f'  {notice}' for notice in notices)
    if capped_notice:
        rendered.append(f'  {capped_notice}')
    return (hdr + f' ({len(real_matches)} found):\n\n'
            + '\n'.join(rendered))
