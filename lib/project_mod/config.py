"""Project Co-Pilot configuration, constants and shared state."""

import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

IGNORE_DIRS = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    '.idea', '.vscode', '.next', 'dist', 'build', '.cache',
    '.tox', '.mypy_cache', '.pytest_cache', 'target', 'vendor',
    '.bundle', 'coverage', '.nyc_output', '.eggs', '.sass-cache',
    'bower_components', '.parcel-cache', '.turbo', '.vercel',
    '.output', '.nuxt', '.svelte-kit', '.angular', 'obj', 'bin',
    '.project_indexes',
    # ★ Bulk runtime/output dirs that explode rg's scan time on FUSE/NFS.
    #   Mirrors the project's .gitignore but also applies in non-git roots
    #   (rg only auto-respects .gitignore inside a .git repo, and we're
    #   often run from exported/copied trees with no .git).  Keeping these
    #   here ensures the same exclusion via rg's `-g '!dir/'` AND grep's
    #   `--exclude-dir` paths in _build_rg_cmd / _build_grep_cmd.
    'logs', 'data', '.project_sessions', 'swebench_workdir',
    'abtest_workdir', 'overleaf_cache', '.ruff_cache',
    'uploads', '.migrate_backup',
}

BINARY_EXTENSIONS = {
    '.pyc', '.pyo', '.class', '.o', '.so', '.dll', '.exe', '.bin',
    '.dat', '.db', '.sqlite', '.sqlite3',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico', '.webp', '.svg',
    '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.ogg',
    '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar', '.xz',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.min.js', '.min.css', '.map',
}

IGNORE_FILES = {
    '.DS_Store', 'Thumbs.db', 'desktop.ini',
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
    'poetry.lock', 'Cargo.lock', 'composer.lock', 'Gemfile.lock',
}

MAX_FILE_SIZE    = 512 * 1024
MAX_SCAN_FILES   = 5000
MAX_TREE_ENTRIES = 500
MAX_READ_CHARS   = 1_000_000     # ★ whole-file read cap lifted; MAX_FILE_SIZE (512KB) is the real bound
MAX_GREP_RESULTS = 50
LINE_COUNT_LIMIT = 50_000        # ★ skip line counting for files above this
SESSIONS_DIR     = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                '.project_sessions')

MAX_COMMAND_TIMEOUT = None      # ★ no timeout limit for run_command
MAX_COMMAND_OUTPUT  = 100_000   # ★ max chars of command output to return
SHELL_PREFIX = os.environ.get('SHELL_PREFIX', '')  # ★ e.g. 'source ~/.bashrc &&'

# ★ Dangerous command patterns to block
# Includes both Unix and Windows equivalents for cross-platform safety.
DANGEROUS_PATTERNS = [
    # ── Unix ──
    r'\brm\s+-rf\s+/',         # rm -rf /
    r'\bmkfs\b',               # format disk
    r'\bdd\s+.*of=/',          # dd overwrite
    r'>\s*/dev/sd',            # overwrite block device
    r'\b:(){ :\|:& };:',      # fork bomb
    r'\bshutdown\b',
    r'\breboot\b',
    r'\binit\s+0\b',
    # ── Windows ──
    r'\bformat\s+[a-zA-Z]:',  # format C:
    r'\brd\s+/s\s+/q\b',      # rd /s /q (recursive delete)
    r'\bdel\s+/s\s+/q\b',     # del /s /q (recursive delete)
    r'\bdiskpart\b',           # disk partitioning
]

CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.java', '.go', '.rs', '.c', '.cpp',
    '.h', '.rb', '.php', '.swift', '.kt', '.scala', '.vue',
    '.jsx', '.tsx', '.html', '.css', '.json', '.yaml', '.toml',
    '.md', '.txt', '.sh', '.dockerfile',
}

# ★ Data / bulk files — not binary but not worth reading in full
DATA_EXTENSIONS = {
    '.jsonl', '.ndjson', '.csv', '.tsv', '.parquet',
    '.log', '.logs', '.out', '.err',
    '.sql', '.dump',
    '.xml', '.xsd', '.dtd',
    '.arff', '.sav', '.rec', '.ftr', '.feather',
}
# ★ Max chars returned to LLM for data files in tool_read_file
MAX_DATA_FILE_PREVIEW = 2000


# ═══════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════

_lock = threading.RLock()
_state = {
    'path': None, 'tree': None,
    'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
    'languages': {}, 'scannedAt': 0,
    # ★ Async scanning
    'scanning': False, 'scanProgress': '', 'scanDetail': '',
    # ★ Modification history for undo (后悔药)
    'sessionId': None, 'modifications': [],
}

# ═══════════════════════════════════════════════════════
#  ★ Multi-Root Workspace Support
# ═══════════════════════════════════════════════════════
# Each root is stored as:  { name: { path, tree, fileCount, ... } }
# The _state above remains the "primary" root for backward compat.
# _roots dict stores *all* roots including the primary.
#
# ═══════════════════════════════════════════════════════
#  ★ Per-Conversation Root Registries (2026-05-05 fix)
# ═══════════════════════════════════════════════════════
# The global _roots dict was shared across every task on the server.
# Under concurrency (e.g. SWE-bench 9 parallel workers), ``set_project``
# would clear _roots for the primary-change case, wiping *other* tasks'
# registrations.  A task whose system prompt was built with root name
# ``instance-A`` would later have its ``instance-A:src/foo.py`` tool
# call rejected as ``Unknown workspace root`` because another task had
# since clobbered _roots with its own primary.
#
# Fix: keep a dedicated per-conversation registry (_conv_roots) that
# each task writes to via its conv_id.  ``resolve_namespaced_path``
# consults the conv's registry FIRST, then falls back to the shared
# _roots (single-user UI / legacy code path).  Concurrent tasks cannot
# clobber each other because their scopes are disjoint.
#
# Keys are full conv_ids (short prefixes risk collision with real conv
# ids).  Stale entries are GC'd on-demand with ``clear_conv_state()``.

_roots = {}  # name → per-root state dict (global / legacy)

# conv_id → {name → root_state}.  Populated by ensure_project_state_for_conv.
# The ``None`` key is reserved for the legacy / unknown-conv path and
# aliases the global _roots registry.
#
# Uses OrderedDict for LRU-style eviction to prevent unbounded growth
# (e.g. SWE-bench runs 1236 unique convs).  Each entry is tiny (a few
# dicts), so we cap at MAX_CONV_ROOTS to bound memory + let old entries
# age out.  Re-registration via set_conv_roots refreshes recency.
import collections as _collections  # re-used below for the LRU

MAX_CONV_ROOTS = 512
_conv_roots: _collections.OrderedDict = _collections.OrderedDict()

# conv_id → primary abs_path (used as fallback by resolve_namespaced_path
# when no ':' prefix is present).
_conv_primary: dict[str, str] = {}


def _make_root_state(abs_path, access='rw'):
    """Create a fresh per-root state dict.

    ``access`` is the per-root write policy: ``'rw'`` (default, writable) or
    ``'ro'`` (read-only — reads/greps/find are allowed, but every write,
    edit, create_project, and destructive run_command targeting this root is
    refused). It lives on the root_state so it travels with the root through
    both the global ``_roots`` registry and the per-conv ``_conv_roots`` one
    — the same isolation seam every other multi-root attribute uses.
    """
    return {
        'path': abs_path, 'tree': None,
        'fileCount': 0, 'dirCount': 0, 'totalSize': 0,
        'languages': {}, 'scannedAt': 0,
        'scanning': False, 'scanProgress': '', 'scanDetail': '',
        'access': 'ro' if access == 'ro' else 'rw',
    }

def get_roots():
    """Return a snapshot of all GLOBAL workspace roots.

    For per-conversation roots, use ``get_conv_roots(conv_id)``.
    """
    with _lock:
        return {name: dict(st) for name, st in _roots.items()}


def get_conv_roots(conv_id):
    """Return a snapshot of a conversation's workspace roots.

    Falls back to the global _roots if no conv-specific registry exists.
    """
    with _lock:
        if conv_id and conv_id in _conv_roots:
            return {n: dict(s) for n, s in _conv_roots[conv_id].items()}
        return {n: dict(s) for n, s in _roots.items()}


def get_root_path(name, conv_id=None):
    """Get the absolute path of a named root, or None.

    If ``conv_id`` is given, look it up in that conv's registry first;
    otherwise (or if not found) fall back to the shared global registry.
    """
    with _lock:
        if conv_id:
            conv_map = _conv_roots.get(conv_id)
            if conv_map:
                r = conv_map.get(name)
                if r:
                    return r['path']
                # Case-insensitive match within the conv's registry
                for rn, rs in conv_map.items():
                    if rn.lower() == name.lower():
                        return rs['path']
        r = _roots.get(name)
        return r['path'] if r else None


def _normalise_readonly_set(readonly_paths):
    """Return a set of abspath'd, expanduser'd read-only root paths."""
    out = set()
    for p in (readonly_paths or []):
        if not p:
            continue
        out.add(os.path.abspath(os.path.expanduser(p)))
    return out


def set_conv_roots(conv_id, primary_path, extras=None, readonly_paths=None):
    """Register the root layout for a conversation (scoped registry).

    This is the per-conv equivalent of ``set_project`` + ``add_project_root``.
    It does NOT touch the global _roots; concurrent conversations never
    clobber each other's namespace.

    Args:
        conv_id:      Conversation identifier (required for scoping).
        primary_path: Absolute path of the primary root.
        extras:       Optional iterable of absolute paths for extra roots.
        readonly_paths: Optional iterable of absolute paths (any of which may
            be the primary or an extra) that must be registered read-only.
            A root whose abspath is in this set gets ``access='ro'``.
    """
    if not conv_id or not primary_path:
        return
    abs_primary = os.path.abspath(os.path.expanduser(primary_path))
    ro_set = _normalise_readonly_set(readonly_paths)
    extras_list = []
    for p in (extras or []):
        ap = os.path.abspath(os.path.expanduser(p))
        if ap != abs_primary and ap not in extras_list:
            extras_list.append(ap)
    with _lock:
        conv_map: dict = {}
        # Primary name = basename of abs path (matches set_project naming).
        prim_name = os.path.basename(abs_primary) or 'root'
        conv_map[prim_name] = _make_root_state(
            abs_primary, access='ro' if abs_primary in ro_set else 'rw')
        used_names = {prim_name}
        for ep in extras_list:
            name = os.path.basename(ep) or 'root'
            orig = name
            counter = 2
            while name in used_names:
                name = f'{orig}_{counter}'
                counter += 1
            used_names.add(name)
            conv_map[name] = _make_root_state(
                ep, access='ro' if ep in ro_set else 'rw')
        # LRU eviction: drop oldest if over cap.  Re-insertion moves an
        # existing conv to the end (most-recent).
        if conv_id in _conv_roots:
            _conv_roots.move_to_end(conv_id)
        _conv_roots[conv_id] = conv_map
        _conv_primary[conv_id] = abs_primary
        while len(_conv_roots) > MAX_CONV_ROOTS:
            _evicted_id, _ = _conv_roots.popitem(last=False)
            _conv_primary.pop(_evicted_id, None)
            logger.debug('[Config] LRU-evicted conv root state for %s '
                         '(over %d cap)', _evicted_id[:12], MAX_CONV_ROOTS)
    logger.debug('[Config] set_conv_roots conv=%s primary=%s extras=%d names=%s',
                 conv_id[:12] if conv_id else '?',
                 abs_primary, len(extras_list), list(conv_map.keys()))


def clear_conv_state(conv_id):
    """Drop a conversation's root registry (call on task/conv teardown)."""
    if not conv_id:
        return
    with _lock:
        _conv_roots.pop(conv_id, None)
        _conv_primary.pop(conv_id, None)


def ensure_project_state_for_conv(conv_id, path, extras=None, readonly_paths=None):
    """Convenience alias for :func:`set_conv_roots` (kept for test stability)."""
    set_conv_roots(conv_id, path, extras=extras, readonly_paths=readonly_paths)


def is_readonly_path(abs_target, conv_id=None):
    """Return True if *abs_target* resolves inside a root marked read-only.

    Resolution mirrors the path resolver's scoping: when ``conv_id`` is given
    and that conv has a registered root set, the conv-local registry is
    consulted (strict isolation); otherwise the global ``_roots`` registry is
    used. The target is matched against the DEEPEST containing root (so an
    ``rw`` sub-root nested under an ``ro`` parent wins, and vice-versa).

    A target inside no registered root returns False — it is not read-only,
    and the write path's auto-register / sandbox logic handles it as before.
    """
    if not abs_target:
        return False
    target = os.path.realpath(os.path.abspath(os.path.expanduser(abs_target)))
    with _lock:
        if conv_id and conv_id in _conv_roots:
            roots = list(_conv_roots[conv_id].values())
        else:
            roots = list(_roots.values())
    best = None
    best_len = -1
    for rs in roots:
        rp = os.path.realpath(rs['path'])
        if target == rp or target.startswith(rp + os.sep):
            if len(rp) > best_len:
                best = rs
                best_len = len(rp)
    return bool(best) and best.get('access') == 'ro'


def resolve_namespaced_path(rel_path, conv_id=None):
    """Parse ``rootname:some/rel/path`` → ``(abs_base, rel)``.

    Resolution order:
      1. If ``conv_id`` is given and that conv has registered roots,
         look there first.
      2. Fall back to the shared global ``_roots`` registry (single-user
         UI / legacy code paths).
      3. If no ':' prefix is present, fall back to the conv's primary
         (if any), else to the global primary.

    Raises ``ValueError`` if the named root cannot be resolved in
    either registry, or if no primary is known.
    """
    with _lock:
        if ':' in rel_path and not os.path.isabs(rel_path.split(':')[0]):
            name, _, rest = rel_path.partition(':')
            # 1) Conv-specific registry — STRICT isolation when the conv has
            #    registered roots.  If a conv has its own registry, we do
            #    NOT fall through to the global one; otherwise a concurrent
            #    task's roots could leak into this conv's namespace and
            #    cause write misrouting (silent clobber).
            if conv_id and conv_id in _conv_roots:
                conv_map = _conv_roots[conv_id]
                r = conv_map.get(name)
                if r:
                    logger.debug('[Config] conv=%s namespaced resolve: %s → base=%s',
                                 conv_id[:12], rel_path, r['path'])
                    return r['path'], rest or '.'
                # Case-insensitive within conv registry
                for rn, rs in conv_map.items():
                    if rn.lower() == name.lower():
                        return rs['path'], rest or '.'
                # Strict isolation: do NOT consult the global _roots here.
                avail = ', '.join(conv_map.keys()) or 'none'
                raise UnknownWorkspaceRootError(
                    f'Unknown workspace root: {name}  (available: {avail})')
            # 2) Global registry (no conv_id, or conv has no registry) —
            #    legacy / single-user UI path.
            r = _roots.get(name)
            if r:
                return r['path'], rest or '.'
            for rn, rs in _roots.items():
                if rn.lower() == name.lower():
                    logger.debug('[Config] Case-insensitive root match: %s → %s', name, rn)
                    return rs['path'], rest or '.'
            avail = ', '.join(_roots.keys()) or 'none'
            raise UnknownWorkspaceRootError(
                f'Unknown workspace root: {name}  (available: {avail})')
        # Fallback: primary (prefer conv-specific, else global)
        primary = None
        if conv_id:
            primary = _conv_primary.get(conv_id)
        if not primary:
            primary = _state['path']
        if not primary:
            raise ValueError('No project path set')
        return primary, rel_path


class _ScanAborted(Exception):
    pass


class ReadOnlyRootError(ValueError):
    """Raised when a write/edit/create targets a root marked read-only.

    Subclasses ``ValueError`` so the existing ``except ValueError`` handlers
    in the write tools surface the message straight to the model as a tool
    result (same path as ``UnknownWorkspaceRootError`` and the system-path
    rejections in ``_resolve_write_path``). The model then knows to write
    elsewhere instead of retrying the same blocked target.
    """
    pass


class UnknownWorkspaceRootError(ValueError):
    """Raised when a ``rootname:rel/path`` spec references an unregistered root.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers still
    work, but allows task-executor layers to distinguish this recoverable,
    LLM-facing path error from other validation failures. The raise site in
    ``lib/project_mod/tools.py`` logs a single WARNING with full context; the
    task-exec layers that re-raise should log at INFO (or not at all) to
    avoid quadruple-logging the same event in ``logs/error.log``.
    """
    pass


def get_state():
    with _lock:
        s = dict(_state)
        # Include modification count for undo
        s['modificationsCount'] = len(_state.get('modifications', []))
        # ★ Always include extra roots so the frontend stays in sync
        extra = []
        primary = _state.get('path')
        for rn, rs in _roots.items():
            if rs['path'] != primary:
                extra.append({'path': rs['path'], 'name': rn,
                              'fileCount': rs['fileCount'],
                              'scanning': rs['scanning'],
                              'readOnly': rs.get('access') == 'ro'})
        s['extraRoots'] = extra
        # ★ Primary root's own access flag (the primary may itself be RO).
        if primary:
            for rs in _roots.values():
                if rs['path'] == primary:
                    s['readOnly'] = rs.get('access') == 'ro'
                    break
        # ★ Cross-DC latency indicator
        try:
            from lib.cross_dc import get_cluster_for_path, get_latency_class, get_latency_s
            if primary:
                lat_class = get_latency_class(primary)
                if lat_class != 'unknown':
                    s['crossDC'] = {
                        'latencyClass': lat_class,
                        'cluster': get_cluster_for_path(primary),
                        'latencyMs': round(get_latency_s(primary) * 1000, 1) if get_latency_s(primary) else None,
                    }
        except Exception as e:
            logger.debug('[Config] cross_dc info unavailable: %s', e)
        return s


def get_project_path():
    with _lock:
        return _state['path']


# ═══════════════════════════════════════════════════════
#  ★ Recent Projects (server-side persistence)
# ═══════════════════════════════════════════════════════

def get_recent_projects():
    """Return list of recent projects sorted by last_used desc.

    No LIMIT — callers (frontend) decide how many to display. Keeping
    the full list server-side ensures a newly-added project never gets
    hidden by an artificial window size.
    """
    from lib.database import DOMAIN_SYSTEM, get_db
    rows = get_db(DOMAIN_SYSTEM).execute(
        'SELECT path, count, last_used FROM recent_projects ORDER BY last_used DESC'
    ).fetchall()
    return [{'path': r['path'], 'count': r['count'], 'last_used': r['last_used']} for r in rows]


def save_recent_project(path):
    """Insert or update a recent project entry."""
    import time

    from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, get_db
    db = get_db(DOMAIN_SYSTEM)
    now = int(time.time())
    db_execute_with_retry(
        db,
        '''INSERT INTO recent_projects (path, "count", last_used) VALUES (?, 1, ?)
           ON CONFLICT(path) DO UPDATE SET "count" = recent_projects."count" + 1, last_used = EXCLUDED.last_used''',
        (path, now),
    )


def clear_recent_projects():
    """Delete all recent project entries."""
    from lib.database import DOMAIN_SYSTEM, get_db
    db = get_db(DOMAIN_SYSTEM)
    db.execute('DELETE FROM recent_projects')
    db.commit()


# ═══════════════════════════════════════════════════════
#  ★ Modification History (后悔药 / Undo)
