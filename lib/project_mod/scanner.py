"""Project file scanning, tree building, and filtering utilities."""
import os
import threading
import time

from lib.log import get_logger
from lib.project_mod.config import (
    BINARY_EXTENSIONS,
    DATA_EXTENSIONS,
    IGNORE_DIRS,
    IGNORE_FILES,
    LINE_COUNT_LIMIT,
    MAX_TREE_ENTRIES,
    _lock,
    _make_root_state,
    _roots,
    _ScanAborted,
    _state,
    get_state,
)
from lib.project_mod.modifications import _start_new_session, cancel_pending_index_updates

logger = get_logger(__name__)

def set_project(path_str):
    """Validate path → return immediately → scan in background thread."""
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        raise ValueError(f'Directory not found: {abs_path}')

    # Start new modification session for undo (后悔药)
    _start_new_session(abs_path)
    # ★ Cancel any pending dirty-file index updates from the old project
    cancel_pending_index_updates()

    with _lock:
        old_path = _state.get('path')
        old_roots = list(_roots.keys())
        _state.update({
            'path': abs_path, 'tree': None,
            'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
            'languages': {}, 'scannedAt': 0,
            'index': None, 'indexing': False, 'indexProgress': '',
            'scanning': True, 'scanProgress': 'Starting…', 'scanDetail': '',
        })
        # ★ Also register as primary root in multi-root workspace
        name = os.path.basename(abs_path) or 'root'
        _roots.clear()  # setting primary clears all extra roots
        _roots[name] = _make_root_state(abs_path)
    logger.info('[Project] set_project: %s → %s (cleared roots: %s)',
                old_path, abs_path, old_roots)

    threading.Thread(target=_scan_worker, args=(abs_path,), daemon=True).start()
    return get_state()


def set_project_paths(paths):
    """Set multiple project paths atomically.

    The first path becomes the primary project; remaining paths are added
    as extra workspace roots.  Any previously-registered extra roots whose
    paths are *not* in the new list are automatically removed.

    Args:
        paths: list of directory path strings (at least one required).
    Returns:
        dict with combined state (primary + roots).
    """
    if not paths:
        raise ValueError("At least one path is required")

    # Normalise all paths up-front so comparisons are consistent
    abs_paths = []
    for p in paths:
        ap = os.path.abspath(os.path.expanduser(p))
        if not os.path.isdir(ap):
            raise ValueError(f"Directory not found: {ap}")
        if ap not in abs_paths:          # deduplicate
            abs_paths.append(ap)

    primary = abs_paths[0]
    extras  = abs_paths[1:]

    # 1) Set (or re-set) the primary project — this clears old roots too
    set_project(primary)

    # 2) Add each extra root
    for ep in extras:
        try:
            add_project_root(ep)
        except Exception as e:
            logger.debug('[Scanner] add_project_root failed for %s, skipping: %s', ep, e, exc_info=True)

    # 3) Build a unified response (get_state() now includes extraRoots)
    return get_state()


def add_project_root(path_str, name=None):
    """Add an additional root directory to the workspace (multi-root support).

    Args:
        path_str: Directory path to add
        name: Optional short name / namespace. Defaults to directory basename.
    Returns:
        dict with roots info
    """
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        raise ValueError(f'Directory not found: {abs_path}')

    rname = name or os.path.basename(abs_path) or 'root'

    # Deduplicate name if collision
    with _lock:
        if not _state['path']:
            raise ValueError('Set a primary project first before adding extra roots')
        orig_name = rname
        counter = 2
        while rname in _roots:
            # If same path already registered, skip
            if _roots[rname]['path'] == abs_path:
                return _get_roots_info()
            rname = f'{orig_name}_{counter}'
            counter += 1
        _roots[rname] = _make_root_state(abs_path)

    # Scan in background
    threading.Thread(target=_scan_root_worker, args=(rname, abs_path), daemon=True).start()
    return _get_roots_info()


def remove_project_root(name):
    """Remove an extra root from the workspace. Cannot remove the primary root."""
    with _lock:
        primary_name = None
        for rn, rs in _roots.items():
            if rs['path'] == _state['path']:
                primary_name = rn
                break
        if name == primary_name:
            raise ValueError('Cannot remove the primary root — use set_project to change it')
        if name not in _roots:
            raise ValueError(f'Root not found: {name}  (available: {", ".join(_roots.keys())})')
        del _roots[name]
    return _get_roots_info()


def list_roots():
    """Return info about all workspace roots."""
    return _get_roots_info()


def _get_roots_info():
    with _lock:
        result = {}
        for rn, rs in _roots.items():
            result[rn] = {
                'path': rs['path'],
                'fileCount': rs['fileCount'],
                'dirCount': rs['dirCount'],
                'totalSize': rs['totalSize'],
                'scanning': rs['scanning'],
                'isPrimary': rs['path'] == _state['path'],
            }
        return result


def _scan_root_worker(root_name, abs_path):
    """Background scanner for an extra root."""
    try:
        t0 = time.time()
        with _lock:
            if root_name not in _roots:
                return
            _roots[root_name]['scanning'] = True
            _roots[root_name]['scanProgress'] = 'Scanning…'

        tree_text, stats = _scan_and_build_tree(abs_path)
        from lib.project_mod.indexer import _load_cached_index
        cached_index = _load_cached_index(abs_path)
        elapsed = time.time() - t0

        with _lock:
            if root_name not in _roots:
                logger.info('[Scan] Extra root %s vanished, discarding scan result', root_name)
                return
            # ★ BUG FIX: Also verify path matches — root name may have been reused
            if _roots[root_name]['path'] != abs_path:
                logger.info('[Scan] Extra root %s path changed (%s → %s), discarding',
                           root_name, abs_path, _roots[root_name]['path'])
                return
            r = _roots[root_name]
            r.update({
                'tree': tree_text,
                'fileCount': stats['fileCount'], 'dirCount': stats['dirCount'],
                'totalSize': stats['totalSize'], 'languages': stats['languages'],
                'scannedAt': int(time.time() * 1000),
                'index': cached_index,
                'scanning': False, 'scanProgress': '',
            })
        logger.info('Extra root scan done %.2fs · %d files — [%s] %s',
              elapsed, stats['fileCount'], root_name, abs_path)
    except Exception as e:
        logger.error('Extra root scan error [%s]: %s', root_name, e, exc_info=True)
        with _lock:
            if root_name in _roots:
                _roots[root_name].update(scanning=False, scanProgress=f'Error: {e}')


def _scan_worker(abs_path):
    """Background: build file tree with live progress → load cached index."""
    try:
        t0 = time.time()

        # ★ Immediately update so frontend sees "Scanning…" instead of "Starting…"
        with _lock:
            if _state['path'] != abs_path:
                return
            _state['scanProgress'] = 'Scanning directory…'

        def on_progress(fc, dc, ts, current_dir):
            with _lock:
                if _state['path'] != abs_path:
                    raise _ScanAborted()
                _state['fileCount'] = fc
                _state['dirCount'] = dc
                _state['totalSize'] = ts
                _state['scanProgress'] = f'{fc} files, {dc} dirs'
                _state['scanDetail'] = current_dir

        tree_text, stats = _scan_and_build_tree(abs_path, on_progress=on_progress)
        from lib.project_mod.indexer import _load_cached_index
        cached_index = _load_cached_index(abs_path)
        elapsed = time.time() - t0

        with _lock:
            if _state['path'] != abs_path:
                return                          # user set another path
            _state.update({
                'tree': tree_text,
                'fileCount': stats['fileCount'], 'dirCount': stats['dirCount'],
                'totalSize': stats['totalSize'], 'languages': stats['languages'],
                'scannedAt': int(time.time() * 1000),
                'index': cached_index,
                'scanning': False, 'scanProgress': '', 'scanDetail': '',
            })
            # ★ Sync primary root in _roots
            for rn, rs in _roots.items():
                if rs['path'] == abs_path:
                    rs.update({
                        'tree': tree_text,
                        'fileCount': stats['fileCount'], 'dirCount': stats['dirCount'],
                        'totalSize': stats['totalSize'], 'languages': stats['languages'],
                        'scannedAt': int(time.time() * 1000),
                        'index': cached_index,
                        'scanning': False, 'scanProgress': '',
                    })
                    break
        logger.info('Scan done %.2fs · %d files %d dirs — %s',
              elapsed, stats['fileCount'], stats['dirCount'], abs_path)

    except _ScanAborted:
        logger.info('Scan aborted (path changed): %s', abs_path)
    except Exception as e:
        logger.error('Scan error: %s', e, exc_info=True)
        with _lock:
            if _state['path'] == abs_path:
                _state.update(scanning=False, scanProgress=f'Error: {e}',
                              scanDetail='')


def clear_project():
    with _lock:
        _state.update({
            'path': None, 'tree': None,
            'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
            'languages': {}, 'scannedAt': 0,
            'index': None, 'indexing': False, 'indexProgress': '',
            'scanning': False, 'scanProgress': '', 'scanDetail': '',
        })


def rescan():
    with _lock:
        path = _state['path']
    if not path:
        raise ValueError('No project set')
    return set_project(path)


# ═══════════════════════════════════════════════════════
#  ★ Fast scanning with os.scandir + progress callback
# ═══════════════════════════════════════════════════════

def _should_ignore(filename):
    if filename in IGNORE_FILES or filename.startswith('.'):
        return True
    return os.path.splitext(filename)[1].lower() in BINARY_EXTENSIONS


def _is_data_file(filename, size=0):
    """Check if a file is a data/bulk file not worth full indexing."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in DATA_EXTENSIONS:
        return True
    # Large .json files are likely data dumps, not config
    if ext == '.json' and size > 50_000:
        return True
    return False


def _is_likely_data_content(text, threshold=0.6):
    """Heuristic: check if text content looks like repetitive data (JSON lines, CSV rows, etc.)."""
    lines = text.split('\n', 30)[:30]
    if len(lines) < 5:
        return False
    # Check for JSON lines pattern
    json_lines = sum(1 for l in lines if l.strip().startswith(('{', '[')))
    if json_lines / len(lines) > threshold:
        return True
    # Check for CSV/TSV: consistent delimiter count across lines
    for delim in (',', '\t', '|'):
        counts = [l.count(delim) for l in lines if l.strip()]
        if len(counts) >= 3 and counts[0] >= 2:
            if all(c == counts[0] for c in counts[1:5]):
                return True
    return False


def _fmt_size(n):
    if n < 1024: return f'{n}B'
    if n < 1024 * 1024: return f'{n / 1024:.1f}KB'
    return f'{n / 1024 / 1024:.1f}MB'


def _estimate_lines(size, ext):
    """Estimate line count from file size without opening the file.
    Average bytes-per-line by file type (empirically measured)."""
    if size <= 0:
        return 0
    bpl = {
        '.py': 32, '.js': 38, '.ts': 36, '.tsx': 38, '.jsx': 38,
        '.java': 35, '.go': 30, '.rs': 32, '.c': 30, '.cpp': 34,
        '.h': 28, '.cs': 34, '.rb': 28, '.php': 34, '.swift': 34,
        '.kt': 34, '.scala': 38, '.vue': 36, '.svelte': 36,
        '.html': 40, '.css': 30, '.scss': 30, '.less': 30,
        '.json': 35, '.yaml': 28, '.yml': 28, '.toml': 30,
        '.xml': 45, '.md': 45, '.txt': 50, '.sh': 25,
        '.sql': 35, '.r': 30, '.lua': 28, '.pl': 30,
    }.get(ext, 35)
    return max(1, size // bpl)


def _scan_and_build_tree(base_path, max_entries=MAX_TREE_ENTRIES,
                         on_progress=None):
    tree_lines = []
    fc = dc = ts = 0
    langs = {}
    truncated = False
    root_name = os.path.basename(base_path) or 'project'
    tree_lines.append(f'{root_name}/')
    last_t = [time.time()]
    call_count = [0]  # track calls to make early reports more frequent

    def _report(dp):
        now = time.time()
        call_count[0] += 1
        # ★ First 20 reports: no throttle (instant feedback);
        #   after that: throttle to every 120ms
        if on_progress and (call_count[0] <= 20 or now - last_t[0] > 0.12):
            rel = os.path.relpath(dp, base_path)
            on_progress(fc, dc, ts, '' if rel == '.' else rel)
            last_t[0] = now

    def walk(dp, prefix, depth=0):
        nonlocal fc, dc, ts, truncated
        if truncated or depth > 12:
            return
        # ★ Report *before* scandir so progress updates even during slow I/O
        _report(dp)
        try:
            entries = sorted(os.scandir(dp), key=lambda e: e.name)
        except (PermissionError, OSError) as e:
            logger.debug('[Scanner] scandir failed for %s: %s', dp, e, exc_info=True)
            return

        dirs, files = [], []
        for entry in entries:
            try:
                is_d = entry.is_dir(follow_symlinks=False)
            except OSError:
                logger.debug('[Scanner] is_dir check failed for entry %s', entry.name)
                continue
            if is_d:
                if entry.name not in IGNORE_DIRS and not entry.name.startswith('.'):
                    dirs.append(entry)
            else:
                try:
                    is_f = entry.is_file(follow_symlinks=False)
                except OSError:
                    logger.debug('[Scanner] is_file check failed for entry %s', entry.name)
                    continue
                if is_f and not _should_ignore(entry.name):
                    files.append(entry)

        items = [(d, True) for d in dirs] + [(f, False) for f in files]
        for i, (entry, is_dir) in enumerate(items):
            if fc + dc > max_entries:
                tree_lines.append(f'{prefix}  … ({fc} files shown, truncated)')
                truncated = True
                return
            is_last = i == len(items) - 1
            conn = '└── ' if is_last else '├── '
            cpfx = prefix + ('    ' if is_last else '│   ')

            if is_dir:
                dc += 1
                tree_lines.append(f'{prefix}{conn}{entry.name}/')
                walk(entry.path, cpfx, depth + 1)
            else:
                try:
                    sz = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    logger.debug('[Scanner] stat failed for entry %s', entry.name)
                    continue
                ts += sz; fc += 1
                ext = os.path.splitext(entry.name)[1].lower()
                if ext:
                    langs[ext] = langs.get(ext, 0) + 1
                # ★ Estimate lines from file size — no file open required
                info = ''
                if ext not in BINARY_EXTENSIONS and sz > 0:
                    lc = _estimate_lines(sz, ext)
                    info = f' ({lc}L, {_fmt_size(sz)})'
                else:
                    info = f' ({_fmt_size(sz)})'
                tree_lines.append(f'{prefix}{conn}{entry.name}{info}')

    walk(base_path, '')
    if on_progress:
        on_progress(fc, dc, ts, '')
    stats = {'fileCount': fc, 'dirCount': dc, 'totalSize': ts,
             'languages': dict(sorted(langs.items(), key=lambda x: -x[1])[:15])}
    return '\n'.join(tree_lines), stats


# ═══════════════════════════════════════════════════════
#  Safety
# ═══════════════════════════════════════════════════════

def _safe_path(base, rel):
    if not base:
        raise ValueError('No project base path')
    base = os.path.abspath(base)
    if not rel or rel in ('.', '/', ''):
        return base
    resolved = os.path.abspath(os.path.join(base, rel))
    if not resolved.startswith(base):
        raise ValueError(f'Path traversal blocked: {rel}')
    return resolved

