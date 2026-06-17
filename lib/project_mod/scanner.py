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

def ensure_project_state(path_str, extra_paths=None, conv_id=None, readonly_paths=None):
    """Ensure the server's project state matches the given path(s).

    Called from the task orchestrator before context injection.
    If the server's _state already matches (primary + extras), this is a no-op.
    Otherwise calls set_project_paths() or set_project() to register.

    ★ 2026-05-05 — ``conv_id`` parameter.  When provided, this ALSO
    writes a per-conversation root registry via :func:`set_conv_roots`.
    The per-conv registry is immune to the global ``_roots.clear()``
    that happens when another task calls ``set_project`` with a
    different primary path.  Tool calls originating from this conv
    will resolve namespaced paths (``name:rel/path``) against this
    conv-local registry first, preventing concurrent tasks from
    clobbering each other's namespaces.

    Args:
        path_str: Primary project path.
        extra_paths: Optional list of additional root paths (multi-root workspace).
        conv_id: Conversation identifier for scoping (optional).

    Returns True if state was already correct or successfully set.
    """
    if not path_str:
        return False
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        return False

    # Normalise extra paths
    abs_extras = []
    if extra_paths:
        for ep in extra_paths:
            aep = os.path.abspath(os.path.expanduser(ep))
            if os.path.isdir(aep) and aep != abs_path and aep not in abs_extras:
                abs_extras.append(aep)

    # ★ Always register the per-conv scope up front (cheap; no disk I/O).
    #   Do this BEFORE touching the global registry so even if the
    #   subsequent set_project* call is delayed, the conv's tool calls
    #   can still resolve via the conv registry.
    if conv_id:
        try:
            from lib.project_mod.config import set_conv_roots
            set_conv_roots(conv_id, abs_path, extras=abs_extras,
                           readonly_paths=readonly_paths)
        except Exception as e:
            logger.warning('[Project] set_conv_roots failed conv=%s: %s',
                           conv_id[:12] if conv_id else '?', e)

    with _lock:
        if _state.get('path') == abs_path:
            # Primary matches — check if extras also match
            current_extra_paths = {rs['path'] for rn, rs in _roots.items()
                                   if rs['path'] != abs_path}
            if set(abs_extras) == current_extra_paths:
                return True  # Already correct — primary + extras match

    # Register this path (+ extras) as the active project
    try:
        if abs_extras:
            set_project_paths([abs_path] + abs_extras,
                              readonly_paths=readonly_paths)
            logger.info('[Project] ensure_project_state: set %s + %d extras',
                        abs_path, len(abs_extras))
        else:
            set_project(abs_path, readonly_paths=readonly_paths)
            logger.info('[Project] ensure_project_state: set %s', abs_path)
        return True
    except Exception as e:
        logger.warning('[Project] ensure_project_state failed for %s (+%d extras): %s',
                       abs_path, len(abs_extras), e)
    return False


def set_project(path_str, readonly_paths=None):
    """Validate path and register it as the active project.

    No background scan is performed — the LLM relies entirely on tools
    (list_dir, grep_search, find_files, read_files) to explore the project.

    ``readonly_paths`` (optional): abspath'd set of roots to mark read-only;
    if the primary is in it, its root_state gets ``access='ro'``.
    """
    from lib.project_mod.config import _normalise_readonly_set
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        raise ValueError(f'Directory not found: {abs_path}')
    _ro_set = _normalise_readonly_set(readonly_paths)
    _primary_access = 'ro' if abs_path in _ro_set else 'rw'

    # Start new modification session for undo (后悔药)
    _start_new_session(abs_path)

    with _lock:
        old_path = _state.get('path')
        old_roots = list(_roots.keys())
        # ★ Idempotence guard: if the primary is unchanged, preserve the
        # existing root set.  Without this guard every set_project call
        # (including frontend auto-restore on page load or repeated
        # /api/project/set from the same conv) would wipe any extra roots
        # registered mid-conversation via tool_create_project — the model's
        # newly-scaffolded project would silently disappear, and subsequent
        # 'name:path' prefixes would fail to resolve, routing writes to the
        # primary root instead.  See create_project_frontend_sync_bug memo.
        same_primary = (old_path == abs_path)
        _state.update({
            'path': abs_path, 'tree': None,
            'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
            'languages': {}, 'scannedAt': int(time.time() * 1000),
            'scanning': False, 'scanProgress': '', 'scanDetail': '',
        })
        # ★ Also register as primary root in multi-root workspace
        name = os.path.basename(abs_path) or 'root'
        if not same_primary:
            _roots.clear()  # switching primary clears all extra roots
        if name in _roots and _roots[name]['path'] != abs_path:
            # Stale entry with same name but different path — replace it.
            del _roots[name]
        if name not in _roots:
            _roots[name] = _make_root_state(abs_path, access=_primary_access)
        _roots[name]['scanning'] = False
        _roots[name]['scannedAt'] = int(time.time() * 1000)
        # Always refresh the primary's access flag — a re-set that only
        # toggles RO must take effect even under the same_primary guard.
        _roots[name]['access'] = _primary_access
    logger.info('[Project] set_project: %s → %s (same_primary=%s, old_roots=%s, '
                'new_roots=%s)', old_path, abs_path, same_primary, old_roots,
                list(_roots.keys()))

    # ★ Cross-DC latency check — log warning if project is on a remote cluster.
    #   This is non-blocking: if benchmark hasn't finished yet, latency_class
    #   will be 'unknown' and we skip the warning.  The benchmark result will
    #   be available for subsequent tool calls (timeout adjustment, etc.).
    try:
        from lib.cross_dc import cross_dc_warning, get_latency_class
        lat_class = get_latency_class(abs_path)
        if lat_class in ('slow', 'very_slow'):
            warning = cross_dc_warning(abs_path)
            logger.warning('[Project] %s', warning)
        elif lat_class == 'unknown':
            logger.debug('[Project] Cross-DC status unknown for %s '
                         '(benchmark may still be running)', abs_path)
    except Exception as e:
        logger.debug('[Project] Cross-DC check skipped: %s', e)

    return get_state()


def set_project_paths(paths, readonly_paths=None):
    """Set multiple project paths atomically.

    The first path becomes the primary project; remaining paths are added
    as extra workspace roots.  Any previously-registered extra roots whose
    paths are *not* in the new list are automatically removed.

    Args:
        paths: list of directory path strings (at least one required).
        readonly_paths: optional list of paths (subset of ``paths``) to mark
            read-only. Each matching root_state gets ``access='ro'``.
    Returns:
        dict with combined state (primary + roots).
    """
    if not paths:
        raise ValueError("At least one path is required")
    from lib.project_mod.config import _normalise_readonly_set
    _ro_set = _normalise_readonly_set(readonly_paths)

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

    # 1) Set (or re-set) the primary project.
    #    NOTE: set_project() preserves existing extra roots when the primary
    #    is unchanged (the `same_primary` idempotence guard, needed so
    #    mid-conversation tool_create_project roots survive repeated
    #    /api/project/set calls). This means we must explicitly prune any
    #    extras not in the new list below — otherwise the modal's "remove
    #    folder" action would silently no-op when the primary is untouched.
    set_project(primary, readonly_paths=readonly_paths)

    # 2) Prune extras that the caller no longer wants. The new extras set
    #    is `extras`; anything currently registered that isn't primary and
    #    isn't in `extras` must go.
    desired_extras = set(extras)
    with _lock:
        to_remove = [
            rn for rn, rs in _roots.items()
            if rs['path'] != primary and rs['path'] not in desired_extras
        ]
    for rn in to_remove:
        try:
            remove_project_root(rn)
            logger.info('[Project] set_project_paths: pruned stale extra root %s', rn)
        except Exception as e:
            logger.warning('[Project] set_project_paths: failed to prune %s: %s', rn, e)

    # 3) Add each extra root not already registered
    for ep in extras:
        try:
            add_project_root(ep, access='ro' if ep in _ro_set else 'rw')
        except Exception as e:
            logger.debug('[Scanner] add_project_root failed for %s, skipping: %s', ep, e, exc_info=True)

    # 4) Build a unified response (get_state() now includes extraRoots)
    return get_state()


def add_project_root(path_str, name=None, access='rw'):
    """Add an additional root directory to the workspace (multi-root support).

    Args:
        path_str: Directory path to add
        name: Optional short name / namespace. Defaults to directory basename.
        access: ``'rw'`` (default) or ``'ro'`` write policy for this root.
    Returns:
        dict with roots info
    """
    abs_path = os.path.abspath(os.path.expanduser(path_str))
    if not os.path.isdir(abs_path):
        raise ValueError(f'Directory not found: {abs_path}')

    rname = name or os.path.basename(abs_path) or 'root'
    _access = 'ro' if access == 'ro' else 'rw'

    # Deduplicate name if collision
    with _lock:
        if not _state['path']:
            raise ValueError('Set a primary project first before adding extra roots')
        orig_name = rname
        counter = 2
        while rname in _roots:
            # If same path already registered, refresh its access flag and skip
            if _roots[rname]['path'] == abs_path:
                _roots[rname]['access'] = _access
                return _get_roots_info()
            rname = f'{orig_name}_{counter}'
            counter += 1
        _roots[rname] = _make_root_state(abs_path, access=_access)
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
                'readOnly': rs.get('access') == 'ro',
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
    # Absolute paths resolve directly — these tools (list_dir/grep/find) are
    # read-only, so reading outside the project root is non-destructive and
    # mirrors read_files, which already accepts any absolute path. Only the
    # *relative* form is confined to the root (a relative arg must not be able
    # to climb out via '../').
    expanded = os.path.expanduser(rel)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    resolved = os.path.abspath(os.path.join(base, rel))
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f'Path traversal blocked: {rel}')
    return resolved

