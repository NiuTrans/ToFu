"""Project path registration and utility functions.

Background scanning has been removed — the LLM relies entirely on tools
(list_dir, grep_search, find_files, read_files) to explore projects.
"""
import os
import time

from lib.log import get_logger
from lib.project_mod.config import (
    BINARY_EXTENSIONS,
    DATA_EXTENSIONS,
    IGNORE_FILES,
    _lock,
    _make_root_state,
    _roots,
    _state,
    get_state,
)
from lib.project_mod.modifications import _start_new_session

logger = get_logger(__name__)

def ensure_project_state(path_str):
    """Ensure the server's project state matches the given path.

    Called from the task orchestrator before context injection.
    If the server's _state already matches, this is a no-op.
    Otherwise calls set_project() to register the path (no scanning).

    Returns True if state was already correct or successfully set.
    """
    if not path_str:
        return False
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        return False

    with _lock:
        if _state.get('path') == abs_path:
            return True  # Already correct

    # Register this path as the active project (no scanning)
    try:
        set_project(abs_path)
        logger.info('[Project] ensure_project_state: set %s', abs_path)
        return True
    except Exception as e:
        logger.warning('[Project] ensure_project_state: set_project failed for %s: %s', abs_path, e)
    return False


def set_project(path_str):
    """Validate path and register it as the active project.

    No background scan is performed — the LLM relies entirely on tools
    (list_dir, grep_search, find_files, read_files) to explore the project.
    """
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        raise ValueError(f'Directory not found: {abs_path}')

    # Start new modification session for undo (后悔药)
    _start_new_session(abs_path)

    with _lock:
        old_path = _state.get('path')
        old_roots = list(_roots.keys())
        _state.update({
            'path': abs_path, 'tree': None,
            'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
            'languages': {}, 'scannedAt': int(time.time() * 1000),
            'scanning': False, 'scanProgress': '', 'scanDetail': '',
        })
        # ★ Also register as primary root in multi-root workspace
        name = os.path.basename(abs_path) or 'root'
        _roots.clear()  # setting primary clears all extra roots
        _roots[name] = _make_root_state(abs_path)
        _roots[name]['scanning'] = False
        _roots[name]['scannedAt'] = int(time.time() * 1000)
    logger.info('[Project] set_project: %s → %s (cleared roots: %s)',
                old_path, abs_path, old_roots)

    # ★ Cross-DC latency check — warn if project is on a remote DolphinFS cluster
    try:
        from lib.cross_dc import cross_dc_warning, get_latency_class
        lat_class = get_latency_class(abs_path)
        if lat_class in ('slow', 'very_slow'):
            warning = cross_dc_warning(abs_path)
            logger.warning('[Project] %s', warning)
    except Exception as e:
        logger.debug('[Project] Cross-DC check skipped: %s', e)

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
        _roots[rname]['scanning'] = False
        _roots[rname]['scannedAt'] = int(time.time() * 1000)

    logger.info('[Project] add_project_root: [%s] %s', rname, abs_path)
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



# _scan_root_worker and _scan_worker removed — scanning is no longer performed.
# The LLM uses tools (list_dir, grep_search, etc.) to explore projects on demand.


def clear_project():
    with _lock:
        _state.update({
            'path': None, 'tree': None,
            'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
            'languages': {}, 'scannedAt': 0,
            'scanning': False, 'scanProgress': '', 'scanDetail': '',
        })
        _roots.clear()


def rescan():
    """Re-scan is now a no-op since scanning was removed.

    Returns the current state for backward compatibility.
    """
    with _lock:
        path = _state['path']
    if not path:
        raise ValueError('No project set')
    return get_state()


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


# _scan_and_build_tree removed — no tree building at project set time.


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

